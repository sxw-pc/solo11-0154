"""Tests for the finite-window receiver capture plan.

Covers, per the capture desk specification:

* the whole-group trade-off on ``fixtures/capture_queue.json`` (per-fragment
  priority ordering guarantees only weight 1; taking both PAIR fragments
  guarantees 4) and the executable plan / guaranteed group result;
* the two real acquisition routes (publication-boundary lock and independent
  receiver retention), including a retention copy lagging one release which
  can never be paired with the current acknowledgement, while locking the old
  group still completes it;
* the offline case where the lock acknowledgement is lost: the first action
  must be legal under both compatible device states, with branches chosen only
  after the mirror observation; plans that skip the observation or stitch
  per-state optimums together are rejected;
* the existing difference-driven pending state and an actual input replay;
* plans saved before device submission, in-progress candidates never reported
  complete, missing pieces retained and real receipts never replaced by plans.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from wastetransit import capture_desk, capture_plan as cp, capture_sim as csim
from wastetransit import classification, closure, transport
from wastetransit.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fragment(fid, packet, weight, ticks, release=cp.DEFAULT_RELEASE):
    return {
        "id": fid,
        "packet": packet,
        "weight": weight,
        "release": release,
        "available_ticks": list(ticks),
    }


class BaseFixtureTradeOff(unittest.TestCase):
    def setUp(self):
        self.doc = read("capture_queue.json")
        self.scenario = cp.scenario_from_doc(self.doc)

    def test_two_slots_two_ticks_end_at_two(self):
        self.assertEqual((0, 1), self.scenario.device_ticks)
        self.assertEqual(2, self.scenario.end_tick)
        self.assertEqual(1, len([f for f in self.scenario.fragments if f.packet == "EARLY"]))
        pair = [f for f in self.scenario.fragments if f.packet == "PAIR"]
        self.assertEqual(2, len(pair))
        for item in pair:
            self.assertEqual((0, 1), item.ticks)

    def test_per_fragment_priority_only_guarantees_early(self):
        # Queueing each fragment by expiry and reading EARLY first leaves no
        # slot for the second PAIR fragment, so PAIR must not count as held.
        self.assertEqual(1, cp.greedy_priority_weight(self.scenario))

    def test_plan_takes_both_pair_fragments_and_guarantees_four(self):
        plan = cp.plan_best(self.scenario)
        self.assertEqual(cp.PLAIN, plan["procedure"])
        self.assertEqual(4, plan["guaranteed_weight"])
        reads = [c for c in plan["commands"] if c["action"] == cp.ACT_READ]
        self.assertEqual(2, len(reads))
        self.assertEqual({0, 1}, {c["tick"] for c in reads})
        self.assertEqual(
            {"PAIR-RAW", "PAIR-SIGN"}, {c["fragment"] for c in reads}
        )
        self.assertTrue(
            all(c["release"] == cp.DEFAULT_RELEASE for c in reads),
            "both fragments must come from the same publication release",
        )
        self.assertEqual(
            [{"packet": "PAIR", "release": "R1", "weight": 4,
              "fragments": ["PAIR-RAW", "PAIR-SIGN"]}],
            plan["completed_packets"],
        )

    def test_execution_simulator_collects_exactly_the_guaranteed_group(self):
        plan = cp.plan_best(self.scenario)
        result = csim.simulate(self.scenario, plan)
        self.assertEqual([], result["errors"])
        self.assertEqual(4, result["acquired_weight"])
        self.assertEqual(
            {"PAIR-RAW", "PAIR-SIGN"},
            {r["fragment"] for r in result["receipts"]},
        )
        self.assertTrue(all(r["token"] for r in result["receipts"]))

    def test_early_is_retained_as_a_confirmed_missing_piece(self):
        plan = cp.plan_best(self.scenario)
        self.assertEqual(
            [{
                "packet": "EARLY",
                "fragment": "EARLY-BODY",
                "last_tick": 0,
                "reason": "readable window closed after tick 0",
            }],
            plan["gaps"],
        )

    def test_incomplete_pair_is_never_a_completed_group(self):
        # One PAIR fragment held alone must not count as any weight.
        weight, completed = cp._evaluate(
            self.scenario, {("PAIR-RAW", "R1"): "main"}
        )
        self.assertEqual(0, weight)
        self.assertEqual([], completed)


class LockRouteTests(unittest.TestCase):
    def test_boundary_lock_lets_two_slots_take_one_frozen_release(self):
        doc = {"fragment_case": [
            fragment("PAIR-RAW", "PAIR", 4, [0]),
            fragment("PAIR-SIGN", "PAIR", 4, [0]),
        ]}
        scenario = cp.scenario_from_doc(doc, device_ticks=[0, 1], end_tick=2)
        plan = cp.plan_best(scenario)
        self.assertEqual(cp.LOCK, plan["procedure"])
        self.assertEqual(1, plan["lock_requests"])
        lock = plan["commands"][0]
        self.assertEqual(cp.ACT_LOCK, lock["action"])
        self.assertEqual(0, lock["boundary_tick"])
        self.assertEqual("R1", lock["release"])
        reads = plan["commands"][1:]
        self.assertEqual(
            [0, 1], [c["tick"] for c in reads]
        )
        self.assertEqual(4, plan["guaranteed_weight"])
        result = csim.simulate(scenario, plan)
        self.assertEqual([], result["errors"])
        self.assertEqual(4, result["acquired_weight"])

    def test_lock_cannot_claim_a_publication_that_was_not_observed(self):
        # R2 only appears at tick 1; a boundary-0 lock must not freeze it.
        doc = {"fragment_case": [
            fragment("PAIR-RAW", "PAIR", 4, [0], "R1"),
            fragment("PAIR-SIGN", "PAIR", 4, [0], "R1"),
            fragment("PAIR-RAW", "PAIR", 4, [1], "R2"),
            fragment("PAIR-SIGN", "PAIR", 4, [1], "R2"),
        ]}
        scenario = cp.scenario_from_doc(doc, device_ticks=[0, 1], end_tick=2)
        candidates = {release for _, release in cp._lock_candidates(scenario)}
        self.assertEqual({"R1"}, candidates)
        plan = cp.plan_best(scenario)
        self.assertNotIn("R2", [c.get("release") for c in plan["commands"]])


class RetentionRouteTests(unittest.TestCase):
    def _base_doc(self):
        return {"fragment_case": [
            fragment("EARLY-BODY", "EARLY", 1, [0]),
            fragment("PAIR-RAW", "PAIR", 4, [0, 1]),
            fragment("PAIR-SIGN", "PAIR", 4, [0, 1]),
        ]}

    def test_receiver_retention_keeps_group_complete_without_stopping_publication(self):
        doc = self._base_doc()
        scenario = cp.scenario_from_doc(
            doc, retention=[{"fragment": "PAIR-RAW", "release": "R1"}]
        )
        plan = cp.plan_best(scenario)
        self.assertEqual(cp.RETENTION, plan["procedure"])
        self.assertEqual(0, plan["lock_requests"])
        self.assertEqual(
            5, plan["guaranteed_weight"], "EARLY plus the retained PAIR group"
        )
        retained = [c for c in plan["commands"] if c["action"] == cp.ACT_RETAIN]
        self.assertEqual(
            [{"action": cp.ACT_RETAIN, "device": "receiver", "tick": None,
              "fragment": "PAIR-RAW", "release": "R1"}],
            retained,
        )
        reads = [c for c in plan["commands"] if c["action"] == cp.ACT_READ]
        self.assertEqual(
            [("EARLY-BODY", 0), ("PAIR-SIGN", 1)],
            [(c["fragment"], c["tick"]) for c in reads],
        )
        result = csim.simulate(scenario, plan)
        self.assertEqual([], result["errors"])
        self.assertEqual(5, result["acquired_weight"])

    def test_retained_frame_still_needs_real_same_release_acknowledgement(self):
        doc = self._base_doc()
        scenario = cp.scenario_from_doc(
            doc, retention=[{"fragment": "PAIR-RAW", "release": "R1"}]
        )
        # The retained raw alone, with no main-device acknowledgement read,
        # must not complete any group.
        weight, completed = cp._evaluate(
            scenario,
            {("PAIR-RAW", "R1"): "receiver"},
        )
        self.assertEqual(0, weight)
        self.assertEqual([], completed)
        # And the simulator refuses to treat the retained copy as a PAIR
        # receipt for the still-missing acknowledgement.
        partial_plan = {
            "procedure": cp.RETENTION,
            "world": {"locked_release": None, "lock_tick": None},
            "commands": [{
                "action": cp.ACT_RETAIN, "device": "receiver", "tick": None,
                "fragment": "PAIR-RAW", "release": "R1",
            }],
        }
        result = csim.simulate(scenario, partial_plan)
        self.assertEqual(0, result["acquired_weight"])
        self.assertEqual(
            {"PAIR-RAW", "PAIR-SIGN"},
            {
                item["fragment"]
                for item in result["missing"]
                if item["packet"] == "PAIR"
            },
        )

    def test_lagging_retention_cannot_pair_with_current_ack(self):
        doc = {"fragment_case": [
            # R1 (lagging) only reachable at a pre-horizon tick; R2 is current.
            fragment("PAIR-RAW", "PAIR", 4, [-1], "R1"),
            fragment("PAIR-SIGN", "PAIR", 4, [-1], "R1"),
            fragment("PAIR-RAW", "PAIR", 4, [0, 1], "R2"),
            fragment("PAIR-SIGN", "PAIR", 4, [2, 3], "R2"),
        ]}
        scenario = cp.scenario_from_doc(
            doc,
            device_ticks=[0, 1],
            end_tick=2,
            retention=[{"fragment": "PAIR-RAW", "release": "R1"}],
        )
        # The tempting cross-version pairing must score nothing.
        weight, completed = cp._evaluate(
            scenario,
            {("PAIR-RAW", "R1"): "receiver", ("PAIR-SIGN", "R2"): "main"},
        )
        self.assertEqual(0, weight)
        self.assertEqual([], completed)
        retention_plan = cp._linear_plan(
            scenario,
            procedure=cp.RETENTION,
            world=cp.World(),
            retained=scenario.retention,
        )
        self.assertEqual(0, retention_plan["guaranteed_weight"])

    def test_locking_the_old_group_still_rescues_the_lagging_copy(self):
        doc = {"fragment_case": [
            fragment("PAIR-RAW", "PAIR", 4, [-1], "R1"),
            fragment("PAIR-SIGN", "PAIR", 4, [-1], "R1"),
            # Current R2 publication lands after the two-slot horizon.
            fragment("PAIR-RAW", "PAIR", 4, [2], "R2"),
            fragment("PAIR-SIGN", "PAIR", 4, [2], "R2"),
        ]}
        scenario = cp.scenario_from_doc(
            doc,
            device_ticks=[0, 1],
            end_tick=2,
            retention=[{"fragment": "PAIR-RAW", "release": "R1"}],
        )
        plan = cp.plan_best(scenario)
        self.assertEqual(cp.LOCK, plan["procedure"])
        self.assertEqual("R1", plan["commands"][0]["release"])
        reads = plan["commands"][1:]
        self.assertTrue(all(c["release"] == "R1" for c in reads))
        self.assertEqual(4, plan["guaranteed_weight"])
        result = csim.simulate(scenario, plan)
        self.assertEqual(4, result["acquired_weight"])
        self.assertEqual([], result["errors"])


class OfflineContingencyTests(unittest.TestCase):
    def _scenario(self):
        doc = {"fragment_case": [
            fragment("A-BODY", "A", 1, [0]),
            fragment("X-RAW", "X", 2, [0, 1, 2, 3]),
            fragment("X-SIGN", "X", 2, [0, 1, 2, 3]),
            fragment("Y-RAW", "Y", 3, [0, 1]),
            fragment("Y-SIGN", "Y", 3, [0, 1]),
        ]}
        return cp.scenario_from_doc(
            doc, offline=True, device_ticks=[0, 1, 2, 3], end_tick=4
        )

    def test_first_action_is_legal_under_both_device_states(self):
        scenario = self._scenario()
        plan = cp.plan_offline(scenario, "R1")
        self.assertEqual(cp.CONTINGENT, plan["procedure"])
        self.assertEqual(1, plan["lock_requests"])
        self.assertEqual("ack_pending", plan["pending_request"]["status"])
        first = plan["commands"][0]
        self.assertEqual(cp.ACT_MIRROR, first["action"])
        self.assertEqual(0, first["tick"])
        self.assertEqual([], cp.validate_contingent(plan, scenario))
        self.assertEqual(set(plan["branches"]), {"locked", "rotated"})

    def test_branches_diverge_only_after_observation(self):
        scenario = self._scenario()
        plan = cp.plan_offline(scenario, "R1")
        locked = plan["branches"]["locked"]
        rotated = plan["branches"]["rotated"]
        # Frozen device can still take the tick-0/1 Y group after inspection,
        # using the last slot for A as well; a rotated device can no longer
        # reach Y or A and must take X later.
        self.assertEqual(
            {"A", "Y"}, {g["packet"] for g in locked["completed_packets"]}
        )
        self.assertEqual(
            {"X"}, {g["packet"] for g in rotated["completed_packets"]}
        )
        self.assertEqual(4, locked["guaranteed_weight"])
        self.assertEqual(2, rotated["guaranteed_weight"])
        # Guarantee is the worst compatible state.
        self.assertEqual(2, plan["guaranteed_weight"])
        for branch in (locked, rotated):
            self.assertTrue(all(c["tick"] >= 1 for c in branch["commands"]))

    def test_simulator_replays_each_observed_branch(self):
        scenario = self._scenario()
        plan = cp.plan_offline(scenario, "R1")
        for state, expected in (("locked", 4), ("rotated", 2)):
            result = csim.simulate(scenario, plan, state)
            self.assertEqual([], result["errors"])
            self.assertEqual(state, result["observed_state"])
            self.assertEqual(expected, result["acquired_weight"])
            self.assertEqual("mirror", result["receipts"][0]["source"])

    def test_plan_skipping_observation_is_rejected(self):
        scenario = self._scenario()
        stitched = {
            "procedure": cp.CONTINGENT,
            "lock_requests": 1,
            "commands": [{
                "action": cp.ACT_READ, "tick": 0,
                "fragment": "Y-RAW", "release": "R1",
            }],
            "pending_request": {"release": "R1", "boundary_tick": 0},
            "branches": {
                state: {
                    "world": {"locked_release": None, "lock_tick": None},
                    "commands": [],
                    "guaranteed_weight": 0,
                    "completed_packets": [],
                    "gaps": [],
                }
                for state in ("locked", "rotated")
            },
        }
        errors = cp.validate_contingent(stitched, scenario)
        self.assertTrue(
            any("mirror inspection" in message for message in errors)
        )

    def test_stitched_per_state_optimum_illegal_in_other_state_is_rejected(self):
        scenario = self._scenario()
        stitched = {
            "procedure": cp.CONTINGENT,
            "lock_requests": 1,
            "commands": [{"action": cp.ACT_MIRROR, "tick": 0}],
            "pending_request": {"release": "R1", "boundary_tick": 0},
            "branches": {
                "locked": {
                    "world": {"locked_release": "R1", "lock_tick": 0},
                    "commands": [],
                    "guaranteed_weight": 0,
                    "completed_packets": [],
                    "gaps": [],
                },
                "rotated": {
                    "world": {"locked_release": None, "lock_tick": None},
                    # A-BODY only exists at tick 0, which the mirror used.
                    "commands": [{
                        "action": cp.ACT_READ, "tick": 1,
                        "fragment": "A-BODY", "release": "R1",
                    }],
                    "guaranteed_weight": 0,
                    "completed_packets": [],
                    "gaps": [],
                },
            },
        }
        errors = cp.validate_contingent(stitched, scenario)
        self.assertTrue(
            any("not mounted" in message for message in errors), errors
        )

    def test_cannot_lock_a_release_that_was_not_published(self):
        scenario = self._scenario()
        with self.assertRaises(ValueError):
            cp.plan_offline(scenario, "R9")


class DifferenceDrivenStateAndReplayTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        classification.import_data(self.db, read("waste_ok.json"))
        transport.import_data(self.db, read("manifest_ok.json"))
        closure.import_data(self.db, read("receipt_ok.json"))
        self.doc = read("capture_queue.json")

    def tearDown(self):
        self.db.close()

    def test_normal_reception_leaves_queue_empty_then_dispute_activates_it(self):
        self.assertEqual([], capture_desk.import_data(self.db, self.doc))
        receipt = read("receipt_ok.json")["weights"][0]
        receipt.update(id="WEIGH-INV", at="2026-01-13", net_kg=991)
        decision = closure.import_data(self.db, {"weights": [receipt]})[0]
        self.assertEqual("difference_investigation", decision["state"])
        self.assertEqual(
            ["EARLY", "PAIR"],
            [row["id"] for row in capture_desk.pick_pending(self.db)],
        )

    def test_plan_is_saved_before_submission_and_replay_is_idempotent(self):
        capture_desk.import_data(self.db, self.doc)
        receipt = read("receipt_ok.json")["weights"][0]
        receipt.update(id="WEIGH-INV", at="2026-01-13", net_kg=991)
        closure.import_data(self.db, {"weights": [receipt]})

        capture_desk.build_pending_plan(self.db, "PLAN-1", self.doc)
        steps_before = list(
            self.db.execute("SELECT seq,branch,command FROM capture_plan_step ORDER BY seq")
        )
        # Replaying the same actual input must not double-book device requests.
        capture_desk.build_pending_plan(self.db, "PLAN-1", self.doc)
        steps_after = list(
            self.db.execute("SELECT seq,branch,command FROM capture_plan_step ORDER BY seq")
        )
        self.assertEqual(steps_before, steps_after)
        row = self.db.execute(
            "SELECT procedure,guaranteed_weight,saved FROM capture_plan WHERE id=?",
            ("PLAN-1",),
        ).fetchone()
        self.assertEqual(cp.PLAIN, row["procedure"])
        self.assertEqual(4, row["guaranteed_weight"])
        self.assertEqual(0, row["saved"], "no device submission happened yet")

    def test_only_real_receipts_complete_the_group_and_missing_pieces_remain(self):
        capture_desk.import_data(self.db, self.doc)
        receipt = read("receipt_ok.json")["weights"][0]
        receipt.update(id="WEIGH-INV", at="2026-01-13", net_kg=991)
        closure.import_data(self.db, {"weights": [receipt]})
        capture_desk.build_pending_plan(self.db, "PLAN-1", self.doc)

        scenario = cp.scenario_from_doc(self.doc)
        simulation = csim.simulate(scenario, cp.plan_best(scenario))
        for item in simulation["receipts"]:
            capture_desk.submit_receipt(self.db, "PLAN-1", item)
        # Re-submitting identical real bytes stays idempotent.
        capture_desk.submit_receipt(self.db, "PLAN-1", simulation["receipts"][0])
        stored = capture_desk.list_receipts(self.db, "PLAN-1")
        self.assertEqual(
            {"PAIR-RAW", "PAIR-SIGN"}, {item["fragment"] for item in stored}
        )

        # A started-but-unacknowledged candidate cannot be reported as done.
        with self.assertRaises(ValueError):
            capture_desk.submit_receipt(self.db, "PLAN-1", {
                "token": "made-up-token",
                "fragment": "EARLY-BODY",
                "packet": "EARLY",
                "release": "R1",
                "tick": 0,
                "source": "main",
            })
        missing = capture_desk.list_missing(self.db, "PLAN-1")
        self.assertEqual(
            [("EARLY", "EARLY-BODY", 0)],
            [(item["packet"], item["fragment"], item["last_tick"]) for item in missing],
        )

    def test_plan_does_not_redecide_reception_or_touch_reception_certificates(self):
        capture_desk.import_data(self.db, self.doc)
        receipt = read("receipt_ok.json")["weights"][0]
        receipt.update(id="WEIGH-INV", at="2026-01-13", net_kg=991)
        closure.import_data(self.db, {"weights": [receipt]})
        before = dict(self.db.execute(
            "SELECT * FROM closure WHERE batch='WASTE-1'"
        ).fetchone())
        capture_desk.build_pending_plan(self.db, "PLAN-1", self.doc)
        after = dict(self.db.execute(
            "SELECT * FROM closure WHERE batch='WASTE-1'"
        ).fetchone())
        self.assertEqual(before, after)
        self.assertEqual("difference_investigation", after["state"])

    def test_bounds_are_at_most_three_packets_four_slots_one_pending_request(self):
        doc = {"fragment_case": [
            fragment("A-BODY", "A", 1, [0]),
            fragment("X-RAW", "X", 2, [0, 1, 2, 3]),
            fragment("X-SIGN", "X", 2, [0, 1, 2, 3]),
            fragment("Y-RAW", "Y", 3, [0, 1]),
            fragment("Y-SIGN", "Y", 3, [0, 1]),
        ]}
        scenario = cp.scenario_from_doc(
            doc, offline=True, device_ticks=[0, 1, 2, 3], end_tick=4
        )
        self.assertLessEqual(len(scenario.packets()), cp.MAX_PACKETS)
        self.assertLessEqual(len(scenario.device_ticks), cp.MAX_SLOTS)
        plan = cp.plan_offline(scenario, "R1")
        self.assertLessEqual(plan["lock_requests"], 1)


class OfflineEntryPointTests(unittest.TestCase):
    def test_cli_replays_base_fixture_and_offline_branches(self):
        base = subprocess.run(
            [sys.executable, "-B", "-m", "wastetransit.capture_sim", "run",
             "--input", str(FIXTURES / "capture_queue.json")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, base.returncode, base.stderr)
        payload = json.loads(base.stdout)
        self.assertEqual(4, payload["plan"]["guaranteed_weight"])
        self.assertEqual(4, payload["simulation"]["acquired_weight"])

        offline = subprocess.run(
            [sys.executable, "-B", "-m", "wastetransit.capture_sim", "run",
             "--input", str(FIXTURES / "capture_queue.json"),
             "--offline", "--observe", "rotated",
             "--ticks", "0", "1", "2", "3", "--end", "4"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, offline.returncode, offline.stderr)
        payload = json.loads(offline.stdout)
        self.assertEqual(cp.CONTINGENT, payload["plan"]["procedure"])
        self.assertEqual(
            cp.ACT_MIRROR, payload["plan"]["commands"][0]["action"]
        )
        self.assertEqual("rotated", payload["simulation"]["observed_state"])


if __name__ == "__main__":
    unittest.main()

"""Tests for the guaranteed-weight capture plan and its offline simulation.

Coverage map (station rule):

* full-group trade-off on the shipped fixture (PAIR 4 beats EARLY 1);
* both real acquisition pathways: boundary lock and independent receiver
  retention, including the window they cannot substitute for each other;
* a lagging receiver whose old release must not be stitched onto the current
  sign-off, where locking the old group remains useful;
* the offline lock-acknowledgement gap: first action legal in every
  still-compatible device state, probe, then state-dependent branches;
* the difference-investigation driven pending state and a full receiving-flow
  replay from the existing fixtures;
* independent proofs may still admit a fragment, plans never replace bytes.
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wastetransit import capture_desk as cd
from wastetransit import classification, closure, transport
from wastetransit.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def read_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def command_index(plan, action):
    return [i for i, c in enumerate(plan["commands"]) if c["action"] == action]


def fragments_at(plan, tick):
    return [
        command["fragment"]
        for command in plan["commands"]
        if command["action"] == "primary_read" and command["tick"] == tick
    ]


class GroupTradeOffTests(unittest.TestCase):
    def setUp(self):
        self.doc = read_fixture("capture_queue.json")

    def test_pair_first_beats_expiry_first_queue(self):
        # The old pending list still orders by expiry: EARLY (tick 1) first.
        # The executable plan must instead secure the whole PAIR group.
        plan = cd.build_plan(self.doc)
        self.assertEqual(4, plan["guaranteed_weight"])
        self.assertEqual(["PAIR"], [g["packet"] for g in plan["completed"]])
        self.assertEqual(
            ["PAIR-RAW", "PAIR-SIGN"],
            sorted(plan["completed"][0]["fragments"]),
        )
        self.assertEqual("R0", plan["completed"][0]["publication"])
        # One primary item per discrete tick, end tick 2 is not a window.
        reads = [c for c in plan["commands"] if c["action"] == "primary_read"]
        self.assertEqual([0, 1], sorted(c["tick"] for c in reads))
        self.assertEqual(1, len(fragments_at(plan, 0)))
        self.assertEqual(1, len(fragments_at(plan, 1)))
        # Nothing is spent on the single read that only guarantees weight 1.
        self.assertNotIn("EARLY-BODY", [c["fragment"] for c in plan["commands"]])
        self.assertEqual(
            [("EARLY", ["EARLY-BODY"])],
            [(g["packet"], g["missing_fragments"]) for g in plan["abandoned"]],
        )

    def test_expiry_first_ordering_only_secures_one(self):
        # The literal per-fragment priority queue (EARLY first) must be shown
        # to be inferior: tick 0 spent on EARLY leaves one slot for a PAIR
        # fragment, so no PAIR group can ever complete.
        greedy = {
            "requests": self.doc["requests"],
            "fragment_case": self.doc["fragment_case"],
        }
        greedy_plan = cd.build_plan(greedy)
        simulation = cd.simulate(greedy)
        self.assertEqual(4, simulation["held_weight"])
        # The greedy schedule itself, replayed honestly, only holds one piece.
        greedy_commands = [
            {"action": "primary_read", "tick": 0, "fragment": "EARLY-BODY",
             "packet": "EARLY", "publication": "R0", "mount": "primary"},
            {"action": "primary_read", "tick": 1, "fragment": "PAIR-RAW",
             "packet": "PAIR", "publication": "R0", "mount": "primary"},
        ]
        partial = cd.simulate(
            dict(greedy, plan={"commands": greedy_commands,
                               "completed": [], "abandoned": []})
        )
        self.assertEqual(0, partial["held_weight"])
        self.assertEqual([], partial["completed"])
        self.assertNotIn("PAIR", [g["packet"] for g in partial["completed"]])

    def test_simulation_reports_real_held_bytes_only(self):
        result = cd.simulate(self.doc)
        held = {item["fragment"]: item["publication"]
                for item in result["held_fragments"]}
        self.assertEqual(
            {"PAIR-RAW": "R0", "PAIR-SIGN": "R0"}, held
        )
        self.assertNotIn("EARLY-BODY", held)
        self.assertEqual(4, result["held_weight"])


class BoundaryLockPathwayTests(unittest.TestCase):
    def setUp(self):
        # R1 is the publication the next sampling moment would bring.  A
        # boundary lock keeps R0 mounted and suppresses R1; without the lock
        # only one R0 slot remains before the window rotates.
        self.doc = {
            "end_tick": 2,
            "horizon": 2,
            "procedures": {"lock": {"boundary_tick": 0}},
            "requests": [
                {"id": "PAIR", "batch": "WASTE-1", "fragments": 2,
                 "expires_tick": 2},
                {"id": "LATE", "batch": "WASTE-1", "fragments": 1,
                 "expires_tick": 2},
            ],
            "fragment_case": [
                {"id": "PAIR-RAW", "packet": "PAIR", "weight": 4, "copies": [
                    {"publication": "R0", "mount": "primary",
                     "available_ticks": [0]},
                    {"publication": "R1", "mount": "primary",
                     "available_ticks": [1]},
                ]},
                {"id": "PAIR-SIGN", "packet": "PAIR", "weight": 4, "copies": [
                    {"publication": "R0", "mount": "primary",
                     "available_ticks": [0]},
                    {"publication": "R1", "mount": "primary",
                     "available_ticks": [1]},
                ]},
                {"id": "LATE-BODY", "packet": "LATE", "weight": 2,
                 "publication": "R1", "mount": "primary",
                 "available_ticks": [1]},
            ],
        }

    def test_lock_guarantees_pair_and_does_not_invent_suppressed_release(self):
        plan = cd.build_plan(self.doc)
        self.assertEqual("boundary_lock", plan["strategy"])
        self.assertEqual(4, plan["guaranteed_weight"])
        locks = [c for c in plan["commands"] if c["action"] == "lock_request"]
        self.assertEqual(1, len(locks))
        self.assertEqual(0, locks[0]["boundary_tick"])
        reads = [c for c in plan["commands"] if c["action"] == "primary_read"]
        self.assertTrue(all(c["publication"] == "R0" for c in reads))
        # R1 was suppressed by the lock: no read may pretend it was observed.
        self.assertFalse(
            any(c["publication"] == "R1" for c in plan["commands"])
        )
        simulation = cd.simulate(self.doc, device_state="locked")
        self.assertEqual(["PAIR"], [g["packet"] for g in simulation["completed"]])
        self.assertEqual(4, simulation["held_weight"])

    def test_without_lock_the_pair_cannot_complete(self):
        doc = copy.deepcopy(self.doc)
        del doc["procedures"]["lock"]
        plan = cd.build_plan(doc)
        self.assertEqual("direct", plan["strategy"])
        self.assertEqual(0, plan["guaranteed_weight"])
        self.assertEqual([], plan["completed"])
        simulation = cd.simulate(doc)
        self.assertEqual(0, simulation["held_weight"])


class ReceiverRetentionPathwayTests(unittest.TestCase):
    def setUp(self):
        # The independent receiver retains the R1 raw frame in parallel; the
        # primary device keeps publishing and only owes the sign-off.
        self.doc = {
            "end_tick": 2,
            "horizon": 2,
            "procedures": {
                "receiver_retention": {"covers": ["PAIR-RAW"]}
            },
            "requests": [
                {"id": "PAIR", "batch": "WASTE-1", "fragments": 2,
                 "expires_tick": 2},
                {"id": "LATE", "batch": "WASTE-1", "fragments": 1,
                 "expires_tick": 2},
            ],
            "fragment_case": [
                {"id": "PAIR-RAW", "packet": "PAIR", "weight": 4, "copies": [
                    {"publication": "R1", "mount": "receiver",
                     "available_ticks": [0, 1]},
                    {"publication": "R1", "mount": "primary",
                     "available_ticks": [0, 1]},
                ]},
                {"id": "PAIR-SIGN", "packet": "PAIR", "weight": 4,
                 "publication": "R1", "mount": "primary",
                 "available_ticks": [0, 1]},
                {"id": "LATE-BODY", "packet": "LATE", "weight": 2,
                 "publication": "R1", "mount": "primary",
                 "available_ticks": [1]},
            ],
        }

    def test_receiver_holds_raw_while_primary_keeps_publishing(self):
        plan = cd.build_plan(self.doc)
        self.assertEqual("direct", plan["strategy"])
        collects = [c for c in plan["commands"]
                    if c["action"] == "receiver_collect"]
        self.assertEqual(1, len(collects))
        self.assertEqual("PAIR-RAW", collects[0]["fragment"])
        # No lock request: the primary device's next publication still happens.
        self.assertEqual([], command_index(plan, "lock_request"))
        sign_reads = [c for c in plan["commands"]
                      if c["action"] == "primary_read"
                      and c["fragment"] == "PAIR-SIGN"]
        self.assertEqual(1, len(sign_reads))
        simulation = cd.simulate(self.doc)
        completed = {g["packet"]: g for g in simulation["completed"]}
        self.assertIn("PAIR", completed)
        self.assertEqual("R1", completed["PAIR"]["publication"])

    def test_receiver_pathway_keeps_the_window_the_lock_would_close(self):
        # The same group can finish *and* the later primary-only packet stays
        # reachable, which a boundary lock could not offer.
        plan = cd.build_plan(self.doc)
        simulation = cd.simulate(self.doc)
        completed = {g["packet"] for g in simulation["completed"]}
        self.assertIn("PAIR", completed)
        self.assertIn("LATE", completed)
        self.assertEqual(6, simulation["held_weight"])


class StaleReceiverTests(unittest.TestCase):
    def test_lagging_receiver_cannot_pair_with_current_signoff(self):
        doc = {
            "end_tick": 2,
            "horizon": 2,
            "procedures": {
                "lock": {"boundary_tick": 0},
                "receiver_retention": {"covers": ["PAIR-RAW"]},
            },
            "requests": [
                {"id": "PAIR", "batch": "WASTE-1", "fragments": 2,
                 "expires_tick": 2},
                {"id": "LATE", "batch": "WASTE-1", "fragments": 1,
                 "expires_tick": 2},
            ],
            "fragment_case": [
                # Receiver is one release behind: it only retains R0.
                {"id": "PAIR-RAW", "packet": "PAIR", "weight": 4, "copies": [
                    {"publication": "R0", "mount": "receiver",
                     "available_ticks": [0]},
                    {"publication": "R1", "mount": "primary",
                     "available_ticks": [1]},
                ]},
                {"id": "PAIR-SIGN", "packet": "PAIR", "weight": 4,
                 "publication": "R1", "mount": "primary",
                 "available_ticks": [1]},
                {"id": "LATE-BODY", "packet": "LATE", "weight": 2,
                 "publication": "R1", "mount": "primary",
                 "available_ticks": [1]},
            ],
        }
        plan = cd.build_plan(doc)
        # Cross-release stitching is invalid, so the receiver copy cannot join
        # the R1 sign-off; the lock on the old R0 group is what still pays off.
        self.assertEqual("boundary_lock", plan["strategy"])
        simulation = cd.simulate(doc, device_state="locked")
        pair = next((g for g in simulation["completed"]
                     if g["packet"] == "PAIR"), None)
        self.assertIsNotNone(pair)
        self.assertEqual("R0", pair["publication"])
        held = {item["fragment"]: item["publication"]
                for item in simulation["held_fragments"]}
        self.assertEqual("R0", held["PAIR-RAW"])
        # And a fabricated R0/R1 pair is rejected by the scorer.
        forged = {"PAIR-RAW": "R0", "PAIR-SIGN": "R1"}
        completed, abandoned = cd._score(cd._normalize(doc), forged, set())
        self.assertEqual([], completed)
        self.assertEqual(["PAIR"], [g["packet"] for g in abandoned])


class OfflinePolicyTests(unittest.TestCase):
    def setUp(self):
        # Lock request already sent at the R0/R1 boundary; no acknowledgement
        # came back.  Both "locked" and "rotated" stay compatible with silence.
        self.doc = {
            "end_tick": 2,
            "horizon": 2,
            "procedures": {
                "receiver_retention": {"covers": ["LATE-RAW"]}
            },
            "offline": {"states": ["locked", "rotated"],
                        "boundary_tick": 0, "probe_tick": 0},
            "requests": [
                {"id": "PAIR", "batch": "WASTE-1", "fragments": 2,
                 "expires_tick": 2},
                {"id": "LATE", "batch": "WASTE-1", "fragments": 1,
                 "expires_tick": 2},
            ],
            "fragment_case": [
                {"id": "PAIR-RAW", "packet": "PAIR", "weight": 4,
                 "publication": "R0", "mount": "primary",
                 "available_ticks": [0]},
                {"id": "PAIR-SIGN", "packet": "PAIR", "weight": 4,
                 "publication": "R0", "mount": "primary",
                 "available_ticks": [0]},
                {"id": "LATE-RAW", "packet": "LATE", "weight": 1, "copies": [
                    {"publication": "R1", "mount": "receiver",
                     "available_ticks": [1]},
                ]},
                {"id": "LATE-SIGN", "packet": "LATE", "weight": 1,
                 "publication": "R1", "mount": "primary",
                 "available_ticks": [1]},
            ],
        }

    def test_first_action_is_probe_and_is_legal_in_each_state(self):
        plan = cd.build_plan(self.doc)
        self.assertTrue(plan["offline"])
        self.assertEqual("state_probe", plan["commands"][0]["action"])
        self.assertEqual(0, plan["commands"][0]["tick"])
        # The probe itself assumes neither hidden state: validation accepts it.
        self.assertTrue(cd.validate_conditional_plan(cd._normalize(self.doc), plan))
        # No branch may read at or before the probe tick.
        for branch in plan["branches"].values():
            self.assertTrue(
                all(c["tick"] > plan["probe_tick"]
                    for c in branch["commands"]
                    if c["action"] == "primary_read")
            )
        # The single outstanding request is never re-issued inside a branch.
        for branch in plan["branches"].values():
            self.assertFalse(
                any(c["action"] == "lock_request" for c in branch["commands"])
            )

    def test_branches_follow_the_observed_device_state(self):
        plan = cd.build_plan(self.doc)
        locked = plan["branches"]["locked"]
        rotated = plan["branches"]["rotated"]
        # Locked device: the R0 PAIR group stayed mounted and can be completed.
        locked_sim = cd.simulate(self.doc, device_state="locked",
                                 probe_observation="locked")
        self.assertIn("PAIR", [g["packet"] for g in locked_sim["completed"]])
        self.assertEqual(
            locked["guaranteed_weight"], locked_sim["held_weight"]
        )
        # Rotated device: R0 primary copies are gone, R1 work is still legal.
        rotated_sim = cd.simulate(self.doc, device_state="rotated",
                                  probe_observation="rotated")
        self.assertNotIn("PAIR",
                         [g["packet"] for g in rotated_sim["completed"]])
        self.assertIn("LATE", [g["packet"] for g in rotated_sim["completed"]])
        # The guarantee is the worst case across the hidden states.
        self.assertEqual(
            min(locked["guaranteed_weight"], rotated["guaranteed_weight"]),
            plan["guaranteed_weight"],
        )

    def test_stitched_per_state_plan_without_probe_is_rejected(self):
        plan = cd.build_plan(self.doc)
        stitched = copy.deepcopy(plan)
        # Pretend the first command is a state-dependent primary read instead
        # of the probe: this is exactly the forbidden "best plan per hidden
        # state, glued into one field order".
        stitched["commands"] = [{
            "action": "primary_read", "tick": 0, "fragment": "PAIR-RAW",
            "packet": "PAIR", "publication": "R0", "mount": "primary",
        }]
        with self.assertRaises(ValueError):
            cd.validate_conditional_plan(cd._normalize(self.doc), stitched)

    def test_branch_read_illegal_in_its_own_state_is_rejected(self):
        plan = cd.build_plan(self.doc)
        tampered = copy.deepcopy(plan)
        # An R0 read after rotation can never be legal.
        tampered["branches"]["rotated"]["commands"].append({
            "action": "primary_read", "tick": 1, "fragment": "PAIR-SIGN",
            "packet": "PAIR", "publication": "R0", "mount": "primary",
        })
        with self.assertRaises(ValueError):
            cd.validate_conditional_plan(cd._normalize(self.doc), tampered)

    def test_abandoned_group_keeps_missing_parts_and_occurrence(self):
        doc = copy.deepcopy(self.doc)
        # Remove the salvageable R1 group so PAIR is unrecoverable when
        # rotated: the plan must still name the missing bytes and tick.
        doc["requests"] = [doc["requests"][0]]
        doc["fragment_case"] = doc["fragment_case"][:2]
        del doc["procedures"]["receiver_retention"]
        plan = cd.build_plan(doc)
        abandoned = next(g for g in plan["abandoned"] if g["packet"] == "PAIR")
        self.assertEqual(["PAIR-RAW", "PAIR-SIGN"],
                         abandoned["missing_fragments"])
        self.assertEqual(0, abandoned["occurrence_tick"])
        # Even the locked branch cannot help once the probe consumed tick 0
        # unless the lock kept R0 mounted - in that branch PAIR still works,
        # so the retention entry only appears when *no* branch salvages it.
        rotated_sim = cd.simulate(doc, device_state="rotated",
                                  probe_observation="rotated")
        self.assertEqual(0, rotated_sim["held_weight"])


class IndependentProofTests(unittest.TestCase):
    def test_independent_proof_admits_fragment_without_faking_bytes(self):
        doc = read_fixture("capture_queue.json")
        # EARLY's single fragment is independently proven; PAIR still has to
        # be really read.  The proof never shows up as a held byte.
        doc["independent_proof"] = ["EARLY-BODY"]
        plan = cd.build_plan(doc)
        completed = {g["packet"] for g in plan["completed"]}
        self.assertEqual({"EARLY", "PAIR"}, completed)
        simulation = cd.simulate(doc)
        held = {item["fragment"] for item in simulation["held_fragments"]}
        self.assertNotIn("EARLY-BODY", held)
        self.assertEqual(["EARLY-BODY"], simulation["proven_without_bytes"])
        self.assertEqual(5, simulation["held_weight"]
                         + sum(1 for _ in simulation["proven_without_bytes"]))


class DifferenceDrivenStateTests(unittest.TestCase):
    def _receiving_desk(self, dispute=True):
        db = connect(":memory:")
        classification.import_data(db, read_fixture("waste_ok.json"))
        transport.import_data(db, read_fixture("manifest_ok.json"))
        closure.import_data(db, read_fixture("receipt_ok.json"))
        self.assertEqual([], cd.import_data(db, read_fixture("capture_queue.json")))
        if dispute:
            receipt = read_fixture("receipt_ok.json")["weights"][0]
            receipt.update(id="WEIGH-INV", at="2026-01-13", net_kg=991)
            result = closure.import_data(db, {"weights": [receipt]})[0]
            self.assertEqual("difference_investigation", result["state"])
        return db

    def test_normal_reception_leaves_queue_empty(self):
        db = self._receiving_desk(dispute=False)
        try:
            self.assertEqual([], cd.pick_pending(db))
            self.assertEqual([], cd.rank_pending(db))
        finally:
            db.close()

    def test_dispute_creates_early_and_pair_tasks_and_plan_from_db(self):
        db = self._receiving_desk()
        try:
            rows = cd.pick_pending(db)
            self.assertEqual(["EARLY", "PAIR"], [row["id"] for row in rows])
            plan = cd.plan_capture(db)
            self.assertEqual(4, plan["guaranteed_weight"])
            self.assertEqual(["PAIR"], [g["packet"] for g in plan["completed"]])
            # The new ranking is weight-driven, not expiry-driven.
            ranked = cd.rank_pending(db)
            self.assertEqual("PAIR", ranked[0]["id"])
        finally:
            db.close()

    def test_replay_through_full_receiving_flow(self):
        db = connect(":memory:")
        try:
            classification.import_data(db, read_fixture("waste_ok.json"))
            transport.import_data(db, read_fixture("manifest_ok.json"))
            closure.import_data(db, read_fixture("receipt_ok.json"))
            cd.import_data(db, read_fixture("capture_queue.json"))
            # Re-importing the same document is idempotent.
            cd.import_data(db, read_fixture("capture_queue.json"))
            receipt = read_fixture("receipt_ok.json")["weights"][0].copy()
            receipt.update(id="WEIGH-INV", at="2026-01-13", net_kg=991)
            closure.import_data(db, {"weights": [receipt]})
            plan = cd.plan_capture(db)
            simulation = cd.simulate(cd.problem_from_db(db))
            self.assertEqual(plan["guaranteed_weight"],
                             simulation["held_weight"])
            # A later in-tolerance reweigh closes the case and parks the queue.
            reweigh = read_fixture("receipt_ok.json")["weights"][0].copy()
            reweigh.update(id="WEIGH-RECHECK", at="2026-01-14", net_kg=1000,
                           kind="reweigh")
            self.assertEqual("closed",
                             closure.import_data(db, {"weights": [reweigh]})[0]["state"])
            self.assertEqual([], cd.pick_pending(db))
        finally:
            db.close()


class StationRuleCapTests(unittest.TestCase):
    def test_caps_are_enforced(self):
        base = read_fixture("capture_queue.json")
        too_many = copy.deepcopy(base)
        too_many["requests"].append(
            {"id": "EXTRA", "batch": "WASTE-1", "fragments": 1,
             "expires_tick": 2}
        )
        too_many["fragment_case"].append(
            {"id": "EXTRA-BODY", "packet": "EXTRA", "weight": 1,
             "available_ticks": [0]}
        )
        with self.assertRaises(ValueError):
            cd.build_plan(too_many)
        wide = copy.deepcopy(base)
        wide["horizon"] = 5
        with self.assertRaises(ValueError):
            cd.build_plan(wide)


class RunnerTests(unittest.TestCase):
    def test_cli_plan_and_simulate_offline(self):
        scenario = {
            "end_tick": 2,
            "horizon": 2,
            "offline": {"states": ["locked", "rotated"],
                        "boundary_tick": 0, "probe_tick": 0},
            "requests": [
                {"id": "PAIR", "batch": "WASTE-1", "fragments": 2,
                 "expires_tick": 2},
            ],
            "fragment_case": [
                {"id": "PAIR-RAW", "packet": "PAIR", "weight": 4,
                 "publication": "R0", "mount": "primary",
                 "available_ticks": [0]},
                {"id": "PAIR-SIGN", "packet": "PAIR", "weight": 4,
                 "publication": "R0", "mount": "primary",
                 "available_ticks": [0]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            scenario_path = Path(tmp) / "scenario.json"
            plan_path = Path(tmp) / "plan.json")
            scenario_path.write_text(
                json.dumps(scenario, ensure_ascii=False), encoding="utf-"
            )
            plan_run = subprocess.run(
                [sys.executable, "-B", "-m", "wastetransit.capture_run",
                 "plan", "--input", str(scenario_path),
                 "--save-plan", str(plan_path)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(0, plan_run.returncode, plan_run.stderr)
            self.assertTrue(plan_path.exists())
            sim_run = subprocess.run(
                [sys.executable, "-B", "-m", "wastetransit.capture_run",
                 "simulate", "--input", str(scenario_path),
                 "--device-state", "rotated", "--observe", "rotated"],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(0, sim_run.returncode, sim_run.stderr)
            payload = json.loads(sim_run.stdout)
            self.assertEqual("rotated", payload["simulation"]["device_state"])
            self.assertEqual(0, payload["simulation"]["held_weight"])


if __name__ == "__main__":
    unittest.main()

"""Field-drill tests for the residue cycle credential.

The three originals are exercised independently:

1. ``residue_cycle.json``        - unordered equipment counts (never enough)
2. interlock attestations        - contract 1 (guarded controller)
3. station raw frames            - contract 2 (component witnesses + run clock)

Everything here is synthetic L/P/E state with arbitrary timestamps; no
disposal operation or real instrument is involved.
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wastetransit.store import connect
from wastetransit import classification, closure, residue_trace, transport

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def fx(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ResidueCycleTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        classification.import_data(self.db, fx("waste_ok.json"))
        transport.import_data(self.db, fx("manifest_ok.json"))
        closure.import_data(self.db, fx("receipt_ok.json"))
        residue_trace.import_data(self.db, fx("residue_cycle.json"))

    def tearDown(self):
        self.db.close()

    def credential(self, component):
        return dict(self.db.execute(
            "SELECT * FROM cycle_credential WHERE component_id=?",
            (component,),
        ).fetchone())

    # -- original 1: counts alone can never answer the question -----------

    def test_count_summary_leaves_both_histories_open(self):
        rows = residue_trace.import_data(self.db, fx("residue_cycle.json"))
        self.assertEqual("claimed_complete", rows[0]["state"])
        answer = residue_trace.query(self.db, "WASTE-1")[0]
        self.assertEqual([], answer["credentials"])
        unresolved = answer["unresolved_counts"][0]
        self.assertIsNone(unresolved["answer_run_inside_residence"])
        # The two executable readings the same three counts cannot separate.
        self.assertIn(["load", "run", "exit"], unresolved["blocking_orders"])
        self.assertIn(["load", "exit", "run"], unresolved["blocking_orders"])

    # -- original 2: contract 1, guarded controller -----------------------

    def test_interlock_certifies_without_precise_run_timestamp(self):
        rows = residue_trace.import_interlock(
            self.db, fx("residue_interlock_ok.json")
        )
        cred = [r for r in rows if r["component_id"] == "BOX-A"][0]
        self.assertEqual("cycle_certified", cred["state"])
        self.assertEqual("certified", cred["proof_state"])
        self.assertEqual("guarded_interlock", cred["basis"])
        self.assertTrue(cred["run_inside_residence"])
        self.assertIsNone(cred["run_at"])  # no precise run moment needed

    def test_unproven_bypass_only_proves_bare_machine_run(self):
        residue_trace.import_interlock(
            self.db, fx("residue_interlock_bypass.json")
        )
        cred = self.credential("BOX-B")
        self.assertEqual("pending_recollection", cred["state"])
        self.assertNotEqual("certified", cred["proof_state"])
        # The bare-machine run fact is retained in the common history.
        query = residue_trace.query(self.db, "WASTE-1")[0]
        entry = next(c for c in query["credentials"]
                     if c["component_id"] == "BOX-B")
        self.assertIn("run", [e["kind"] for e in entry["events"]])

    def test_each_interlock_fact_is_individually_necessary(self):
        base = {
            "device_id": "STATION-1", "batch": "WASTE-1",
            "ticket_id": "TICKET-GUARD", "lock_engaged": True,
            "binding_continuous": True, "run_confirmed": True,
            "released_at_removal": True, "bypass_state": "off",
            "run_at": None,
        }
        for missing in ("lock_engaged", "binding_continuous",
                        "run_confirmed", "released_at_removal"):
            item = dict(base, id="ILK-MISS-" + missing,
                        component_id="BOX-M-" + missing)
            item[missing] = False
            residue_trace.import_interlock(self.db, {"attestations": [item]})
            self.assertNotEqual(
                "cycle_certified", self.credential("BOX-M-" + missing)["state"],
                missing,
            )

    def test_explicit_bypass_on_blocks_interlock_certificate(self):
        item = dict(fx("residue_interlock_ok.json")["attestations"][0],
                    id="ILK-ON", component_id="BOX-ON",
                    bypass_state="on")
        residue_trace.import_interlock(self.db, {"attestations": [item]})
        self.assertNotEqual(
            "cycle_certified", self.credential("BOX-ON")["state"]
        )

    def test_later_frames_can_contradict_an_interlock_certificate(self):
        residue_trace.import_interlock(
            self.db, fx("residue_interlock_ok.json")
        )
        self.assertEqual(
            "cycle_certified", self.credential("BOX-A")["state"]
        )
        frames = {"frames": [
            {"id": "X-L", "event_id": "X-L", "ticket_id": "T",
             "device_id": "STATION-1", "component_id": "BOX-A",
             "batch": "WASTE-1", "kind": "load", "seq": 1,
             "at": "t1", "witness": "w"},
            {"id": "X-E", "event_id": "X-E", "ticket_id": "T",
             "device_id": "STATION-1", "component_id": "BOX-A",
             "batch": "WASTE-1", "kind": "exit", "seq": 2,
             "at": "t2", "witness": "w"},
            {"id": "X-P", "event_id": "X-P", "ticket_id": "T",
             "device_id": "STATION-1", "component_id": "BOX-A",
             "batch": "WASTE-1", "kind": "run", "seq": 3,
             "at": "t3", "source": "independent_output"},
        ]}
        residue_trace.import_station(self.db, frames)
        cred = self.credential("BOX-A")
        self.assertEqual("sequence_conflict", cred["state"])
        self.assertFalse(cred["run_inside_residence"])

    # -- original 3: contract 2, station frames ---------------------------

    def test_station_frames_certify_with_ordered_timeline(self):
        rows = residue_trace.import_station(
            self.db, fx("residue_station_ok.json")
        )
        cred = [r for r in rows if r["component_id"] == "BOX-C"][0]
        self.assertEqual("cycle_certified", cred["state"])
        self.assertEqual("station_frames", cred["basis"])
        self.assertEqual("2026-01-12T08:30:00Z", cred["run_at"])

    def test_swapable_run_exit_blocks_signing_and_shows_orders(self):
        residue_trace.import_station(
            self.db, fx("residue_station_swappable.json")
        )
        cred = self.credential("BOX-D")
        self.assertEqual("uncertain_blocking", cred["state"])
        self.assertIsNone(cred["run_inside_residence"])
        entry = next(
            c for c in residue_trace.query(self.db, "WASTE-1")[0]["credentials"]
            if c["component_id"] == "BOX-D"
        )
        flat = {" ".join(order) for order in entry["blocking_orders"]}
        self.assertIn("load run exit", flat)
        self.assertIn("load exit run", flat)

    def test_later_evidence_narrows_uncertainty_without_synthesising_events(self):
        residue_trace.import_station(
            self.db, fx("residue_station_swappable.json")
        )
        self.assertEqual(
            "uncertain_blocking", self.credential("BOX-D")["state"]
        )
        before = self.db.execute(
            "SELECT count(*) FROM residue_frame"
        ).fetchone()[0]
        # A second ticket re-reads the same physical exit event with a clock.
        supplement = {"frames": [{
            "id": "FRM-NEW", "event_id": "EV2-E",
            "ticket_id": "TICKET-RUN", "device_id": "STATION-2",
            "component_id": "BOX-D", "batch": "WASTE-1",
            "kind": "exit", "seq": 2,
            "at": "2026-01-12T09:00:00Z", "witness": "station_officer",
        }]}
        residue_trace.import_station(self.db, supplement)
        cred = self.credential("BOX-D")
        self.assertEqual("cycle_certified", cred["state"])
        self.assertEqual("station_frames", cred["basis"])
        # Only one new frame row, and still one merged physical exit event.
        self.assertEqual(before + 1,
                         self.db.execute(
                             "SELECT count(*) FROM residue_frame").fetchone()[0])
        entry = next(
            c for c in residue_trace.query(self.db, "WASTE-1")[0]["credentials"]
            if c["component_id"] == "BOX-D"
        )
        exits = [e for e in entry["events"] if e["kind"] == "exit"]
        self.assertEqual(1, len(exits))
        self.assertEqual({"TICKET-S2", "TICKET-RUN"},
                         set(exits[0]["tickets"]))

    def test_cross_ticket_continuation_duplicate_is_one_physical_event(self):
        doc = {"frames": [
            {"id": "F-L", "event_id": "E-L", "ticket_id": "T1",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "load", "seq": 1, "at": "t0", "witness": "w"},
            {"id": "F-C1", "event_id": "E-C", "ticket_id": "T1",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "continuation", "seq": 2,
             "witness": "w", "requires_lock": True},
            {"id": "F-LK", "event_id": "E-LK", "ticket_id": "T2",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "lock", "seq": 1, "witness": "controller"},
            {"id": "F-UL", "event_id": "E-UL", "ticket_id": "T2",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "unlock", "seq": 2, "witness": "controller"},
            {"id": "F-C2", "event_id": "E-C", "ticket_id": "T3",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "continuation", "seq": 1,
             "witness": "w", "requires_lock": True},
            {"id": "F-E", "event_id": "E-E", "ticket_id": "T3",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "exit", "seq": 2, "at": "t3", "witness": "w"},
        ]}
        residue_trace.import_station(self.db, doc)
        entry = next(
            c for c in residue_trace.query(self.db, "WASTE-1")[0]["credentials"]
            if c["component_id"] == "K"
        )
        continuations = [e for e in entry["events"]
                         if e["kind"] == "continuation"]
        self.assertEqual(1, len(continuations))
        self.assertEqual(["F-C1", "F-C2"], continuations[0]["sources"])
        self.assertEqual({"T1", "T3"}, set(continuations[0]["tickets"]))

    def test_impossible_lock_state_across_tickets_is_a_conflict(self):
        doc = {"frames": [
            {"id": "F-L", "event_id": "E-L", "ticket_id": "T2",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "load", "seq": 1, "at": "t0", "witness": "w"},
            {"id": "F-LK", "event_id": "E-LK", "ticket_id": "T2",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "lock", "seq": 2, "witness": "controller"},
            {"id": "F-UL", "event_id": "E-UL", "ticket_id": "T2",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "unlock", "seq": 3, "witness": "controller"},
            {"id": "F-C2", "event_id": "E-C2", "ticket_id": "T2",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "continuation", "seq": 4,
             "witness": "w", "requires_lock": True},
            {"id": "F-E", "event_id": "E-E", "ticket_id": "T2",
             "device_id": "M", "component_id": "K", "batch": "WASTE-1",
             "kind": "exit", "seq": 5, "at": "t3", "witness": "w"},
        ]}
        residue_trace.import_station(self.db, doc)
        cred = self.credential("K")
        self.assertEqual("sequence_conflict", cred["state"])
        entry = next(
            c for c in residue_trace.query(self.db, "WASTE-1")[0]["credentials"]
            if c["component_id"] == "K"
        )
        self.assertTrue(entry["blocking_orders"])

    def test_run_after_exit_is_false_inside_residence(self):
        doc = {"frames": [
            {"id": "G-L", "event_id": "G-L", "ticket_id": "U1",
             "device_id": "M", "component_id": "J", "batch": "WASTE-1",
             "kind": "load", "seq": 1, "at": "t1", "witness": "w"},
            {"id": "G-C", "event_id": "G-C", "ticket_id": "U1",
             "device_id": "M", "component_id": "J", "batch": "WASTE-1",
             "kind": "continuation", "seq": 2, "witness": "w"},
            {"id": "G-E", "event_id": "G-E", "ticket_id": "U1",
             "device_id": "M", "component_id": "J", "batch": "WASTE-1",
             "kind": "exit", "seq": 3, "at": "t2", "witness": "w"},
            {"id": "G-P", "event_id": "G-P", "ticket_id": "U2",
             "device_id": "M", "component_id": "J", "batch": "WASTE-1",
             "kind": "run", "seq": 1, "at": "t3",
             "source": "independent_output"},
        ]}
        residue_trace.import_station(self.db, doc)
        cred = self.credential("J")
        self.assertEqual("sequence_conflict", cred["state"])
        self.assertFalse(cred["run_inside_residence"])

    def test_equal_timestamps_do_not_invent_a_conflict(self):
        # Two ticket-ordered events sharing one clock reading are consistent.
        doc = {"frames": [
            {"id": "Q1", "event_id": "Q-L", "ticket_id": "U",
             "device_id": "D", "component_id": "Q", "batch": "WASTE-1",
             "kind": "load", "seq": 1, "at": "same", "witness": "w"},
            {"id": "Q2", "event_id": "Q-C", "ticket_id": "U",
             "device_id": "D", "component_id": "Q", "batch": "WASTE-1",
             "kind": "continuation", "seq": 2, "at": "same",
             "witness": "w"},
        ]}
        residue_trace.import_station(self.db, doc)
        _events, _edges, conflict = residue_trace._physical_events(
            self.db, "WASTE-1", "D", "Q"
        )
        self.assertIsNone(conflict)

    def test_timestamps_contradicting_ticket_order_are_a_conflict(self):
        doc = {"frames": [
            {"id": "R1", "event_id": "R-L", "ticket_id": "V",
             "device_id": "D", "component_id": "R", "batch": "WASTE-1",
             "kind": "load", "seq": 1, "at": "t2", "witness": "w"},
            {"id": "R2", "event_id": "R-E", "ticket_id": "V",
             "device_id": "D", "component_id": "R", "batch": "WASTE-1",
             "kind": "exit", "seq": 2, "at": "t1", "witness": "w"},
        ]}
        residue_trace.import_station(self.db, doc)
        self.assertEqual(
            "sequence_conflict", self.credential("R")["state"]
        )

    def test_exit_without_proof_keeps_synthetic_pending_and_keeps_facts(self):
        doc = {"frames": [
            {"id": "W-L", "event_id": "W-L", "ticket_id": "U1",
             "device_id": "M", "component_id": "P", "batch": "WASTE-1",
             "kind": "load", "seq": 1, "witness": "w"},
            {"id": "W-E", "event_id": "W-E", "ticket_id": "U1",
             "device_id": "M", "component_id": "P", "batch": "WASTE-1",
             "kind": "exit", "seq": 2, "witness": "w"},
        ]}
        residue_trace.import_station(self.db, doc)
        cred = self.credential("P")
        self.assertEqual("pending_recollection", cred["state"])
        self.assertEqual(2, self.db.execute(
            "SELECT count(*) FROM residue_frame WHERE component_id='P'"
        ).fetchone()[0])

    # -- receipt authorisations gate the two contracts --------------------

    def test_receipt_authorisations_gate_each_contract(self):
        guarded_only = {
            "requires_intact_cycle_proof": False,
            "accepts_guarded_controller": True,
        }
        intact_only = {
            "requires_intact_cycle_proof": True,
            "accepts_guarded_controller": False,
        }
        for restriction, component, fixture, expect_basis in (
            (guarded_only, "BOX-A", "residue_interlock_ok.json",
             "guarded_interlock"),
            (intact_only, "BOX-C", "residue_station_ok.json",
             "station_frames"),
        ):
            db = connect(":memory:")
            try:
                classification.import_data(db, fx("waste_ok.json"))
                transport.import_data(db, fx("manifest_ok.json"))
                closure.import_data(db, fx("receipt_ok.json"))
                doc = fx("residue_cycle.json")
                doc["receipt_restrictions"] = restriction
                residue_trace.import_data(db, doc)
                if expect_basis == "guarded_interlock":
                    residue_trace.import_interlock(db, fx(fixture))
                else:
                    residue_trace.import_station(db, fx(fixture))
                cred = db.execute(
                    "SELECT * FROM cycle_credential WHERE component_id=?",
                    (component,),
                ).fetchone()
                self.assertEqual("cycle_certified", cred["state"])
                self.assertEqual(expect_basis, cred["basis"])
            finally:
                db.close()

        db = connect(":memory:")
        try:
            classification.import_data(db, fx("waste_ok.json"))
            transport.import_data(db, fx("manifest_ok.json"))
            closure.import_data(db, fx("receipt_ok.json"))
            doc = fx("residue_cycle.json")
            doc["receipt_restrictions"] = intact_only
            residue_trace.import_data(db, doc)
            residue_trace.import_interlock(
                db, fx("residue_interlock_ok.json")
            )
            cred = db.execute(
                "SELECT * FROM cycle_credential WHERE component_id='BOX-A'"
            ).fetchone()
            self.assertNotEqual("cycle_certified", cred["state"])
        finally:
            db.close()

    # -- mother-batch gate and late reweigh -------------------------------

    def test_credential_is_held_at_origin_without_any_closure(self):
        db = connect(":memory:")
        try:
            residue_trace.import_interlock(
                db, fx("residue_interlock_ok.json")
            )
            cred = db.execute(
                "SELECT * FROM cycle_credential WHERE component_id='BOX-A'"
            ).fetchone()
            self.assertEqual("hold_origin", cred["state"])
            self.assertEqual("certified", cred["proof_state"])
        finally:
            db.close()

    def test_open_mother_batch_holds_credential_but_keeps_field_facts(self):
        residue_trace.import_interlock(
            self.db, fx("residue_interlock_ok.json")
        )
        self.assertEqual(
            "cycle_certified", self.credential("BOX-A")["state"]
        )
        later = fx("receipt_ok.json")["weights"][0].copy()
        later.update(id="WEIGH-RECHECK", net_kg=991, at="2026-01-13")
        closure.import_data(self.db, {"weights": [later]})
        self.assertEqual(
            "hold_origin",
            self.db.execute(
                "SELECT state FROM residue_summary").fetchone()[0],
        )
        cred = self.credential("BOX-A")
        self.assertEqual("hold_origin", cred["state"])
        self.assertEqual("certified", cred["proof_state"])
        self.assertEqual(1, self.db.execute(
            "SELECT count(*) FROM interlock_attestation").fetchone()[0])

    def test_reweigh_recovery_reissues_after_batches_re_close(self):
        residue_trace.import_interlock(
            self.db, fx("residue_interlock_ok.json")
        )
        bad = fx("receipt_ok.json")["weights"][0].copy()
        bad.update(id="WEIGH-RECHECK", net_kg=991, at="2026-01-13")
        closure.import_data(self.db, {"weights": [bad]})
        self.assertEqual("hold_origin", self.credential("BOX-A")["state"])
        good = fx("receipt_ok.json")["weights"][0].copy()
        good.update(id="WEIGH-RECHECK-2", net_kg=998, at="2026-01-14",
                    kind="reweigh")
        closure.import_data(self.db, {"weights": [good]})
        self.assertEqual(
            "cycle_certified", self.credential("BOX-A")["state"]
        )

    # -- replay and idempotence -------------------------------------------

    def test_full_journal_replay_reproduces_credentials(self):
        import json as _json
        from wastetransit.source import file_record

        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "state.sqlite")
            db = connect(db_path)
            try:
                journal = (
                    ("waste_ok.json", "waste-import",
                     classification.import_data),
                    ("manifest_ok.json", "manifest-import",
                     transport.import_data),
                    ("receipt_ok.json", "receipt-import",
                     closure.import_data),
                    ("residue_cycle.json", "residue-import",
                     residue_trace.import_data),
                    ("residue_interlock_ok.json", "interlock-import",
                     residue_trace.import_interlock),
                    ("residue_station_swappable.json", "station-import",
                     residue_trace.import_station),
                )
                for name, command, handler in journal:
                    raw = (FIXTURES / name).read_bytes()
                    handler(db, _json.loads(raw))
                    with db:
                        file_record(db, command, raw)
            finally:
                db.close()
            result = subprocess.run(
                [sys.executable, "-m", "wastetransit", "audit",
                 "--db", db_path],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])

    def test_re_import_is_idempotent(self):
        residue_trace.import_interlock(
            self.db, fx("residue_interlock_ok.json")
        )
        first = self.credential("BOX-A")
        residue_trace.import_interlock(
            self.db, fx("residue_interlock_ok.json")
        )
        second = self.credential("BOX-A")
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(1, self.db.execute(
            "SELECT count(*) FROM interlock_attestation").fetchone()[0])

    def test_evidence_identity_conflict_rolls_back(self):
        residue_trace.import_interlock(
            self.db, fx("residue_interlock_ok.json")
        )
        tampered = copy.deepcopy(fx("residue_interlock_ok.json"))
        tampered["attestations"][0]["run_confirmed"] = False
        with self.assertRaises(ValueError):
            residue_trace.import_interlock(self.db, tampered)
        # The original fact survives unchanged.
        self.assertEqual(
            "cycle_certified", self.credential("BOX-A")["state"]
        )

    def test_cli_intake_and_cycle_query(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "cli.sqlite")
            for name, command in (
                ("waste_ok.json", "waste-import"),
                ("manifest_ok.json", "manifest-import"),
                ("receipt_ok.json", "receipt-import"),
                ("residue_cycle.json", "residue-import"),
                ("residue_interlock_ok.json", "interlock-import"),
                ("residue_station_swappable.json", "station-import"),
            ):
                result = subprocess.run(
                    [sys.executable, "-m", "wastetransit", command,
                     "--db", db_path, "--input", str(FIXTURES / name)],
                    cwd=ROOT, capture_output=True, text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr)
            query = subprocess.run(
                [sys.executable, "-m", "wastetransit", "cycle-query",
                 "--db", db_path, "--batch", "WASTE-1"],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(0, query.returncode, query.stderr)
            payload = json.loads(query.stdout)[0]
            by_component = {
                c["component_id"]: c for c in payload["credentials"]
            }
            self.assertEqual(
                "cycle_certified", by_component["BOX-A"]["state"]
            )
            self.assertEqual(
                "uncertain_blocking", by_component["BOX-D"]["state"]
            )
            self.assertTrue(by_component["BOX-D"]["blocking_orders"])


if __name__ == "__main__":
    unittest.main()

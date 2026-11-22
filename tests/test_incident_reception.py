import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wastetransit import classification, incident_notice, transport
from wastetransit.source import file_record
from wastetransit.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
BATCH = "WASTE-1"


def fx(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def base_db():
    """Database with the real current-period target change from the baseline."""
    db = connect(":memory:")
    classification.import_data(db, fx("waste_ok"))
    transport.import_data(db, fx("manifest_ok"))
    incident_notice.import_data(db, fx("bulletin_collated"))
    return db


def terminals_report(query, edition=1):
    return {t["terminal"]: t for t in
            next(e for e in query["editions"] if e["edition"] == edition)["terminals"]}


def register_devices(db, neutral=("A", "B", "C"), plain=()):
    incident_notice.import_data(db, {
        "device_registrations": [
            {"terminal": t, "supports_neutral": True} for t in neutral
        ] + [
            {"terminal": t, "supports_neutral": False} for t in plain
        ],
    })


class RelayWorldTests(unittest.TestCase):
    def setUp(self):
        self.db = base_db()

    def tearDown(self):
        self.db.close()

    def test_overlapping_summaries_are_one_interpretation_not_two_deliveries(self):
        query = incident_notice.reception_query(self.db, BATCH)[0]
        edition = query["editions"][0]
        # Exactly two compatible worlds: only B, or only A+C.  The counts are
        # never added to claim two screens received the bulletin.
        self.assertEqual(edition["compatible_worlds"], [["B"], ["A", "C"]])
        self.assertEqual(edition["state"], "unknown")
        reports = terminals_report(query)
        for terminal in ("A", "B", "C"):
            self.assertEqual(reports[terminal]["reception"], "unknown")

    def test_witnessed_terminal_confirmation_pins_the_world(self):
        incident_notice.import_data(self.db, {"terminal_confirmations": [{
            "id": "CONF-A-1", "batch": BATCH, "edition": 1, "terminal": "A",
            "confirmed": True, "witness": "duty-officer-1",
            "at": "2026-01-20T08:00",
        }]})
        query = incident_notice.reception_query(self.db, BATCH)[0]
        edition = query["editions"][0]
        self.assertEqual(edition["compatible_worlds"], [["A", "C"]])
        reports = terminals_report(query)
        self.assertEqual(reports["A"]["reception"], "confirmed")
        self.assertEqual(reports["C"]["reception"], "confirmed")
        self.assertEqual(reports["B"]["reception"], "denied")

    def test_retransmitted_receipt_keeps_the_original_fact(self):
        payload = {"terminal_confirmations": [{
            "id": "CONF-A-1", "batch": BATCH, "edition": 1, "terminal": "A",
            "confirmed": True, "witness": "duty-officer-1",
            "at": "2026-01-20T08:00",
        }]}
        incident_notice.import_data(self.db, payload)
        incident_notice.import_data(self.db, payload)  # retransmission
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM terminal_confirmation").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence WHERE kind=?",
                            ("terminal-confirmation",)).fetchone()[0],
            1,
        )

    def test_mutually_exclusive_originals_leave_no_compatible_world(self):
        # A third valid-looking fragment that cannot coexist with the first two.
        incident_notice.import_data(self.db, {"reception_constraints": [{
            "id": "FRAG-IMPOSSIBLE", "batch": BATCH, "edition": 1,
            "relay": "RELAY-1", "recipients": ["A", "B", "C"],
            "received_count": 3,
        }]})
        query = incident_notice.reception_query(self.db, BATCH)[0]
        edition = query["editions"][0]
        self.assertEqual(edition["world_count"], 0)
        self.assertEqual(edition["state"], "conflict")
        for terminal in ("A", "B", "C"):
            self.assertEqual(terminals_report(query)[terminal]["reception"], "conflict")
        # An empty compatible world must never be used to sign off old boards.
        self.assertTrue(all(not c["cleared"] for c in query["old_board_clearance"])
                        or query["old_board_clearance"] == [])

    def test_evidence_identity_conflict_is_rejected(self):
        good = {"id": "FRAG-X", "batch": BATCH, "edition": 1, "relay": "RELAY-1",
                "recipients": ["A", "B"], "received_count": 0}
        bad = dict(good, received_count=1)
        incident_notice.import_data(self.db, {"reception_constraints": [good]})
        with self.assertRaises(ValueError):
            incident_notice.import_data(self.db, {"reception_constraints": [bad]})


class AuthorizationAndCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.db = base_db()
        register_devices(self.db, neutral=("A", "B", "C"))

    def tearDown(self):
        self.db.close()

    def _neutral_auth(self, **over):
        auth = {
            "id": "AUTH-NEUTRAL-1", "batch": BATCH, "edition": 2,
            "kind": "neutral", "allowed_terminals": ["A", "B", "C"],
            "registered_at": "2026-01-21T09:00",
        }
        auth.update(over)
        return incident_notice.register_authorization(self.db, auth)

    def test_neutral_card_is_pre_authorized_and_group_wide(self):
        self._neutral_auth()
        result = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-1", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["A", "B", "C"],
            "issued_at": "2026-01-21T10:00",
        })
        self.assertEqual(result["status"], "issued")
        # Submission is not delivery: nothing is confirmed yet.
        query = incident_notice.reception_query(self.db, BATCH)[0]
        dispatch = query["dispatches"][0]
        self.assertEqual(dispatch["status"], "issued")
        self.assertEqual(dispatch["delivered_terminals"], [])
        self.assertEqual(sorted(dispatch["pending_terminals"]), ["A", "B", "C"])

    def test_neutral_card_cannot_carry_restricted_batch_number(self):
        self._neutral_auth()
        result = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-BAD", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["A"],
            "restricted_batch_ref": "LOT-SECRET-9",
            "issued_at": "2026-01-21T10:00",
        })
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["reason_code"], "neutral_card_must_be_batch_free")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM correction_dispatch").fetchone()[0], 0)

    def test_authorization_registered_after_result_is_not_backdated(self):
        # Issue first, register later: the command must be denied.
        result = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-LATE", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["A"],
            "issued_at": "2026-01-21T10:00",
        })
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["reason_code"], "no_pre_registered_neutral_authorization")
        self._neutral_auth(registered_at="2026-01-21T11:00")
        # Still denied even after the authorization lands.
        again = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-LATE", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["A"],
            "issued_at": "2026-01-21T10:00",
        })
        self.assertEqual(again["status"], "denied")

    def test_device_confirmation_is_what_replaces_and_delivers(self):
        self._neutral_auth()
        incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-1", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["A", "B", "C"],
            "issued_at": "2026-01-21T10:00",
        })
        # One device accepts the new version: that screen is replaced, the
        # other two remain pending (sent but not confirmed).
        incident_notice.record_occupation(self.db, {
            "id": "OCC-A-V2", "batch": BATCH, "terminal": "A", "edition": 2,
            "command_id": "CMD-NEUTRAL-1", "at": "2026-01-21T10:05",
        })
        dispatch = incident_notice.reception_query(self.db, BATCH)[0]["dispatches"][0]
        self.assertEqual(dispatch["status"], "partially_confirmed")
        self.assertEqual(dispatch["delivered_terminals"], ["A"])
        self.assertEqual(sorted(dispatch["pending_terminals"]), ["B", "C"])
        v2 = terminals_report(incident_notice.reception_query(self.db, BATCH)[0], 2)
        self.assertEqual(v2["A"]["occupied_edition"], 2)
        self.assertIsNone(v2["B"]["occupied_edition"])

        for terminal in ("B", "C"):
            incident_notice.record_occupation(self.db, {
                "id": f"OCC-{terminal}-V2", "batch": BATCH, "terminal": terminal,
                "edition": 2, "command_id": "CMD-NEUTRAL-1",
                "at": "2026-01-21T10:06",
            })
        dispatch = incident_notice.reception_query(self.db, BATCH)[0]["dispatches"][0]
        self.assertEqual(dispatch["status"], "confirmed")
        self.assertEqual(dispatch["pending_terminals"], [])

    def test_retransmitted_command_does_not_create_a_second_delivery(self):
        self._neutral_auth()
        command = {
            "command_id": "CMD-NEUTRAL-1", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["A"],
            "issued_at": "2026-01-21T10:00",
        }
        incident_notice.issue_correction(self.db, command)
        again = incident_notice.issue_correction(self.db, dict(command))
        self.assertEqual(again["status"], "duplicate_retransmission")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM correction_dispatch").fetchone()[0], 1)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM terminal_delivery").fetchone()[0], 1)

    def test_plain_workstation_rejects_neutral_but_keeps_own_readback_path(self):
        register_devices(self.db, neutral=("A", "B"), plain=("C",))
        self._neutral_auth()
        result = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-C", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["C"],
            "issued_at": "2026-01-21T10:00",
        })
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["reason_code"], "unsupported_workstation")
        # A plain workstation can still independently read back its own board.
        readback = incident_notice.record_readback(self.db, {
            "id": "READ-C-1", "batch": BATCH, "terminal": "C",
            "edition": 1, "board_serial": "BOARD-C-SN-1",
            "witness": "duty-officer-2", "at": "2026-01-21T09:30",
        })
        self.assertEqual(readback["board_serial"], "BOARD-C-SN-1")
        options = terminals_report(
            incident_notice.reception_query(self.db, BATCH)[0])["C"]["correction"]
        self.assertIn("plain_workstation_no_neutral_rule",
                      options["neutral"]["reasons"])
        # Restricted path now only needs authorization, the readback exists.
        self.assertNotIn("terminal_board_readback_required",
                         options["restricted"]["reasons"])


class RestrictedCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.db = base_db()
        register_devices(self.db, neutral=("A", "B", "C"))
        incident_notice.register_authorization(self.db, {
            "id": "AUTH-REST-CARRIER-V1", "batch": BATCH, "edition": 1,
            "kind": "restricted", "subject": "carrier_console",
            "allowed_terminals": ["A", "B", "C"],
            "registered_at": "2026-01-20T09:00",
        })

    def tearDown(self):
        self.db.close()

    def test_restricted_correction_requires_independent_readback_first(self):
        result = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-REST-NOREAD", "batch": BATCH, "edition": 2,
            "kind": "restricted", "old_edition": 1, "board_serial": "BOARD-A-SN-1",
            "subject": "carrier_console", "targets": ["A"],
            "restricted_batch_ref": "LOT-SECRET-9",
            "issued_at": "2026-01-20T10:00",
        })
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["reason_code"], "terminal_board_readback_required")

    def test_named_board_must_match_the_actual_readback(self):
        incident_notice.record_readback(self.db, {
            "id": "READ-A-1", "batch": BATCH, "terminal": "A",
            "edition": 1, "board_serial": "BOARD-A-SN-1",
            "witness": "duty-officer-2", "at": "2026-01-20T09:30",
        })
        wrong = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-REST-WRONGSN", "batch": BATCH, "edition": 2,
            "kind": "restricted", "old_edition": 1, "board_serial": "BOARD-A-SN-OTHER",
            "subject": "carrier_console", "targets": ["A"],
            "restricted_batch_ref": "LOT-SECRET-9",
            "issued_at": "2026-01-20T10:00",
        })
        self.assertEqual(wrong["status"], "denied")
        self.assertEqual(wrong["reason_code"], "readback_does_not_match_named_board")

        ok = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-REST-A", "batch": BATCH, "edition": 2,
            "kind": "restricted", "old_edition": 1, "board_serial": "BOARD-A-SN-1",
            "subject": "carrier_console", "targets": ["A"],
            "restricted_batch_ref": "LOT-SECRET-9",
            "issued_at": "2026-01-20T10:00",
        })
        self.assertEqual(ok["status"], "issued")

    def test_neutral_path_still_works_when_board_owner_is_unidentifiable(self):
        # No readback anywhere: restricted path is blocked, but a pre-registered
        # neutral group authorization still allows the neutral replacement.
        incident_notice.register_authorization(self.db, {
            "id": "AUTH-NEUTRAL-2", "batch": BATCH, "edition": 2,
            "kind": "neutral", "allowed_terminals": ["A", "B", "C"],
            "registered_at": "2026-01-20T09:00",
        })
        neutral = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-GROUP", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["A", "B", "C"],
            "issued_at": "2026-01-20T10:00",
        })
        self.assertEqual(neutral["status"], "issued")
        restricted = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-REST-BLOCKED", "batch": BATCH, "edition": 2,
            "kind": "restricted", "old_edition": 1, "board_serial": "BOARD-B-SN-1",
            "subject": "carrier_console", "targets": ["B"],
            "restricted_batch_ref": "LOT-SECRET-9",
            "issued_at": "2026-01-20T10:00",
        })
        self.assertEqual(restricted["status"], "denied")
        self.assertEqual(restricted["reason_code"], "terminal_board_readback_required")

    def test_role_change_blocks_old_party_from_new_restricted_correction(self):
        # The bulletin target moves from carrier to producer (the real current
        # period change), while carrier authorization stays valid for V1.
        updated = dict(fx("waste_ok")["profiles"][0])
        updated.update(id="PROFILE-STOOD-DOWN", version=2, withdrawn=True)
        classification.import_data(self.db, {"profiles": [updated]})
        self.assertEqual(
            self.db.execute("SELECT confirmation_target FROM bulletin").fetchone()[0],
            "producer_console",
        )
        incident_notice.record_readback(self.db, {
            "id": "READ-A-2", "batch": BATCH, "terminal": "A",
            "edition": 1, "board_serial": "BOARD-A-SN-1",
            "witness": "duty-officer-2", "at": "2026-01-22T09:30",
        })
        # Producer-side restricted authorization for the new edition.
        incident_notice.register_authorization(self.db, {
            "id": "AUTH-REST-PROD-V2", "batch": BATCH, "edition": 1,
            "kind": "restricted", "subject": "producer_console",
            "allowed_terminals": ["A"],
            "registered_at": "2026-01-22T10:00",
        })
        carrier_attempt = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-REST-CARRIER-NEW", "batch": BATCH, "edition": 3,
            "kind": "restricted", "old_edition": 1, "board_serial": "BOARD-A-SN-1",
            "subject": "carrier_console", "targets": ["A"],
            "restricted_batch_ref": "LOT-SECRET-9",
            "issued_at": "2026-01-22T11:00",
        })
        self.assertEqual(carrier_attempt["status"], "denied")
        producer_attempt = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-REST-PROD", "batch": BATCH, "edition": 3,
            "kind": "restricted", "old_edition": 1, "board_serial": "BOARD-A-SN-1",
            "subject": "producer_console", "targets": ["A"],
            "restricted_batch_ref": "LOT-SECRET-9",
            "issued_at": "2026-01-22T11:00",
        })
        self.assertEqual(producer_attempt["status"], "issued")

    def test_old_party_may_still_confirm_neutral_clearance(self):
        # After the target switch, the carrier-held neutral authorization for a
        # batch-free replacement remains usable to stop old boards.
        updated = dict(fx("waste_ok")["profiles"][0])
        updated.update(id="PROFILE-STOOD-DOWN", version=2, withdrawn=True)
        classification.import_data(self.db, {"profiles": [updated]})
        incident_notice.register_authorization(self.db, {
            "id": "AUTH-NEUTRAL-OLD", "batch": BATCH, "edition": 3,
            "kind": "neutral", "subject": "carrier_console",
            "allowed_terminals": ["A", "B", "C"],
            "registered_at": "2026-01-22T08:00",
        })
        result = incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-OLD-PARTY", "batch": BATCH, "edition": 3,
            "kind": "neutral", "targets": ["A", "B", "C"],
            "issued_at": "2026-01-22T09:00",
        })
        self.assertEqual(result["status"], "issued")


class VersionedOccupationTests(unittest.TestCase):
    def setUp(self):
        self.db = base_db()
        register_devices(self.db)

    def tearDown(self):
        self.db.close()

    def _bulletins(self, edition):
        return {"bulletins": [{"id": f"BUL-{edition}", "batch": BATCH,
                               "edition": edition}],
                "recipient_keys": ["A", "B", "C"]}

    def test_v2_one_cross_screen_count_leaves_display_unresolved(self):
        incident_notice.import_data(self.db, self._bulletins(2))
        incident_notice.import_data(self.db, {"reception_constraints": [{
            "id": "FRAG-V2-1", "batch": BATCH, "edition": 2,
            "relay": "RELAY-1", "recipients": ["A", "B"],
            "received_count": 1,
        }]})
        query = incident_notice.reception_query(self.db, BATCH)[0]
        v2 = next(e for e in query["editions"] if e["edition"] == 2)
        self.assertEqual(v2["compatible_worlds"], [["A"], ["B"]])
        reports = {t["terminal"]: t for t in v2["terminals"]}
        # Either A or B currently shows V2; C cannot, and each of A/B might
        # still be showing V1.
        self.assertEqual(reports["A"]["possible_display"], [1, 2])
        self.assertEqual(reports["B"]["possible_display"], [1, 2])
        self.assertEqual(reports["C"]["possible_display"], [1])
        # First step must be executable in every compatible world: own-board
        # read-back always is; no restricted correction is robust yet.
        for terminal in ("A", "B", "C"):
            self.assertIn("readback_own_board",
                          reports[terminal]["collection_actions"])
            self.assertFalse(
                reports[terminal]["correction"]["restricted"]["allowed"])

    def test_v3_partial_coverage_does_not_fabricate_full_v2_delivery(self):
        incident_notice.import_data(self.db, self._bulletins(2))
        incident_notice.import_data(self.db, self._bulletins(3))
        incident_notice.import_data(self.db, {"reception_constraints": [{
            "id": "FRAG-V2-1", "batch": BATCH, "edition": 2,
            "relay": "RELAY-1", "recipients": ["A", "B"],
            "received_count": 1,
        }]})
        incident_notice.register_authorization(self.db, {
            "id": "AUTH-NEUTRAL-V3", "batch": BATCH, "edition": 3,
            "kind": "neutral", "allowed_terminals": ["A", "B", "C"],
            "registered_at": "2026-01-23T08:00",
        })
        # V3 accurately covers part of the V2 audience (screen A only).
        incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-V3-A", "batch": BATCH, "edition": 3,
            "kind": "neutral", "targets": ["A"],
            "issued_at": "2026-01-23T09:00",
        })
        incident_notice.record_occupation(self.db, {
            "id": "OCC-A-V3", "batch": BATCH, "terminal": "A",
            "edition": 3, "command_id": "CMD-NEUTRAL-V3-A",
            "at": "2026-01-23T09:05",
        })
        query = incident_notice.reception_query(self.db, BATCH)[0]
        v2 = next(e for e in query["editions"] if e["edition"] == 2)
        # V2 still has two compatible worlds even though A now shows V3: the
        # later occupation cannot be read backwards as V2 having been delivered
        # to every target.
        self.assertEqual(v2["compatible_worlds"], [["A"], ["B"]])
        v3 = next(e for e in query["editions"] if e["edition"] == 3)
        self.assertEqual(v3["terminals"][0]["occupied_edition"], 3)
        # V1 cannot be signed off as fully cleared while B may still hold it.
        v1_clearance = next(c for c in query["old_board_clearance"]
                            if c["edition"] == 1)
        self.assertFalse(v1_clearance["cleared"])
        self.assertIn("B", v1_clearance["screens_still_holding"])

    def test_repeated_occupation_respects_window_order_and_aggregate_counts(self):
        incident_notice.import_data(self.db, self._bulletins(2))
        # Two fragment windows for V2: each window stands on its own count, and
        # occupation history must keep version order on the same screen.
        incident_notice.import_data(self.db, {"reception_constraints": [
            {"id": "FRAG-V2-W1", "batch": BATCH, "edition": 2,
             "relay": "RELAY-1", "window_start": "2026-01-22T08:00",
             "window_end": "2026-01-22T12:00",
             "recipients": ["A", "B"], "received_count": 1},
            {"id": "FRAG-V2-W2", "batch": BATCH, "edition": 2,
             "relay": "RELAY-1", "window_start": "2026-01-22T12:00",
             "window_end": "2026-01-22T18:00",
             "recipients": ["A", "B"], "received_count": 2},
        ]})
        # W1 says one of A/B received; W2 says both did: jointly satisfiable.
        query = incident_notice.reception_query(self.db, BATCH)[0]
        v2 = next(e for e in query["editions"] if e["edition"] == 2)
        self.assertTrue(v2["world_count"] >= 1)
        # Occupation order: A holds V2, then V3 may replace it; a downgrade is
        # rejected outright.
        incident_notice.record_occupation(self.db, {
            "id": "OCC-A-V2", "batch": BATCH, "terminal": "A",
            "edition": 2, "at": "2026-01-22T09:00",
        })
        with self.assertRaises(ValueError):
            incident_notice.record_occupation(self.db, {
                "id": "OCC-A-REGRESS", "batch": BATCH, "terminal": "A",
                "edition": 1, "at": "2026-01-22T10:00",
            })
        history = [dict(r) for r in self.db.execute(
            "SELECT edition FROM board_version_history WHERE terminal='A' ORDER BY at,id")]
        self.assertEqual([r["edition"] for r in history], [2])

    def test_clearance_only_after_every_screen_physically_moves_off_old_board(self):
        incident_notice.import_data(self.db, self._bulletins(2))
        incident_notice.register_authorization(self.db, {
            "id": "AUTH-NEUTRAL-V2", "batch": BATCH, "edition": 2,
            "kind": "neutral", "allowed_terminals": ["A", "B", "C"],
            "registered_at": "2026-01-22T08:00",
        })
        incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NEUTRAL-V2", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["A", "B", "C"],
            "issued_at": "2026-01-22T09:00",
        })
        for terminal in ("A", "B"):
            incident_notice.record_occupation(self.db, {
                "id": f"OCC-{terminal}-V2", "batch": BATCH,
                "terminal": terminal, "edition": 2,
                "command_id": "CMD-NEUTRAL-V2",
                "at": "2026-01-22T09:05",
            })
        query = incident_notice.reception_query(self.db, BATCH)[0]
        clearance = query["old_board_clearance"][0]
        self.assertFalse(clearance["cleared"])
        self.assertEqual(clearance["screens_still_holding"], ["C"])
        incident_notice.record_occupation(self.db, {
            "id": "OCC-C-V2", "batch": BATCH, "terminal": "C",
            "edition": 2, "command_id": "CMD-NEUTRAL-V2",
            "at": "2026-01-22T09:06",
        })
        clearance = incident_notice.reception_query(self.db, BATCH)[0]["old_board_clearance"][0]
        self.assertTrue(clearance["cleared"])
        self.assertEqual(clearance["screens_still_holding"], [])

    def test_screen_outside_reception_evidence_cannot_be_signed_off(self):
        # Pin the V1 world to A+C, so B is proven not to hold a *confirmed* V1
        # reception, then register V2 evidence covering A only.  With no
        # physical occupation anywhere, every screen still conservatively
        # blocks V1 clearance: unknown screens are never treated as cleared.
        incident_notice.import_data(self.db, {"terminal_confirmations": [{
            "id": "CONF-A-PIN", "batch": BATCH, "edition": 1,
            "terminal": "A", "confirmed": True,
            "witness": "duty-officer-1", "at": "2026-01-20T08:00",
        }]})
        incident_notice.import_data(self.db, self._bulletins(2))
        incident_notice.import_data(self.db, {"reception_constraints": [{
            "id": "FRAG-V2-ONLY", "batch": BATCH, "edition": 2,
            "relay": "RELAY-1", "recipients": ["A"], "received_count": 1,
        }]})
        clearance = incident_notice.reception_query(
            self.db, BATCH)[0]["old_board_clearance"][0]
        self.assertFalse(clearance["cleared"])
        # No device has physically moved off V1, so none can be signed off.
        self.assertEqual(
            set(clearance["screens_still_holding"])
            | set(clearance["screens_without_clearance_evidence"]),
            {"A", "B", "C"},
        )


class EvidenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.db = base_db()
        register_devices(self.db)

    def tearDown(self):
        self.db.close()

    def test_correction_only_terminal_has_no_reception_verdict_or_confirmation_action(self):
        incident_notice.register_authorization(self.db, {
            "id": "AUTH-NE", "batch": BATCH, "edition": 2, "kind": "neutral",
            "allowed_terminals": ["A"], "registered_at": "2026-01-21T09:00",
        })
        incident_notice.issue_correction(self.db, {
            "command_id": "CMD-NE", "batch": BATCH, "edition": 2,
            "kind": "neutral", "targets": ["A"],
            "issued_at": "2026-01-21T10:00",
        })
        v2 = next(e for e in incident_notice.reception_query(self.db, BATCH)[0]["editions"]
                  if e["edition"] == 2)
        self.assertEqual(v2["state"], "no_reception_evidence")
        screen_a = next(t for t in v2["terminals"] if t["terminal"] == "A")
        self.assertEqual(screen_a["reception"], "no_reception_evidence")
        self.assertEqual(screen_a["collection_actions"], ["readback_own_board"])

    def test_reception_evidence_never_re_adjudicates_transport_liability(self):
        incident_notice.import_data(self.db, {"terminal_confirmations": [{
            "id": "CONF-A-X", "batch": BATCH, "edition": 1, "terminal": "A",
            "confirmed": True, "witness": "duty-officer-1",
            "at": "2026-01-20T08:00",
        }]})
        # The transport state and liable party are untouched by reception work.
        transport = dict(self.db.execute(
            "SELECT state,liable_party FROM transport").fetchone())
        self.assertEqual(transport["state"], "carrier_responsible")
        self.assertEqual(transport["liable_party"], "carrier")
        # A confirmed reception still does not mean any board was replaced.
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM board_version_history").fetchone()[0], 0)

    def test_target_history_records_the_role_switch_once(self):
        before = self.db.execute(
            "SELECT count(*) AS n FROM target_history").fetchone()["n"]
        updated = dict(fx("waste_ok")["profiles"][0])
        updated.update(id="PROFILE-STOOD-DOWN", version=2, withdrawn=True)
        classification.import_data(self.db, {"profiles": [updated]})
        # Refresh runs again via an unrelated import: the history must not gain
        # a duplicate row for an unchanged target.
        incident_notice.import_data(self.db, {"device_registrations": [
            {"terminal": "A", "supports_neutral": True}]})
        targets = [r["target"] for r in self.db.execute(
            "SELECT target FROM target_history ORDER BY id")]
        self.assertEqual(targets, ["carrier_console", "producer_console"])
        self.assertEqual(len(targets), before + 1)
        self.assertEqual(
            incident_notice.current_target(self.db, BATCH), "producer_console")


class PersistenceAndReplayTests(unittest.TestCase):
    def test_reception_state_categories_are_persisted(self):
        db = base_db()
        try:
            stored = {r["terminal"]: r["verdict"] for r in db.execute(
                "SELECT terminal,verdict FROM reception_analysis WHERE edition=1")}
            self.assertEqual(stored, {"A": "unknown", "B": "unknown", "C": "unknown"})
            incident_notice.import_data(db, {"terminal_confirmations": [{
                "id": "CONF-A-1", "batch": BATCH, "edition": 1,
                "terminal": "A", "confirmed": True,
                "witness": "duty-officer-1", "at": "2026-01-20T08:00",
            }]})
            stored = {r["terminal"]: r["verdict"] for r in db.execute(
                "SELECT terminal,verdict FROM reception_analysis WHERE edition=1")}
            self.assertEqual(stored, {"A": "confirmed", "B": "denied", "C": "confirmed"})
            # Sent-but-unconfirmed and actual replacement history live apart.
            register_devices(db)
            incident_notice.register_authorization(db, {
                "id": "AUTH-N", "batch": BATCH, "edition": 2, "kind": "neutral",
                "allowed_terminals": ["A", "B", "C"],
                "registered_at": "2026-01-21T09:00",
            })
            incident_notice.issue_correction(db, {
                "command_id": "CMD-N", "batch": BATCH, "edition": 2,
                "kind": "neutral", "targets": ["A"],
                "issued_at": "2026-01-21T10:00",
            })
            self.assertEqual(
                db.execute("SELECT delivered FROM terminal_delivery").fetchone()[0], 0)
            incident_notice.record_occupation(db, {
                "id": "OCC-A", "batch": BATCH, "terminal": "A",
                "edition": 2, "command_id": "CMD-N",
                "at": "2026-01-21T10:05",
            })
            self.assertEqual(
                db.execute("SELECT count(*) FROM board_version_history").fetchone()[0], 1)
        finally:
            db.close()

    def test_file_journal_replay_reproduces_reception_state(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "reception.sqlite")
            db = connect(db_path)
            docs = [
                ("reception-import", fx("bulletin_collated")),
                ("reception-import", {"device_registrations": [
                    {"terminal": t, "supports_neutral": True}
                    for t in ("A", "B", "C")]}),
                ("reception-import", {"terminal_confirmations": [{
                    "id": "CONF-A-1", "batch": BATCH, "edition": 1,
                    "terminal": "A", "confirmed": True,
                    "witness": "duty-officer-1", "at": "2026-01-20T08:00"}]}),
                ("authorize-correction", {
                    "id": "AUTH-N", "batch": BATCH, "edition": 2,
                    "kind": "neutral", "allowed_terminals": ["A", "B", "C"],
                    "registered_at": "2026-01-21T09:00"}),
                ("issue-correction", {
                    "command_id": "CMD-N", "batch": BATCH, "edition": 2,
                    "kind": "neutral", "targets": ["A", "B", "C"],
                    "issued_at": "2026-01-21T10:00"}),
                ("record-occupation", {
                    "id": "OCC-A", "batch": BATCH, "terminal": "A",
                    "edition": 2, "command_id": "CMD-N",
                    "at": "2026-01-21T10:05"}),
            ]
            handlers = {
                "reception-import": incident_notice.import_data,
                "authorize-correction": incident_notice.register_authorization,
                "issue-correction": incident_notice.issue_correction,
                "record-occupation": incident_notice.record_occupation,
            }
            for command, doc in docs:
                raw = json.dumps(doc, ensure_ascii=False).encode()
                handlers[command](db, doc)
                with db:
                    file_record(db, command, raw)
            expected = incident_notice.reception_query(db, BATCH)
            db.close()

            replay = connect(":memory:")
            import json as _json
            from wastetransit import report
            for row in connect(db_path).execute(
                    "SELECT command,raw FROM file_journal ORDER BY id"):
                report_handlers = {
                    "reception-import": incident_notice.import_data,
                    "authorize-correction": incident_notice.register_authorization,
                    "issue-correction": incident_notice.issue_correction,
                    "record-occupation": incident_notice.record_occupation,
                }
                report_handlers[row["command"]](replay, _json.loads(row["raw"]))
            self.assertEqual(incident_notice.reception_query(replay, BATCH), expected)
            replay.close()

    def test_cli_reception_commands_and_audit(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "cli.sqlite")
            auth_doc = Path(d) / "auth.json"
            auth_doc.write_text(json.dumps({
                "id": "AUTH-CLI", "batch": BATCH, "edition": 2,
                "kind": "neutral", "allowed_terminals": ["A", "B", "C"],
                "registered_at": "2026-01-21T09:00",
            }), encoding="utf-8")

            def run(*args):
                proc = subprocess.run(
                    [sys.executable, "-m", "wastetransit", *args],
                    cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return proc.stdout

            run("reception-import", "--db", db_path,
                "--input", str(FIXTURES / "bulletin_collated.json"))
            run("authorize-correction", "--db", db_path,
                "--input", str(auth_doc))
            query = json.loads(run("reception-query", "--db", db_path,
                                   "--batch", BATCH))
            self.assertEqual(query[0]["editions"][0]["compatible_worlds"],
                             [["B"], ["A", "C"]])
            audit = json.loads(run("audit", "--db", db_path))
            self.assertTrue(audit["ok"])


class BaselineTargetPreservedTests(unittest.TestCase):
    def test_real_period_target_change_is_not_overstated(self):
        db = base_db()
        try:
            self.assertEqual(
                db.execute("SELECT confirmation_target FROM bulletin").fetchone()[0],
                "carrier_console",
            )
            updated = dict(fx("waste_ok")["profiles"][0])
            updated.update(id="PROFILE-STOOD-DOWN", version=2, withdrawn=True)
            classification.import_data(db, {"profiles": [updated]})
            self.assertEqual(
                db.execute("SELECT state FROM transport").fetchone()[0],
                "classification_hold",
            )
            self.assertEqual(
                db.execute("SELECT confirmation_target FROM bulletin").fetchone()[0],
                "producer_console",
            )
            # Reception evidence must never re-adjudicate the transport chain's
            # liable party.
            self.assertEqual(
                db.execute("SELECT liable_party FROM transport").fetchone()[0],
                "producer",
            )
            # Unknown reception cannot be presented as corrected external screens.
            query = incident_notice.reception_query(db, BATCH)[0]
            self.assertEqual(query["editions"][0]["state"], "unknown")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()

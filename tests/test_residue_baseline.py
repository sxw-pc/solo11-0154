import json
import unittest
from pathlib import Path

from wastetransit import classification, closure, residue_trace, transport
from wastetransit.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ResidueBaseline(unittest.TestCase):
    def test_origin_changes_the_existing_residue_claim(self):
        db = connect(":memory:")
        try:
            classification.import_data(db, read("waste_ok.json"))
            transport.import_data(db, read("manifest_ok.json"))
            self.assertEqual("closed", closure.import_data(db, read("receipt_ok.json"))[0]["state"])
            summary = residue_trace.import_data(db, read("residue_cycle.json"))[0]
            self.assertEqual("claimed_complete", summary["state"])
            later = read("receipt_ok.json")["weights"][0]
            later.update(id="WEIGH-RECHECK", net_kg=991, at="2026-01-13")
            self.assertEqual(
                "difference_investigation", closure.import_data(db, {"weights":[later]})[0]["state"]
            )
            self.assertEqual("hold_origin", db.execute("SELECT state FROM residue_summary").fetchone()[0])
        finally:
            db.close()

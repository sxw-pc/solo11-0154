import json
import unittest
from pathlib import Path

from wastetransit import capture_desk, classification, closure, transport
from wastetransit.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class CaptureDeskBaseline(unittest.TestCase):
    def test_receiving_dispute_activates_existing_capture_queue(self):
        db = connect(":memory:")
        try:
            classification.import_data(db, read("waste_ok.json"))
            transport.import_data(db, read("manifest_ok.json"))
            closure.import_data(db, read("receipt_ok.json"))
            self.assertEqual([], capture_desk.import_data(db, read("capture_queue.json")))
            receipt = read("receipt_ok.json")["weights"][0]
            receipt.update(id="WEIGH-INV", at="2026-01-13", net_kg=991)
            self.assertEqual(
                "difference_investigation",
                closure.import_data(db, {"weights":[receipt]})[0]["state"],
            )
            rows = capture_desk.pick_pending(db)
            self.assertEqual(["EARLY", "PAIR"], [row["id"] for row in rows])
        finally:
            db.close()

import json
import unittest
from pathlib import Path

from wastetransit import classification, incident_notice, transport
from wastetransit.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class BulletinBaseline(unittest.TestCase):
    def test_current_custody_changes_the_confirmation_request_target(self):
        db = connect(":memory:")
        try:
            declaration = read("waste_ok.json")
            classification.import_data(db, declaration)
            self.assertEqual(
                "carrier_responsible",
                transport.import_data(db, read("manifest_ok.json"))[0]["state"],
            )
            result = incident_notice.import_data(db, read("bulletin_collated.json"))[0]
            self.assertEqual("carrier_console", result["confirmation_target"])
            updated = dict(declaration["profiles"][0])
            updated.update(id="PROFILE-STOOD-DOWN", version=2, withdrawn=True)
            classification.import_data(db, {"profiles": [updated]})
            state = db.execute("SELECT state FROM transport").fetchone()[0]
            self.assertEqual("classification_hold", state)
            row = db.execute("SELECT confirmation_target FROM bulletin").fetchone()
            self.assertEqual("producer_console", row[0])
        finally:
            db.close()

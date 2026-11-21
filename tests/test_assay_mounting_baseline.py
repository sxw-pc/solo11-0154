import json
import unittest
from pathlib import Path

from wastetransit import classification, transport
from wastetransit.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class AssayMountingBaseline(unittest.TestCase):
    def test_profile_state_recomputes_existing_dispatch(self):
        db = connect(":memory:")
        try:
            case = read("assay_mountings.json")
            declaration = read("waste_ok.json")
            classification.import_data(db, declaration)
            transport.import_data(db, read("manifest_ok.json"))
            batch = case["batch"]
            initial = db.execute(
                "SELECT state FROM transport WHERE batch=?", (batch,)
            ).fetchone()[0]
            self.assertEqual("carrier_responsible", initial)
            profile = dict(declaration["profiles"][0])
            profile.update(id="PROFILE-WITHDRAWN", version=2, withdrawn=True)
            classification.import_data(db, {"profiles": [profile]})
            current = db.execute(
                "SELECT state FROM transport WHERE batch=?", (batch,)
            ).fetchone()[0]
            self.assertEqual("classification_hold", current)
        finally:
            db.close()

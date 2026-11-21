"""Offline intake of the synthetic residue station's phase summary."""

from .source import record


def initialize(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS residue_summary (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            completed_count INTEGER NOT NULL,
            state TEXT NOT NULL
        )"""
    )


def refresh(db, batch):
    initialize(db)
    receipt = db.execute("SELECT state FROM closure WHERE batch=?", (batch,)).fetchone()
    if receipt and receipt["state"] == "closed":
        db.execute(
            """UPDATE residue_summary SET state=CASE WHEN completed_count>0
                  THEN 'claimed_complete' ELSE 'no_claim' END WHERE batch=?""",
            (batch,),
        )
    else:
        db.execute("UPDATE residue_summary SET state='hold_origin' WHERE batch=?", (batch,))


def import_data(db, doc):
    batches = set()
    with db:
        initialize(db)
        for item in doc["summaries"]:
            if record(db, "residue-summary", item):
                db.execute(
                    "INSERT INTO residue_summary VALUES (?,?,?,?)",
                    (item["id"], item["batch"], item["completed_count"], "hold_origin"),
                )
            batches.add(item["batch"])
        for batch in sorted(batches):
            refresh(db, batch)
    return [dict(row) for row in db.execute("SELECT * FROM residue_summary ORDER BY id")]

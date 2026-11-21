"""Offline enrollment for confirmations of a synthetic site bulletin."""

from .source import record


def initialize(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS bulletin (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            edition INTEGER NOT NULL,
            confirmation_target TEXT NOT NULL
        )"""
    )


def refresh(db, batch):
    initialize(db)
    transport = db.execute(
        "SELECT state,liable_party FROM transport WHERE batch=?", (batch,)
    ).fetchone()
    if not transport:
        target = "not_assigned"
    elif transport["state"] == "carrier_responsible":
        target = "carrier_console"
    elif transport["liable_party"] == "producer":
        target = "producer_console"
    else:
        target = "not_assigned"
    db.execute("UPDATE bulletin SET confirmation_target=? WHERE batch=?", (target, batch))


def import_data(db, data):
    batches = set()
    with db:
        initialize(db)
        for bulletin in data["bulletins"]:
            if record(db, "site-bulletin", bulletin):
                db.execute(
                    "INSERT INTO bulletin VALUES (?,?,?,?)",
                    (bulletin["id"], bulletin["batch"], bulletin["edition"], "not_assigned"),
                )
            batches.add(bulletin["batch"])
        for batch in sorted(batches):
            refresh(db, batch)
    return [dict(row) for row in db.execute("SELECT * FROM bulletin ORDER BY id")]

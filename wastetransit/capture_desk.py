"""Offline acquisition desk for finite retention devices."""

from .source import record


def _prepare(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS capture_request (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            fragments INTEGER NOT NULL,
            expires_tick INTEGER NOT NULL,
            state TEXT NOT NULL
        )"""
    )


def reconcile(db, batch):
    _prepare(db)
    closure = db.execute("SELECT state FROM closure WHERE batch=?", (batch,)).fetchone()
    if closure and closure["state"] == "difference_investigation":
        state = "capture_due"
    else:
        state = "archive_only"
    db.execute("UPDATE capture_request SET state=? WHERE batch=?", (state, batch))


def import_data(db, doc):
    batches = set()
    with db:
        _prepare(db)
        for item in doc["requests"]:
            if item["fragments"] <= 0:
                raise ValueError("capture request must identify at least one fragment")
            if record(db, "capture-request", item):
                db.execute(
                    "INSERT INTO capture_request VALUES (?,?,?,?,?)",
                    (item["id"], item["batch"], item["fragments"],
                     item["expires_tick"], "archive_only"),
                )
            batches.add(item["batch"])
        for batch in sorted(batches):
            reconcile(db, batch)
    return pick_pending(db)


def pick_pending(db):
    _prepare(db)
    return [
        dict(row)
        for row in db.execute(
            """SELECT * FROM capture_request WHERE state='capture_due'
               ORDER BY expires_tick,id"""
        )
    ]

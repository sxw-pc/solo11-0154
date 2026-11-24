"""Offline acquisition desk for finite retention devices.

The existing difference-driven pending queue (``capture_request`` rows flipped
between ``capture_due`` and ``archive_only`` by :func:`reconcile`) stays
untouched.  On top of it the desk now saves executable acquisition plans built
by :mod:`wastetransit.capture_plan`: plans are persisted *before* any device
submission, in-progress candidates missing their acknowledgement are never
reported complete, and confirmed unobtainable fragments stay recorded as
missing pieces with their last readable tick.  A plan is an instruction set,
not evidence: :func:`submit_receipt` only stores bytes-equivalent receipts the
simulator or a real device actually produced.
"""

import json

from .source import record
from . import capture_plan as cp

_PLAN_KIND = "capture-plan"
_FRAGMENT_KIND = "capture-fragment"


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
    db.execute(
        """CREATE TABLE IF NOT EXISTS capture_plan (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            procedure TEXT NOT NULL,
            guaranteed_weight INTEGER NOT NULL,
            saved INTEGER NOT NULL DEFAULT 0
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS capture_plan_step (
            plan_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            branch TEXT,
            command TEXT NOT NULL,
            PRIMARY KEY (plan_id, seq, branch)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS capture_receipt (
            token TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            fragment TEXT NOT NULL,
            packet TEXT NOT NULL,
            release TEXT NOT NULL,
            tick INTEGER,
            source TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS missing_piece (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL,
            packet TEXT NOT NULL,
            fragment TEXT NOT NULL,
            last_tick INTEGER,
            reason TEXT NOT NULL
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


def _flatten_commands(plan):
    """Yield (seq, branch_or_None, command) rows, branches ordered by state."""
    seq = 0
    for command in plan.get("commands", []):
        yield seq, None, command
        seq += 1
    for state in sorted(plan.get("branches") or {}):
        for command in plan["branches"][state]["commands"]:
            yield seq, state, command
            seq += 1


def save_plan(db, batch, plan, plan_id):
    """Persist the plan before any device submission occurs.

    The same ``plan_id`` is idempotent: re-saving an identical plan is a
    no-op so actual-input replays never double-book a device request.
    """
    _prepare(db)
    previous = db.execute("SELECT procedure FROM capture_plan WHERE id=?", (plan_id,)).fetchone()
    if previous:
        if previous["procedure"] != plan["procedure"]:
            raise ValueError(f"plan identity conflict: {plan_id}")
        return plan_id
    with db:
        record(
            db,
            _PLAN_KIND,
            {"id": plan_id, "batch": batch, "plan": plan},
        )
        db.execute(
            "INSERT INTO capture_plan VALUES (?,?,?,?,0)",
            (plan_id, batch, plan["procedure"], plan["guaranteed_weight"]),
        )
        for seq, branch, command in _flatten_commands(plan):
            db.execute(
                "INSERT INTO capture_plan_step VALUES (?,?,?,?)",
                (plan_id, seq, branch,
                 json.dumps(command, sort_keys=True, separators=(",", ":"))),
            )
        for gap in plan.get("gaps", []):
            db.execute(
                "INSERT INTO missing_piece(plan_id,packet,fragment,last_tick,reason)"
                " VALUES (?,?,?,?,?)",
                (plan_id, gap["packet"], gap["fragment"], gap["last_tick"], gap["reason"]),
            )
    return plan_id


def list_missing(db, plan_id):
    _prepare(db)
    return [dict(row) for row in db.execute(
        "SELECT plan_id,packet,fragment,last_tick,reason FROM missing_piece "
        "WHERE plan_id=? ORDER BY packet,fragment", (plan_id,)
    )]


def build_pending_plan(db, plan_id, doc, *, offline=False, retention=None,
                       device_ticks=None, end_tick=None, lock_release=cp.DEFAULT_RELEASE):
    """Plan the currently ``capture_due`` queue and save it before submission."""
    scenario = cp.scenario_from_doc(
        doc, offline=offline, retention=retention,
        device_ticks=device_ticks, end_tick=end_tick,
    )
    due = {row["id"] for row in pick_pending(db)}
    planned_packets = {
        fragment.packet for fragment in scenario.fragments if fragment.packet in due
    }
    if not planned_packets:
        raise ValueError("no capture_due packet matches the scenario fragment case")
    plan = cp.plan_offline(scenario, lock_release) if offline else cp.plan_best(scenario)
    return save_plan(db, sorted(planned_packets)[0], plan, plan_id)


def submit_receipt(db, plan_id, receipt):
    """Store an actually obtained fragment receipt; plans never substitute bytes.

    The receipt must name a fragment/ release a plan command really read, and
    evidence identity conflicts are delegated to :func:`source.record`.
    """
    _prepare(db)
    if not db.execute("SELECT 1 FROM capture_plan WHERE id=?", (plan_id,)).fetchone():
        raise ValueError(f"unknown plan {plan_id}")
    fragment, release = receipt["fragment"], receipt["release"]
    planned = _flatten_commands(_load_plan_payload(db, plan_id))
    if not any(
        command.get("action") == cp.ACT_READ
        and command.get("fragment") == fragment
        and command.get("release") == release
        for _, _, command in planned
    ):
        raise ValueError(f"receipt {fragment}/{release} is not backed by a planned read")
    evidence = dict(receipt)
    evidence.setdefault("id", receipt["token"])
    with db:
        record(db, _FRAGMENT_KIND, evidence)
        db.execute(
            "INSERT OR IGNORE INTO capture_receipt VALUES (?,?,?,?,?,?,?)",
            (receipt["token"], plan_id, fragment, receipt["packet"], release,
             receipt.get("tick"), receipt["source"]),
        )
        db.execute("UPDATE capture_plan SET saved=1 WHERE id=?", (plan_id,))
    return dict(db.execute("SELECT * FROM capture_receipt WHERE token=?",
                           (receipt["token"],)).fetchone())


def list_receipts(db, plan_id):
    _prepare(db)
    return [dict(row) for row in db.execute(
        "SELECT * FROM capture_receipt WHERE plan_id=? ORDER BY tick,fragment", (plan_id,)
    )]


def _load_plan_payload(db, plan_id):
    row = db.execute("SELECT raw FROM evidence WHERE id=?", (plan_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown plan {plan_id}")
    return json.loads(row["raw"])["plan"]

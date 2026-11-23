"""Offline acquisition desk for finite retention devices.

The desk owns two layers:

* the legacy difference-driven queue (:func:`reconcile`, :func:`pick_pending`)
  that flips capture requests between ``archive_only`` and ``capture_due``
  whenever a receiving closure changes;
* a finite-horizon acquisition planner that turns the *fragment cases* behind
  those requests into an executable capture program.

The planner works on discrete sampling ticks.  The main device exposes one
read window per tick.  A packet (an evidence original) is only counted as
 obtained when **every** fragment role of one and the same release has been
captured: a raw frame retained from release R0 can never be paired with the
independent attestation published under R1.  Two pre-authorized acquisition
procedures are modelled:

``lock``
    A single lock request signed at a publication boundary.  Frames of the
    targeted packet that have already appeared stay mounted on the main
    device past their normal expiry, while the next normal publication does
    not happen on that device.  It costs the device's next publication slot.

``retain``
    An independent receiver may retain raw frames in parallel.  It never
    halts the main device publication, but the receiver only records
    configured signals, and a retained raw frame still needs the matching
    attestation of the *same* release to be read on the main device in time.

When a lock acknowledgement is lost, the desk must issue a command that is
legal under every still-compatible device state.  :func:`solve_plan`
therefore returns a contingent policy whose first action is a mirror
inspection, with branches selected only by the observed device state.
"""

import argparse
import json
from pathlib import Path

from .source import record

MAX_GROUPS = 3
MAX_TICKS = 4
DEFAULT_RELEASE = "R1"
MAIN = "main"
RECEIVER = "receiver"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

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
        """CREATE TABLE IF NOT EXISTS capture_fragment (
            id TEXT NOT NULL,
            packet TEXT NOT NULL,
            role TEXT NOT NULL,
            release TEXT NOT NULL,
            weight INTEGER NOT NULL,
            device TEXT NOT NULL,
            available_ticks TEXT NOT NULL,
            configured INTEGER NOT NULL,
            PRIMARY KEY (id, release)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS capture_plan (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            created_tick INTEGER NOT NULL,
            payload TEXT NOT NULL,
            committed INTEGER NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS capture_evidence (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            batch TEXT NOT NULL,
            fragment TEXT NOT NULL,
            packet TEXT NOT NULL,
            release TEXT NOT NULL,
            device TEXT NOT NULL,
            captured_tick INTEGER NOT NULL,
            reality TEXT NOT NULL
        )"""
    )


def reconcile(db, batch):
    """Re-evaluate the capture queue after a receiving-net-weight change."""
    _prepare(db)
    closure = db.execute("SELECT state FROM closure WHERE batch=?", (batch,)).fetchone()
    if closure and closure["state"] == "difference_investigation":
        state = "capture_due"
    else:
        state = "archive_only"
    db.execute("UPDATE capture_request SET state=? WHERE batch=?", (state, batch))


def _default_role(item):
    marker = item["id"].rsplit("-", 1)
    return marker[-1].lower() if len(marker) == 2 else "body"


def import_data(db, doc):
    """Import capture requests and their readable fragment cases.

    The return value stays the legacy pending queue so existing receiving
    pipelines are unaffected; fragment cases are persisted alongside for the
    planner.
    """
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
        for case in doc.get("fragment_case", []):
            _store_fragment(db, case)
        for retained in doc.get("retained", []):
            if not retained.get("configured", False):
                continue
            db.execute(
                "UPDATE capture_fragment SET configured=1 WHERE id=? AND release=?",
                (retained["fragment"], retained.get("release", DEFAULT_RELEASE)),
            )
        for batch in sorted(batches):
            reconcile(db, batch)
    return pick_pending(db)


def _store_fragment(db, case):
    ticks = sorted(case["available_ticks"])
    if not ticks:
        raise ValueError("fragment case must list at least one readable tick: " + case["id"])
    release = case.get("release", DEFAULT_RELEASE)
    db.execute(
        "INSERT OR REPLACE INTO capture_fragment VALUES (?,?,?,?,?,?,?,?)",
        (
            case["id"],
            case["packet"],
            case.get("role", _default_role(case)),
            release,
            int(case["weight"]),
            case.get("device", MAIN),
            json.dumps(ticks),
            1 if case.get("configured", False) else 0,
        ),
    )


def pick_pending(db):
    """Legacy queue: expiry ordering, kept stable for the receiving pipeline."""
    _prepare(db)
    return [
        dict(row)
        for row in db.execute(
            """SELECT * FROM capture_request WHERE state='capture_due'
               ORDER BY expires_tick,id"""
        )
    ]


def ordered_pending(db, batch=None):
    """Capture-due requests ranked by the weight of the evidence group they
    can guarantee instead of bare expiry time.

    A single-fragment request that expires first but weighs less than a
    complete multi-fragment group is queued behind the group once the group
    is still fully reachable.
    """
    _prepare(db)
    sql = """SELECT r.*, COALESCE(g.group_weight, 0) AS group_weight,
                    COALESCE(g.groups, 0) AS fragment_groups
             FROM capture_request r
             LEFT JOIN (
                 SELECT packet, MAX(weight) AS group_weight,
                        COUNT(DISTINCT release) AS groups
                 FROM capture_fragment GROUP BY packet
             ) g ON g.packet = r.id
             WHERE r.state='capture_due'"""
    args = ()
    if batch is not None:
        sql += " AND r.batch=?"
        args = (batch,)
    sql += " ORDER BY group_weight DESC, r.expires_tick, r.id"
    return [dict(row) for row in db.execute(sql, args)]


def save_plan(db, batch, plan, created_tick=0):
    """Persist a plan *before* any device submission.

    Saving a plan never writes evidence: a candidate that has merely been
    started (for example a raw frame without its attestation) is not a
    completed capture.
    """
    _prepare(db)
    plan_id = "PLAN-" + batch + "-" + str(created_tick)
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True)
    with db:
        db.execute(
            "INSERT OR REPLACE INTO capture_plan VALUES (?,?,?,?,?)",
            (plan_id, batch, created_tick, payload, 0),
        )
    return plan_id


def commit_capture(db, plan_id, simulation):
    """Commit the real fragments produced by an offline simulation.

    Only fragments that the simulation actually captured become evidence
    rows.  A plan on its own can never stand in for missing bytes.
    """
    _prepare(db)
    row = db.execute("SELECT * FROM capture_plan WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise ValueError("unknown plan: " + plan_id)
    captured = {(c["fragment"], c["release"]) for c in simulation["captured"]}
    if not captured:
        raise ValueError("refusing to commit a simulation without real captures")
    with db:
        for idx, cap in enumerate(simulation["captured"]):
            key = (cap["fragment"], cap["release"])
            if key not in captured:
                raise ValueError("capture not produced by simulation: " + cap["fragment"])
            db.execute(
                "INSERT OR IGNORE INTO capture_evidence VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    plan_id + ":" + cap["fragment"] + ":" + cap["release"] + ":" + str(idx),
                    plan_id,
                    row["batch"],
                    cap["fragment"],
                    cap["packet"],
                    cap["release"],
                    cap["device"],
                    cap["tick"],
                    simulation.get("reality", "certain"),
                ),
            )
        db.execute("UPDATE capture_plan SET committed=1 WHERE id=?", (plan_id,))
    return [dict(r) for r in db.execute(
        "SELECT * FROM capture_evidence WHERE plan_id=?", (plan_id,))]


# ---------------------------------------------------------------------------
# scenario construction and validation
# ---------------------------------------------------------------------------

def build_scenario(db, batch=None, ticks=None, device_request=None):
    """Build a planner scenario from persisted requests and fragment cases."""
    _prepare(db)
    where = "WHERE r.state='capture_due'"
    args = ()
    if batch is not None:
        where += " AND r.batch=?"
        args = (batch,)
    requests = [dict(r) for r in db.execute(
        "SELECT * FROM capture_request r " + where + " ORDER BY r.expires_tick,r.id", args)]
    packets = {r["id"]: r for r in requests}
    cases = []
    retained = []
    frag_sql = "SELECT * FROM capture_fragment"
    frag_args = ()
    if packets:
        frag_sql += " WHERE packet IN (%s)" % ",".join("?" * len(packets))
        frag_args = tuple(packets)
    for row in db.execute(frag_sql, frag_args):
        case = {
            "id": row["id"],
            "packet": row["packet"],
            "role": row["role"],
            "release": row["release"],
            "weight": row["weight"],
            "device": row["device"],
            "available_ticks": json.loads(row["available_ticks"]),
            "configured": bool(row["configured"]),
        }
        cases.append(case)
        if row["device"] != MAIN and row["configured"]:
            retained.append({"fragment": row["id"], "release": row["release"],
                             "configured": True})
    if ticks is None:
        max_tick = max((t for c in cases for t in c["available_ticks"]), default=0)
        ticks = list(range(max_tick + 1))
    scenario = {
        "batch": batch,
        "ticks": list(ticks),
        "end_tick": max(ticks) + 1,
        "packets": [{"id": p, "weight": packets[p]["fragments"],
                     "expires_tick": packets[p]["expires_tick"]}
                    for p in sorted(packets)],
        "fragments": cases,
        "retained": retained,
        "procedures": {"lock": {"enabled": True, "duration_ticks": 1},
                       "receiver": {"enabled": True}},
        "device_request": device_request,
        "executed": [],
    }
    validate_scenario(scenario)
    return scenario


def scenario_from_doc(doc, ticks=None, device_request=None, executed=None):
    """Build a scenario directly from a capture-queue style document."""
    cases = []
    retained = []
    for case in doc.get("fragment_case", []):
        c = dict(case)
        c.setdefault("role", _default_role(case))
        c.setdefault("release", DEFAULT_RELEASE)
        c.setdefault("device", MAIN)
        c["configured"] = bool(c.get("configured", False))
        c["available_ticks"] = sorted(c["available_ticks"])
        cases.append(c)
        if c["device"] != MAIN and c["configured"]:
            retained.append({"fragment": c["id"], "release": c["release"],
                             "configured": True})
    for item in doc.get("retained", []):
        if item.get("configured", False):
            retained.append({"fragment": item["fragment"],
                             "release": item.get("release", DEFAULT_RELEASE),
                             "configured": True})
    if ticks is None:
        max_tick = max((t for c in cases for t in c["available_ticks"]), default=0)
        ticks = list(range(max_tick + 1))
    scenario = {
        "batch": doc.get("batch"),
        "ticks": list(ticks),
        "end_tick": max(ticks) + 1,
        "packets": [{"id": r["id"], "weight": r["fragments"],
                     "expires_tick": r["expires_tick"]}
                    for r in doc.get("requests", [])],
        "fragments": cases,
        "retained": retained,
        "procedures": doc.get("procedures",
                              {"lock": {"enabled": True, "duration_ticks": 1},
                               "receiver": {"enabled": True}}),
        "device_request": device_request,
        "executed": list(executed or []),
    }
    validate_scenario(scenario)
    return scenario


def validate_scenario(scenario):
    ticks = scenario["ticks"]
    if len(ticks) > MAX_TICKS:
        raise ValueError("at most %d device windows are supported" % MAX_TICKS)
    if ticks and ticks != list(range(min(ticks), max(ticks) + 1)):
        raise ValueError("ticks must be a contiguous discrete range")
    groups = {(c["packet"], c["release"]) for c in scenario["fragments"]}
    if len(groups) > MAX_GROUPS:
        raise ValueError("at most %d evidence groups are supported" % MAX_GROUPS)
    pending = [r for r in [scenario.get("device_request")] if r is not None]
    if len(pending) > 1:
        raise ValueError("at most one device request may be pending")
    for req in pending:
        if req.get("outcome", "pending") != "pending":
            raise ValueError("only pending device requests create hidden states")
    return True


# ---------------------------------------------------------------------------
# world model
# ---------------------------------------------------------------------------

def _groups(cases):
    """Full required role set per (packet, release).

    A packet's complete role set is defined across every release in which
    it appears: a release that lost one fragment (for example an attestation
    that no longer exists) keeps the full requirement and cannot complete
    from the surviving raw frame alone.
    """
    roles_by_packet = {}
    present = {}
    for c in cases:
        roles_by_packet.setdefault(c["packet"], set()).add(c["role"])
        present.setdefault((c["packet"], c["release"]), set()).add(c["role"])
    return {group: set(roles_by_packet[group[0]]) for group in present}


def _group_weight(cases, packet, release):
    weights = [c["weight"] for c in cases
               if c["packet"] == packet and c["release"] == release]
    return max(weights) if weights else 0


def _active_locks(locks, tick):
    return [lk for lk in locks if lk["boundary"] <= tick <= lk["lock_end"]]


def mounted(case, tick, locks, reality="certain", rotation_boundary=None):
    """Whether a fragment case is physically readable on the main device."""
    if case["device"] not in (MAIN, "both"):
        return False
    active = _active_locks(locks, tick)
    for lk in active:
        # A lock consumes the next normal publication: a fragment that first
        # appears after the boundary cannot be observed while held.  Frames
        # already present at the boundary tick itself remain readable.
        if tick > lk["boundary"] and min(case["available_ticks"]) > lk["boundary"]:
            return False
    if tick in case["available_ticks"]:
        if reality == "rotated" and rotation_boundary is not None:
            # After a real rotation, pre-boundary frames were wiped; only
            # publications at/after the lost boundary still exist.
            if max(case["available_ticks"]) < rotation_boundary:
                return False
        return True
    for lk in active:
        if lk["packet"] != case["packet"]:
            continue
        if min(case["available_ticks"]) <= lk["boundary"] and lk["boundary"] <= tick <= lk["lock_end"]:
            return True
    return False


def _completed_groups(captured, cases):
    """Groups whose every role of one release is present.

    Cross-release pairs never complete: roles are keyed by (packet, release).
    """
    have = {}
    for fragment_id, release in captured:
        match = next((c for c in cases if c["id"] == fragment_id
                      and c["release"] == release), None)
        if match is None:
            raise ValueError("captured unknown fragment: " + fragment_id)
        have.setdefault((match["packet"], release), set()).add(match["role"])
    done = []
    for group, roles in sorted(_groups(cases).items()):
        if roles and roles <= have.get(group, set()):
            done.append(group)
    return done


# ---------------------------------------------------------------------------
# deterministic search
# ---------------------------------------------------------------------------

def _retain_commands(scenario):
    """Configured receiver copies are collected in parallel at tick 0.

    The retainer records its own configured signal; its availability table
    only describes main-device readability and does not gate retention.
    """
    if not scenario["procedures"].get("receiver", {}).get("enabled", True):
        return [], set()
    by_id = {(c["id"], c["release"]): c for c in scenario["fragments"]}
    commands, captured, seen_keys = [], set(), set()
    for item in scenario["retained"]:
        if not item.get("configured"):
            continue
        key = (item["fragment"], item["release"])
        if key in seen_keys:
            continue
        case = by_id.get(key)
        if case is None or case["device"] not in (RECEIVER, "both"):
            continue
        seen_keys.add(key)
        commands.append({"tick": 0, "op": "retain", "fragment": case["id"],
                         "packet": case["packet"], "release": case["release"],
                         "device": RECEIVER})
        captured.add(key)
    return commands, captured


def solve_deterministic(scenario, start_tick=None, initial_captured=None,
                        initial_locks=None, reality="certain",
                        rotation_boundary=None):
    """Exhaustive finite-horizon search for the best guaranteed group weight.

    At every tick the main device gets exactly one read window.  A lock, if
    chosen, is signed at the publication boundary *after* that read, so it
    does not consume the window itself.
    """
    cases = scenario["fragments"]
    ticks = scenario["ticks"]
    start = ticks[0] if start_tick is None else start_tick
    lock_enabled = scenario["procedures"].get("lock", {}).get("enabled", False)
    duration = scenario["procedures"].get("lock", {}).get("duration_ticks", 1)
    retain_commands, retained_set = _retain_commands(scenario)
    base_captured = set(initial_captured or ()) | retained_set

    def freeze(lock_list):
        return tuple(sorted(
            (lk["kind"], lk["packet"], lk["boundary"], lk["lock_end"])
            for lk in lock_list))

    def thaw(frozen_locks):
        return tuple({"kind": kind, "packet": packet, "boundary": boundary,
                      "lock_end": lock_end}
                     for kind, packet, boundary, lock_end in frozen_locks)

    base_locks = freeze(initial_locks or ())
    memo = {}

    def value_of(captured):
        return sum(_group_weight(cases, p, r)
                   for (p, r) in _completed_groups(captured, cases))

    def dfs(tick, captured, locks):
        if tick > max(ticks):
            return value_of(captured), []
        k = (tick, frozenset(captured), locks)
        if k in memo:
            return memo[k]
        live_locks = thaw(locks)

        # Everything that can follow this tick's read, with or without a
        # boundary lock.  A lock is only available once before the horizon
        # ends and only for frames already published before this boundary.
        def continuations(read_captured):
            results = [dfs(tick + 1, read_captured, locks)]
            if (lock_enabled and reality != "rotated"
                    and tick < max(ticks) and len(locks) == 0
                    and not _has_pending_request(scenario)):
                for case in cases:
                    # Holdable frames must have appeared no later than this
                    # boundary tick; a later publication cannot be frozen.
                    # The lock is only useful when it extends at least one
                    # frame of this packet past the boundary tick.
                    if min(case["available_ticks"]) > tick:
                        continue
                    if not any(t <= tick for t in case["available_ticks"]) or not (
                            tick + 1 <= min(tick + duration, max(ticks) + 1)):
                        continue
                    if max(case["available_ticks"]) >= tick + 1:
                        continue
                    lock = {"kind": "lock", "packet": case["packet"],
                            "boundary": tick,
                            "lock_end": min(tick + duration, max(ticks) + 1)}
                    new_locks = freeze(live_locks + (lock,))
                    val, tail = dfs(tick + 1, read_captured, new_locks)
                    cmd = {"tick": tick, "op": "lock", "packet": case["packet"],
                           "boundary": tick, "lock_end": lock["lock_end"],
                           "publication_held_tick": tick}
                    results.append((val, [cmd] + tail))
            return results

        # Idle this window (a boundary lock may still follow).
        options = list(continuations(captured))
        for case in cases:
            if (case["id"], case["release"]) in captured:
                continue
            if not mounted(case, tick, live_locks, reality, rotation_boundary):
                continue
            nxt_captured = set(captured)
            nxt_captured.add((case["id"], case["release"]))
            read_cmd = {"tick": tick, "op": "read", "fragment": case["id"],
                        "packet": case["packet"], "release": case["release"],
                        "device": MAIN}
            for val, tail in continuations(nxt_captured):
                options.append((val, [read_cmd] + tail))

        best = max(options, key=lambda item: _rank(item, cases))
        memo[k] = best
        return best

    value, commands = dfs(start, base_captured, base_locks)
    # Retains run in parallel on the independent receiver and never occupy a
    # main-device window; keep each configured retain exactly once.
    seen = {(c.get("fragment"), c.get("release"))
            for c in commands if c["op"] == "retain"}
    prefix = [c for c in retain_commands
              if (c["fragment"], c["release"]) not in seen]
    return value, prefix + commands, base_captured, thaw(base_locks)


def _has_pending_request(scenario):
    req = scenario.get("device_request")
    return req is not None and req.get("outcome", "pending") == "pending"


def _rank(item, cases):
    value, commands = item
    # Prefer more guaranteed weight, then fewer lock requests and reads,
    # then a lexicographically stable signature.
    requests = sum(1 for c in commands if c["op"] == "lock")
    reads = sum(1 for c in commands if c["op"] == "read")
    signature = tuple((c["tick"], c["op"], c.get("fragment", c.get("packet", "")))
                      for c in commands)
    return value, -requests, -reads, tuple(-ord(ch) for ch in
                                           json.dumps(signature, ensure_ascii=False))


# ---------------------------------------------------------------------------
# contingent policy under a lost lock acknowledgement
# ---------------------------------------------------------------------------

def solve_uncertain(scenario):
    """Plan when a lock request has no acknowledgement.

    Two device realities remain compatible: the lock was executed, or the
    device rotated before executing it.  The first command must be legal in
    both; a mirror inspection is the only such command when the realities
    diverge, and each later read is selected by the observed state.
    """
    req = scenario["device_request"]
    if req is None or req.get("outcome", "pending") != "pending":
        raise ValueError("solve_uncertain requires a pending device request")
    boundary = req["boundary"]
    cases = scenario["fragments"]
    ticks = scenario["ticks"]
    if boundary not in ticks:
        raise ValueError("lock boundary must lie inside the readable horizon")

    prefix_captured = set()
    prefix_commands = list(scenario.get("executed", ()))
    for cmd in prefix_commands:
        _assert_prefix_legal(cmd, scenario)
        if cmd["op"] == "read":
            prefix_captured.add((cmd["fragment"], cmd["release"]))
        elif cmd["op"] == "retain":
            prefix_captured.add((cmd["fragment"], cmd["release"]))

    inspect = {"tick": boundary, "op": "inspect",
               "target": "device_lock_state",
               "legal_when": ("device locked at boundary %d" % boundary,
                              "device rotated at boundary %d" % boundary)}
    lock = {"kind": "lock", "packet": req["packet"], "boundary": boundary,
            "lock_end": min(boundary
                            + scenario["procedures"].get("lock", {})
                            .get("duration_ticks", 1), max(ticks) + 1)}
    branches = {}
    realities = {}
    for reality, locks in (("locked", (lock,)), ("rotated", ())):
        sub_value, sub_commands, _, _ = solve_deterministic(
            scenario, start_tick=boundary + 1,
            initial_captured=prefix_captured, initial_locks=locks,
            reality=reality,
            rotation_boundary=boundary if reality == "rotated" else None)
        unobtainable = _unobtainable(scenario, prefix_captured, sub_commands,
                                     locks, reality, boundary)
        branches[reality] = {
            "observation": reality,
            "commands": sub_commands,
            "unobtainable": unobtainable,
            "guaranteed_weight": sub_value,
        }
        realities[reality] = sub_value

    guaranteed = min(realities.values())
    plan = {
        "kind": "contingent_capture_plan",
        "ticks": list(ticks),
        "end_tick": scenario["end_tick"],
        "device_request": req,
        "prefix_commands": prefix_commands + [inspect],
        "branches": branches,
        "guaranteed_weight": guaranteed,
        "guaranteed_groups": _groups_at_weight(scenario, branches, guaranteed),
        "commands": prefix_commands + [inspect],
        "unobtainable": _merge_unobtainable(branches),
    }
    validate_plan(scenario, plan)
    return plan


def _assert_prefix_legal(cmd, scenario):
    case = next((c for c in scenario["fragments"]
                 if c["id"] == cmd.get("fragment")
                 and c["release"] == cmd.get("release", DEFAULT_RELEASE)), None)
    if cmd["op"] == "read":
        if case is None or not mounted(case, cmd["tick"], (), "certain"):
            raise ValueError("prefix command is not legal in every device state: "
                             + json.dumps(cmd))
    elif cmd["op"] == "retain":
        if case is None or case["device"] not in (RECEIVER, "both"):
            raise ValueError("prefix retain is not configured: " + cmd["fragment"])


def _unobtainable(scenario, prefix_captured, commands, locks, reality, boundary):
    captured = set(prefix_captured)
    for cmd in commands:
        if cmd["op"] in ("read", "retain"):
            captured.add((cmd["fragment"], cmd["release"]))
    missing = []
    remaining_ticks = [t for t in scenario["ticks"] if t > boundary]
    for case in scenario["fragments"]:
        if (case["id"], case["release"]) in captured:
            continue
        ever_mountable = any(
            mounted(case, t, locks, reality,
                    rotation_boundary=boundary if reality == "rotated" else None)
            for t in remaining_ticks)
        if not ever_mountable:
            if reality == "rotated" and max(case["available_ticks"]) < boundary:
                lost = boundary
            else:
                lost = min(case["available_ticks"])
            missing.append({
                "fragment": case["id"],
                "packet": case["packet"],
                "release": case["release"],
                "role": case["role"],
                "lost_tick": lost,
                "reason": ("device rotation wiped the frame before capture"
                           if reality == "rotated"
                           else "no readable copy remains inside the horizon"),
            })
    return missing


def _merge_unobtainable(branches):
    seen, merged = set(), []
    for reality in sorted(branches):
        for item in branches[reality]["unobtainable"]:
            key = (item["fragment"], item["release"], reality)
            if key not in seen:
                record = dict(item)
                record["reality"] = reality
                merged.append(record)
                seen.add(key)
    return merged


def _groups_at_weight(scenario, branches, guaranteed):
    groups = set()
    for reality, branch in branches.items():
        if branch["guaranteed_weight"] < guaranteed:
            continue
        for cmd in branch["commands"]:
            if cmd["op"] in ("read", "retain"):
                groups.add((cmd["packet"], cmd["release"]))
    return [{"packet": p, "release": r} for p, r in sorted(groups)]


# ---------------------------------------------------------------------------
# public planning API
# ---------------------------------------------------------------------------

def solve_plan(scenario):
    """Return the best executable capture program for a scenario."""
    validate_scenario(scenario)
    if _has_pending_request(scenario):
        return solve_uncertain(scenario)
    value, commands, _, _ = solve_deterministic(scenario)
    cases = scenario["fragments"]
    captured = {(c["fragment"], c["release"])
                for c in commands if c["op"] in ("read", "retain")}
    done = _completed_groups(captured, cases)
    deferred = []
    for packet, release in sorted(_groups(cases)):
        if (packet, release) in done:
            continue
        deferred.append({
            "packet": packet,
            "release": release,
            "weight": _group_weight(cases, packet, release),
            "reason": "still readable but sacrificed for a higher-weight group",
        })
    lock_cmd = next((c for c in commands if c["op"] == "lock"), None)
    plan = {
        "kind": "capture_plan",
        "ticks": list(scenario["ticks"]),
        "end_tick": scenario["end_tick"],
        "commands": commands,
        "guaranteed_weight": value,
        "guaranteed_groups": [
            {"packet": p, "release": r,
             "weight": _group_weight(cases, p, r)} for p, r in done
        ],
        "deferred": deferred,
        "unobtainable": [],
        "device_request": lock_cmd,
    }
    validate_plan(scenario, plan)
    return plan


def validate_plan(scenario, plan):
    """Static + multi-reality validation of a capture program.

    A program is rejected when any shared prefix command is illegal in one
    of the compatible device realities (a stitched-together per-state plan),
    when a read targets an unmounted fragment, or when releases are mixed
    across a group boundary.
    """
    if plan.get("kind") == "contingent_capture_plan":
        return _validate_contingent(scenario, plan)
    captured = set()
    locks = ()
    for cmd in plan["commands"]:
        locks = _apply_command(cmd, scenario, captured, locks, reality="certain")
    done = _completed_groups(captured, scenario["fragments"])
    expected = sum(_group_weight(scenario["fragments"], p, r) for p, r in done)
    if plan["guaranteed_weight"] != expected:
        raise ValueError("plan claims weight %d but simulation yields %d"
                         % (plan["guaranteed_weight"], expected))
    for held in (c for c in plan["commands"] if c["op"] == "lock"):
        # A fragment whose first publication was suppressed by the lock
        # cannot have been observed while the lock is active.
        for case in scenario["fragments"]:
            if (min(case["available_ticks"]) > held["boundary"]
                    and (case["id"], case["release"]) in captured):
                raise ValueError("plan claims a publication suppressed by the lock")
    return True


def _validate_contingent(scenario, plan):
    req = scenario["device_request"]
    boundary = req["boundary"]
    prefix_captured = set()
    saw_inspect = False
    for cmd in plan["prefix_commands"]:
        if cmd["op"] == "inspect":
            saw_inspect = True
            continue
        # Prefix reads happen before the boundary: the rotation, if any, has
        # not occurred yet, so the read must be legal in both realities.
        _apply_command(cmd, scenario, set(prefix_captured), (), reality="certain")
        if cmd["op"] in ("read", "retain"):
            prefix_captured.add((cmd["fragment"], cmd["release"]))
    if not saw_inspect:
        raise ValueError("uncertain plan must reserve a mirror inspection tick "
                         "before state-dependent reads")
    if plan["prefix_commands"][-1]["op"] != "inspect":
        raise ValueError("inspection must be the last shared prefix command")
    for reality, branch in plan["branches"].items():
        is_rotated = reality == "rotated"
        locks = ({"kind": "lock", "packet": req["packet"], "boundary": boundary,
                  "lock_end": req.get("lock_end", boundary + 1)},) if not is_rotated else ()
        captured = set(prefix_captured)
        for cmd in branch["commands"]:
            locks = _apply_command(
                cmd, scenario, captured, locks, reality=reality,
                rotation_boundary=boundary if is_rotated else None)
        done = _completed_groups(captured, scenario["fragments"])
        weight = sum(_group_weight(scenario["fragments"], p, r) for p, r in done)
        if weight != branch["guaranteed_weight"]:
            raise ValueError("branch %s claims weight %d but yields %d"
                             % (reality, branch["guaranteed_weight"], weight))
    return True


def _apply_command(cmd, scenario, captured, locks, reality="certain",
                   rotation_boundary=None):
    cases = {(c["id"], c["release"]): c for c in scenario["fragments"]}
    if cmd["op"] == "read":
        case = cases.get((cmd["fragment"], cmd["release"]))
        if case is None:
            raise ValueError("unknown fragment in plan: " + cmd["fragment"])
        if not mounted(case, cmd["tick"], locks, reality, rotation_boundary):
            raise ValueError("fragment not mounted when read: %s @tick %d (%s)"
                             % (cmd["fragment"], cmd["tick"], reality))
        captured.add((cmd["fragment"], cmd["release"]))
    elif cmd["op"] == "retain":
        case = cases.get((cmd["fragment"], cmd["release"]))
        if case is None or case["device"] not in (RECEIVER, "both") or not case["configured"]:
            raise ValueError("receiver is not configured to retain: " + cmd["fragment"])
        captured.add((cmd["fragment"], cmd["release"]))
    elif cmd["op"] == "lock":
        locks = locks + ({"kind": "lock", "packet": cmd["packet"],
                          "boundary": cmd["boundary"], "lock_end": cmd["lock_end"]},)
    elif cmd["op"] == "inspect":
        return locks
    else:
        raise ValueError("unknown command op: " + cmd["op"])
    return locks


# ---------------------------------------------------------------------------
# offline execution simulation
# ---------------------------------------------------------------------------

def simulate(scenario, plan, reality="certain"):
    """Execute a plan against one concrete device reality.

    Returns the real fragments captured, completed groups, total weight and
    any confirmed-missing fragments with the tick at which they vanished.
    Planning output without this simulation never becomes evidence.
    """
    if plan.get("kind") == "contingent_capture_plan":
        return _simulate_contingent(scenario, plan, reality)
    captured = set()
    locks = ()
    trace = []
    for cmd in plan["commands"]:
        locks = _apply_command(cmd, scenario, captured, locks, reality="certain")
        if cmd["op"] in ("read", "retain"):
            trace.append({"tick": cmd["tick"], "op": cmd["op"],
                          "fragment": cmd["fragment"], "packet": cmd["packet"],
                          "release": cmd["release"], "device": cmd.get("device", MAIN)})
    return _simulation_result(scenario, captured, trace, plan.get("unobtainable", []),
                              reality)


def _simulate_contingent(scenario, plan, reality):
    if reality not in plan["branches"]:
        raise ValueError("reality must be one of: " + ", ".join(sorted(plan["branches"])))
    captured = set()
    locks = ()
    trace = []
    for cmd in plan["prefix_commands"]:
        if cmd["op"] == "inspect":
            trace.append({"tick": cmd["tick"], "op": "inspect",
                          "observation": reality})
            continue
        locks = _apply_command(cmd, scenario, captured, locks, reality=reality)
        if cmd["op"] in ("read", "retain"):
            trace.append({"tick": cmd["tick"], "op": cmd["op"],
                          "fragment": cmd["fragment"], "packet": cmd["packet"],
                          "release": cmd["release"], "device": cmd.get("device", MAIN)})
    req = plan["device_request"]
    if reality == "locked":
        locks = ({"kind": "lock", "packet": req["packet"],
                  "boundary": req["boundary"],
                  "lock_end": req.get("lock_end", req["boundary"] + 1)},)
    branch = plan["branches"][reality]
    is_rotated = reality == "rotated"
    for cmd in branch["commands"]:
        locks = _apply_command(
            cmd, scenario, captured, locks, reality=reality,
            rotation_boundary=req["boundary"] if is_rotated else None)
        if cmd["op"] in ("read", "retain"):
            trace.append({"tick": cmd["tick"], "op": cmd["op"],
                          "fragment": cmd["fragment"], "packet": cmd["packet"],
                          "release": cmd["release"], "device": cmd.get("device", MAIN)})
    return _simulation_result(scenario, captured, trace, branch["unobtainable"], reality)


def _simulation_result(scenario, captured, trace, unobtainable, reality):
    done = _completed_groups(captured, scenario["fragments"])
    captured_records = []
    for step in trace:
        if step["op"] not in ("read", "retain"):
            continue
        captured_records.append(step)
    return {
        "reality": reality,
        "captured": captured_records,
        "completed_groups": [{"packet": p, "release": r,
                              "weight": _group_weight(scenario["fragments"], p, r)}
                             for p, r in done],
        "weight": sum(_group_weight(scenario["fragments"], p, r) for p, r in done),
        "unobtainable": unobtainable,
        "trace": trace,
    }


# ---------------------------------------------------------------------------
# offline execution entry
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m wastetransit.capture_desk",
        description="Offline finite-retention capture planner and simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="solve a capture-queue document")
    p_plan.add_argument("--input", required=True)
    p_plan.add_argument("--db")
    p_plan.add_argument("--batch")
    p_plan.add_argument("--out")

    p_sim = sub.add_parser("simulate", help="execute a saved plan in one reality")
    p_sim.add_argument("--input", required=True)
    p_sim.add_argument("--plan", required=True)
    p_sim.add_argument("--reality", default="certain")
    p_sim.add_argument("--out")

    p_db = sub.add_parser("replay-db", help="solve persisted requests from a database")
    p_db.add_argument("--db", required=True)
    p_db.add_argument("--batch")

    args = parser.parse_args(argv)
    if args.command == "plan":
        doc = json.loads(Path(args.input).read_text(encoding="utf-8"))
        scenario = scenario_from_doc(doc)
        plan = solve_plan(scenario)
        if args.db and args.batch:
            from .store import connect
            db = connect(args.db)
            try:
                save_plan(db, args.batch, plan, created_tick=min(scenario["ticks"]))
            finally:
                db.close()
        _emit(plan, args.out)
    elif args.command == "simulate":
        doc = json.loads(Path(args.input).read_text(encoding="utf-8"))
        scenario = scenario_from_doc(doc)
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        validate_plan(scenario, plan)
        result = simulate(scenario, plan, reality=args.reality)
        _emit(result, args.out)
    else:
        from .store import connect
        db = connect(args.db)
        try:
            scenario = build_scenario(db, batch=args.batch)
            _emit(solve_plan(scenario))
        finally:
            db.close()


def _emit(payload, out=None):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()

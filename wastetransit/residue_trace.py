"""Offline intake and reconstruction of the synthetic residue station.

Two evidence layers are kept strictly separate:

* ``residue_summary`` holds the old equipment count summary ("loaded 1,
  cycle 1, removed 1").  The same three counts are compatible with both an
  effective L->P->E history and an L->E->P history where the run command
  executed on an empty machine, so a count summary can never by itself sign
  a re-transferable cycle credential.
* ``residue_frame`` / ``interlock_attestation`` hold the two independent
  originals: station raw frames (load / continuation / run / exit witnesses)
  and protection-controller interlock attestations.  From them a single
  physical event history is reconstructed per device and component as an
  ordering constraint graph.  Later intake can only narrow the remaining
  possibilities or prove a conflict; no L/P/E action is ever synthesised.
"""

from .source import record
from .store import history

# Physical event kinds (all synthetic, no real instruments).
LOAD = "load"
LOCK = "lock"
RUN = "run"
UNLOCK = "unlock"
CONTINUATION = "continuation"
EXIT = "exit"

# Final credential states.
CERTIFIED = "cycle_certified"
HOLD_ORIGIN = "hold_origin"
UNCERTAIN = "uncertain_blocking"
CONFLICT = "sequence_conflict"
PENDING = "pending_recollection"
NO_CLAIM = "no_claim"

# Proof-layer states, before the mother-batch gate is applied.
P_CERTIFIED = "certified"
P_UNCERTAIN = "uncertain"
P_CONFLICT = "conflict"
P_PENDING = "pending"
P_NO_CLAIM = "no_claim"

_INTERLOCK_KINDS = (LOCK, RUN, UNLOCK)
_KEY_ORDER = (LOAD, LOCK, RUN, UNLOCK, EXIT)
_MAX_ENUMERATIONS = 20


def initialize(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS residue_summary (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            completed_count INTEGER NOT NULL,
            state TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS receipt_restriction (
            batch TEXT PRIMARY KEY,
            guarded_controller INTEGER NOT NULL,
            intact_cycle INTEGER NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS residue_frame (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            component_id TEXT NOT NULL,
            batch TEXT NOT NULL,
            kind TEXT NOT NULL,
            seq INTEGER NOT NULL,
            at TEXT,
            witness TEXT,
            source TEXT,
            requires_lock INTEGER
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS interlock_attestation (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            component_id TEXT NOT NULL,
            batch TEXT NOT NULL,
            ticket_id TEXT,
            lock_engaged INTEGER NOT NULL,
            binding_continuous INTEGER NOT NULL,
            run_confirmed INTEGER NOT NULL,
            released_at_removal INTEGER NOT NULL,
            bypass_state TEXT NOT NULL,
            run_at TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS cycle_credential (
            batch TEXT NOT NULL,
            component_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            state TEXT NOT NULL,
            proof_state TEXT NOT NULL,
            basis TEXT,
            run_inside_residence INTEGER,
            run_at TEXT,
            reason TEXT NOT NULL,
            version INTEGER NOT NULL,
            PRIMARY KEY (batch, component_id, device_id)
        )"""
    )


# ---------------------------------------------------------------------------
# Original 1: equipment count summary
# ---------------------------------------------------------------------------

def refresh(db, batch):
    """Recompute every residue conclusion for one mother batch.

    Deterministic over the stored originals, so late facts (a re-weigh that
    re-opens the mother batch, an additional station ticket) simply re-run
    the same reading instead of overwriting field facts.
    """
    initialize(db)
    receipt = db.execute(
        "SELECT state FROM closure WHERE batch=?", (batch,)
    ).fetchone()
    if receipt and receipt["state"] == "closed":
        db.execute(
            """UPDATE residue_summary SET state=CASE WHEN completed_count>0
                  THEN 'claimed_complete' ELSE 'no_claim' END WHERE batch=?""",
            (batch,),
        )
    else:
        db.execute(
            "UPDATE residue_summary SET state='hold_origin' WHERE batch=?",
            (batch,),
        )
    for device, component in db.execute(
        """SELECT DISTINCT device_id, component_id FROM residue_frame
           WHERE batch=?
           UNION
           SELECT device_id, component_id FROM interlock_attestation
           WHERE batch=?""",
        (batch, batch),
    ):
        _recompute_credential(db, batch, device, component, receipt)


def import_data(db, doc):
    """Intake original 1: the unordered equipment summary."""
    batches = set()
    with db:
        initialize(db)
        restrictions = doc.get("receipt_restrictions")
        for item in doc["summaries"]:
            if record(db, "residue-summary", item):
                db.execute(
                    "INSERT INTO residue_summary VALUES (?,?,?,?)",
                    (item["id"], item["batch"], item["completed_count"], "hold_origin"),
                )
            batches.add(item["batch"])
        if restrictions is not None:
            for batch in sorted(batches):
                db.execute(
                    """INSERT INTO receipt_restriction VALUES(?,?,?)
                       ON CONFLICT(batch) DO UPDATE SET
                         guarded_controller=excluded.guarded_controller,
                         intact_cycle=excluded.intact_cycle""",
                    (
                        batch,
                        int(bool(restrictions.get("accepts_guarded_controller", False))),
                        int(bool(restrictions.get("requires_intact_cycle_proof", False))),
                    ),
                )
        for batch in sorted(batches):
            refresh(db, batch)
    return [dict(row) for row in db.execute(
        "SELECT * FROM residue_summary ORDER BY id"
    )]


# ---------------------------------------------------------------------------
# Original 2: independent protection-controller interlock attestation
# ---------------------------------------------------------------------------

def import_interlock(db, doc):
    """Intake original 2: guarded-controller interlock attestations."""
    batches = set()
    with db:
        initialize(db)
        for item in doc.get("attestations", []):
            if record(db, "interlock-attestation", item):
                db.execute(
                    "INSERT INTO interlock_attestation VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item["id"], item["device_id"], item["component_id"],
                        item["batch"], item.get("ticket_id"),
                        int(bool(item["lock_engaged"])),
                        int(bool(item["binding_continuous"])),
                        int(bool(item["run_confirmed"])),
                        int(bool(item["released_at_removal"])),
                        item.get("bypass_state", "unknown"),
                        item.get("run_at"),
                    ),
                )
            batches.add(item["batch"])
        for batch in sorted(batches):
            refresh(db, batch)
    return _credential_rows(db, sorted(batches))


# ---------------------------------------------------------------------------
# Original 3: processing-station raw frames
# ---------------------------------------------------------------------------

def import_station(db, doc):
    """Intake original 3: raw on-component witness frames and run output."""
    batches = set()
    with db:
        initialize(db)
        for item in doc.get("frames", []):
            if record(db, "station-frame", item):
                db.execute(
                    "INSERT INTO residue_frame VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item["id"], item.get("event_id", item["id"]),
                        item["ticket_id"], item["device_id"], item["component_id"],
                        item["batch"], item["kind"], item["seq"], item.get("at"),
                        item.get("witness"), item.get("source"),
                        None if item.get("requires_lock") is None
                        else int(bool(item["requires_lock"])),
                    ),
                )
            batches.add(item["batch"])
        for batch in sorted(batches):
            refresh(db, batch)
    return _credential_rows(db, sorted(batches))


# ---------------------------------------------------------------------------
# Physical event reconstruction (one actual first reading)
# ---------------------------------------------------------------------------

def _physical_events(db, batch, device, component):
    """Merge frames and attestations into deduplicated physical events.

    Returns (events, edges, conflict):

    * events: event_id -> {"kind", "at", "sources", "tickets"}
    * edges: set of (earlier_event, later_event) hard ordering constraints
    * conflict: None or a machine-readable reason string
    """
    events = {}
    conflict = None

    def add(event_id, kind, at, source):
        nonlocal conflict
        ev = events.get(event_id)
        if ev is None:
            events[event_id] = {
                "kind": kind, "at": at, "sources": [source],
                "tickets": set(),
            }
        else:
            if ev["kind"] != kind:
                conflict = conflict or "event_kind_conflict:%s" % event_id
            ev["sources"].append(source)
            if at is not None and ev["at"] is not None and at != ev["at"]:
                conflict = conflict or "event_timestamp_conflict:%s" % event_id
            elif ev["at"] is None:
                ev["at"] = at

    frames = db.execute(
        """SELECT * FROM residue_frame WHERE batch=? AND device_id=?
           AND component_id=? ORDER BY ticket_id, seq, id""",
        (batch, device, component),
    ).fetchall()
    for f in frames:
        add(f["event_id"], f["kind"], f["at"], f["id"])
        events[f["event_id"]]["tickets"].add(f["ticket_id"])

    attestations = db.execute(
        """SELECT * FROM interlock_attestation WHERE batch=? AND device_id=?
           AND component_id=? ORDER BY id""",
        (batch, device, component),
    ).fetchall()
    for a in attestations:
        prefix = "att:%s" % a["id"]
        if a["lock_engaged"]:
            add(prefix + "#lock", LOCK, None, a["id"])
        if a["run_confirmed"]:
            # Even under an unproven bypass this remains a bare-machine run
            # fact: it is kept in the common history, just not certified.
            add(prefix + "#run", RUN, a["run_at"], a["id"])
        if a["released_at_removal"]:
            add(prefix + "#unlock", UNLOCK, None, a["id"])

    edges = set()
    self_conflicts = []

    def edge(src, dst):
        if src == dst:
            # The same physical event would have to precede itself: two
            # witness streams cannot share it under incompatible positions
            # (e.g. one continuation seen before lock and another after).
            self_conflicts.append(src)
        else:
            edges.add((src, dst))

    # Constraint 1: ticket order (each ticket is one ordered witness stream).
    for ticket in {f["ticket_id"] for f in frames}:
        seq_ids = [f["event_id"] for f in frames if f["ticket_id"] == ticket]
        for earlier, later in zip(seq_ids, seq_ids[1:]):
            edge(earlier, later)

    # Constraint 2: explicit timestamps order the events carrying them.
    # Equal timestamps carry no direction, and a timestamp edge is only added
    # when it does not contradict a constraint already collected.
    stamped = sorted(
        ((ev["at"], eid) for eid, ev in events.items() if ev["at"] is not None),
        key=lambda x: (x[0], x[1]),
    )
    for (at_a, earlier), (at_b, later) in zip(stamped, stamped[1:]):
        if at_a == at_b:
            continue
        edge(earlier, later)

    by_kind = {}
    for eid, ev in events.items():
        by_kind.setdefault(ev["kind"], []).append(eid)

    # Constraint 3: interlock / physical-lock semantics.  The run-vs-exit
    # relation is deliberately NOT hard-coded: evidence alone must decide
    # whether the run happened while the residue was still inside, so an
    # L->E->P history stays representable.
    locks = by_kind.get(LOCK, [])
    unlocks = by_kind.get(UNLOCK, [])
    runs = by_kind.get(RUN, [])
    loads = by_kind.get(LOAD, [])
    exits = by_kind.get(EXIT, [])
    for lk in locks:
        for rn in runs:
            edge(lk, rn)
        for ul in unlocks:
            edge(lk, ul)
        for ld in loads:
            edge(ld, lk)
    for ul in unlocks:
        for ex in exits:
            edge(ul, ex)
    for ld in loads:
        for ex in exits:
            edge(ld, ex)

    # A continuation that can only occur while locked must sit between the
    # lock and its release; an unprovable requirement is itself a conflict.
    for f in frames:
        if f["requires_lock"]:
            cid = f["event_id"]
            if not locks or not unlocks:
                conflict = conflict or "lock_requirement_without_interlock:%s" % cid
                continue
            for lk in locks:
                edge(lk, cid)
            for ul in unlocks:
                edge(cid, ul)

    if conflict is None and self_conflicts:
        conflict = "self_precedence:%s" % events[self_conflicts[0]]["kind"]
    if conflict is None:
        cycle = _find_cycle(events, edges)
        if cycle is not None:
            conflict = "ordering_cycle:" + ">".join(
                events[eid]["kind"] for eid in cycle
            )
    return events, edges, conflict


def _find_cycle(events, edges):
    """Return one cycle of event ids, or None when the graph is acyclic."""
    adjacency = {eid: [] for eid in events}
    indegree = {eid: 0 for eid in events}
    for src, dst in edges:
        if src in adjacency and dst in indegree:
            adjacency[src].append(dst)
            indegree[dst] += 1
    queue = sorted(eid for eid, deg in indegree.items() if deg == 0)
    seen = 0
    while queue:
        node = queue.pop(0)
        seen += 1
        for nxt in sorted(adjacency[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen == len(events):
        return None
    # DFS back-edge extraction on the remaining cyclic subgraph.
    stack, on_stack, visited = [], set(), set()

    def dfs(node):
        visited.add(node)
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(adjacency[node]):
            if nxt in on_stack:
                return stack[stack.index(nxt):] + [nxt]
            if nxt not in visited:
                found = dfs(nxt)
                if found:
                    return found
        stack.pop()
        on_stack.remove(node)
        return None

    for eid in sorted(events):
        if eid not in visited:
            found = dfs(eid)
            if found:
                return found
    return None


def _reachable(events, edges):
    reach = {eid: set() for eid in events}
    for src, dst in edges:
        if src in reach and dst in reach:
            reach[src].add(dst)
    changed = True
    while changed:
        changed = False
        for eid in events:
            before = len(reach[eid])
            reach[eid] = set().union(
                *(reach[mid] | {mid} for mid in list(reach[eid])),
                set(),
            )
            if len(reach[eid]) != before:
                changed = True
    return reach


def _enumerate_key_orders(events, edges):
    """All topological orders of the L/lock/P/unlock/E key events."""
    key_ids = sorted(
        (eid for eid, ev in events.items() if ev["kind"] in _KEY_ORDER),
        key=lambda eid: (_KEY_ORDER.index(events[eid]["kind"]), eid),
    )
    key_set = set(key_ids)
    induced = {eid: set() for eid in key_ids}
    indegree = {eid: 0 for eid in key_ids}
    for src, dst in edges:
        if src in key_set and dst in key_set:
            if dst not in induced[src]:
                induced[src].add(dst)
                indegree[dst] += 1
    orders, path = [], []

    def enumerate_orders(remaining_indegree):
        if len(orders) >= _MAX_ENUMERATIONS:
            return
        ready = sorted(eid for eid in key_ids if remaining_indegree[eid] == 0
                       and eid not in path)
        if not ready:
            if len(path) == len(key_ids):
                orders.append([events[eid]["kind"] for eid in path])
            return
        for eid in ready:
            path.append(eid)
            next_indegree = dict(remaining_indegree)
            for nxt in induced[eid]:
                next_indegree[nxt] -= 1
            enumerate_orders(next_indegree)
            path.pop()

    enumerate_orders(indegree)
    return orders


# ---------------------------------------------------------------------------
# The two compliance contracts
# ---------------------------------------------------------------------------

def _restrictions(db, batch):
    row = db.execute(
        "SELECT * FROM receipt_restriction WHERE batch=?", (batch,)
    ).fetchone()
    if row is None:
        return True, True  # no restriction registered: both contracts open
    return bool(row["guarded_controller"]), bool(row["intact_cycle"])


def _try_interlock_contract(db, batch, device, component, events, edges,
                            reach, allow_guarded):
    """Contract 1: physical interlock engaged for this very load box."""
    if not allow_guarded:
        return None
    attestations = db.execute(
        """SELECT * FROM interlock_attestation WHERE batch=? AND device_id=?
           AND component_id=? ORDER BY id""",
        (batch, device, component),
    ).fetchall()
    qualifying = [
        a for a in attestations
        if a["lock_engaged"] and a["binding_continuous"]
        and a["run_confirmed"] and a["released_at_removal"]
        and a["bypass_state"] == "off"
    ]
    if not qualifying:
        return None
    # Later field facts may contradict the controller: a run provably after
    # the exit cannot be a treatment of this residue.
    run_ids = [eid for eid, ev in events.items() if ev["kind"] == RUN]
    exit_ids = [eid for eid, ev in events.items() if ev["kind"] == EXIT]
    for ex in exit_ids:
        for rn in run_ids:
            if rn in reach[ex]:
                return None
    chosen = qualifying[0]
    return {
        "basis": "guarded_interlock",
        "run_at": chosen["run_at"],
        "attestation_id": chosen["id"],
    }


def _try_station_contract(db, batch, device, component, events, edges,
                          reach, allow_intact):
    """Contract 2: component witnesses plus an independent run timestamp."""
    if not allow_intact:
        return None
    frames = db.execute(
        """SELECT * FROM residue_frame WHERE batch=? AND device_id=?
           AND component_id=?""",
        (batch, device, component),
    ).fetchall()
    witnessed = {f["kind"]: f for f in frames if f["witness"]}
    run_outputs = [
        f for f in frames
        if f["kind"] == RUN and f["source"] == "independent_output" and f["at"]
    ]
    if not all(k in witnessed for k in (LOAD, CONTINUATION, EXIT)):
        return None
    if not run_outputs:
        return None
    load_id = witnessed[LOAD]["event_id"]
    exit_id = witnessed[EXIT]["event_id"]
    run_frame = sorted(run_outputs, key=lambda f: (f["at"], f["id"]))[0]
    run_id = run_frame["event_id"]
    # The same component must be provably inside for the whole interval:
    # the graph has to force load -> run -> exit, not merely permit it.
    if run_id not in reach[load_id] or exit_id not in reach[run_id]:
        return None
    return {
        "basis": "station_frames",
        "run_at": run_frame["at"],
        "attestation_id": None,
    }


def _recompute_credential(db, batch, device, component, receipt):
    events, edges, conflict = _physical_events(db, batch, device, component)
    reach = _reachable(events, edges)
    allow_guarded, allow_intact = _restrictions(db, batch)

    kinds = {ev["kind"] for ev in events.values()}
    run_ids = sorted(eid for eid, ev in events.items() if ev["kind"] == RUN)
    exit_ids = sorted(eid for eid, ev in events.items() if ev["kind"] == EXIT)
    load_ids = sorted(eid for eid, ev in events.items() if ev["kind"] == LOAD)

    proof = None
    run_inside = None
    reason = ""
    blocking_orders = []

    if conflict is not None:
        proof_state = P_CONFLICT
        reason = "no jointly executable field history: " + conflict
        blocking_orders = [conflict.split(":", 1)[1].split(">")] \
            if conflict.startswith("ordering_cycle:") else []
    else:
        proof = _try_interlock_contract(
            db, batch, device, component, events, edges, reach, allow_guarded
        )
        if proof is None:
            proof = _try_station_contract(
                db, batch, device, component, events, edges, reach,
                allow_intact
            )
        if proof is not None:
            proof_state = P_CERTIFIED
            run_inside = 1
            reason = "effective cycle proven by %s" % proof["basis"]
        else:
            run_after_exit = bool(exit_ids) and bool(run_ids) and all(
                any(rn in reach[ex] for rn in run_ids) for ex in exit_ids
            )
            swappable = bool(run_ids) and bool(exit_ids) and all(
                ex not in reach[rn] and rn not in reach[ex]
                for rn in run_ids for ex in exit_ids
            )
            if run_after_exit:
                proof_state = P_CONFLICT
                run_inside = 0
                reason = "run is provably later than the component exit"
            elif swappable:
                proof_state = P_UNCERTAIN
                reason = ("run/exit order still exchangeable and the key "
                          "interval cannot be re-collected from current "
                          "originals")
                blocking_orders = _enumerate_key_orders(events, edges)
            elif EXIT in kinds:
                proof_state = P_PENDING
                reason = ("exit witnessed without intact cycle proof; "
                          "synthetic pending state retained, process not "
                          "re-run")
            else:
                proof_state = P_PENDING
                reason = "awaiting recollection of the missing cycle interval"

    # Mother-batch gate: an open origin blocks issuance of a new re-transfer
    # credential but never deletes the field facts that already arrived.
    gate_closed = bool(receipt and receipt["state"] == "closed")
    hold = db.execute(
        "SELECT state FROM future_dispatch_hold WHERE batch=?", (batch,)
    ).fetchone()
    if hold and hold["state"] == "blocked":
        gate_closed = False

    if proof_state == P_CERTIFIED:
        state = CERTIFIED if gate_closed else HOLD_ORIGIN
        if not gate_closed:
            reason = "cycle proven but mother batch is not closed: held at origin"
        run_at = proof["run_at"]
        basis = proof["basis"]
    else:
        state = {
            P_UNCERTAIN: UNCERTAIN,
            P_CONFLICT: CONFLICT,
            P_PENDING: PENDING,
            P_NO_CLAIM: NO_CLAIM,
        }[proof_state]
        run_at = None
        basis = None

    old = db.execute(
        "SELECT * FROM cycle_credential WHERE batch=? AND component_id=? "
        "AND device_id=?",
        (batch, component, device),
    ).fetchone()
    values = (state, proof_state, basis, run_inside, run_at, reason)
    if old is None or (old["state"], old["proof_state"], old["basis"],
                       old["run_inside_residence"], old["run_at"],
                       old["reason"]) != values:
        version = old["version"] + 1 if old else 1
        db.execute(
            """INSERT INTO cycle_credential VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(batch, component_id, device_id) DO UPDATE SET
                 state=excluded.state, proof_state=excluded.proof_state,
                 basis=excluded.basis,
                 run_inside_residence=excluded.run_inside_residence,
                 run_at=excluded.run_at, reason=excluded.reason,
                 version=excluded.version""",
            (batch, component, device, *values, version),
        )
        history(db, "residue-cycle", batch, state, reason, component)


def _credential_rows(db, batches):
    if not batches:
        return []
    marks = ",".join("?" for _ in batches)
    return [dict(row) for row in db.execute(
        "SELECT * FROM cycle_credential WHERE batch IN (%s) "
        "ORDER BY batch, device_id, component_id" % marks, batches
    )]


# ---------------------------------------------------------------------------
# Offline query entry
# ---------------------------------------------------------------------------

def _event_listing(db, batch, device, component):
    events, edges, conflict = _physical_events(db, batch, device, component)
    reach = _reachable(events, edges)
    # Present physical events in one executable topological reading when the
    # graph allows it; fall back to id order for conflicting histories.
    indegree = {eid: 0 for eid in events}
    adjacency = {eid: [] for eid in events}
    for src, dst in edges:
        if src in adjacency and dst in indegree:
            adjacency[src].append(dst)
            indegree[dst] += 1
    queue = sorted(eid for eid, deg in indegree.items() if deg == 0)
    ordered = []
    while queue:
        node = queue.pop(0)
        ordered.append(node)
        for nxt in sorted(adjacency[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(ordered) != len(events):
        ordered = sorted(events)
    return [
        {
            "event_id": eid,
            "kind": events[eid]["kind"],
            "at": events[eid]["at"],
            "tickets": sorted(events[eid]["tickets"]),
            "sources": sorted(events[eid]["sources"]),
        }
        for eid in ordered
    ], sorted(edges), conflict, reach


def query(db, batch=None):
    """Return the common device/component histories and credentials."""
    initialize(db)
    if batch is None:
        batches = sorted({
            r[0] for r in db.execute(
                "SELECT batch FROM residue_summary UNION "
                "SELECT batch FROM residue_frame UNION "
                "SELECT batch FROM interlock_attestation"
            )
        })
    else:
        batches = [batch]
    result = []
    for b in batches:
        receipt = db.execute(
            "SELECT * FROM closure WHERE batch=?", (b,)
        ).fetchone()
        summaries = [dict(r) for r in db.execute(
            "SELECT * FROM residue_summary WHERE batch=? ORDER BY id", (b,)
        )]
        keys = list(db.execute(
            """SELECT DISTINCT device_id, component_id FROM residue_frame
               WHERE batch=?
               UNION
               SELECT device_id, component_id FROM interlock_attestation
               WHERE batch=?""",
            (b, b),
        ))
        credentials = []
        for key in keys:
            device, component = key["device_id"], key["component_id"]
            cred = db.execute(
                "SELECT * FROM cycle_credential WHERE batch=? AND device_id=? "
                "AND component_id=?",
                (b, device, component),
            ).fetchone()
            events, edges, conflict, _ = _event_listing(
                db, b, device, component
            )
            entry = dict(cred) if cred else {}
            entry.update({
                "device_id": device,
                "component_id": component,
                "events": events,
                "ordering_edges": [[src, dst] for src, dst in edges],
                "run_inside_residence": (
                    None if cred is None or cred["run_inside_residence"] is None
                    else bool(cred["run_inside_residence"])
                ),
            })
            if cred and cred["proof_state"] == P_UNCERTAIN:
                phys, phys_edges, _c = _physical_events(db, b, device, component)
                entry["blocking_orders"] = _enumerate_key_orders(
                    phys, phys_edges
                )
            elif cred and cred["proof_state"] == P_CONFLICT:
                if conflict and conflict.startswith("ordering_cycle:"):
                    entry["blocking_orders"] = [
                        conflict.split(":", 1)[1].split(">")
                    ]
                elif conflict and conflict.startswith("self_precedence:"):
                    entry["blocking_orders"] = [
                        [conflict.split(":", 1)[1], conflict.split(":", 1)[1]]
                    ]
                else:
                    entry["blocking_orders"] = []
            else:
                entry["blocking_orders"] = []
            credentials.append(entry)
        # Count-only originals can never answer the headline question: both
        # L->P->E and L->E->P remain executable readings of the same counts.
        unresolved = []
        if not keys:
            for s in summaries:
                if s["completed_count"] > 0:
                    unresolved.append({
                        "summary_id": s["id"],
                        "answer_run_inside_residence": None,
                        "blocking_orders": [
                            ["load", "run", "exit"],
                            ["load", "exit", "run"],
                        ],
                    })
        result.append({
            "batch": b,
            "closure": dict(receipt) if receipt else None,
            "summaries": summaries,
            "credentials": credentials,
            "unresolved_counts": unresolved,
        })
    return result

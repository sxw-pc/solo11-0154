"""Offline acquisition desk for finite retention devices.

The desk keeps the difference-investigation driven capture queue that already
existed in the first version and adds a discrete-time acquisition planner.

Domain vocabulary (kept in English so the code reads like the rest of the
package):

* **packet**  - an evidence group (a ``capture_request`` row).  A packet only
  counts as acquired when *every* one of its fragments is held.
* **fragment** - one piece of a packet, e.g. the raw frame or the independent
  sign-off.
* **copy** - a readable occurrence of a fragment.  Every copy belongs to a
  *publication* (a release round) and is mounted on one device.  Fragments of
  one packet only match when their held copies come from the same
  publication; a lagging receiver holding an old release can never be stitched
  onto the current sign-off.
* **tick** - a discrete sampling moment.  The primary device may take exactly
  one item per tick; the end tick itself is not a readable window.
* **boundary lock** - a pre-authorised lock signed at a publication boundary.
  Frames of publications already visible at the boundary stay mounted on the
  primary device until the lock ends, but the next sampling moment publishes
  nothing.  Signing the boundary itself does not consume a read slot.
* **receiver retention** - a pre-authorised independent receiver that keeps
  raw frames in parallel without stopping the primary device.  It only
  records the signals it was configured for and never produces the matching
  sign-off; the sign-off still has to be read from the primary device while
  it is reachable.
"""

from .source import record

# Station rule caps: at most three packets, four primary device slots and one
# outstanding device request may be planned at once.
MAX_PACKETS = 3
MAX_PRIMARY_SLOTS = 4
MAX_DEVICE_REQUESTS = 1

_PRIMARY = "primary"
_RECEIVER = "receiver"
_DEFAULT_PUBLICATION = "R0"


# ---------------------------------------------------------------------------
# Persistence (the original queue plus fragment-case storage)
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
            weight REAL NOT NULL,
            publication TEXT NOT NULL,
            mount TEXT NOT NULL,
            available_ticks TEXT NOT NULL
        )"""
    )


def reconcile(db, batch):
    """Re-evaluate the queue after a receiving adjudication changed.

    Receiving adjudications and the receiving certificate itself are produced
    elsewhere; this function only flips capture requests between the
    difference-driven ``capture_due`` state and ``archive_only``.
    """
    _prepare(db)
    closure = db.execute("SELECT state FROM closure WHERE batch=?", (batch,)).fetchone()
    if closure and closure["state"] == "difference_investigation":
        state = "capture_due"
    else:
        state = "archive_only"
    db.execute("UPDATE capture_request SET state=? WHERE batch=?", (state, batch))


def import_data(db, doc):
    """Import a capture-queue document and return the difference-driven list.

    The return value is deliberately unchanged from the first version: the
    baseline receiving flow asserts an empty list on a normal reception and
    both ``EARLY`` and ``PAIR`` rows once a net-weight difference opens an
    investigation.  Fragment cases are stored alongside the requests and feed
    the planner instead of replacing the adjudication-driven queue.
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
            for copy in _copies_of(case):
                _store_fragment(db, case, copy)
        for batch in sorted(batches):
            reconcile(db, batch)
    return pick_pending(db)


def _copies_of(case):
    """Expand a fragment case into its individual device copies.

    A case either lists explicit ``copies`` (different publications/releases
    of the same fragment) or describes a single implicit copy through the
    top-level publication/mount/available_ticks fields.
    """
    if "copies" in case:
        return [
            {
                "publication": str(c.get("publication", _DEFAULT_PUBLICATION)),
                "mount": str(c.get("mount", _PRIMARY)),
                "available_ticks": sorted(int(t) for t in c["available_ticks"]),
            }
            for c in case["copies"]
        ]
    return [
        {
            "publication": str(case.get("publication", _DEFAULT_PUBLICATION)),
            "mount": str(case.get("mount", _PRIMARY)),
            "available_ticks": sorted(int(t) for t in case["available_ticks"]),
        }
    ]


def _store_fragment(db, case, copy):
    existing = db.execute(
        "SELECT 1 FROM capture_fragment WHERE id=? AND publication=? AND mount=?",
        (case["id"], copy["publication"], copy["mount"]),
    ).fetchone()
    if existing:
        return
    if case["weight"] <= 0:
        raise ValueError("fragment weight must be positive: " + case["id"])
    ticks = copy["available_ticks"]
    if not ticks:
        raise ValueError("fragment must be readable at least once: " + case["id"])
    db.execute(
        "INSERT INTO capture_fragment VALUES (?,?,?,?,?,?)",
        (
            case["id"],
            case["packet"],
            case["weight"],
            copy["publication"],
            copy["mount"],
            _encode_ticks(ticks),
        ),
    )


def _encode_ticks(ticks):
    return ",".join(str(t) for t in ticks)


def _decode_ticks(text):
    return [int(t) for t in text.split(",") if t != ""]


def pick_pending(db):
    """Difference-driven pending list, in the original expiry ordering."""
    _prepare(db)
    return [
        dict(row)
        for row in db.execute(
            """SELECT * FROM capture_request WHERE state='capture_due'
               ORDER BY expires_tick,id"""
        )
    ]


def rank_pending(db):
    """Pending packets ordered by what is actually still acquirable.

    Unlike :func:`pick_pending` this no longer sorts by expiry alone: the
    guaranteed group weight of the best executable plan is the primary key.
    """
    pending = pick_pending(db)
    if not pending:
        return []
    packet_ids = [row["id"] for row in pending]
    if not db.execute("SELECT 1 FROM capture_fragment LIMIT 1").fetchone():
        return pending
    plan = plan_capture(db, packets=packet_ids)
    weight_of = {group["packet"]: group["weight"]
                 for group in plan.get("completed", [])}
    return sorted(
        pending,
        key=lambda row: (-weight_of.get(row["id"], 0), row["expires_tick"], row["id"]),
    )


def problem_from_db(db, packets=None):
    """Build a planner problem document from the persisted desk state."""
    _prepare(db)
    where = ""
    args = ()
    if packets is not None:
        where = " WHERE id IN (%s)" % ",".join("?" for _ in packets)
        args = tuple(packets)
    requests = [
        dict(row)
        for row in db.execute("SELECT * FROM capture_request" + where, args)
    ]
    grouped = {}
    for row in db.execute("SELECT * FROM capture_fragment ORDER BY id,publication,mount"):
        if packets is not None and row["packet"] not in packets:
            continue
        grouped.setdefault(
            row["id"],
            {"id": row["id"], "packet": row["packet"], "weight": row["weight"],
             "copies": []},
        )["copies"].append(
            {
                "publication": row["publication"],
                "mount": row["mount"],
                "available_ticks": _decode_ticks(row["available_ticks"]),
            }
        )
    return _normalize(
        {
            "requests": requests,
            "fragment_case": [grouped[key] for key in sorted(grouped)],
        }
    )


# ---------------------------------------------------------------------------
# Problem normalisation and validation
# ---------------------------------------------------------------------------

def _normalize(doc):
    """Validate a problem document and fill the station-rule defaults."""
    requests = list(doc["requests"])
    cases = list(doc.get("fragment_case", ()))
    if not requests:
        raise ValueError("capture plan needs at least one packet")
    if len(requests) > MAX_PACKETS:
        raise ValueError("station rule allows at most %d packets" % MAX_PACKETS)

    packet_ids = [item["id"] for item in requests]
    if len(set(packet_ids)) != len(packet_ids):
        raise ValueError("duplicate packet ids")
    expected = {item["id"]: int(item["fragments"]) for item in requests}
    end_tick = int(doc.get("end_tick",
                           max(item["expires_tick"] for item in requests)))

    cases = list(doc.get("fragment_case", ()))
    if not cases and isinstance(doc.get("fragments"), dict):
        # Already-normalised internal shape: flatten the packet grouping.
        cases = [case for group in doc["fragments"].values() for case in group]
    fragments = {}
    for case in cases:
        packet = case["packet"]
        if packet not in expected:
            raise ValueError("fragment belongs to unknown packet: " + case["id"])
        weight = case["weight"]
        if weight <= 0:
            raise ValueError("fragment weight must be positive: " + case["id"])
        if "copies" in case and case["copies"] and isinstance(
            case["copies"][0], dict
        ) and "available_ticks" in case["copies"][0] and "id" not in case["copies"][0]:
            # Already-normalised internal shape: copies are copy descriptors.
            raw_copies = case["copies"]
        else:
            raw_copies = _copies_of(case)
        copies = []
        for copy in raw_copies:
            ticks = sorted(int(t) for t in copy["available_ticks"])
            if min(ticks) < 0 or max(ticks) >= end_tick:
                raise ValueError(
                    "fragment readable ticks must lie inside the window: " + case["id"]
                )
            if copy["mount"] not in (_PRIMARY, _RECEIVER):
                raise ValueError("unknown fragment mount: " + copy["mount"])
            copies.append({
                "publication": copy["publication"],
                "mount": copy["mount"],
                "available_ticks": ticks,
            })
        if not copies:
            raise ValueError("fragment must offer at least one copy: " + case["id"])
        fragments.setdefault(packet, []).append(
            {"id": case["id"], "packet": packet, "weight": weight, "copies": copies}
        )

    for packet, need in expected.items():
        group = fragments.get(packet, [])
        if len(group) != need:
            raise ValueError(
                "packet %s declares %d fragments but the desk knows %d"
                % (packet, need, len(group))
            )
        weights = {frag["weight"] for frag in group}
        if len(weights) != 1:
            raise ValueError("fragments of one packet must share a weight: " + packet)

    procedures = doc.get("procedures") or {}
    lock = procedures.get("lock")
    if lock is not None:
        lock = {"boundary_tick": int(lock["boundary_tick"])}
        if not 0 <= lock["boundary_tick"] < end_tick:
            raise ValueError("lock boundary must be a readable tick")
    retention = procedures.get("receiver_retention")
    if retention is not None:
        covered = set(retention["covers"])
        known = {case["id"] for case in cases}
        if not covered <= known:
            raise ValueError("receiver retention covers unknown fragment")
        retention = {"covers": sorted(covered)}

    horizon = int(doc.get("horizon", end_tick))
    if horizon > MAX_PRIMARY_SLOTS:
        raise ValueError(
            "station rule allows at most %d primary device slots" % MAX_PRIMARY_SLOTS
        )
    offline = dict(doc["offline"]) if doc.get("offline") else None
    return {
        "end_tick": end_tick,
        "horizon": horizon,
        "requests": [dict(item) for item in requests],
        "fragments": fragments,
        "lock": lock,
        "receiver_retention": retention,
        "offline": offline,
        "independent_proof": list(doc.get("independent_proof", ())),
    }


# ---------------------------------------------------------------------------
# Device-state semantics
# ---------------------------------------------------------------------------

def _visible_ticks(frag, end_tick, device_state):
    """Map publication -> ticks at which each primary/receiver copy is mounted.

    * normal device: every copy follows its declared availability list;
    * executed boundary lock: primary copies of publications already visible
      at the boundary stay mounted until the end, primary copies of later
      publications are suppressed (the next sampling moment publishes
      nothing), receiver copies are unaffected;
    * rotated device: primary copies of publications that existed at the
      boundary are gone, later primary publications keep their declared
      window, receiver copies are unaffected.
    """
    visible = {}
    for copy in frag["copies"]:
        publication = copy["publication"]
        ticks = list(copy["available_ticks"])
        if copy["mount"] == _RECEIVER or device_state is None:
            visible.setdefault(publication, []).extend(ticks)
            continue
        state = device_state["state"]
        boundary = device_state["boundary_tick"]
        appeared = min(ticks)
        if state == "locked":
            if appeared <= boundary:
                ticks = list(range(appeared, end_tick))
            else:
                # Suppressed release: it must not be treated as observed.
                continue
        elif state == "rotated":
            if appeared <= boundary:
                continue
        visible.setdefault(publication, []).extend(ticks)
    return {publication: sorted(set(ticks)) for publication, ticks in visible.items()}


def _find_fragment(problem, fragment_id):
    for fragments in problem["fragments"].values():
        for frag in fragments:
            if frag["id"] == fragment_id:
                return frag
    raise ValueError("unknown fragment: " + fragment_id)


def _primary_options(problem, tick, held, device_state, min_tick):
    """Copies the primary device may legally read at ``tick`` right now."""
    if tick < min_tick:
        return []
    options = []
    seen = set()
    for fragments in problem["fragments"].values():
        for frag in fragments:
            if frag["id"] in held:
                continue
            for publication, ticks in _visible_ticks(
                frag, problem["end_tick"], device_state
            ).items():
                if tick in ticks and (frag["id"], publication) not in seen:
                    seen.add((frag["id"], publication))
                    options.append((frag, publication))
    return options


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score(problem, held, proven):
    """Split packets into completed groups and unrecoverable leftovers.

    A packet counts only if all its fragments are held from copies of one
    shared publication.  A fragment backed by an independent proof may stand
    in for its own bytes at the publication the rest of the group is held at,
    but a plan never substitutes for real bytes and a proof cannot join
    fragments across different publications.
    """
    completed = []
    abandoned = []
    for item in problem["requests"]:
        packet = item["id"]
        fragments = problem["fragments"][packet]
        need = {frag["id"] for frag in fragments}
        candidate_pubs = set()
        missing = []
        for frag in fragments:
            publication = held.get(frag["id"])
            if publication is not None:
                candidate_pubs.add(publication)
            elif frag["id"] in proven:
                missing.append(frag["id"])
            else:
                missing.append(frag["id"])
        matched = None
        for publication in sorted(candidate_pubs):
            ok = True
            for frag in fragments:
                if held.get(frag["id"]) == publication:
                    continue
                if frag["id"] in proven:
                    continue
                ok = False
                break
            if ok:
                matched = publication
                break
        if matched is not None:
            completed.append(
                {
                    "packet": packet,
                    "publication": matched,
                    "weight": fragments[0]["weight"],
                    "fragments": sorted(need),
                }
            )
        else:
            abandoned.append(
                {"packet": packet, "missing_fragments": sorted(set(missing)),
                 "weight": 0}
            )
    completed.sort(key=lambda g: (-g["weight"], g["packet"]))
    abandoned.sort(key=lambda g: g["packet"])
    return completed, abandoned


# ---------------------------------------------------------------------------
# Deterministic planner (one known device state)
# ---------------------------------------------------------------------------

def _receiver_holdings(problem):
    """Bytes the pre-authorised independent receiver is already retaining."""
    if not problem["receiver_retention"]:
        return {}, []
    held = {}
    commands = []
    for fragments in problem["fragments"].values():
        for frag in fragments:
            if frag["id"] not in problem["receiver_retention"]["covers"]:
                continue
            receiver_copies = [c for c in frag["copies"] if c["mount"] == _RECEIVER]
            if len(receiver_copies) != 1:
                raise ValueError(
                    "receiver retention needs exactly one receiver copy: " + frag["id"]
                )
            copy = receiver_copies[0]
            held[frag["id"]] = copy["publication"]
            commands.append(
                {
                    "action": "receiver_collect",
                    "tick": min(copy["available_ticks"]),
                    "fragment": frag["id"],
                    "packet": frag["packet"],
                    "publication": copy["publication"],
                    "mount": _RECEIVER,
                }
            )
    return held, commands


def _plan_known_state(problem, device_state, min_tick=0, with_lock=None):
    """Best fixed primary-device schedule under one concrete device state."""
    receiver_held, receiver_commands = _receiver_holdings(problem)
    effective_state = device_state
    if with_lock is not None:
        effective_state = {"state": "locked",
                           "boundary_tick": with_lock["boundary_tick"]}

    memo = {}

    def search(tick, used_slots, held):
        key = (tick, used_slots, tuple(sorted(held.items())))
        if key in memo:
            return memo[key]
        if tick >= problem["horizon"]:
            completed, abandoned = _score(
                problem, held, set(problem["independent_proof"])
            )
            result = (sum(g["weight"] for g in completed), completed,
                      abandoned, [])
            memo[key] = result
            return result
        candidates = [search(tick + 1, used_slots, held)]  # idle this tick
        for frag, publication in _primary_options(
            problem, tick, held, effective_state, min_tick
        ):
            if used_slots + 1 > MAX_PRIMARY_SLOTS:
                continue
            next_held = dict(held)
            next_held[frag["id"]] = publication
            command = {
                "action": "primary_read",
                "tick": tick,
                "fragment": frag["id"],
                "packet": frag["packet"],
                "publication": publication,
                "mount": _PRIMARY,
            }
            tail = search(tick + 1, used_slots + 1, next_held)
            candidates.append(
                (tail[0], tail[1], tail[2], [command] + tail[3])
            )
        chosen = max(candidates, key=_schedule_key)
        memo[key] = chosen
        return chosen

    schedule = search(min_tick, 0, dict(receiver_held))
    weight, completed, abandoned, primary_commands = schedule
    commands = list(receiver_commands)
    if with_lock is not None:
        commands.append(
            {
                "action": "lock_request",
                "tick": with_lock["boundary_tick"],
                "boundary_tick": with_lock["boundary_tick"],
            }
        )
    commands.extend(primary_commands)
    commands.sort(key=lambda c: (c["tick"], 0 if c["action"] == "primary_read" else 1,
                                 c.get("fragment", "")))
    return {
        "strategy": "boundary_lock" if with_lock is not None else "direct",
        "commands": commands,
        "guaranteed_weight": weight,
        "completed": completed,
        "abandoned": abandoned,
    }


def _schedule_key(schedule):
    weight, completed, _abandoned, commands = schedule
    return (weight, len(completed), -len(commands),
            tuple(c.get("fragment", "") for c in commands))


# ---------------------------------------------------------------------------
# Offline planner (lock request sent, no device acknowledgement)
# ---------------------------------------------------------------------------

def _build_offline_plan(problem):
    """Plan against an unacknowledged lock request.

    Two hidden device states remain compatible with the observed silence:

    * ``locked``  - the device executed the boundary lock and still holds the
      pre-boundary publications;
    * ``rotated`` - the retention window already rotated, so anything only the
      primary device could read from the old release is gone.

    The first action has to be legal in *both* states.  A primary read is not
    (its legality differs by state), so the plan spends its first action on a
    mirror probe; only afterwards may it branch on the observed device state.
    """
    offline = problem["offline"]
    states = tuple(offline["states"])
    if set(states) != {"locked", "rotated"}:
        raise ValueError("offline planning needs locked and rotated states")
    probe_tick = int(offline.get("probe_tick", 0))
    boundary = int(offline["boundary_tick"])
    if not 0 <= probe_tick < problem["horizon"]:
        raise ValueError("probe tick must lie inside the readable window")

    branches = {}
    guarantees = []
    for state in states:
        device_state = {"state": state, "boundary_tick": boundary}
        sub = _plan_known_state(
            problem, device_state, min_tick=probe_tick + 1
        )
        branches[state] = {
            "when_device_state": state,
            "commands": sub["commands"],
            "guaranteed_weight": sub["guaranteed_weight"],
            "completed": sub["completed"],
            "abandoned": sub["abandoned"],
        }
        guarantees.append(sub["guaranteed_weight"])

    plan = {
        "strategy": "offline_probe",
        "offline": True,
        "commands": [
            {"action": "state_probe", "tick": probe_tick, "mirror": True}
        ],
        "branches": branches,
        # The guarantee is the worst case over the still-compatible states.
        "guaranteed_weight": min(guarantees),
        "completed": [],
        "abandoned": [],
        "probe_tick": probe_tick,
    }

    # Groups no branch can complete stay on the retention record with their
    # missing fragments and the occurrence tick at which the evidence vanishes.
    salvageable = {
        group["packet"]
        for sub in branches.values()
        for group in sub["completed"]
    }
    for item in problem["requests"]:
        if item["id"] in salvageable:
            continue
        fragments = problem["fragments"][item["id"]]
        plan["abandoned"].append(
            {
                "packet": item["id"],
                "missing_fragments": sorted(frag["id"] for frag in fragments),
                "occurrence_tick": min(
                    min(copy["available_ticks"])
                    for frag in fragments for copy in frag["copies"]
                ),
                "weight": 0,
            }
        )
    validate_conditional_plan(problem, plan)
    return plan


def validate_conditional_plan(problem, plan):
    """Reject plans secretly stitched from per-state optimisations.

    A valid offline policy starts with a state probe (the only action legal in
    every still-compatible state) and keeps every primary read inside the
    matching branch and after the probe.  Plans whose first action is a
    state-dependent read, that skip the probe, or that issue more than the
    single outstanding device request fail here even when each branch looks
    good on its own.
    """
    commands = plan["commands"]
    if not commands or commands[0].get("action") != "state_probe":
        raise ValueError("offline policy must probe the device state before any read")
    probe_tick = commands[0]["tick"]
    request_count = sum(1 for c in commands if c["action"] == "lock_request")
    if request_count > MAX_DEVICE_REQUESTS:
        raise ValueError("at most one outstanding device request is allowed")

    boundary = problem["offline"]["boundary_tick"]
    for state, branch in plan["branches"].items():
        device_state = {"state": state, "boundary_tick": boundary}
        ticks_used = set()
        for command in branch["commands"]:
            if command["action"] == "state_probe":
                raise ValueError("a branch may not issue a second state probe")
            if command["action"] == "lock_request":
                raise ValueError(
                    "the lock request is already outstanding; do not re-issue it"
                )
            if command["action"] == "primary_read":
                if command["tick"] <= probe_tick:
                    raise ValueError("branch reads must happen after the state probe")
                frag = _find_fragment(problem, command["fragment"])
                visible = _visible_ticks(frag, problem["end_tick"], device_state)
                if command["publication"] not in visible:
                    raise ValueError(
                        "branch read assumes a publication absent in state " + state
                    )
                if command["tick"] not in visible[command["publication"]]:
                    raise ValueError(
                        "branch read of %s is not mounted at tick %d in state %s"
                        % (frag["id"], command["tick"], state)
                    )
                if command["tick"] in ticks_used:
                    raise ValueError("two primary reads scheduled in one tick")
                ticks_used.add(command["tick"])
            elif command["action"] == "receiver_collect":
                if not problem["receiver_retention"]:
                    raise ValueError("receiver read without pre-authorisation")
                if command["fragment"] not in problem["receiver_retention"]["covers"]:
                    raise ValueError("receiver was not configured for this signal")
    return True


# ---------------------------------------------------------------------------
# Public planning entry points
# ---------------------------------------------------------------------------

def build_plan(doc):
    """Return the executable capture plan for a problem document.

    The result always names concrete commands and the groups they guarantee.
    Packets that cannot be completed inside the device window come back as
    abandoned groups with their missing fragments (and, for offline cases, the
    occurrence tick at which the evidence disappears).
    """
    problem = _normalize(doc)
    if problem["offline"] is not None:
        return _build_offline_plan(problem)

    candidates = [_plan_known_state(problem, None)]
    if problem["lock"] is not None:
        locked = _plan_known_state(
            problem, None, with_lock=problem["lock"]
        )
        candidates.append(locked)
    plan = max(
        candidates,
        key=lambda p: (p["guaranteed_weight"], len(p["completed"]),
                       -len(p["commands"])),
    )
    requests = sum(1 for c in plan["commands"] if c["action"] == "lock_request")
    if requests > MAX_DEVICE_REQUESTS:
        raise ValueError("at most one device request is allowed")
    return plan


def plan_capture(db, packets=None):
    """Build the plan straight from persisted desk state."""
    return build_plan(problem_from_db(db, packets))


# ---------------------------------------------------------------------------
# Offline execution simulation (used by the standalone runner and tests)
# ---------------------------------------------------------------------------

def simulate(doc, device_state=None, probe_observation=None):
    """Execute a plan against a concrete simulated device world.

    ``device_state`` is ``"normal"`` (default), ``"locked"`` or ``"rotated"``.
    For conditional offline plans the caller states which hidden state the
    mirror probe would observe; the matching branch is then executed.  Real
    bytes are represented by held fragment ids - nothing is ever reported as
    captured without a legal read or an explicit independent proof.
    """
    problem = doc if "horizon" in doc else _normalize(doc)
    plan = doc.get("plan") or build_plan(problem)

    if plan.get("offline"):
        if device_state not in ("locked", "rotated"):
            raise ValueError("offline plans need a concrete locked/rotated state")
        observation = probe_observation or device_state
        if observation not in plan["branches"]:
            raise ValueError("probe observed an unknown state: " + str(observation))
        commands = list(plan["commands"]) + list(
            plan["branches"][observation]["commands"]
        )
        effective_state = observation
        boundary = problem["offline"]["boundary_tick"]
        world_state = {"state": effective_state, "boundary_tick": boundary}
    else:
        effective_state = device_state or "normal"
        if effective_state == "rotated":
            raise ValueError("a fixed plan cannot be replayed on a rotated device")
        if effective_state == "locked":
            world_state = {"state": "locked",
                           "boundary_tick": problem["lock"]["boundary_tick"]}
        else:
            world_state = None
        commands = list(plan["commands"])

    held, events = _simulate_commands(problem, commands, world_state)
    proven = set(problem["independent_proof"])
    completed, abandoned = _score(problem, held, proven)
    return {
        "device_state": effective_state,
        "events": events,
        "held_fragments": [
            {"fragment": frag, "publication": publication}
            for frag, publication in sorted(held.items())
        ],
        "completed": completed,
        "abandoned": abandoned,
        "held_weight": sum(group["weight"] for group in completed),
        "proven_without_bytes": sorted(frag for frag in proven if frag not in held),
    }


def _simulate_commands(problem, commands, world_state):
    held = {}
    primary_busy = set()
    events = []
    for command in commands:
        action = command["action"]
        tick = command.get("tick")
        events.append(
            {
                "tick": tick,
                "action": action,
                "fragment": command.get("fragment"),
                "publication": command.get("publication"),
            }
        )
        if action == "state_probe":
            continue
        if action == "lock_request":
            # On a rotated device the request stays pending and cannot be
            # treated as an executed lock.
            if world_state is not None and world_state["state"] == "rotated":
                continue
            if world_state is None:
                world_state = {"state": "locked",
                               "boundary_tick": command["boundary_tick"]}
            continue
        if action not in ("primary_read", "receiver_collect"):
            raise ValueError("unknown action: " + action)
        frag = _find_fragment(problem, command["fragment"])
        publication = command["publication"]
        if action == "primary_read":
            if tick in primary_busy:
                raise ValueError("primary device can only take one item per tick")
            visible = _visible_ticks(frag, problem["end_tick"], world_state)
            if publication not in visible or tick not in visible[publication]:
                raise ValueError(
                    "illegal read of %s@%s at tick %d"
                    % (frag["id"], publication, tick)
                )
            primary_busy.add(tick)
        else:
            if (not problem["receiver_retention"]
                    or frag["id"] not in problem["receiver_retention"]["covers"]
                    or tick not in [
                        t for c in frag["copies"] if c["mount"] == _RECEIVER
                        for t in c["available_ticks"]
                    ]
                    or publication not in [
                        c["publication"] for c in frag["copies"]
                        if c["mount"] == _RECEIVER
                    ]):
                raise ValueError("illegal receiver collection of " + frag["id"])
        prior = held.get(frag["id"])
        if prior is not None and prior != publication:
            raise ValueError("conflicting publications held for " + frag["id"])
        held[frag["id"]] = publication
    return held, events

"""Finite-window evidence acquisition planning core.

Pure functions only: no sqlite, no IO.  The offline capture desk in
:mod:`wastetransit.capture_desk` persists the plans produced here and the
offline simulator in :mod:`wastetransit.capture_sim` replays them.

Domain model
------------
A capture *packet* is a group of original fragments (for example a raw frame
and its independent acknowledgement) that only counts as acquired when **every**
fragment is obtained from the *same publication release*.  Fragments from
different releases can never be paired into one group.

The main device exposes one read per discrete tick and a fragment is only
readable while its release is mounted on the device (its ``available_ticks``).
Two pre-authorised procedures exist:

* ``lock``    - a single boundary lock keeps one already published release
  mounted on the main device until the lock ends, so two later slots can both
  read that release; the next scheduled publication does not happen while the
  lock is active, so it must never be reported as observed;
* ``retention`` - an independent receiver may already retain a fragment copy
  without consuming a main-device slot and without stopping publication, but a
  retained fragment only completes a group together with same-release
  fragments; a retained copy lagging one release can never be paired with the
  current release's acknowledgement.

When the lock acknowledgement is lost (offline case) the device may be either
locked or already rotated.  The first field action has to be legal under every
still-compatible device state; a mirror inspection distinguishes the two
states, after which reading continues per observed branch.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

MAX_PACKETS = 3
MAX_SLOTS = 4
DEFAULT_RELEASE = "R1"

# Procedure identifiers.
PLAIN = "plain"
LOCK = "lock"
RETENTION = "retention"
CONTINGENT = "contingent"

# Command action identifiers.
ACT_LOCK = "lock"
ACT_READ = "read"
ACT_RETAIN = "retain"
ACT_MIRROR = "mirror_check"


@dataclass(frozen=True)
class Fragment:
    """One mounted original fragment, bound to one publication release."""

    id: str
    packet: str
    weight: int
    release: str
    ticks: Tuple[int, ...]


@dataclass(frozen=True)
class RetainedCopy:
    """A fragment copy already held by the independent receiver."""

    fragment: str
    release: str


@dataclass(frozen=True)
class Scenario:
    device_ticks: Tuple[int, ...]
    end_tick: int
    fragments: Tuple[Fragment, ...]
    retention: Tuple[RetainedCopy, ...]
    offline: bool = False

    def packets(self) -> Tuple[str, ...]:
        return tuple(sorted({f.packet for f in self.fragments}))

    def slots(self, packet: str) -> Tuple[str, ...]:
        """Fragment ids that make up a packet (release-independent slots)."""
        return tuple(sorted({f.id for f in self.fragments if f.packet == packet}))

    def releases_of(self, packet: str) -> Tuple[str, ...]:
        return tuple(sorted({f.release for f in self.fragments if f.packet == packet}))

    def fragments_of(self, packet: str) -> Tuple[Fragment, ...]:
        return tuple(f for f in self.fragments if f.packet == packet)

    def packet_weight(self, packet: str) -> int:
        return self.fragments_of(packet)[0].weight

    def with_ticks(self, ticks: Tuple[int, ...]) -> "Scenario":
        return Scenario(tuple(ticks), self.end_tick, self.fragments, self.retention, self.offline)


@dataclass(frozen=True)
class World:
    """Device state.  ``locked_release is None`` means normal rotation."""

    locked_release: Optional[str] = None
    lock_tick: Optional[int] = None


def scenario_from_doc(
    doc: dict,
    *,
    offline: Optional[bool] = None,
    retention: Optional[List[dict]] = None,
    device_ticks: Optional[List[int]] = None,
    end_tick: Optional[int] = None,
) -> Scenario:
    fragments = tuple(
        sorted(
            (
                Fragment(
                    id=item["id"],
                    packet=item["packet"],
                    weight=int(item["weight"]),
                    release=item.get("release", DEFAULT_RELEASE),
                    ticks=tuple(sorted(item["available_ticks"])),
                )
                for item in doc.get("fragment_case", [])
            ),
            key=lambda f: (f.id, f.release),
        )
    )
    if not fragments:
        raise ValueError("scenario has no fragment_case entries")
    ticks = (
        tuple(sorted(device_ticks))
        if device_ticks is not None
        else tuple(sorted({t for f in fragments for t in f.ticks}))
    )
    if not ticks:
        raise ValueError("scenario has no readable device ticks")
    if len(ticks) > MAX_SLOTS:
        raise ValueError(f"at most {MAX_SLOTS} device slots are supported")
    retention_doc = retention if retention is not None else doc.get("receiver_retention", [])
    retained = tuple(
        RetainedCopy(item["fragment"], item.get("release", DEFAULT_RELEASE))
        for item in retention_doc
    )
    known = {(f.id, f.release) for f in fragments}
    for copy in retained:
        if (copy.fragment, copy.release) not in known:
            raise ValueError(
                f"retained copy {copy.fragment}/{copy.release} matches no mounted fragment"
            )
    packets = {f.packet for f in fragments}
    if len(packets) > MAX_PACKETS:
        raise ValueError(f"at most {MAX_PACKETS} packets are supported")
    return Scenario(
        device_ticks=ticks,
        end_tick=end_tick if end_tick is not None else max(ticks) + 1,
        fragments=fragments,
        retention=retained,
        offline=bool(doc.get("offline", False) if offline is None else offline),
    )


def world_readable(scenario: Scenario, world: World, fragment: Fragment, tick: int) -> bool:
    """Whether the main device can really read ``fragment`` at ``tick``."""
    if world.locked_release is None:
        return tick in fragment.ticks
    if fragment.release == world.locked_release:
        # Frozen release stays mounted from the lock boundary onwards; its
        # regular windows remain valid as well.
        return tick in fragment.ticks or tick >= world.lock_tick
    # A different release: future publications are suppressed by the lock, so
    # it only stays mounted through windows that pre-date the boundary.
    return tick in fragment.ticks and tick < world.lock_tick


def _read_command(tick: int, fragment: Fragment) -> dict:
    return {
        "action": ACT_READ,
        "device": "main",
        "tick": tick,
        "fragment": fragment.id,
        "packet": fragment.packet,
        "release": fragment.release,
    }


def _evaluate(scenario: Scenario, obtained: Dict[Tuple[str, str], str]) -> Tuple[int, List[dict]]:
    """Weight and completed same-release groups given obtained (slot, release)."""
    completed: List[dict] = []
    weight = 0
    for packet in scenario.packets():
        chosen_release = None
        for release in scenario.releases_of(packet):
            if all((slot, release) in obtained for slot in scenario.slots(packet)):
                chosen_release = release
                break
        if chosen_release is not None:
            completed.append(
                {
                    "packet": packet,
                    "release": chosen_release,
                    "weight": scenario.packet_weight(packet),
                    "fragments": list(scenario.slots(packet)),
                }
            )
            weight += scenario.packet_weight(packet)
    return weight, completed


def best_schedule(
    scenario: Scenario,
    world: World,
    retained: Tuple[RetainedCopy, ...] = (),
) -> dict:
    """Exhaustive DFS over main-device reads for one fixed device world."""
    ticks = scenario.device_ticks
    initial = {(copy.fragment, copy.release): "receiver" for copy in retained}
    best = None

    def consider(obtained, reads):
        nonlocal best
        weight, completed = _evaluate(scenario, obtained)
        candidate = {
            "weight": weight,
            "completed": completed,
            "reads": list(reads),
            "obtained": dict(obtained),
        }
        key = (
            weight,
            -len(reads),
            -len(completed),
            tuple((c["tick"], c["fragment"], c["release"]) for c in reads),
        )
        if best is None or key > best[0]:
            best = (key, candidate)

    def rec(index, obtained, reads):
        if index == len(ticks):
            consider(obtained, reads)
            return
        tick = ticks[index]
        rec(index + 1, obtained, reads)  # leave the slot idle
        held_ids = {slot for slot, _ in obtained}
        for fragment in scenario.fragments:
            if fragment.id in held_ids:
                continue
            if world_readable(scenario, world, fragment, tick):
                rec(
                    index + 1,
                    {**obtained, (fragment.id, fragment.release): "main"},
                    reads + [_read_command(tick, fragment)],
                )

    rec(0, dict(initial), [])
    return best[1]


def _gap_report(scenario: Scenario, completed_packets: List[dict], world: World) -> List[dict]:
    done = {item["packet"]: item["release"] for item in completed_packets}
    gaps = []
    for packet in scenario.packets():
        if packet in done:
            continue
        for slot in scenario.slots(packet):
            readable_ticks = [
                tick
                for fragment in scenario.fragments
                if fragment.id == slot
                for tick in scenario.device_ticks
                if world_readable(scenario, world, fragment, tick)
            ]
            last_tick = max(readable_ticks) if readable_ticks else None
            if readable_ticks:
                reason = f"readable window closed after tick {last_tick}"
            else:
                reason = "no mounted copy inside the device horizon"
            gaps.append(
                {
                    "packet": packet,
                    "fragment": slot,
                    "last_tick": last_tick,
                    "reason": reason,
                }
            )
    return gaps


def _linear_plan(
    scenario: Scenario,
    *,
    procedure: str,
    world: World,
    retained: Tuple[RetainedCopy, ...],
    lock_command: Optional[dict] = None,
) -> dict:
    schedule = best_schedule(scenario, world, retained)
    commands: List[dict] = []
    if lock_command is not None:
        commands.append(lock_command)
    for copy in retained:
        commands.append(
            {
                "action": ACT_RETAIN,
                "device": "receiver",
                "tick": None,
                "fragment": copy.fragment,
                "release": copy.release,
            }
        )
    commands.extend(schedule["reads"])
    return {
        "procedure": procedure,
        "world": {"locked_release": world.locked_release, "lock_tick": world.lock_tick},
        "commands": commands,
        "main_reads": len(schedule["reads"]),
        "lock_requests": 1 if lock_command is not None else 0,
        "guaranteed_weight": schedule["weight"],
        "completed_packets": schedule["completed"],
        "gaps": _gap_report(scenario, schedule["completed"], world),
        "branches": None,
    }


def _lock_candidates(scenario: Scenario) -> List[Tuple[int, str]]:
    # A boundary lock can only freeze a release already published by then:
    # locking must not claim a future publication as observed.
    releases = tuple(sorted({f.release for f in scenario.fragments}))
    return [
        (boundary_tick, release)
        for boundary_tick in scenario.device_ticks[:-1]
        for release in releases
        if any(
            f.release == release and min(f.ticks) <= boundary_tick
            for f in scenario.fragments
        )
    ]


def plan_best(scenario: Scenario) -> dict:
    """Best guaranteed-weight plan over the plain, lock and retention routes."""
    candidates = [_linear_plan(scenario, procedure=PLAIN, world=World(), retained=())]
    if scenario.retention:
        candidates.append(
            _linear_plan(
                scenario,
                procedure=RETENTION,
                world=World(),
                retained=scenario.retention,
            )
        )
    for boundary_tick, release in _lock_candidates(scenario):
        lock_command = {
            "action": ACT_LOCK,
            "device": "main",
            "boundary_tick": boundary_tick,
            "release": release,
        }
        candidates.append(
            _linear_plan(
                scenario,
                procedure=LOCK,
                world=World(locked_release=release, lock_tick=boundary_tick),
                retained=(),
                lock_command=lock_command,
            )
        )

    def rank(plan):
        # Higher weight first; ties prefer fewer device reads, no lock over a
        # lock, fewer completed groups (same weight), stable identifiers last.
        return (
            plan["guaranteed_weight"],
            -plan["main_reads"],
            -plan["lock_requests"],
            -len(plan["completed_packets"]),
            tuple(sorted((g["packet"], g["release"]) for g in plan["completed_packets"])),
            tuple(
                (c.get("tick"), c.get("fragment"), c.get("release"), c.get("boundary_tick"))
                for c in plan["commands"]
            ),
        )

    ordered = sorted(candidates, key=rank, reverse=True)
    best = ordered[0]
    assert rank(best) == max(rank(p) for p in candidates)
    return best


def greedy_priority_weight(scenario: Scenario) -> int:
    """Weight obtained by the old per-fragment priority queue behaviour.

    Fragments queue by earliest readable tick (expiry order); each slot takes
    the highest-priority fragment still readable.  On the base fixture this
    reads EARLY first and can never complete PAIR, so it guarantees only 1.
    """
    world = World()
    obtained: Dict[Tuple[str, str], str] = {}
    for tick in scenario.device_ticks:
        held_ids = {slot for slot, _ in obtained}
        choices = sorted(
            (
                fragment
                for fragment in scenario.fragments
                if fragment.id not in held_ids and world_readable(scenario, world, fragment, tick)
            ),
            key=lambda f: (min(f.ticks), f.packet, f.id, f.release),
        )
        if choices:
            chosen = choices[0]
            obtained[(chosen.id, chosen.release)] = "main"
    weight, _ = _evaluate(scenario, obtained)
    return weight


def plan_offline(scenario: Scenario, lock_release: str) -> dict:
    """Contingent plan for a lost lock acknowledgement.

    The first field action is a mirror inspection that is legal whether the
    device executed the lock or already rotated.  Each observed state then gets
    its own branch planned over the remaining device slots.
    """
    if not scenario.offline:
        raise ValueError("plan_offline requires an offline scenario")
    ticks = scenario.device_ticks
    if len(ticks) < 2:
        raise ValueError("offline contingency needs a slot for the mirror inspection")
    first_tick = ticks[0]
    if not any(
        f.release == lock_release and min(f.ticks) <= first_tick
        for f in scenario.fragments
    ):
        raise ValueError(
            f"release {lock_release} is not published by tick {first_tick}; "
            "a lock cannot freeze an unobserved publication"
        )
    remaining = ticks[1:]
    sub = scenario.with_ticks(remaining)
    mirror = {
        "action": ACT_MIRROR,
        "device": "main",
        "tick": first_tick,
        "purpose": "distinguish locked device from rotated device before reading",
    }
    branch_worlds = {
        "locked": World(locked_release=lock_release, lock_tick=first_tick),
        "rotated": World(),
    }
    branches = {}
    for state, world in branch_worlds.items():
        schedule = best_schedule(sub, world, sub.retention)
        branches[state] = {
            "world": {"locked_release": world.locked_release, "lock_tick": world.lock_tick},
            "commands": list(schedule["reads"]),
            "guaranteed_weight": schedule["weight"],
            "completed_packets": schedule["completed"],
            "gaps": _gap_report(sub, schedule["completed"], world),
        }
    guaranteed = min(branch["guaranteed_weight"] for branch in branches.values())
    return {
        "procedure": CONTINGENT,
        "world": None,
        "commands": [mirror],
        "main_reads": sum(len(b["commands"]) for b in branches.values()),
        "lock_requests": 1,
        "guaranteed_weight": guaranteed,
        "completed_packets": [],
        "gaps": [gap for branch in branches.values() for gap in branch["gaps"]],
        "pending_request": {
            "action": ACT_LOCK,
            "release": lock_release,
            "boundary_tick": first_tick,
            "status": "ack_pending",
        },
        "branches": branches,
    }


def validate_contingent(plan: dict, scenario: Scenario) -> List[str]:
    """Return validation errors (empty list means the contingent plan is legal)."""
    errors: List[str] = []
    if plan.get("procedure") != CONTINGENT:
        errors.append("offline plan must use the contingent procedure")
        return errors
    commands = plan.get("commands", [])
    if not commands or commands[0].get("action") != ACT_MIRROR:
        errors.append(
            "first action must be a mirror inspection legal under every compatible device state"
        )
    elif commands[0].get("tick") != scenario.device_ticks[0]:
        errors.append("mirror inspection must occupy the first available device slot")
    if len(commands) > 1:
        errors.append("no state-dependent read may be issued before the mirror observation")
    if plan.get("lock_requests", 0) > 1:
        errors.append("at most one pending device lock request is allowed")
    branches = plan.get("branches") or {}
    if set(branches) != {"locked", "rotated"}:
        errors.append("contingent plan must branch over both locked and rotated states")
        return errors
    request = plan.get("pending_request") or {}
    for state, branch in branches.items():
        if state == "locked":
            world = World(request.get("release"), request.get("boundary_tick"))
        else:
            world = World()
        for command in branch.get("commands", []):
            if command.get("action") != ACT_READ:
                errors.append(f"branch {state} may only schedule reads after the inspection")
                continue
            fragment = next(
                (
                    candidate
                    for candidate in scenario.fragments
                    if candidate.id == command["fragment"]
                    and candidate.release == command["release"]
                ),
                None,
            )
            if fragment is None:
                errors.append(f"branch {state} reads unknown fragment {command.get('fragment')}")
            elif not world_readable(scenario, world, fragment, command["tick"]):
                errors.append(
                    f"branch {state} read of {fragment.id} at tick {command['tick']} "
                    "is not mounted in that device state"
                )
    return errors

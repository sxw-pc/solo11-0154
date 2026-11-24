"""Offline execution simulator for capture plans.

Replays the deterministic commands produced by :mod:`wastetransit.capture_plan`
against an in-memory model of the main device (and the independent receiver),
recording simulated evidence receipts only when a fragment is genuinely
readable at the moment of the read.  A plan never replaces real bytes:
unobtainable fragments stay recorded as missing pieces.

Runnable as an offline entry point::

    python -m wastetransit.capture_sim run --input fixtures/capture_queue.json
    python -m wastetransit.capture_sim run --input scenario.json --offline \\
        --observe locked
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

from . import capture_plan as cp


def _receipt_token(command: dict) -> str:
    raw = json.dumps(command, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _simulate_linear(scenario: cp.Scenario, plan: dict) -> dict:
    world = cp.World(
        locked_release=plan["world"].get("locked_release"),
        lock_tick=plan["world"].get("lock_tick"),
    )
    retained: Dict[tuple, str] = {}
    receipts: List[dict] = []
    errors: List[str] = []
    per_slot: Dict[int, int] = {}

    for command in plan["commands"]:
        action = command["action"]
        if action == cp.ACT_LOCK:
            # Recorded as an authorised request; the simulator models the
            # resulting frozen world rather than yielding evidence.
            continue
        if action == cp.ACT_RETAIN:
            retained[(command["fragment"], command["release"])] = "receiver"
            continue
        if action != cp.ACT_READ:
            errors.append(f"unexpected command {action} in linear plan")
            continue
        tick = command["tick"]
        per_slot.setdefault(tick, 0)
        if per_slot[tick] >= 1:
            errors.append(f"tick {tick} would exceed the one-read device limit")
            continue
        fragment = next(
            (
                item
                for item in scenario.fragments
                if item.id == command["fragment"] and item.release == command["release"]
            ),
            None,
        )
        if fragment is None:
            errors.append(
                f"{command['fragment']}/{command['release']} is not a mounted fragment"
            )
            continue
        if not cp.world_readable(scenario, world, fragment, tick):
            errors.append(
                f"{fragment.id} release {fragment.release} is not mounted at tick {tick}"
            )
            continue
        per_slot[tick] += 1
        retained[(fragment.id, fragment.release)] = "main"
        receipts.append(
            {
                "fragment": fragment.id,
                "packet": fragment.packet,
                "release": fragment.release,
                "tick": tick,
                "source": "main",
                "token": _receipt_token(command),
            }
        )

    weight, completed = cp._evaluate(scenario, retained)
    return {
        "observed_state": "rotated" if world.locked_release is None else "locked",
        "receipts": receipts,
        "obtained": [
            {"fragment": slot, "release": release, "source": source}
            for (slot, release), source in sorted(retained.items())
        ],
        "completed_packets": completed,
        "acquired_weight": weight,
        "missing": cp._gap_report(scenario, completed, world),
        "errors": errors,
    }


def simulate(scenario: cp.Scenario, plan: dict, observe: Optional[str] = None) -> dict:
    """Replay a plan.  For contingent plans ``observe`` picks the branch."""
    if plan["procedure"] == cp.CONTINGENT:
        errors = cp.validate_contingent(plan, scenario)
        if observe not in (None, "locked", "rotated"):
            raise ValueError("observe must be 'locked' or 'rotated'")
        state = observe or "rotated"
        mirror = plan["commands"][0]
        branch = plan["branches"][state]
        world = cp.World(
            locked_release=branch["world"].get("locked_release"),
            lock_tick=branch["world"].get("lock_tick"),
        )
        receipts = [
            {
                "fragment": None,
                "packet": None,
                "release": None,
                "tick": mirror["tick"],
                "source": "mirror",
                "token": _receipt_token(mirror),
                "observed_state": state,
            }
        ]
        obtained: Dict[tuple, str] = {}
        per_slot: Dict[int, int] = {mirror["tick"]: 1}
        for command in branch["commands"]:
            tick = command["tick"]
            per_slot.setdefault(tick, 0)
            if per_slot[tick] >= 1:
                errors.append(f"tick {tick} would exceed the one-read device limit")
                continue
            fragment = next(
                (
                    item
                    for item in scenario.fragments
                    if item.id == command["fragment"] and item.release == command["release"]
                ),
                None,
            )
            if fragment is None or not cp.world_readable(scenario, world, fragment, tick):
                errors.append(
                    f"branch {state}: {command['fragment']}/{command['release']} "
                    f"not mounted at tick {tick}"
                )
                continue
            per_slot[tick] += 1
            obtained[(fragment.id, fragment.release)] = "main"
            receipts.append(
                {
                    "fragment": fragment.id,
                    "packet": fragment.packet,
                    "release": fragment.release,
                    "tick": tick,
                    "source": "main",
                    "token": _receipt_token(command),
                }
            )
        weight, completed = cp._evaluate(scenario, obtained)
        return {
            "observed_state": state,
            "receipts": receipts,
            "obtained": [
                {"fragment": slot, "release": release, "source": source}
                for (slot, release), source in sorted(obtained.items())
            ],
            "completed_packets": completed,
            "acquired_weight": weight,
            "missing": cp._gap_report(scenario, completed, world),
            "errors": errors,
        }
    return _simulate_linear(scenario, plan)


def build_scenario(doc: dict, args: argparse.Namespace) -> cp.Scenario:
    retention = None
    if args.retention:
        retention = json.loads(Path(args.retention).read_text(encoding="utf-8"))
    return cp.scenario_from_doc(
        doc,
        offline=args.offline,
        retention=retention,
        device_ticks=args.ticks,
        end_tick=args.end,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="offline capture plan simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="build a plan and replay it")
    run.add_argument("--input", required=True, help="capture queue scenario JSON")
    run.add_argument("--offline", action="store_true", help="lock acknowledgement lost")
    run.add_argument("--observe", choices=["locked", "rotated"], default="rotated")
    run.add_argument("--lock-release", default=cp.DEFAULT_RELEASE)
    run.add_argument("--retention", help="JSON file listing receiver retained copies")
    run.add_argument("--ticks", type=int, nargs="*", help="override readable device ticks")
    run.add_argument("--end", type=int, help="override horizon end tick")
    run.add_argument("--plan-only", action="store_true", help="print the plan without replay")
    args = parser.parse_args(argv)

    doc = json.loads(Path(args.input).read_text(encoding="utf-8"))
    scenario = build_scenario(doc, args)
    if args.offline:
        plan = cp.plan_offline(scenario, args.lock_release)
    else:
        plan = cp.plan_best(scenario)
    payload = {"plan": plan}
    if not args.plan_only:
        payload["simulation"] = simulate(scenario, plan, args.observe if args.offline else None)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

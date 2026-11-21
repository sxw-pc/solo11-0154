"""Offline execution simulation entry for the capture desk.

Run without touching any real device:

    python -B -m wastetransit.capture_run plan --input scenario.json
    python -B -m wastetransit.capture_run simulate --input scenario.json \
        --device-state locked
    python -B -m wastetransit.capture_run simulate --input scenario.json \
        --device-state rotated --observe rotated

For a database-backed desk (the same SQLite state the receiving flow writes):

    python -B -m wastetransit.capture_run plan --db state.sqlite
    python -B -m wastetransit.capture_run simulate --db state.sqlite \
        --device-state normal

The runner only prints JSON.  It never writes capture results back into the
database: the plan is saved before a real device commits anything, and bytes
that were not legally read (or independently proven) never appear as held.
"""

import argparse
import json
from pathlib import Path

from . import capture_desk
from .store import connect


def _load_scenario(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _problem_from_db(path, packets):
    db = connect(path)
    try:
        return capture_desk.problem_from_db(db, packets)
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline capture-plan builder and execution simulator"
    )
    parser.add_argument("command", choices=("plan", "simulate"))
    parser.add_argument("--input", help="scenario JSON document")
    parser.add_argument("--db", help="sqlite desk state written by the receiving flow")
    parser.add_argument(
        "--packets",
        help="comma-separated packet ids to plan (defaults to every imported packet)",
    )
    parser.add_argument(
        "--device-state",
        choices=("normal", "locked", "rotated"),
        help="concrete device world for 'simulate'",
    )
    parser.add_argument(
        "--observe",
        choices=("locked", "rotated"),
        help="mirror-probe observation when simulating an offline policy",
    )
    parser.add_argument(
        "--save-plan",
        help="write the produced plan to this path before any simulated commit",
    )
    args = parser.parse_args(argv)

    if not args.input and not args.db:
        parser.error("either --input or --db is required")
    packets = args.packets.split(",") if args.packets else None

    if args.input:
        scenario = _load_scenario(Path(args.input))
    else:
        scenario = _problem_from_db(args.db, packets)

    if args.command == "plan":
        plan = capture_desk.build_plan(scenario)
        result = {"scenario": _scenario_summary(scenario), "plan": plan}
    else:
        device_state = args.device_state or (
            "normal" if not scenario.get("offline") else None
        )
        if scenario.get("offline") and device_state not in ("locked", "rotated"):
            parser.error("offline scenarios require --device-state locked|rotated")
        result = capture_desk.simulate(
            scenario,
            device_state=device_state,
            probe_observation=args.observe,
        )
        result = {"scenario": _scenario_summary(scenario), "simulation": result}

    if args.save_plan:
        Path(args.save_plan).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _scenario_summary(scenario):
    if "horizon" not in scenario:
        return {
            "packets": [item["id"] for item in scenario["requests"]],
            "end_tick": max(item["expires_tick"] for item in scenario["requests"]),
        }
    return {
        "packets": [item["id"] for item in scenario["requests"]],
        "end_tick": scenario["end_tick"],
        "horizon": scenario["horizon"],
        "offline": scenario.get("offline"),
    }


if __name__ == "__main__":
    raise SystemExit(main())

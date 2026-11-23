#!/usr/bin/env python3
"""Query the planner from the terminal."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transport.planner import Planner  # noqa: E402


def table(result: dict) -> str:
    lines = [f"Window {result['query']['window_seconds'][0]}–{result['query']['window_seconds'][1]} "
             f"(inclusive); {result['distinct_departures_in_window']} distinct departures", ""]
    header = f"{'route':<14} {'dep':>5} {'arr':>5} {'wait':>6} {'ride':>6} {'load':>6} {'peak':>6} {'crowd min':>10} {'conf':<13} {'evidence':<24}"
    lines.append(header)
    lines.append("-" * len(header))
    for o in result["options"]:
        lines.append(f"{o['route'][:14]:<14} {o['departure_at_board']:>5} {o['arrival_at_destination']:>5} "
                     f"{o['wait_minutes']:>6} {o['ride_minutes']:>6} {o['expected_recorded_load']:>6} "
                     f"{o['busiest_load']:>6} {o['minutes_crowded']:>10} {o['confidence']:<13} {o['evidence']:<24}")
    for o in result["options"]:
        if not o["stops"]:
            continue
        lines.append("")
        lines.append(f"  {o['route']} {o['trip_id']} stop-by-stop")
        for s in o["stops"]:
            lines.append(f"    {s['from']} -> {s['to']}: {s['minutes']} min, "
                         f"recorded load {s['load']} (p75 {s['p75']}, n={s['support']}), {s['recorded_class']}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", required=True)
    ap.add_argument("--to", dest="alight", required=True)
    ap.add_argument("--date", default="2026-10-23")
    ap.add_argument("--time", dest="time_str", default="08:40")
    ap.add_argument("--window", type=int, default=2)
    ap.add_argument("--sort", choices=("arrival", "crowding"), default="arrival")
    ap.add_argument("--db", type=Path, default=ROOT / "output" / "transport.db")
    ap.add_argument("--capacity-map", type=Path, help="optional vehicle-number -> vehicle-model JSON")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-earlier", action="store_true")
    args = ap.parse_args()

    planner = Planner(args.db, args.capacity_map)
    result = planner.plan(args.board, args.alight, args.date, args.time_str,
                          args.window, args.sort, not args.no_earlier)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(table(result))
        print()
        print(f"Pool: {result['pooled']['total_recorded_passengers']} recorded passengers / "
              f"{result['pooled']['eligible_departures']} {result['pooled']['denominator_source']} departures "
              f"= {result['pooled']['average_recorded_per_departure']:.1f} per departure")
        print(f"Individual loads: {result['pooled']['individual_recorded_passengers']}")
        if result["later_comparison"]:
            c = result["later_comparison"]
            print(f"Later option {c['later_route']} at {c['later_departure']}: "
                  f"load change {c['load_change']}, crowded minutes change {c['crowded_minutes_change']}, "
                  f"arrival change {c['arrival_change_minutes']} min")
        for caveat in result["caveats"]:
            print(f"NOTE: {caveat}")


if __name__ == "__main__":
    main()

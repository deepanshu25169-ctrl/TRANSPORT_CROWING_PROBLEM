#!/usr/bin/env python3
"""Export a compact all-pairs browser index for GitHub Pages."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transport.model import HistoricalModel  # noqa: E402
from transport.planner import Planner  # noqa: E402


def main() -> None:
    planner = Planner(ROOT / "output" / "transport.db")
    net, model = planner.net, planner.model
    model: HistoricalModel = model

    routes = list(net.routes.keys())
    route_idx = {name: i for i, name in enumerate(routes)}
    pattern_ids = list(net.patterns.keys())
    pattern_idx = {pid: i for i, pid in enumerate(pattern_ids)}
    service_ids = list(net.calendar.keys())
    service_idx = {sid: i for i, sid in enumerate(service_ids)}

    stops = [[net.stop_name(i)] for i in range(len(net.stops))]
    patterns = [list(net.patterns[pid].stops) for pid in pattern_ids]
    trips = []
    for tid, trip in net.trips.items():
        deps = list(trip.dep_secs)
        arrs = list(trip.arr_secs)
        trips.append([
            route_idx[trip.route_name], pattern_idx[trip.pattern_id],
            service_idx.get(trip.service_id, 0), deps[0],
            [deps[i] - deps[i - 1] for i in range(1, len(deps))],
            [arrs[i] - deps[i] for i in range(len(arrs))],
            tid,
        ])

    profiles = []
    for key, p in (model.agg.profiles if model.agg else {}).items():
        pid, day_type, tbin = key
        profiles.append([pattern_idx.get(pid, -1), 1 if day_type == "weekend" else 0, tbin,
                         round(p.median_load, 3), round(p.p75_load, 3), round(p.n_runs)])
    pattern_day = []
    for key, vals in (model.agg.by_pattern_day if model.agg else {}).items():
        pid, day_type = key
        if not vals:
            continue
        from transport.common import median, percentile
        pattern_day.append([pattern_idx.get(pid, -1), 1 if day_type == "weekend" else 0,
                            round(median(vals), 3), round(percentile(vals, 75), 3), len(vals)])

    route_medians = [round(float(model.agg.route_medians.get(r, 0)), 3) if model.agg else 0
                     for r in routes]
    aliases = json.loads((ROOT / "transport" / "aliases.json").read_text(encoding="utf-8"))
    payload = {
        "meta": {
            "bin_minutes": model.bin_minutes,
            "shrinkage_k": model.shrinkage_k,
            "reference": planner.reference,
            "note": "Model-only all-pairs index; no live AVL or fleet-seat mapping.",
        },
        "stops": stops,
        "aliases": aliases,
        "routes": routes,
        "patterns": patterns,
        "trips": trips,
        "calendar": [[net.calendar[s]["flags"], net.calendar[s]["start"], net.calendar[s]["end"]]
                     for s in service_ids],
        "profiles": profiles,
        "pattern_day": pattern_day,
        "route_medians": route_medians,
    }
    out = ROOT / "docs" / "network.json"
    out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {out}: {len(stops)} stops, {len(routes)} routes, {len(patterns)} patterns, "
          f"{len(trips)} trips, {len(profiles)} profiles")


if __name__ == "__main__":
    main()

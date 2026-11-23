#!/usr/bin/env python3
"""Train on Oct 1-21, validate on Oct 22-31 against a route-only baseline."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transport.common import is_weekend, mean, time_bin  # noqa: E402
from transport.gtfs import load_network  # noqa: E402
from transport.model import HistoricalModel  # noqa: E402
from transport.store import put_meta  # noqa: E402

TRAIN_END = "2026-10-21"
TEST_START = "2026-10-22"


def bucket(sec: int) -> str:
    h = (sec // 3600) % 24
    if 7 <= h < 10:
        return "morning_peak"
    if 17 <= h < 20:
        return "evening_peak"
    if 10 <= h < 17:
        return "quiet_day"
    return "night"


def error_stats(errors: list[float]) -> dict:
    if not errors:
        return {"n": 0, "mae": None, "rmse": None}
    return {"n": len(errors), "mae": round(mean(errors), 3),
            "rmse": round(math.sqrt(mean([e * e for e in errors])), 3)}


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "transport.db"
    conn = sqlite3.connect(db)
    net = load_network(conn)
    model = HistoricalModel(conn, net)
    report = model.select_window()
    chosen = int(report.get("selected") or min(report["candidates"], key=lambda w: report["candidates"][w]["mae"]))
    model.fit(TRAIN_END, chosen)
    put_meta(conn, "window_report", {**report, "selected": chosen})
    audit = {r[0]: r[1] for r in conn.execute("SELECT key,CAST(value AS INTEGER) FROM audit")}

    errors, base_errors = [], []
    by_bucket = defaultdict(lambda: {"model": [], "baseline": []})
    worst = []
    run_meta = {r[0]: r for r in conn.execute(
        "SELECT run_key,date,route_name,pattern_id,trip_id FROM run_info WHERE date>=?", (TEST_START,))}
    for key, loads_json in conn.execute("SELECT run_key,loads FROM run_loads"):
        meta = run_meta.get(key)
        if not meta:
            continue
        _, date, route, pattern, trip_id = meta
        trip = net.trips.get(trip_id)
        if not trip:
            continue
        loads = json.loads(loads_json)
        day_type = "weekend" if is_weekend(date) else "weekday"
        for pos, actual in enumerate(loads):
            if actual <= 0 or pos >= len(trip.dep_secs) - 1:
                continue
            t = trip.dep_secs[pos]
            tbin = time_bin(t, model.bin_minutes)
            pred, profile = model.predict(pattern, day_type, tbin, route)
            if math.isnan(pred):
                continue
            base = model.agg.route_medians.get(route, pred)
            e = abs(pred - actual)
            eb = abs(base - actual)
            errors.append(e)
            base_errors.append(eb)
            b = bucket(t)
            by_bucket[b]["model"].append(e)
            by_bucket[b]["baseline"].append(eb)
            if is_weekend(date):
                by_bucket["weekend"]["model"].append(e)
                by_bucket["weekend"]["baseline"].append(eb)
            worst.append((e, date, route, trip_id, pos, actual, pred, base, profile.fallback))

    total_pax = audit.get("passengers_total", 1)
    result = {
        "train": {"start": "2026-10-01", "end": TRAIN_END},
        "test": {"start": TEST_START, "end": "2026-10-31"},
        "historical_window_days_selected": chosen,
        "window_selection": report,
        "segment_error": {
            "model": error_stats(errors),
            "baseline_route_only": error_stats(base_errors),
            "mae_delta_vs_baseline": round(mean(errors) - mean(base_errors), 4) if errors else None,
            "model_beats_baseline_mae": bool(errors and mean(errors) <= mean(base_errors)),
            "model_beats_baseline_rmse": bool(errors and mean([e * e for e in errors]) <= mean([e * e for e in base_errors])),
        },
        "error_by_period": {k: {"model": error_stats(v["model"]),
                                "baseline": error_stats(v["baseline"])} for k, v in by_bucket.items()},
        "worst_cases": [
            {"abs_error": round(e, 2), "date": d, "route": r, "trip_id": t, "segment": p,
             "actual_recorded_load": a, "predicted": round(pr, 2), "baseline": round(b, 2),
             "method": fb} for e, d, r, t, p, a, pr, b, fb in sorted(worst, reverse=True)[:5]
        ],
        "coverage": {
            "tickets_total": audit.get("tickets_total"),
            "passengers_total": total_pax,
            "pct_tickets_confident_stop_pair": round(100 * audit.get("stop_pair_confident", 0) / audit.get("tickets_total", 1), 2),
            "pct_passengers_matched_to_trip": round(100 * audit.get("pax_matched_trip", 0) / total_pax, 2),
            "pct_passengers_in_vehicle_runs": round(100 * audit.get("pax_vehicle_run_matched", 0) / total_pax, 2),
            "pct_passengers_no_vehicle": round(100 * audit.get("pax_no_vehicle", 0) / total_pax, 2),
            "ambiguous_start_records": audit.get("start_ambiguous"),
            "ambiguous_end_records": audit.get("end_ambiguous"),
            "unmatched_start_records": audit.get("start_unmatched"),
            "unmatched_end_records": audit.get("end_unmatched"),
            "observed_runs": audit.get("runs_observed"),
        },
        "supported_conclusions": [
            "Relative recorded passenger demand by route pattern, direction, time and segment.",
            "Comparison of scheduled alternatives using recorded loads on identical stretches.",
            "Identification of data gaps: unknown vehicle coverage, no fleet-seat mapping.",
        ],
        "requires_independent_observation": [
            "Total onboard passengers (ticket coverage q is unknown).",
            "Physical seats/standing availability and true comfort.",
            "Whether an unobserved scheduled bus actually ran.",
        ],
    }
    out = ROOT / "output" / "validation.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("historical_window_days_selected", "segment_error",
                                            "error_by_period", "coverage")}, indent=2))
    print(f"Written {out}")


if __name__ == "__main__":
    main()

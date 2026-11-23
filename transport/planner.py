"""Commuter-facing journey planning logic."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .capacity import capacity_for
from .common import clock, hms_to_sec, is_weekend
from .gtfs import load_network
from .loads import distinct_departures, inclusive_window
from .model import (HistoricalModel, analyse_journey, build_reference, pooled_average,
                    run_loads_for_trip, run_series_for_trip)
from .store import get_meta


class Planner:
    def __init__(self, db: Path, capacity_map: Path | None = None):
        self.conn = sqlite3.connect(db, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.net = load_network(self.conn)
        self.model = HistoricalModel(self.conn, self.net)
        report = get_meta(self.conn, "window_report")
        if report and report.get("selected"):
            self.model.window_days = int(report["selected"])
            self.model.window_report = report
        else:
            self.model.select_window()
        self.model.fit("2026-10-21", self.model.window_days)
        self.reference = build_reference(self.model)
        self.capacity_map = capacity_map

    def plan(self, board: str, alight: str, date: str, time_str: str,
             window_min: int = 2, sort: str = "arrival", include_earlier: bool = True) -> dict:
        target = hms_to_sec(time_str)
        lo, hi = inclusive_window(target, window_min)
        all_options, info = self.net.options(board, alight, target_sec=None, date_iso=date)
        in_window = [o for o in all_options if lo <= o.dep_sec <= hi]
        if not include_earlier:
            in_window = [o for o in in_window if o.dep_sec >= target]
        later = [o for o in all_options if o.dep_sec > hi]
        next_later = later[0] if later else None
        use = in_window + ([next_later] if next_later and not in_window else [])

        capacity = capacity_for(None, 0, self.capacity_map)
        results = []
        for option in use:
            observed = run_loads_for_trip(self.conn, option.trip_id, date)
            result = analyse_journey(self.net, option, self.model, capacity, self.reference, date, observed)
            runs = self.conn.execute(
                "SELECT count(*) FROM run_info WHERE trip_id=? AND date=?", (option.trip_id, date)
            ).fetchone()[0]
            result.evidence = ("observed" if runs and any(observed.values()) else
                               "observed_empty_on_stretch" if runs else "unobserved")
            result.n_observed_runs = runs
            results.append(result)
        if sort == "crowding":
            results.sort(key=lambda r: (r.avg_load, r.option.arr_sec))
        else:
            results.sort(key=lambda r: (r.option.arr_sec, r.option.dep_sec))

        rows = []
        for r in results:
            o = r.option
            rows.append({
                "route": o.route_name, "direction": o.direction, "trip_id": o.trip_id,
                "departure_at_board": clock(o.dep_sec), "arrival_at_destination": clock(o.arr_sec),
                "wait_minutes": round((o.dep_sec - target) / 60, 1),
                "ride_minutes": round(o.ride_seconds / 60, 1),
                "expected_recorded_load": round(r.avg_load, 1),
                "busiest_stretch": r.peak_segment, "busiest_load": round(r.peak_load, 1),
                "minutes_crowded": round(r.crowded_minutes, 1),
                "confidence": r.confidence, "support": r.support, "mode": r.mode,
                "crowding": [e.recorded_class for e in r.estimates],
                "evidence": r.evidence, "observed_runs": r.n_observed_runs,
                "boarding_recommended": o.dep_sec >= target,
                "stops": [{"from": e.stop_from, "to": e.stop_to, "minutes": round(e.seconds / 60, 1),
                           "load": round(e.load, 1), "p75": round(e.p75, 1),
                           "support": e.support, "recorded_class": e.recorded_class,
                           "credible_class": e.credible_class, "method": e.fallback}
                          for e in r.estimates],
            })
        comparable = [r for r in results if r.evidence != "unobserved"]
        loads = []
        for r in comparable:
            for series in run_series_for_trip(self.conn, r.option.trip_id, date):
                loads.append(series[r.option.board_pos:r.option.alight_pos])
        stretch_len = max((len(x) for x in loads), default=0)
        pool = pooled_average(loads, 0, stretch_len, len(in_window),
                              "observed" if loads else "scheduled_only")
        pool["denominator_note"] = (
            "GTFS scheduled departures only; which buses actually operated is uncertain."
            if not loads else "Mixed evidence: observed departures used for the observed-only average; "
                              "the scheduled-departure denominator is reported separately with uncertainty."
        )
        if loads:
            pool["observed_only_average"] = pool["average_recorded_per_departure"]
            scheduled = pooled_average(loads, 0, stretch_len, len(in_window), "scheduled_only")
            pool["scheduled_denominator_average"] = scheduled["average_recorded_per_departure"]
            pool["scheduled_denominator_uncertain"] = True
        else:
            pool["observed_only_average"] = None
            pool["scheduled_denominator_average"] = pool["average_recorded_per_departure"]
            pool["scheduled_denominator_uncertain"] = True
        first_upcoming = next((r for r in results if r.option.dep_sec >= target), None)
        first_in_window = results[0] if results else None
        later_gain = None
        if first_upcoming and first_in_window and first_upcoming is not first_in_window:
            later_gain = {
                "later_route": first_upcoming.option.route_name,
                "later_departure": clock(first_upcoming.option.dep_sec),
                "load_change": round(first_upcoming.avg_load - first_in_window.avg_load, 1),
                "crowded_minutes_change": round(first_upcoming.crowded_minutes - first_in_window.crowded_minutes, 1),
                "arrival_change_minutes": round((first_upcoming.option.arr_sec - first_in_window.option.arr_sec) / 60, 1),
            }
        return {
            "query": {"board": board, "alight": alight, "date": date, "target_time": time_str,
                      "day_type": "weekend" if is_weekend(date) else "weekday",
                      "window_seconds": inclusive_window(target, window_min)},
            "stop_matching": info,
            "total_scheduled_direct_options_today": len(all_options),
            "distinct_departures_in_window": len(distinct_departures(in_window)),
            "options": rows,
            "later_comparison": later_gain,
            "pooled": pool,
            "reference": self.reference,
            "capacity_mode": capacity.mode,
            "caveats": [
                "Recorded passengers are e-ticket counts, not total onboard passengers; no independent coverage q is available.",
                "No fleet register maps vehicle numbers to seat layouts, so physical comfort is not asserted.",
                "An unobserved scheduled departure is not evidence of an empty bus.",
                "Earlier departures are shown for context only; boarding recommendations use departures at or after arrival.",
            ],
        }

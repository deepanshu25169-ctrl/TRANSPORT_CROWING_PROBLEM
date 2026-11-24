"""Commuter-facing journey planning logic."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .capacity import capacity_for
from .common import clock, hms_to_sec, is_weekend
from .gtfs import load_network
from .loads import distinct_departures, inclusive_window
from .model import HistoricalModel, analyse_journey, build_reference, pooled_average, run_series_for_trip
from .store import get_meta


def rounded(value, digits: int = 1):
    return round(value, digits) if value is not None else None


class Planner:
    def __init__(self, db: Path, capacity_map: Path | None = None,
                 aliases_path: Path | None = None):
        self.conn = sqlite3.connect(db, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if aliases_path is None:
            aliases_path = Path(__file__).resolve().parents[1] / "transport" / "aliases.json"
        self.net = load_network(self.conn, aliases_path)
        self.model = HistoricalModel(self.conn, self.net)
        report = get_meta(self.conn, "window_report")
        if report and report.get("selected"):
            self.model.window_days = int(report["selected"])
            self.model.shrinkage_k = float(report.get("selected_shrinkage", 0))
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
        all_left = bool(all_options and not in_window and not later)
        next_later = later[0] if later else (all_options[0] if all_left else None)
        use = in_window + ([next_later] if next_later and not in_window else [])

        capacity = capacity_for(None, 0, self.capacity_map)
        results = []
        for option in use:
            result = analyse_journey(self.net, option, self.model, capacity, self.reference, date)
            runs = self.conn.execute(
                "SELECT count(*) FROM run_info WHERE trip_id=? AND date=?", (option.trip_id, date)
            ).fetchone()[0]
            result.n_observed_runs = runs
            results.append(result)
        if sort == "crowding":
            results.sort(key=lambda r: (r.avg_load is None, r.avg_load or 0, r.option.arr_sec))
        else:
            results.sort(key=lambda r: (r.option.arr_sec, r.option.dep_sec))

        rows = []
        for r in results:
            o = r.option
            rows.append({
                "route": o.route_name, "direction": o.direction, "trip_id": o.trip_id,
                "pattern_id": o.pattern_id,
                "board_platform": self.net.stop_id(o.board_stop_idx),
                "alight_platform": self.net.stop_id(o.alight_stop_idx),
                "departure_at_board": clock(o.dep_sec), "arrival_at_destination": clock(o.arr_sec),
                "wait_minutes": round((o.dep_sec - target) / 60, 1),
                "ride_minutes": round(o.ride_seconds / 60, 1),
                "expected_recorded_load": rounded(r.avg_load),
                "busiest_stretch": r.peak_segment,
                "busiest_stretches": r.peak_segments,
                "busiest_load": rounded(r.peak_load),
                "minutes_crowded": rounded(r.crowded_minutes),
                "confidence": r.confidence, "support": r.support, "mode": r.mode,
                "crowding": [e.recorded_class for e in r.estimates],
                "evidence": r.evidence, "observed_runs": r.n_observed_runs,
                "boarding_recommended": o.dep_sec >= target,
                "stops": [{"from": e.stop_from, "to": e.stop_to,
                           "platform_from": e.platform_from, "platform_to": e.platform_to,
                           "position": e.position, "minutes": round(e.seconds / 60, 1),
                           "load": rounded(e.load), "p75": rounded(e.p75),
                           "support": e.support, "n_days": e.n_days,
                           "recorded_class": e.recorded_class,
                           "credible_class": e.credible_class, "method": e.method,
                           "status": e.status, "window_minutes": e.window_minutes,
                           "evidence_based": e.evidence_based,
                           "observed_loads": [rounded(v, 2) for v in e.observed_loads[:12]]}
                          for e in r.estimates],
            })
        loads = []
        for r in results:
            for series in run_series_for_trip(self.conn, r.option.trip_id, date):
                loads.append(series[r.option.board_pos:r.option.alight_pos])
        stretch_len = max((len(x) for x in loads), default=0)
        pool = pooled_average(loads, 0, stretch_len, len(in_window),
                              "observed" if loads else "scheduled_only")
        pool["denominator_note"] = (
            "GTFS scheduled departures only; which buses actually operated is uncertain."
            if not loads else "Observed departures are kept separate; the scheduled-departure "
                              "denominator is reported with uncertainty."
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
        first_shown = results[0] if results else None
        later_gain = None
        if first_upcoming and first_shown and first_upcoming is not first_shown:
            delta_load = None
            if first_upcoming.avg_load is not None and first_shown.avg_load is not None:
                delta_load = round(first_upcoming.avg_load - first_shown.avg_load, 1)
            delta_crowded = None
            if first_upcoming.crowded_minutes is not None and first_shown.crowded_minutes is not None:
                delta_crowded = round(first_upcoming.crowded_minutes - first_shown.crowded_minutes, 1)
            later_gain = {
                "later_route": first_upcoming.option.route_name,
                "later_departure": clock(first_upcoming.option.dep_sec),
                "load_change": delta_load,
                "crowded_minutes_change": delta_crowded,
                "arrival_change_minutes": round((first_upcoming.option.arr_sec - first_shown.option.arr_sec) / 60, 1),
            }
        after_october = date > "2026-10-31"
        return {
            "query": {"board": board, "alight": alight, "date": date, "target_time": time_str,
                      "day_type": "weekend" if is_weekend(date) else "weekday",
                      "window_seconds": inclusive_window(target, window_min),
                      "data_vintage": "Based on October 2026 ticket history" if after_october else
                                      "Based on recorded October 2026 ticket history up to 21 October"},
            "stop_matching": info,
            "total_scheduled_direct_options_today": len(all_options),
            "distinct_departures_in_window": len(distinct_departures(in_window)),
            "all_nearby_departures_left": all_left,
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
                "Estimates are medians by exact route pattern, direction, stop position, day type and local time; "
                "limited evidence is labelled rather than treated as a low load.",
            ],
        }

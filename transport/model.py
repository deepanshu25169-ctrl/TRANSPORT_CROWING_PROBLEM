"""Historical load model, journey metrics and pooling rules."""

from __future__ import annotations

import collections
import datetime as dt
import json
import math
from dataclasses import dataclass

from .common import is_weekend, mean, median, percentile, safe_div, time_bin
from .capacity import Capacity, classify_credible, classify_recorded
from .gtfs import JourneyOption, Network

MIN_SUPPORT = 3
CANDIDATE_WINDOWS = (3, 7, 14, 21)
EVAL_DAYS = 7
SELECTION_SAMPLE_STEP = 10


@dataclass
class Profile:
    key: tuple
    n_runs: int
    n_days: int
    median_load: float
    p75_load: float
    p90_load: float
    fallback: str = "exact"


@dataclass
class Aggregated:
    profiles: dict[tuple, Profile]
    by_pattern_day: dict[tuple, list[float]]
    route_medians: dict[str, float]
    route_support: dict[str, int]


@dataclass
class SegmentEstimate:
    position: int
    stop_from: str
    stop_to: str
    seconds: int
    load: float
    support: int
    n_days: int
    p75: float
    fallback: str
    credible_class: str
    recorded_class: str
    recorded_ratio: float


@dataclass
class JourneyResult:
    option: JourneyOption
    estimates: list[SegmentEstimate]
    avg_load: float
    peak_load: float
    peak_segment: str
    crowded_minutes: float
    total_minutes: float
    support: int
    confidence: str
    mode: str


class HistoricalModel:
    """Median load by route pattern, direction, day type and segment time."""

    def __init__(self, conn, net: Network, bin_minutes: int = 30, train_end: str = "2026-10-21"):
        self.conn = conn
        self.net = net
        self.bin_minutes = bin_minutes
        self.train_end = train_end
        self.obs: dict[tuple, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
        self.route_obs: dict[str, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
        self.route_by_pattern: dict[str, str] = {}
        self.agg: Aggregated | None = None
        self.window_days: int | None = None
        self.window_report: dict = {}
        self._load_observations()

    def _load_observations(self) -> None:
        run_meta = {}
        for key, date, route, pattern, trip_id in self.conn.execute(
            "SELECT run_key,date,route_name,pattern_id,trip_id FROM run_info"
        ):
            run_meta[key] = (date, route, pattern, trip_id)
            self.route_by_pattern[pattern] = route
        for key, loads_json in self.conn.execute("SELECT run_key,loads FROM run_loads"):
            meta = run_meta.get(key)
            if not meta:
                continue
            date, route, pattern, trip_id = meta
            loads = json.loads(loads_json)
            trip = self.net.trips.get(trip_id)
            if not trip:
                continue
            day_type = "weekend" if is_weekend(date) else "weekday"
            for pos, load in enumerate(loads):
                if load <= 0 or pos >= len(trip.dep_secs) - 1:
                    continue
                tbin = time_bin(trip.dep_secs[pos], self.bin_minutes)
                self.obs[(pattern, day_type, tbin)][date].append(float(load))
                self.route_obs[route][date].append(float(load))

    def _aggregate(self, start: str, end: str) -> Aggregated:
        profiles = {}
        by_pattern_day = collections.defaultdict(list)
        for key, by_date in self.obs.items():
            values = [v for date, vals in by_date.items() if start <= date <= end for v in vals]
            if not values:
                continue
            days = {date for date, vals in by_date.items() if start <= date <= end and vals}
            profiles[key] = Profile(key, len(values), len(days), median(values),
                                    percentile(values, 75), percentile(values, 90))
            by_pattern_day[(key[0], key[1])].extend(values)
        route_medians, route_support = {}, {}
        for route, by_date in self.route_obs.items():
            vals = [v for date, vs in by_date.items() if date <= self.train_end for v in vs]
            if vals:
                route_medians[route] = median(vals)
                route_support[route] = len(vals)
        return Aggregated(profiles, dict(by_pattern_day), route_medians, route_support)

    def _predict_from(self, agg: Aggregated, key: tuple, tbin: int, route: str) -> tuple[float, Profile]:
        p = agg.profiles.get(key)
        if p and p.n_runs >= MIN_SUPPORT:
            return p.median_load, p
        wide = agg.profiles.get((key[0], key[1], tbin // 2))
        if wide and wide.n_runs >= MIN_SUPPORT:
            return wide.median_load, Profile(wide.key, wide.n_runs, wide.n_days, wide.median_load,
                                             wide.p75_load, wide.p90_load, "wider_time_bin")
        vals = agg.by_pattern_day.get((key[0], key[1]))
        if vals:
            return median(vals), Profile(key, len(vals), 0, median(vals), percentile(vals, 75),
                                         percentile(vals, 90), "pattern_day_no_time")
        if route in agg.route_medians:
            val = agg.route_medians[route]
            return val, Profile(key, agg.route_support[route], 0, val, val, val, "route_no_time")
        return float("nan"), Profile(key, 0, 0, float("nan"), float("nan"), float("nan"), "none")

    def select_window(self, train_end: str | None = None) -> dict:
        train_end = train_end or self.train_end
        dates = sorted({d for by_date in self.obs.values() for d in by_date})
        cutoffs = []
        for offset in (14, 7):
            cutoff = (dt.date.fromisoformat(train_end) - dt.timedelta(days=offset)).isoformat()
            if cutoff in dates:
                cutoffs.append(cutoff)
        sampled_obs = [(k, d, vs) for k, by_date in self.obs.items() for d, vs in by_date.items()][::SELECTION_SAMPLE_STEP]
        report = {"cutoffs": cutoffs, "sample_step": SELECTION_SAMPLE_STEP, "candidates": {}}
        for W in CANDIDATE_WINDOWS:
            errors = []
            for cutoff in cutoffs:
                start = (dt.date.fromisoformat(cutoff) - dt.timedelta(days=W - 1)).isoformat()
                agg = self._aggregate(start, cutoff)
                eval_end = (dt.date.fromisoformat(cutoff) + dt.timedelta(days=EVAL_DAYS)).isoformat()
                abs_errors = []
                for key, date, vals in sampled_obs:
                    if not (cutoff < date <= eval_end):
                        continue
                    pred, _ = self._predict_from(agg, key, key[2], self.route_by_pattern.get(key[0], ""))
                    if math.isnan(pred):
                        continue
                    abs_errors.extend(abs(pred - v) for v in vals)
                if abs_errors:
                    errors.append({"cutoff": cutoff, "mae": mean(abs_errors), "n": len(abs_errors)})
            score = mean(e["mae"] for e in errors) if errors else float("inf")
            report["candidates"][W] = {"mae": score, "cutoffs": errors}
        best = min(report["candidates"], key=lambda w: report["candidates"][w]["mae"])
        self.window_days = best
        self.window_report = report
        return report

    def fit(self, end_date: str | None = None, window_days: int | None = None) -> None:
        end_date = end_date or self.train_end
        W = window_days or self.window_days or 21
        start = (dt.date.fromisoformat(end_date) - dt.timedelta(days=W - 1)).isoformat()
        self.agg = self._aggregate(start, end_date)
        self.window_days = W

    def predict(self, pattern: str, day_type: str, tbin: int, route: str,
                observed_loads: list[float] | None = None):
        if self.agg is None:
            self.fit()
        if observed_loads:
            vals = list(observed_loads)
            return median(vals), Profile((pattern, day_type, tbin), len(vals), 1, median(vals),
                                         percentile(vals, 75), percentile(vals, 90), "observed_departures")
        return self._predict_from(self.agg, (pattern, day_type, tbin), tbin, route)


def build_reference(model: HistoricalModel, train_end: str = "2026-10-21") -> dict:
    values = [v for by_date in model.route_obs.values() for date, vals in by_date.items()
              if date <= train_end for v in vals]
    return {"n": len(values), "lower": percentile(values, 33) if values else None,
            "typical": percentile(values, 50) if values else None,
            "higher": percentile(values, 67) if values else None}


def analyse_journey(net: Network, option: JourneyOption, model: HistoricalModel, capacity: Capacity,
                    reference: dict, date_iso: str, observed: dict[int, list[float]] | None = None
                    ) -> JourneyResult:
    pattern = net.patterns[option.pattern_id]
    day_type = "weekend" if is_weekend(date_iso) else "weekday"
    estimates = []
    for offset, pos in enumerate(range(option.board_pos, option.alight_pos)):
        seconds = option.segment_seconds[offset]
        tbin = time_bin(net.trips[option.trip_id].dep_secs[pos], model.bin_minutes)
        vals = (observed or {}).get(pos)
        load, profile = model.predict(option.pattern_id, day_type, tbin, option.route_name,
                                      observed_loads=vals)
        if math.isnan(load):
            load = 0.0
        credible = classify_credible(load, capacity)
        recorded, ratio = classify_recorded(load, reference)
        estimates.append(SegmentEstimate(
            position=pos, stop_from=net.stop_name(pattern.stops[pos]),
            stop_to=net.stop_name(pattern.stops[pos + 1]), seconds=seconds, load=load,
            support=profile.n_runs, n_days=profile.n_days, p75=profile.p75_load,
            fallback=profile.fallback, credible_class=credible, recorded_class=recorded,
            recorded_ratio=ratio,
        ))
    weights = [e.seconds for e in estimates] or [1]
    total = sum(weights)
    avg_load = sum(e.seconds * e.load for e in estimates) / total
    peak = max(estimates, key=lambda e: e.load) if estimates else None
    threshold = capacity.comfortable_load if capacity.comfortable_load is not None else reference.get("higher")
    crowded_seconds = sum(e.seconds for e in estimates if threshold is not None and e.load > threshold)
    support = min((e.support for e in estimates), default=0)
    confidence = "high" if support >= 5 else "moderate" if support >= MIN_SUPPORT else "Limited data"
    return JourneyResult(option, estimates, avg_load, peak.load if peak else 0.0,
                         f"{peak.stop_from} -> {peak.stop_to}" if peak else "-",
                         crowded_seconds / 60, total / 60, support, confidence, capacity.mode)


def run_series_for_trip(conn, trip_id: str, date_iso: str) -> list[list[float]]:
    rows = conn.execute(
        "SELECT rl.loads FROM run_loads rl JOIN run_info ri ON ri.run_key=rl.run_key "
        "WHERE ri.trip_id=? AND ri.date=?", (trip_id, date_iso)
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def run_loads_for_trip(conn, trip_id: str, date_iso: str) -> dict[int, list[float]]:
    series = run_series_for_trip(conn, trip_id, date_iso)
    by_pos: dict[int, list[float]] = collections.defaultdict(list)
    for loads in series:
        for pos, load in enumerate(loads):
            if load > 0:
                by_pos[pos].append(float(load))
    return dict(by_pos)


def pooled_average(loads_per_run: list[list[float]], board_pos: int, alight_pos: int,
                   eligible_departures: int | None, denominator_source: str) -> dict:
    """Numerator and denominator must describe the same runs, date and stretch."""
    individual = [sum(loads[board_pos:alight_pos]) for loads in loads_per_run]
    total = sum(individual)
    denom = eligible_departures if eligible_departures is not None else len(loads_per_run)
    return {
        "individual_recorded_passengers": individual,
        "total_recorded_passengers": total,
        "eligible_departures": denom,
        "denominator_source": denominator_source,
        "average_recorded_per_departure": safe_div(total, denom),
        "pooled_hides_individual_differences": len(set(individual)) > 1,
    }

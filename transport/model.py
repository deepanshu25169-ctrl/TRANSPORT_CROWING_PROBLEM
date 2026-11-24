"""Historical load model, journey metrics and pooling rules."""

from __future__ import annotations

import collections
import datetime as dt
import json
import math
from dataclasses import dataclass, field

from .capacity import Capacity, classify_credible, classify_recorded
from .common import is_weekend, mean, median, percentile, safe_div, time_bin
from .gtfs import JourneyOption, Network

MIN_SUPPORT = 3
MAX_STRETCH_RADIUS = 3
CANDIDATE_WINDOWS = (3, 7, 14, 21)
SHRINKAGE_CANDIDATES = (0, 5, 15, 40, 100, 250, 600, 1500)


def weighted_percentile(values: list[tuple[float, int]], pct: float) -> float:
    total = sum(count for _, count in values)
    if not total:
        return float("nan")
    target = total * pct / 100.0
    cumulative = 0
    for value, count in sorted(values):
        cumulative += count
        if cumulative >= target:
            return value
    return sorted(values)[-1][0]


@dataclass(frozen=True)
class Observation:
    run_key: str
    date: str
    route_name: str
    pattern_id: str
    trip_id: str
    day_type: str
    time_bin: int
    position: int
    scheduled_sec: int
    load: float
    reconstruction_rule: str


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
class Prediction:
    load: float | None
    upper: float | None
    n_runs: int
    n_days: int
    method: str
    window_minutes: int | None
    status: str
    observed_loads: list[float] = field(default_factory=list)
    evidence_based: bool = True


@dataclass
class Aggregated:
    profiles: dict[tuple, Profile]
    by_pattern_day: dict[tuple, list[float]]
    route_medians: dict[str, float]
    route_support: dict[str, int]
    stretch_profiles: dict[tuple, Profile]
    stretch_by_pattern_day: dict[tuple, list[float]]
    stretch_day_profiles: dict[tuple, Profile]
    stretch_bin_observations: dict[tuple, list[Observation]]
    stretch_observations: list[Observation]


@dataclass
class SegmentEstimate:
    position: int
    stop_from: str
    stop_to: str
    platform_from: str
    platform_to: str
    seconds: int
    load: float | None
    support: int
    n_days: int
    p75: float | None
    method: str
    status: str
    window_minutes: int | None
    observed_loads: list[float]
    evidence_based: bool
    credible_class: str
    recorded_class: str
    recorded_ratio: float


@dataclass
class JourneyResult:
    option: JourneyOption
    estimates: list[SegmentEstimate]
    avg_load: float | None
    peak_load: float | None
    peak_segment: str
    peak_segments: list[dict]
    crowded_minutes: float | None
    total_minutes: float
    support: int
    confidence: str
    mode: str
    evidence: str
    n_observed_runs: int = 0


class HistoricalModel:
    """Median recorded load by pattern, direction, day type, time and stretch position."""

    def __init__(self, conn, net: Network, bin_minutes: int = 30, train_end: str = "2026-10-21"):
        self.conn = conn
        self.net = net
        self.bin_minutes = bin_minutes
        self.train_end = train_end
        self.observations: list[Observation] = []
        self.obs: dict[tuple, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
        self.route_obs: dict[str, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
        self.route_by_pattern: dict[str, str] = {}
        self.agg: Aggregated | None = None
        self.window_days: int | None = None
        self.shrinkage_k: float = 0.0
        self.window_report: dict = {}
        self._load_observations()

    def _load_observations(self) -> None:
        rows = self.conn.execute(
            "SELECT ri.run_key,ri.date,ri.route_name,ri.pattern_id,ri.trip_id,"
            "ri.first_board_pos,ri.last_alight_pos,ri.reconstruction_rule,rl.loads "
            "FROM run_info ri JOIN run_loads rl ON ri.run_key=rl.run_key"
        ).fetchall()
        for (run_key, date, route, pattern, trip_id, first_board, last_alight, rule, loads_json) in rows:
            trip = self.net.trips.get(trip_id)
            if not trip or pattern not in self.net.patterns:
                continue
            self.route_by_pattern[pattern] = route
            loads = json.loads(loads_json)
            day_type = "weekend" if is_weekend(date) else "weekday"
            n_stops = len(self.net.patterns[pattern].stops)
            end_pos = min(last_alight, n_stops - 1)
            for pos in range(max(0, first_board), end_pos):
                if pos >= len(loads) or pos >= len(trip.dep_secs) - 1:
                    continue
                load = float(loads[pos])
                if load < 0:
                    continue
                scheduled_sec = trip.dep_secs[pos]
                local_sec = scheduled_sec % 86400
                tbin = time_bin(local_sec, self.bin_minutes)
                observation = Observation(run_key, date, route, pattern, trip_id, day_type,
                                          tbin, pos, scheduled_sec, load, rule)
                self.observations.append(observation)
                self.obs[(pattern, day_type, tbin)][date].append(load)
                self.route_obs[route][date].append(load)

    def all_observations(self) -> list[Observation]:
        return list(self.observations)

    def _aggregate(self, end_date: str | None = None, window_days: int | None = None,
                   observations: list[Observation] | None = None) -> Aggregated:
        end_date = end_date or self.train_end
        W = window_days or self.window_days or 14
        start = (dt.date.fromisoformat(end_date) - dt.timedelta(days=W - 1)).isoformat()
        rows = self.observations if observations is None else observations
        selected = [o for o in rows if start <= o.date <= end_date]

        legacy: dict[tuple, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
        legacy_runs: dict[tuple, set[str]] = collections.defaultdict(set)
        pattern_day: dict[tuple, list[float]] = collections.defaultdict(list)
        stretch_bins: dict[tuple, list[Observation]] = collections.defaultdict(list)
        stretch_day: dict[tuple, list[float]] = collections.defaultdict(list)
        route_values: dict[str, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
        for o in selected:
            legacy_key = (o.pattern_id, o.day_type, o.time_bin)
            legacy[legacy_key][o.date].append(o.load)
            legacy_runs[legacy_key].add(o.run_key)
            pattern_day[(o.pattern_id, o.day_type)].append(o.load)
            stretch_bins[(o.pattern_id, o.day_type, o.position, o.time_bin)].append(o)
            stretch_day[(o.pattern_id, o.day_type, o.position)].append(o.load)
            route_values[o.route_name][o.date].append(o.load)

        profiles = {}
        for key, by_date in legacy.items():
            values = [v for vals in by_date.values() for v in vals]
            if values:
                profiles[key] = Profile(key, len(legacy_runs[key]), len(by_date),
                                        median(values), percentile(values, 75),
                                        percentile(values, 90), "pattern_time")

        stretch_profiles = {}
        for key, obs in stretch_bins.items():
            values = [o.load for o in obs]
            stretch_profiles[key] = Profile(key, len({o.run_key for o in obs}),
                                            len({o.date for o in obs}), median(values),
                                            percentile(values, 75), percentile(values, 90),
                                            "exact_stretch")
        stretch_day_profiles = {}
        day_runs: dict[tuple, set[str]] = collections.defaultdict(set)
        day_dates: dict[tuple, set[str]] = collections.defaultdict(set)
        for o in selected:
            key = (o.pattern_id, o.day_type, o.position)
            day_runs[key].add(o.run_key)
            day_dates[key].add(o.date)
        for key, values in stretch_day.items():
            stretch_day_profiles[key] = Profile(key, len(day_runs[key]), len(day_dates[key]),
                                                median(values), percentile(values, 75),
                                                percentile(values, 90), "same_stretch_all_day")
        route_medians, route_support = {}, {}
        for route, by_date in route_values.items():
            values = [v for vals in by_date.values() for v in vals]
            if values:
                route_medians[route] = median(values)
                route_support[route] = len(values)
        return Aggregated(profiles, dict(pattern_day), route_medians, route_support,
                          stretch_profiles, dict(stretch_day), stretch_day_profiles,
                          dict(stretch_bins), selected)

    def _shrink(self, value: float, n: int, route: str) -> float:
        base = self.agg.route_medians.get(route) if self.agg else None
        if base is None or not self.shrinkage_k or n <= 0:
            return value
        k = float(self.shrinkage_k)
        return (n * value + k * base) / (n + k)

    def _prediction_from_values(self, values: list[float], obs: list[Observation], route: str,
                                method: str, window_minutes: int | None, evidence_based: bool = True) -> Prediction:
        unique = {o.run_key: o for o in obs}
        n_runs = len(unique) if obs else len(values)
        n_days = len({o.date for o in unique.values()}) if obs else 0
        value = median(values)
        status = ("high" if n_runs >= 5 and n_days >= 2 else
                  "supported" if n_runs >= MIN_SUPPORT else
                  "Limited data" if values else "no_data")
        return Prediction(self._shrink(value, n_runs, route) if value is not None else None,
                          percentile(values, 75) if values else None, n_runs, n_days, method,
                          window_minutes, status, list(values), evidence_based)

    def _combine_profiles(self, profiles: list[Profile], route: str, method: str,
                          window_minutes: int) -> Prediction | None:
        if not profiles:
            return None
        counts = [(p.median_load, max(1, p.n_runs)) for p in profiles]
        n_runs = sum(p.n_runs for p in profiles)
        n_days = sum(p.n_days for p in profiles)
        value = weighted_percentile(counts, 50)
        upper = weighted_percentile([(p.p75_load, max(1, p.n_runs)) for p in profiles], 50)
        status = ("high" if n_runs >= 5 and n_days >= 2 else
                  "supported" if n_runs >= MIN_SUPPORT else "Limited data")
        return Prediction(self._shrink(value, n_runs, route), upper, n_runs, n_days, method,
                          window_minutes, status, [])

    def _predict_from(self, agg: Aggregated, key: tuple, tbin: int, route: str):
        p = agg.profiles.get(key)
        if p and p.n_runs >= MIN_SUPPORT:
            return self._shrink(p.median_load, p.n_runs, route), p
        wide = agg.profiles.get((key[0], key[1], tbin // 2))
        if wide and wide.n_runs >= MIN_SUPPORT:
            return self._shrink(wide.median_load, wide.n_runs, route), Profile(
                wide.key, wide.n_runs, wide.n_days, wide.median_load,
                wide.p75_load, wide.p90_load, "wider_time_bin")
        vals = agg.by_pattern_day.get((key[0], key[1]))
        if vals:
            value = median(vals)
            return self._shrink(value, len(vals), route), Profile(
                key, len(vals), 0, value, percentile(vals, 75),
                percentile(vals, 90), "pattern_day_no_time")
        return float("nan"), Profile(key, 0, 0, float("nan"), float("nan"), float("nan"), "none")

    def predict_legacy(self, pattern: str, day_type: str, tbin: int, route: str):
        if self.agg is None:
            self.fit()
        return self._predict_from(self.agg, (pattern, day_type, tbin), tbin, route)

    def predict_stretch(self, pattern: str, day_type: str, position: int, scheduled_sec: int,
                        route: str) -> Prediction:
        if self.agg is None:
            self.fit()
        agg = self.agg
        local_sec = scheduled_sec % 86400
        tbin = time_bin(local_sec, self.bin_minutes)
        bins_per_day = max(1, round(86400 / (self.bin_minutes * 60)))
        p = agg.stretch_profiles.get((pattern, day_type, tbin, position))
        if p and p.n_runs >= MIN_SUPPORT:
            values = [o.load for o in agg.stretch_bin_observations.get(
                (pattern, day_type, position, tbin), [])]
            return Prediction(self._shrink(p.median_load, p.n_runs, route), p.p75_load,
                              p.n_runs, p.n_days, "exact_stretch", self.bin_minutes,
                              "high" if p.n_runs >= 5 and p.n_days >= 2 else "supported",
                              values)
        for radius in range(0, MAX_STRETCH_RADIUS + 1):
            nearby = [p for delta in range(-radius, radius + 1)
                      for p in [agg.stretch_profiles.get((pattern, day_type,
                                                          (tbin + delta) % bins_per_day, position))]
                      if p and p.n_runs >= MIN_SUPPORT]
            prediction = self._combine_profiles(nearby, route, "nearby_stretch",
                                                (2 * radius + 1) * self.bin_minutes)
            if prediction and prediction.n_runs >= MIN_SUPPORT:
                return prediction
        day_profile = agg.stretch_day_profiles.get((pattern, day_type, position))
        if day_profile:
            prediction = self._combine_profiles([day_profile], route,
                                                "same_stretch_all_day", 1440)
            prediction.evidence_based = prediction.n_runs >= MIN_SUPPORT
            return prediction
        if local_sec < 3600 or local_sec >= 82800:
            other_day = "weekend" if day_type == "weekday" else "weekday"
            cross = [p for delta in (-1, 0, 1)
                     for p in [agg.stretch_profiles.get((pattern, other_day,
                                                        (tbin + delta) % bins_per_day, position))]
                     if p and p.n_runs >= MIN_SUPPORT]
            prediction = self._combine_profiles(cross, route,
                                                "adjacent_day_type_boundary", 3 * self.bin_minutes)
            if prediction:
                prediction.evidence_based = prediction.n_runs >= MIN_SUPPORT
                return prediction
        if route in agg.route_medians:
            value = agg.route_medians[route]
            return Prediction(value, value, agg.route_support[route], 0,
                              "route_only_no_stretch_evidence", None,
                              "Limited data", [], False)
        return Prediction(None, None, 0, 0, "none", None, "no_data", [], False)

    def predict(self, pattern: str, day_type: str, tbin: int, route: str,
                position: int | None = None, scheduled_sec: int | None = None,
                observed_loads: list[float] | None = None):
        if position is not None and scheduled_sec is not None:
            return self.predict_stretch(pattern, day_type, position, scheduled_sec, route)
        return self.predict_legacy(pattern, day_type, tbin, route)

    def select_window(self, train_end: str | None = None) -> dict:
        self.window_days = 14
        self.shrinkage_k = 0.0
        self.window_report = {
            "selected": self.window_days, "selected_shrinkage": self.shrinkage_k,
            "candidate_windows": list(CANDIDATE_WINDOWS),
            "note": "Selection is recomputed in scripts/validate.py with rolling date splits.",
        }
        return self.window_report

    def fit(self, end_date: str | None = None, window_days: int | None = None,
            observations: list[Observation] | None = None) -> None:
        end_date = end_date or self.train_end
        W = window_days or self.window_days or 14
        self.agg = self._aggregate(end_date, W, observations)
        self.window_days = W


def build_reference(model: HistoricalModel, train_end: str = "2026-10-21") -> dict:
    values = [v for by_date in model.route_obs.values() for date, vals in by_date.items()
              if date <= train_end for v in vals]
    return {"n": len(values), "lower": percentile(values, 50) if values else None,
            "typical": percentile(values, 75) if values else None,
            "higher": percentile(values, 90) if values else None}


def analyse_journey(net: Network, option: JourneyOption, model: HistoricalModel, capacity: Capacity,
                    reference: dict, date_iso: str, observed: dict[int, list[float]] | None = None
                    ) -> JourneyResult:
    pattern = net.patterns[option.pattern_id]
    day_type = "weekend" if is_weekend(date_iso) else "weekday"
    estimates: list[SegmentEstimate] = []
    elapsed = 0
    for offset, pos in enumerate(range(option.board_pos, option.alight_pos)):
        seconds = option.segment_seconds[offset]
        scheduled_sec = option.dep_sec + elapsed
        elapsed += seconds
        prediction = model.predict_stretch(option.pattern_id, day_type, pos, scheduled_sec,
                                           option.route_name)
        load = prediction.load
        credible = classify_credible(load, capacity) if load is not None else "unknown"
        recorded, ratio = classify_recorded(load, reference) if load is not None else ("No data", float("nan"))
        estimates.append(SegmentEstimate(
            position=pos, stop_from=net.stop_name(pattern.stops[pos]),
            stop_to=net.stop_name(pattern.stops[pos + 1]),
            platform_from=net.stop_id(pattern.stops[pos]),
            platform_to=net.stop_id(pattern.stops[pos + 1]),
            seconds=seconds, load=load, support=prediction.n_runs, n_days=prediction.n_days,
            p75=prediction.upper, method=prediction.method, status=prediction.status,
            window_minutes=prediction.window_minutes, observed_loads=prediction.observed_loads,
            evidence_based=prediction.evidence_based, credible_class=credible,
            recorded_class=recorded, recorded_ratio=ratio,
        ))
    valid = [e for e in estimates if e.load is not None and e.evidence_based]
    weights = [e.seconds for e in valid]
    total_seconds = sum(e.seconds for e in estimates)
    avg_load = (sum(e.load * e.seconds for e in valid) / sum(weights)) if weights else None
    peak_load = max((e.load for e in valid), default=None)
    peak_segments = ([{"position": e.position, "from": e.stop_from, "to": e.stop_to,
                       "platform_from": e.platform_from, "platform_to": e.platform_to,
                       "load": e.load, "support": e.support, "n_days": e.n_days,
                       "method": e.method}
                      for e in valid if abs(e.load - peak_load) < 1e-9] if peak_load is not None else [])
    peak_segment = " / tie ".join(f"{p['from']} -> {p['to']}" for p in peak_segments) or "-"
    threshold = reference.get("higher")
    crowded_seconds = sum(e.seconds for e in valid if threshold is not None and e.load > threshold)
    support = min((e.support for e in estimates), default=0)
    methods = sorted({e.method for e in valid})
    if not valid:
        confidence = "No data"
    elif all(e.status == "high" for e in valid):
        confidence = "high"
    elif all(e.status in ("high", "supported") for e in valid):
        confidence = "moderate"
    else:
        confidence = "Limited data"
    return JourneyResult(option, estimates, avg_load, peak_load, peak_segment, peak_segments,
                         crowded_seconds / 60 if valid else None, total_seconds / 60,
                         support, confidence, capacity.mode, "; ".join(methods) or "no evidence")


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

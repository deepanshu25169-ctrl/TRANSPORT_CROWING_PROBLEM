"""GTFS ingestion and ordered-stop route network (stdlib only)."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .common import hms_to_sec, is_weekend, sec_to_hms
from .normalize import NameMatcher, norm

csv.field_size_limit(10**9)


@dataclass
class Stop:
    idx: int
    stop_id: str
    name: str
    lat: float
    lon: float


@dataclass
class Route:
    route_id: str
    name: str
    agency: str
    description: str
    direction: str


@dataclass
class Pattern:
    pattern_id: str
    route_name: str
    direction: str
    stops: tuple[int, ...]


@dataclass
class Trip:
    trip_id: str
    route_name: str
    pattern_id: str
    dep_secs: tuple[int, ...]
    arr_secs: tuple[int, ...]
    headsign: str
    service_id: str
    direction: str

    @property
    def first_dep(self) -> int:
        return self.dep_secs[0]

    @property
    def last_arr(self) -> int:
        return self.arr_secs[-1]


@dataclass
class JourneyOption:
    trip_id: str
    route_name: str
    direction: str
    pattern_id: str
    board_stop_idx: int
    alight_stop_idx: int
    board_pos: int
    alight_pos: int
    dep_sec: int
    arr_sec: int
    segment_seconds: tuple[int, ...]

    @property
    def n_segments(self) -> int:
        return self.alight_pos - self.board_pos

    @property
    def ride_seconds(self) -> int:
        return self.arr_sec - self.dep_sec


class Network:
    """In-memory view of the supplied static feed."""

    def __init__(self) -> None:
        self.stops: list[Stop] = []
        self.stop_idx: dict[str, int] = {}
        self.routes: dict[str, Route] = {}
        self.patterns: dict[str, Pattern] = {}
        self.trips: dict[str, Trip] = {}
        self.trips_by_route: dict[str, list[str]] = {}
        self.trips_by_stop: dict[int, set[str]] = {}
        self.calendar: dict[str, dict] = {}
        self.matcher: NameMatcher | None = None
        self.feed_files: dict[str, int] = {}

    @classmethod
    def from_gtfs(cls, gtfs_dir: Path, aliases_path: Path | None = None) -> "Network":
        net = cls()
        with (gtfs_dir / "stops.txt").open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                idx = len(net.stops)
                sid = row["stop_id"].strip()
                net.stop_idx[sid] = idx
                try:
                    lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
                except (ValueError, TypeError):
                    lat, lon = 0.0, 0.0
                net.stops.append(Stop(idx, sid, row["stop_name"], lat, lon))
        net.feed_files["stops"] = len(net.stops)

        with (gtfs_dir / "routes.txt").open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("route_short_name") or row.get("route_long_name") or "").strip()
                desc = row.get("route_desc") or ""
                direction = desc.split(" to ")[-1] if " to " in desc else ""
                net.routes[name] = Route(
                    row["route_id"].strip(), name, row.get("agency_id", ""), desc, direction
                )
        net.feed_files["routes"] = len(net.routes)

        cal_path = gtfs_dir / "calendar.txt"
        if cal_path.exists():
            with cal_path.open(newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    net.calendar[row["service_id"].strip()] = {
                        "flags": [int(row[d]) for d in
                                  ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")],
                        "start": row["start_date"].strip(),
                        "end": row["end_date"].strip(),
                    }
        net.feed_files["calendar_dates_files"] = int((gtfs_dir / "calendar_dates.txt").exists())

        trip_rows: dict[str, dict] = {}
        with (gtfs_dir / "trips.txt").open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                trip_rows[row["trip_id"].strip()] = row
        net.feed_files["trips"] = len(trip_rows)

        stop_times: dict[str, list[tuple[int, int, int, int]]] = {}
        with (gtfs_dir / "stop_times.txt").open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            net.feed_files["stop_times_columns"] = ",".join(reader.fieldnames or [])
            for row in reader:
                tid = row["trip_id"].strip()
                stop_times.setdefault(tid, []).append((
                    int(row["stop_sequence"]),
                    net.stop_idx[row["stop_id"].strip()],
                    hms_to_sec(row["departure_time"]),
                    hms_to_sec(row["arrival_time"]),
                ))
        net.feed_files["stop_times"] = sum(len(v) for v in stop_times.values())

        for tid, rows in stop_times.items():
            rows.sort(key=lambda x: x[0])
            stop_seq = tuple(r[1] for r in rows)
            dep_secs = tuple(r[2] for r in rows)
            arr_secs = tuple(r[3] for r in rows)
            pattern_id = hashlib.sha1(stop_seq.__repr__().encode()).hexdigest()[:12]
            trow = trip_rows.get(tid, {})
            route_id = (trow.get("route_id") or "").strip()
            route_name = next((r.name for r in net.routes.values() if r.route_id == route_id), route_id)
            direction = (trow.get("direction_id") or "").strip() or RouteDirection.from_name(route_name)
            if pattern_id not in net.patterns:
                net.patterns[pattern_id] = Pattern(pattern_id, route_name, direction, stop_seq)
            net.trips[tid] = Trip(
                tid, route_name, pattern_id, dep_secs, arr_secs,
                trow.get("trip_headsign", ""), (trow.get("service_id") or "").strip(), direction,
            )
            net.trips_by_route.setdefault(route_name, []).append(tid)
            for sidx in set(stop_seq):
                net.trips_by_stop.setdefault(sidx, set()).add(tid)

        net.matcher = NameMatcher(
            {s.stop_id: s.name for s in net.stops}, net.routes.keys(), aliases_path
        )
        return net

    def service_active(self, date_iso: str, service_id: str) -> bool:
        cal = self.calendar.get(service_id)
        if not cal:
            return True
        import datetime as dt
        weekday = dt.date.fromisoformat(date_iso).weekday()
        inside = cal["start"] <= date_iso.replace("-", "") <= cal["end"]
        return inside and bool(cal["flags"][weekday])

    def resolve_stops(self, raw: str) -> tuple[str, list[int]]:
        """Return (audit label, candidate stop indices) for a commuter stop name."""
        if not self.matcher:
            return "unmatched", []
        m = self.matcher.match_stop(raw)
        return m.label, [self.stop_idx[sid] for sid in m.ids if sid in self.stop_idx]

    def stop_name(self, idx: int) -> str:
        return self.stops[idx].name

    def stop_id(self, idx: int) -> str:
        return self.stops[idx].stop_id

    def pattern_positions(self, trip_id: str, stop_idx: int) -> list[int]:
        trip = self.trips[trip_id]
        stops = self.patterns[trip.pattern_id].stops
        return [i for i, s in enumerate(stops) if s == stop_idx]

    def segment_seconds(self, trip: Trip, board_pos: int, alight_pos: int) -> tuple[int, ...]:
        out = []
        for s in range(board_pos, alight_pos):
            d = trip.arr_secs[s + 1] - trip.dep_secs[s]
            if d <= 0:
                d = trip.dep_secs[s + 1] - trip.dep_secs[s]
            out.append(max(1, d))
        return tuple(out)

    def options(
        self,
        board_raw: str,
        alight_raw: str,
        target_sec: int | None = None,
        window_min: int = 2,
        date_iso: str | None = None,
        direction_filter: str | None = None,
    ) -> tuple[list[JourneyOption], dict]:
        """Direct journeys where one trip serves board before alight.

        `window_min` is inclusive in both directions; the caller decides whether
        to show earlier departures as boarding recommendations.
        """
        board_label, boards = self.resolve_stops(board_raw)
        alight_label, alights = self.resolve_stops(alight_raw)
        if not boards or not alights:
            return [], {"board_match": board_label, "alight_match": alight_label}
        alight_set = set(alights)
        window = window_min * 60
        options: list[JourneyOption] = []
        for bidx in boards:
            for tid in self.trips_by_stop.get(bidx, ()):
                trip = self.trips[tid]
                if date_iso and not self.service_active(date_iso, trip.service_id):
                    continue
                if direction_filter and route_direction(trip.route_name) != direction_filter:
                    continue
                pattern = self.patterns[trip.pattern_id]
                for bpos, s in enumerate(pattern.stops):
                    if s != bidx:
                        continue
                    aparts = [i for i in range(bpos + 1, len(pattern.stops)) if pattern.stops[i] in alight_set]
                    if not aparts:
                        continue
                    apos = aparts[0]
                    dep = trip.dep_secs[bpos]
                    if target_sec is not None and abs(dep - target_sec) > window:
                        continue
                    options.append(JourneyOption(
                        trip_id=tid, route_name=trip.route_name, direction=direction_label(trip.route_name),
                        pattern_id=trip.pattern_id, board_stop_idx=bidx,
                        alight_stop_idx=pattern.stops[apos], board_pos=bpos, alight_pos=apos,
                        dep_sec=dep, arr_sec=trip.arr_secs[apos],
                        segment_seconds=self.segment_seconds(trip, bpos, apos),
                    ))
        options.sort(key=lambda o: (o.dep_sec, o.route_name, o.trip_id))
        return options, {"board_match": board_label, "alight_match": alight_label,
                         "board_candidates": boards, "alight_candidates": alights}


def route_direction(route_name: str) -> str:
    from .normalize import route_direction as rd
    return rd(route_name).split(":")[-1]


def direction_label(route_name: str) -> str:
    d = route_direction(route_name)
    return d if d != "UNKNOWN" else "unknown"


class RouteDirection:
    @staticmethod
    def from_name(route_name: str) -> str:
        return route_direction(route_name)


def save_network(net: Network, conn) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?)",
        [(s.idx, s.stop_id, s.name, s.lat, s.lon) for s in net.stops],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?)",
        [(r.route_id, r.name, r.agency, r.description, r.direction) for r in net.routes.values()],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO patterns VALUES (?,?,?,?)",
        [(p.pattern_id, p.route_name, p.direction, json.dumps(p.stops)) for p in net.patterns.values()],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO trips VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(t.trip_id, t.route_name, t.pattern_id, json.dumps(t.dep_secs), json.dumps(t.arr_secs),
          t.first_dep, t.last_arr, t.headsign, t.service_id, t.direction) for t in net.trips.values()],
    )
    for sid, cal in net.calendar.items():
        conn.execute("INSERT OR REPLACE INTO service_calendar VALUES (?,?,?,?)",
                     (sid, json.dumps(cal["flags"]), cal["start"], cal["end"]))
    conn.commit()


def load_network(conn, aliases_path: Path | None = None) -> Network:
    net = Network()
    for idx, sid, name, lat, lon in conn.execute("SELECT idx, stop_id, stop_name, lat, lon FROM stops"):
        net.stop_idx[sid] = idx
        net.stops.append(Stop(idx, sid, name, lat, lon))
    for rid, name, agency, desc, d in conn.execute("SELECT route_id, name, agency, description, direction FROM routes"):
        net.routes[name] = Route(rid, name, agency, desc, d)
    for pid, rname, d, stops_json in conn.execute("SELECT pattern_id, route_name, direction, stop_indices FROM patterns"):
        net.patterns[pid] = Pattern(pid, rname, d, tuple(json.loads(stops_json)))
    for (tid, rname, pid, deps, arrs, headsign, sid, d) in conn.execute(
        "SELECT trip_id, route_name, pattern_id, dep_secs, arr_secs, headsign, service_id, direction FROM trips"
    ):
        net.trips[tid] = Trip(tid, rname, pid, tuple(json.loads(deps)), tuple(json.loads(arrs)), headsign, sid, d)
        net.trips_by_route.setdefault(rname, []).append(tid)
        for s in set(net.patterns[pid].stops):
            net.trips_by_stop.setdefault(s, set()).add(tid)
    for sid, flags, start, end in conn.execute("SELECT service_id, flags, start_date, end_date FROM service_calendar"):
        net.calendar[sid] = {"flags": json.loads(flags), "start": start, "end": end}
    net.matcher = NameMatcher({s.stop_id: s.name for s in net.stops}, net.routes.keys(), aliases_path)
    net.feed_files = {"stops": len(net.stops), "routes": len(net.routes), "trips": len(net.trips),
                      "patterns": len(net.patterns)}
    return net

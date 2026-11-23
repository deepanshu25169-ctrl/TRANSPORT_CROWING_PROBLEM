"""Ticket matching, bus-run inference and segment-load reconstruction."""

from __future__ import annotations

import bisect
import collections
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .common import parse_ticket_timestamp
from .gtfs import Network
from .normalize import Match

csv.field_size_limit(10**9)

HIST_TICKET_TOL_SEC = 30 * 60
MAX_RUN_GAP_SEC = 90 * 60
TRIP_CHANGE_GAP_SEC = 10 * 60
TIME_REGRESSION_SEC = 120

INSERT_EVENT = (
    "INSERT INTO ticket_events (date,pax,vehicle,route_name,pattern_id,trip_id,run_key,"
    "board_idx,alight_idx,board_pos,alight_pos,ticket_sec,match_start,match_end,match_route,"
    "is_ac,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


@dataclass
class TicketAssignment:
    trip_id: str | None
    board_pos: int | None
    alight_pos: int | None
    board_idx: int | None
    alight_idx: int | None
    delta_sec: int | None
    status: str


class TicketIndex:
    """Scheduled departures indexed by (route, boarding stop) for fast matching."""

    def __init__(self, net: Network):
        self.net = net
        self.departures: dict[tuple[str, int], list[tuple[int, str, int]]] = collections.defaultdict(list)
        self.route_stops: dict[str, set[int]] = collections.defaultdict(set)
        self.trip_positions: dict[tuple[str, int], list[int]] = collections.defaultdict(list)
        for tid, trip in net.trips.items():
            stops = net.patterns[trip.pattern_id].stops
            for pos, sidx in enumerate(stops):
                self.departures[(trip.route_name, sidx)].append((trip.dep_secs[pos], tid, pos))
                self.route_stops[trip.route_name].add(sidx)
                self.trip_positions[(tid, sidx)].append(pos)
        self.dep_secs = {key: [x[0] for x in rows] for key, rows in self.departures.items()}
        for rows in self.departures.values():
            rows.sort(key=lambda x: x[0])
        for key in self.dep_secs:
            self.dep_secs[key] = [x[0] for x in self.departures[key]]

    def assign(
        self,
        route_name: str,
        board_ids: Iterable[str],
        alight_ids: Iterable[str],
        ticket_sec: int,
        tolerance: int = HIST_TICKET_TOL_SEC,
    ) -> TicketAssignment:
        alights = {self.net.stop_idx[s] for s in alight_ids if s in self.net.stop_idx}
        if not alights:
            return TicketAssignment(None, None, None, None, None, None, "no_trip_candidate")
        best = None
        for sid in board_ids:
            try:
                board_idx = self.net.stop_idx[sid]
            except KeyError:
                continue
            departures = self.departures.get((route_name, board_idx))
            if not departures:
                continue
            secs = self.dep_secs[(route_name, board_idx)]
            for anchor in (ticket_sec - 86400, ticket_sec, ticket_sec + 86400):
                lo = bisect.bisect_left(secs, anchor - tolerance)
                hi = bisect.bisect_right(secs, anchor + tolerance)
                for dep, tid, bpos in departures[lo:hi]:
                    for aidx in alights:
                        aparts = [p for p in self.trip_positions.get((tid, aidx), ()) if p > bpos]
                        if not aparts:
                            continue
                        apos = aparts[0]
                        delta = anchor - dep
                        cand = (abs(delta), delta, bpos, apos, board_idx, aidx, tid)
                        if best is None or cand[:2] < best[:2]:
                            best = cand
        if best is None:
            return TicketAssignment(None, None, None, None, None, None, "no_trip_candidate")
        _, delta, bpos, apos, board_idx, alight_idx, tid = best
        return TicketAssignment(tid, bpos, apos, board_idx,
                                self.net.stops[alight_idx].stop_id, delta, "matched")


def match_ticket_names(net: Network, index: TicketIndex, raw_route: str, raw_board: str, raw_alight: str):
    route_match = net.matcher.match_route(raw_route)
    route_names = [r for r in route_match.ids if r in net.trips_by_route]
    if not route_names:
        return route_match, Match("unmatched", "no_route", ()), Match("unmatched", "no_route", ()), None
    if len(route_names) > 1:
        return route_match, Match("ambiguous", "route_ambiguous", ()), Match("ambiguous", "route_ambiguous", ()), None
    route_name = route_names[0]
    board_match = net.matcher.match_stop(raw_board)
    alight_match = net.matcher.match_stop(raw_alight)
    allowed = index.route_stops[route_name]

    def narrow(m: Match) -> Match:
        if len(m.ids) <= 1:
            return m
        hits = tuple(sid for sid in m.ids if sid in allowed)
        return Match(m.label, m.method + "+route_context", hits) if hits else m

    return route_match, narrow(board_match), narrow(alight_match), route_name


def new_audit() -> collections.Counter:
    return collections.Counter()


def build_ticket_events(net: Network, csv_path: Path, conn, audit: collections.Counter) -> None:
    index = TicketIndex(net)
    event_batch: list[tuple] = []
    run_rows: list[tuple] = []
    run_tickets: dict[str, list[tuple[int, int, int]]] = collections.defaultdict(list)
    active: dict[tuple, dict] = {}
    run_seq = 0

    def store_event(t: dict, run_key: str | None) -> tuple:
        return (t["date"], t["pax"], t["vehicle"], t["route_name"], t["pattern_id"], t["trip_id"],
                run_key, t["board_idx"], t["alight_idx"], t["board_pos"], t["alight_pos"],
                t["ticket_sec"], t["match_start"], t["match_end"], t["match_route"], t["is_ac"], t["status"])

    def split_runs(date: str, vehicle: str, route_name: str, tickets: list[dict]) -> list[tuple[list[dict], str, str]]:
        tickets.sort(key=lambda t: t["ticket_sec"])
        groups: list[tuple[list[dict], str]] = []
        current: list[dict] = []
        reason = "first_ticket"
        for ticket in tickets:
            if current:
                last = current[-1]
                gap = ticket["ticket_sec"] - last["ticket_sec"]
                if gap > MAX_RUN_GAP_SEC:
                    reason = "time_gap"
                elif gap < -TIME_REGRESSION_SEC:
                    reason = "time_regression"
                elif (ticket["trip_id"] != last["trip_id"] and gap > TRIP_CHANGE_GAP_SEC):
                    reason = "different_trip_after_turnaround"
                elif ticket["board_pos"] < last["board_pos"] - 1:
                    reason = "board_position_regression"
                else:
                    reason = ""
                if reason:
                    groups.append((current, reason))
                    current = []
            current.append(ticket)
        if current:
            groups.append((current, "first_ticket"))
        return groups

    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            audit["tickets_total"] += 1
            date, ticket_sec, _ = parse_ticket_timestamp(row["created_at"])
            pax = int(float(row["passenger_count"]))
            audit["passengers_total"] += pax
            raw_vehicle = (row["vehicle_number"] or "").strip()
            has_vehicle = bool(raw_vehicle) and raw_vehicle.upper() != "NULL"
            route_match, bm, am, route_name = match_ticket_names(
                net, index, row["route"], row["start_stop_name"], row["end_stop_name"]
            )
            audit[f"route_{route_match.label}"] += 1
            audit[f"start_{bm.label}"] += 1
            audit[f"end_{am.label}"] += 1
            if bm.label in ("exact", "verified") and am.label in ("exact", "verified"):
                audit["stop_pair_confident"] += 1
                audit["pax_stop_pair_confident"] += pax
            else:
                audit["stop_pair_weak"] += 1
                audit["pax_stop_pair_weak"] += pax
            if not route_name or bm.label not in ("exact", "verified") or am.label not in ("exact", "verified"):
                audit["pax_unmatched_names"] += pax
                continue
            assignment = index.assign(route_name, bm.ids, am.ids, ticket_sec)
            audit[f"trip_{assignment.status}"] += 1
            if assignment.trip_id is None:
                audit["pax_no_trip"] += pax
                continue
            if abs(assignment.delta_sec) > HIST_TICKET_TOL_SEC:
                audit["pax_outside_tolerance"] += pax
                continue
            audit["pax_matched_trip"] += pax
            trip = net.trips[assignment.trip_id]
            ticket = {
                "date": date, "pax": pax, "vehicle": raw_vehicle if has_vehicle else None,
                "route_name": route_name, "pattern_id": trip.pattern_id, "trip_id": assignment.trip_id,
                "board_idx": assignment.board_idx, "alight_idx": assignment.alight_idx,
                "board_pos": assignment.board_pos, "alight_pos": assignment.alight_pos,
                "ticket_sec": ticket_sec, "match_start": bm.label, "match_end": am.label,
                "match_route": route_match.label, "is_ac": 1 if row["is_ac"] == "True" else 0,
                "status": "matched", "has_vehicle": has_vehicle,
            }
            if not has_vehicle:
                audit["pax_no_vehicle"] += pax
                audit["tickets_no_vehicle"] += 1
                event_batch.append(store_event(ticket, None))
            else:
                key = (date, raw_vehicle, route_name)
                active.setdefault(key, []).append(ticket)
                audit["pax_vehicle_run_matched"] += pax
                audit["tickets_vehicle_run_matched"] += 1
            if len(event_batch) >= 100000:
                conn.executemany(INSERT_EVENT, event_batch)
                event_batch.clear()

    for (date, vehicle, route_name), tickets in active.items():
        for group, reason in split_runs(date, vehicle, route_name, tickets):
            run_seq += 1
            run_key = f"{date}|{vehicle}|{route_name}|{run_seq:05d}"
            event_batch.extend(store_event(t, run_key) for t in group)
            run_rows.append((run_key, date, vehicle, route_name, group[0]["pattern_id"],
                             group[0]["trip_id"], len(group), sum(t["pax"] for t in group),
                             min(t["ticket_sec"] for t in group), reason))
            for t in group:
                run_tickets[run_key].append((t["board_pos"], t["alight_pos"], t["pax"]))
    if event_batch:
        conn.executemany(INSERT_EVENT, event_batch)
    conn.executemany(
        "INSERT OR REPLACE INTO run_info (run_key,date,vehicle,route_name,pattern_id,trip_id,n_tickets,"
        "total_pax,first_ticket_sec,split_reason) VALUES (?,?,?,?,?,?,?,?,?,?)", run_rows
    )
    writes = []
    for key, tickets in run_tickets.items():
        length = 2 + max(max(a, b) for b, a, _ in tickets)
        diff = [0] * (length + 1)
        for b, a, pax in tickets:
            diff[b] += pax
            diff[a] -= pax
        running = 0
        loads = []
        for value in diff[:-1]:
            running += value
            loads.append(running)
        writes.append((key, json.dumps(loads)))
    conn.executemany("INSERT OR REPLACE INTO run_loads VALUES (?,?)", writes)
    conn.commit()
    audit["runs_observed"] = len(run_rows)
    audit["runs_with_loads"] = len(writes)

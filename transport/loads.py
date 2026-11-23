"""Pure functions for segment loads and the commuter window rule."""

from __future__ import annotations

from typing import Iterable


def segment_loads(tickets: Iterable[tuple[int, int, float]], n_stops: int | None = None) -> list[float]:
    """Half-open intervals [board, alight): difference array then running sum.

    A passenger boarding at stop 2 and one alighting at stop 2 never share a
    stretch.  Returns load for segment s = stop s -> stop s+1.
    """
    tickets = list(tickets)
    max_pos = max((max(b, a) for b, a, _ in tickets), default=(n_stops or 1) - 1)
    n_segments = max(n_stops or 0, max_pos + 1) - 1
    if n_segments < 0:
        return []
    diff = [0.0] * (n_segments + 2)
    for board, alight, pax in tickets:
        if alight <= board:
            raise ValueError(f"alighting position {alight} must follow boarding position {board}")
        diff[board] += pax
        diff[alight] -= pax
    running = 0.0
    loads = []
    for value in diff[:-2]:
        running += value
        loads.append(running)
    return loads


def inclusive_window(target_sec: int, window_min: int = 2) -> tuple[int, int]:
    return target_sec - window_min * 60, target_sec + window_min * 60


def in_window(departure_sec: int, target_sec: int, window_min: int = 2) -> bool:
    lo, hi = inclusive_window(target_sec, window_min)
    return lo <= departure_sec <= hi


def distinct_departures(options) -> list[tuple[str, str]]:
    """A departure is a trip, not a ticket; two buses on one route count twice."""
    return sorted({(o.route_name, o.trip_id) for o in options})


def time_weighted_average(loads: list[float], seconds: list[int]) -> float:
    total = sum(seconds)
    return sum(l * d for l, d in zip(loads, seconds)) / total if total else 0.0

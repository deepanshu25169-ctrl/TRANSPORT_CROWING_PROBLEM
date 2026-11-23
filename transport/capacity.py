"""Capacity evidence and the two crowding output modes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .common import mean, median, percentile, safe_div

JBM_SOURCE = (
    "https://www.jbmgroup.com/press-release/"
    "jbm-citylife-low-floor-air-conditioned-city-buses-flagged-off-by-"
    "shri-arvind-kejriwal-honble-chief-minister-of-delhi"
)
TCQSM_SOURCE = "https://onlinepubs.trb.org/onlinepubs/tcrp/tcrp_webdoc_6-e.pdf"

CAPACITY_TABLE = [
    {
        "vehicle_model": "JBM CITYLIFE, Delhi low-floor AC cluster configuration (2020)",
        "passenger_seats": 35,
        "documented_standing_limit": None,
        "source": JBM_SOURCE,
        "source_note": "JBM press release: 'The buses have 35 seats with provision of a wheelchair.'",
        "confidence": "medium",
        "mapping": "Only when vehicle model is confirmed by an independent fleet register; "
                   "is_ac alone does not establish model or layout.",
    },
    {
        "vehicle_model": "JBM CITYLIFE, alternate Delhi configuration",
        "passenger_seats": 36,
        "documented_standing_limit": None,
        "source": "https://www.motorindiaonline.in/jbm-delivers-100-cng-citylife-buses-to-new-delhi-government",
        "source_note": "Contemporary report of 36 seats (9 reserved); conflicts with JBM's 35-seat release.",
        "confidence": "low",
        "mapping": "Do not apply without a confirmed configuration.",
    },
    {
        "vehicle_model": "TCQSM bus load factor reference (not a Delhi vehicle)",
        "passenger_seats": None,
        "documented_standing_limit": None,
        "source": TCQSM_SOURCE,
        "source_note": "TCQSM 2nd ed., Exhibit 5-14 'Passenger Load LOS': bus 1.01-1.25 p/seat is "
                       "the comfortable-standee band, 1.26-1.50 maximum schedule load. "
                       "The 1.25S benchmark is a transit-planning convention, not a Delhi legal limit.",
        "confidence": "reference_only",
        "mapping": "Used only to justify the configurable comfort multiplier.",
    },
]


@dataclass
class Capacity:
    seats: int | None
    comfortable_load: float | None
    label: str
    source: str
    confidence: str
    mode: str


def comfortable_load(seats: int, multiplier: float = 1.25, documented_cap: int | None = None) -> float:
    value = int(multiplier * seats)
    return min(value, documented_cap) if documented_cap else value


def capacity_for(vehicle_number: str | None, is_ac: int, mapping_path: Path | None,
                 multiplier: float = 1.25) -> Capacity:
    """Resolve a vehicle to capacity only when an explicit fleet mapping exists."""
    mapping = {}
    if mapping_path and mapping_path.exists():
        with mapping_path.open(encoding="utf-8") as fh:
            mapping = json.load(fh)
    model = mapping.get(vehicle_number or "")
    if model:
        for row in CAPACITY_TABLE:
            if row["vehicle_model"] == model and row["passenger_seats"]:
                seats = row["passenger_seats"]
                return Capacity(seats, comfortable_load(seats, multiplier), row["vehicle_model"],
                                row["source"], row["confidence"], "credible")
    return Capacity(None, None, "vehicle model not identified",
                    "No fleet register links ticket vehicle numbers to a seat layout. "
                    "is_ac is air conditioning, not seating capacity.", "none", "recorded_only")


def classify_credible(load: float, capacity: Capacity) -> str:
    if capacity.seats is None or capacity.comfortable_load is None:
        return "unknown"
    if load < capacity.seats:
        return "below_seated_capacity"
    if load <= capacity.comfortable_load:
        return "standing_range"
    return "crowded"


def recorded_reference(train_loads: list[float]) -> dict:
    if not train_loads:
        return {"n": 0, "lower": None, "typical": None, "higher": None}
    return {
        "n": len(train_loads),
        "lower": percentile(train_loads, 50),
        "typical": percentile(train_loads, 75),
        "higher": percentile(train_loads, 90),
    }


def classify_recorded(load: float, reference: dict) -> tuple[str, float]:
    if not reference.get("n") or reference.get("typical") is None:
        return "Limited data", float("nan")
    low, mid = reference["lower"], reference["typical"]
    if load <= low:
        return "Lower", safe_div(load, mid)
    if load <= mid:
        return "Typical", safe_div(load, mid)
    return "Higher", safe_div(load, mid)

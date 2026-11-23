"""Shared time and geometry helpers (standard library only)."""

from __future__ import annotations

import datetime as _dt
import re
from typing import Iterable

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def hms_to_sec(value: str) -> int:
    """GTFS times may exceed 24:00:00; keep raw seconds from service day start."""
    m = TIME_RE.match((value or "").strip())
    if not m:
        return -1
    h, mi, s = (int(x) if x is not None else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def sec_to_hms(sec: int) -> str:
    sign = "-" if sec < 0 else ""
    sec = abs(int(sec))
    return f"{sign}{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def clock(sec: int) -> str:
    """Wall-clock display for a service-day second (modulo 24 h)."""
    sec = int(sec) % 86400
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"


def day_offset(sec: int) -> int:
    return int(sec) // 86400


def parse_ticket_timestamp(value: str) -> tuple[str, int, str]:
    """Return (ISO date, seconds from local midnight, original offset string)."""
    text = (value or "").strip()
    dt = _dt.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    dt = dt.astimezone(IST)
    return dt.date().isoformat(), dt.hour * 3600 + dt.minute * 60 + dt.second, text[-6:]


def is_weekend(date_iso: str) -> bool:
    return _dt.date.fromisoformat(date_iso).weekday() >= 5


def time_bin(sec: int, minutes: int = 30) -> int:
    return int(sec // (minutes * 60))


def window_sec(minutes: int = 2) -> int:
    return int(minutes) * 60


def median(values: Iterable[float]) -> float:
    data = sorted(float(v) for v in values)
    if not data:
        return float("nan")
    n = len(data)
    mid = n // 2
    return data[mid] if n % 2 else (data[mid - 1] + data[mid]) / 2.0


def percentile(values: Iterable[float], pct: float) -> float:
    data = sorted(float(v) for v in values)
    if not data:
        return float("nan")
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * (pct / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(data) - 1)
    frac = pos - lo
    return data[lo] * (1 - frac) + data[hi] * frac


def mean(values: Iterable[float]) -> float:
    data = list(values)
    return sum(data) / len(data) if data else float("nan")


def safe_div(num: float, den: float) -> float:
    return num / den if den else float("nan")

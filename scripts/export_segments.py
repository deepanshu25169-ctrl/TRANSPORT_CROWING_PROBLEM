#!/usr/bin/env python3
"""Export segment-level observed loads for the demo stop pair."""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transport.gtfs import load_network  # noqa: E402

BOARD = "Tara Apartment"
ALIGHT = "Punj Sons / Govind Puri Metro Station"


def main() -> None:
    conn = sqlite3.connect(ROOT / "output" / "transport.db")
    net = load_network(conn)
    _, info = net.options(BOARD, ALIGHT, date_iso="2026-10-23")
    out = ROOT / "output" / "segments_demo.csv"
    count = 0
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["run_key", "date", "route", "pattern_id", "segment", "stop_from", "stop_to",
                         "recorded_load", "board_match", "alight_match"])
        for key, pid, loads_json, date, route in conn.execute(
            "SELECT rl.run_key,ri.pattern_id,rl.loads,ri.date,ri.route_name "
            "FROM run_loads rl JOIN run_info ri ON ri.run_key=rl.run_key "
            "WHERE ri.date<='2026-10-21'"
        ):
            pattern = net.patterns.get(pid)
            if not pattern or len(pattern.stops) <= 1:
                continue
            stops = pattern.stops
            for pos, load in enumerate(json.loads(loads_json)):
                if load <= 0 or pos >= len(stops) - 1:
                    continue
                a, b = net.stop_name(stops[pos]), net.stop_name(stops[pos + 1])
                if {BOARD, ALIGHT} & {a, b}:
                    writer.writerow([key, date, route, pid, pos, a, b, load,
                                     info.get("board_match"), info.get("alight_match")])
                    count += 1
    print(f"Wrote {count} segment rows to {out}")


if __name__ == "__main__":
    main()

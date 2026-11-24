#!/usr/bin/env python3
"""Export a compact static demo for GitHub Pages from the built DB."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transport.capacity import capacity_for  # noqa: E402
from transport.common import clock  # noqa: E402
from transport.model import analyse_journey  # noqa: E402
from transport.planner import Planner  # noqa: E402

PAIRS = [
    ("demo", "Tara Apartment", "Punj Sons / Govind Puri Metro Station"),
    ("iit", "Munirka Village (T)", "IIT Gate"),
    ("iit_reverse", "IIT Gate", "Munirka Village (T)"),
]
START = dt.date(2026, 10, 1)
DAYS = 31


def rounded(value):
    return round(value, 1) if value is not None else None


def main() -> None:
    planner = Planner(ROOT / "output" / "transport.db")
    capacity = capacity_for(None, 0, None)
    payload = {
        "generated_for": "October 2026 static GitHub Pages demo",
        "train_end": "2026-10-21",
        "reference": planner.reference,
        "note": "Compact precomputed results; full stop-by-stop and arbitrary stop pairs are in the all-pairs app.",
        "pairs": [],
    }
    for pair_id, board, alight in PAIRS:
        dates = {}
        for offset in range(DAYS):
            date = (START + dt.timedelta(days=offset)).isoformat()
            options, info = planner.net.options(board, alight, date_iso=date)
            rows = []
            for option in options:
                result = analyse_journey(planner.net, option, planner.model, capacity,
                                         planner.reference, date, None)
                runs = planner.conn.execute(
                    "SELECT count(*) FROM run_info WHERE trip_id=? AND date=?",
                    (option.trip_id, date),
                ).fetchone()[0]
                rows.append({
                    "route": option.route_name,
                    "trip": option.trip_id,
                    "dep": clock(option.dep_sec),
                    "arr": clock(option.arr_sec),
                    "dep_sec": option.dep_sec,
                    "arr_sec": option.arr_sec,
                    "board_platform": planner.net.stop_id(option.board_stop_idx),
                    "alight_platform": planner.net.stop_id(option.alight_stop_idx),
                    "load": rounded(result.avg_load),
                    "peak": rounded(result.peak_load),
                    "peak_segments": result.peak_segments,
                    "crowded_min": rounded(result.crowded_minutes),
                    "confidence": result.confidence,
                    "support": result.support,
                    "evidence": result.evidence,
                    "observed_runs": runs,
                    "crowding": [e.recorded_class for e in result.estimates],
                })
            dates[date] = {"stop_matching": info, "options": rows}
        payload["pairs"].append({"id": pair_id, "board": board, "alight": alight, "dates": dates})
        print(f"Exported {pair_id}: {sum(len(v['options']) for v in dates.values())} options")
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "data.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {docs / 'data.json'}")


if __name__ == "__main__":
    main()

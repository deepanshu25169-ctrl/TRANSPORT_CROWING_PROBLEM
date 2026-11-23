#!/usr/bin/env python3
"""Build the analysis database from the supplied CSV and GTFS feed."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transport.common import parse_ticket_timestamp  # noqa: E402
from transport.gtfs import Network, save_network  # noqa: E402
from transport.store import connect, put_audit, put_meta  # noqa: E402
from transport.tickets import build_ticket_events, new_audit  # noqa: E402


def calendar_report(net: Network, csv_path: Path) -> dict:
    first = last = None
    with csv_path.open(newline="", encoding="utf-8") as fh:
        import csv
        csv.field_size_limit(10**9)
        for row in csv.DictReader(fh):
            date, _, _ = parse_ticket_timestamp(row["created_at"])
            first = date if first is None or date < first else first
            last = date if last is None or date > last else last
    oct_start, oct_end = "2026-10-01", "2026-10-31"
    cal = net.calendar.get("1", {})
    covers = bool(cal) and cal.get("start", "9999") <= oct_start.replace("-", "") and cal.get("end", "") >= oct_end.replace("-", "")
    return {
        "ticket_date_range": [first, last],
        "gtfs_calendar": cal,
        "calendar_dates_files_present": net.feed_files.get("calendar_dates_files", 0),
        "covers_october_2026": covers,
        "note": "GTFS service_id 1 runs every day of the week from 2025-01-01 to 2040-01-01; "
                "it therefore covers October 2026. No calendar_dates.txt exceptions are supplied.",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gtfs", type=Path, required=True)
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--db", type=Path, default=ROOT / "output" / "transport.db")
    ap.add_argument("--aliases", type=Path, default=ROOT / "transport" / "aliases.json")
    args = ap.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.db.exists():
        args.db.unlink()
    conn = connect(args.db)

    started = time.time()
    print("Loading GTFS ...", flush=True)
    net = Network.from_gtfs(args.gtfs, args.aliases)
    save_network(net, conn)
    cal = calendar_report(net, args.csv)
    put_meta(conn, "feed_files", net.feed_files)
    put_meta(conn, "calendar_check", cal)
    put_meta(conn, "built_at_utc", dt.datetime.now(dt.timezone.utc).isoformat())
    print(f"  stops={len(net.stops)} routes={len(net.routes)} trips={len(net.trips)} "
          f"patterns={len(net.patterns)}", flush=True)
    print(f"  calendar covers October 2026: {cal['covers_october_2026']}", flush=True)

    print("Matching tickets and reconstructing runs ...", flush=True)
    audit = new_audit()
    build_ticket_events(net, args.csv, conn, audit)
    put_audit(conn, audit)
    for key in sorted(audit):
        print(f"  {key}: {audit[key]}", flush=True)
    print(f"Done in {time.time() - started:.1f}s -> {args.db}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()

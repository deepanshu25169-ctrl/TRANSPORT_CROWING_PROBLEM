# TRANSPORT_CROWING_PROBLEM

Crowdedness-aware Delhi bus journey planner — Phase 1. Python 3 standard library only.

## What it does

Enter a boarding stop, destination, date and departure time. It shows direct buses,
scheduled departure at *your* stop, arrival, wait/ride time, historical recorded-passenger
load on your exact directed stretch, the busiest stretch, crowded minutes, and whether a
later option looks better. It keeps a ±2-minute inclusive comparison window and a separate
“around this time” view.

**Evidence mode:** only recorded ticket demand is available (no fleet-seat mapping, no
ticket-coverage evidence), so all comfort labels are relative recorded-crowding labels,
not physical seat/standing claims.

## Exact Mac commands

From a terminal in the repository folder:

```bash
cd ~/TRANSPORT_CROWING_PROBLEM

# 1. Build the database (raw data stays in Downloads, never in Git)
python3 scripts/build.py \
  --gtfs ~/Downloads/Transport/delhi_buses_static_gtfs_v1 \
  --csv  ~/Downloads/Transport/October-Delhi.csv \
  --db   output/transport.db

# 2. Terminal query (three-option real demo)
python3 scripts/query.py --board "Tara Apartment" \
  --to "Punj Sons / Govind Puri Metro Station" \
  --date 2026-10-23 --time 08:40

# 3. Requested demo query (IIIT Delhi is absent from the supplied feed)
python3 scripts/query.py --board "Munirka Village (T)" --to "IIT Gate" \
  --date 2026-10-23 --time 08:40

# 4. JSON output for scripts
python3 scripts/query.py --board "Tara Apartment" \
  --to "Punj Sons / Govind Puri Metro Station" \
  --date 2026-10-23 --time 08:40 --json

# 5. Local web interface
python3 scripts/serve.py
# open http://localhost:8765

# 6. Validation (Oct 1-21 train, Oct 22-31 test)
python3 scripts/validate.py

# 7. Automated checks
python3 -m unittest discover -s tests -v

# 8. Export segment-level outputs for the demo stretch
python3 scripts/export_segments.py
```

No `pip install` is needed. Verified with Python 3.14.2 on macOS.

## Demo facts from the supplied data

- `Munirka Village (T) → IIT Gate`: 0 departures in 08:38–08:42 on 2026-10-23; 104 direct
  scheduled options across the whole day. “IIIT Delhi” is not a stop in either supplied
  file; “IIT Gate” is the nearest real stop name.
- Three-option demo: `Tara Apartment → Punj Sons / Govind Puri Metro Station` at 08:40 gives
  `D-042DOWN 08:38`, `463DOWN 08:38`, `469DOWN 08:40`; next later option is `425CLDOWN 08:43:36`.
- GTFS service calendar `2025-01-01 … 2040-01-01`, all seven days, matches October 2026;
  no `calendar_dates.txt` exceptions are supplied.

## Generated files (not in Git)

- `output/transport.db` — network, matched tickets, 452,076 reconstructed runs, segment loads.
- `output/validation.json` — validation metrics.
- `output/segments_demo.csv` — segment-level observed loads for the demo pair.

See `TECHNICAL_NOTE.md` for methods and `RESULTS.md` for actual validation results.

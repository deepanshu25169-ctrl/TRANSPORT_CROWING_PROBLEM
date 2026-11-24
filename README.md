# TRANSPORT_CROWING_PROBLEM

Crowdedness-aware Delhi bus journey planner — stretch-aware Phase 1 extension. Python 3 standard library only.

Current work is on local branch `improve-stretch-aware`; changes are uncommitted until final review. The live Pages URL below still serves `main` until this branch is merged.

## Live demo

All-pairs browser app: https://deepanshu25169-ctrl.github.io/TRANSPORT_CROWING_PROBLEM/
It ships a compact GTFS + historical-model index (6,862 stops, 80,357 trips) and runs the
search entirely in the browser, so any stop pair/date/time works without a server; it also
handles name parts (e.g. `munirka`) and typo aliases (e.g. `neruhu place → Nehru Place Terminal`). It shows
real boarding/alighting platform IDs, per-stretch evidence, busiest-stretch ties and explicit
`No data` rows. Loads are model-based recorded-demand estimates; no live AVL or fleet-seat
mapping. Dates after the supplied October tickets are labelled “Based on October 2026 ticket
history”. A compact precomputed 3-pair demo is at
https://deepanshu25169-ctrl.github.io/TRANSPORT_CROWING_PROBLEM/demo.html. The full Python app
below supports richer stop-by-stop and pooling output locally.

## What it does

Enter a boarding stop, destination, date and departure time. It shows direct buses,
scheduled departure at *your* stop, arrival, wait/ride time, historical recorded-passenger
load on your exact directed stretch by outgoing stop position, the busiest stretch (with
ties), high-demand minutes, and whether a later option looks better. It keeps a ±2-minute
inclusive comparison window and a separate “around this time” view. Fallbacks widen around
the same clock time and wrap midnight; weekday/weekend boundaries are checked explicitly.
No-data options are never treated as empty or least crowded.

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

# 1b. After changing the model/DB, refresh the live browser index:
python3 scripts/export_allpairs_app.py
python3 scripts/export_static_app.py

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

# Optional: only if you have a real fleet register, copy
# transport/capacity_map.example.json, replace the placeholder key with the real
# ticket vehicle number and the model with a supported CAPACITY_TABLE entry, then:
python3 scripts/query.py --board "Tara Apartment" \
  --to "Punj Sons / Govind Puri Metro Station" --date 2026-10-23 --time 08:40 \
  --capacity-map transport/capacity_map.example.json
python3 scripts/serve.py 8765 transport/capacity_map.example.json

# 6. Validation (rolling folds inside Oct 1-21 select window/shrinkage;
#    Oct 22-31 is the untouched final test, run-level splits)
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
- Curated aliases in `transport/aliases.json` are used by both the build and the query/web
  app; confident stop-pair coverage is now 80.02% (2,048,484 / 2,559,899 tickets), up from
  79.0% measured before the alias pass in Phase 1.
- Rebuilt run reconstruction produces 471,215 runs: 455,684 strict unique-trip runs and
  15,531 separately reported vehicle-timeline runs. Missing vehicles (726,242 passengers)
  and ambiguous trip assignments (16,046 passengers) are excluded, not merged.
- Final held-out MAE: 0.450 for stretch-aware, 0.444 for both simpler re-run baselines. The
  regression is reported in `RESULTS.md`; the positional answer and evidence labelling are
  the reason the stretch-aware model is retained.

## Generated files (not in Git)

- `output/transport.db` — network, matched tickets, 471,215 reconstructed runs, segment loads,
  stretch profiles and reconstruction audit.
- `output/validation.json` — rolling selection and final three-method validation metrics.
- `output/segments_demo.csv` — segment-level observed loads for the demo pair.
- `docs/network.json` — 30 MB static all-pairs browser index with 354,581 stretch profiles.
- `docs/data.json` — regenerated compact 3-pair demo data with platform IDs and stretch evidence.

See `TECHNICAL_NOTE.md` for methods and `RESULTS.md` for actual validation results.

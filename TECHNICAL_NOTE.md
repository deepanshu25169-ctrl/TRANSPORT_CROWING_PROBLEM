# Technical note

## Data actually inspected

- CSV: 2,559,899 rows, 2026-10-01 … 2026-10-31; `ticket_type_id=1` for every row and is excluded from the model. `passenger_count` sums to 2,753,606. Blank and literal `NULL` vehicle values are missing vehicle IDs, never one shared bus. `is_ac` is AC only, not a seat-layout identifier.
- GTFS: 6,862 stops, 2,574 routes, 80,357 trips, 3,462 ordered stop patterns, 2,737,136 stop-time rows. `stop_times.txt` has **no** `pickup_type`/`drop_off_type`, so all stops are treated as permitting boarding and alighting; this is documented rather than invented.
- The supplied metadata PDF describes a larger feed (10,559 stops / 2,403 routes / 89,393 trips) than the files actually supplied; the code uses the actual files.
- Service calendar `2025-01-01 … 2040-01-01`, all seven days, with no `calendar_dates.txt` exceptions. Dates after October 2026 are therefore valid scheduled-service dates and are labelled “Based on October 2026 ticket history”.

## Route network and matching

A trip's ordered `stop_sequence` is authoritative. A direct journey A→C is valid only when the same trip serves A before C; UP/DOWN suffixes are preserved. Repeated stop names, opposite-side platforms and repeated circular-route stops stay distinct by `stop_id`; the browser shows real platform IDs. If a name maps to several platform IDs, all candidates are listed rather than guessed.

Stop matching audit tiers: `exact`, `verified`, `ambiguous`, `unmatched`. “Verified” is deterministic rule-based verification, not per-row human review. `transport/aliases.json` contains manually checked spelling/abbreviation aliases; ambiguous cases with several real targets are left ambiguous. Route matching preserves UP/DOWN and never guesses a new route.

## Historical ticket matching and run reconstruction

Ticket issue time was calibrated on a 0.4% sample: 50% of closest scheduled boarding times were within 3.5 min, 90% within 16 min, 98% within 60 min. A documented **±30-minute** tolerance is used for historical tickets; this is deliberately wider than the commuter's ±2-minute rule so the preference does not silently discard history.

Each ticket is assigned to a feasible scheduled trip serving its stop pair in order. Candidate trip starts with the same absolute time error are counted and marked `ambiguous`; those tickets are kept separate and excluded from load runs. Tickets with no vehicle number are also excluded from individual runs. No ambiguous or missing-vehicle ticket is ever merged into another bus.

Runs are inferred per (date, vehicle, route), then split on direction change, pattern change, time gap (>90 min), time regression, board-position regression, or an infeasible trip sequence (overlapping/backward scheduled departure). Two evidence rules remain separate:

- `unique_trip`: one vehicle/date/route/direction with one feasible scheduled trip.
- `vehicle_timeline`: multiple feasible trips for the same vehicle/date/route/direction, accepted only when boarding order, ticket times and scheduled departures are consistent.

Coverage and passenger counts for both rules and every split reason are written to the audit table and reported in `RESULTS.md`. The added timeline rule contributes 15,531 runs; it is not blended into the stricter rule.

## Loads, zero and no evidence

For each run, `L[k,s]` uses a difference array: `+passenger_count` at boarding position, `−passenger_count` at exit, running sum in stop order, half-open `[b_i, a_i)`. A rider boarding at stop 2 and alighting at stop 3 contributes to stretch `2→3` only; a rider boarding at stop 1 and alighting at stop 10 contributes through stretch `9→10`. Stretches before the first boarding or after the last alighting are **not observed**, not zero.

A zero load inside the observed board–alight span is stored and used as evidence of no recorded ticket passengers on that stretch. A missing prediction has no sample and is returned as `No data`; it is never converted to zero and never sorted as the least-crowded option.

The journey estimate is time-weighted once: `Σ(predicted_stretch_load × travel_seconds) / Σ(travel_seconds)`, using only stretches with accepted evidence. Stretches with thin or route-only evidence are labelled and excluded from that average; they are visible in the stop-by-stop detail.

## Pooling

`pooled_average` keeps the numerator and denominator on the same date, window, direction and stretch. Individual observed departures stay visible: a 100-passenger total over 3 buses is shown as `60/25/15`, never flattened to `33.3`. When the denominator is only scheduled departures, the uncertainty is stated because which buses actually ran is unknown.

## Stretch-aware recorded-demand model

The primary key is `(exact pattern, direction encoded in pattern, weekday/weekend, local 30-minute time bin, outgoing stop position)`. One reconstructed run contributes at most one observation per stretch, so ten stretches from one bus count as one run, not ten independent buses. Profiles store median, p75, p90, distinct runs and distinct service days.

Fallback order is deliberately time-local and positional:

1. exact pattern/day/local-time/position profile;
2. same pattern/day/position, widening in 30-minute bins around the **same clock time**, wrapping across midnight; the window is shown as 30/90/150/210 minutes;
3. same pattern/day/position across all times, shown as a 1440-minute window;
4. at a weekday/weekend midnight boundary only, the adjacent day type near the same clock time;
5. route median with `route_only_no_stretch_evidence`, never treated as stretch evidence;
6. `No data`.

Thin evidence (<3 runs) is labelled `Limited data` and not used for ranking or the journey average. Browser and Python use the same profile arrays, widths, shrinkage and labels; parity was checked for five end-to-end queries.

## Window and shrinkage selection

Oct 22–31 is untouched. Two rolling folds inside Oct 1–21 (train through the 7th/test 8–14, and train through the 14th/test 15–21) choose window length and shrinkage from candidates `3, 7, 14, 21` and `0, 5, 15, 40, 100, 250, 600, 1500`. All route medians and profile statistics are refit using only data available by each cutoff; run observations never straddle a split. The selected values on this feed are 14 days and `k=250`. Final three-method results are in `RESULTS.md`, including the MAE regression.

## Capacity and output modes

Capacity table in `transport/capacity.py`: JBM's own Delhi CITYLIFE release states 35 seats (URL recorded in code); a contemporary report says 36, so the mapping confidence is medium and configuration-dependent. TCQSM Exhibit 5-14 supports `C = floor(1.25 S)` as a **planning benchmark**, not a Delhi legal limit. `is_ac` is not used as a capacity proxy. An optional JSON map accepts `{"<ticket vehicle number>": "<exact CAPACITY_TABLE vehicle_model>"}`; only a real, independently sourced register should populate it.

Because no fleet register links vehicle numbers to seat layouts and no independent coverage q exists, the app runs in `recorded_only` mode. On the rebuilt training data the recorded percentiles are p50=1, p75=2, p90=2, so the labels are Lower ≤1, Typical ≤2, Higher >2 e-ticket passengers per stretch. “High-demand minutes” means minutes whose stretch prediction is above p90. These are relative recorded-demand labels, not physical comfort; seat/standing predictions are implemented but deliberately disabled.

## Limitations

- No live AVL: earlier departures are context only; boarding recommendations use departures at or after the requested time.
- Ticket issue time is a proxy for boarding; ±30 min tolerance is a model choice, not truth.
- Ticket coverage q is unknown; vehicle numbers are often absent; ambiguous name/trip matches are excluded rather than guessed.
- The stretch-aware model is worse than simpler alternatives on aggregate MAE on the final test. It is retained for its positional answer, tie reporting and honest evidence labels, not because it wins every point-forecast metric.
- Physical comfort, total onboard load, and whether an unobserved bus ran require onboard observations, AVL or operator data.

# Technical note

## Data actually inspected

- CSV: 2,559,899 rows, 2026-10-01 … 2026-10-31; `ticket_type_id=1` for every row and is
  excluded from the model. `passenger_count` sums to 2,753,606. 48.5% of rows have
  `vehicle_number = NULL`. `is_ac` is AC only, not a seat-layout identifier.
- GTFS: 6,862 stops, 2,574 routes, 80,357 trips, 3,462 ordered stop patterns, 2,737,136
  stop-time rows. `stop_times.txt` has **no** `pickup_type`/`drop_off_type`, so all stops are
  treated as permitting boarding and alighting; this is documented rather than invented.
- The supplied metadata PDF describes a larger feed (10,559 stops / 2,403 routes /
  89,393 trips) than the files actually supplied; the code uses the actual files.
- Service calendar matches October 2026. Timetable matches are therefore valid for all
  October dates; historical ticket-to-trip matches are a separate, tolerance-based step.

## Route network and matching

A trip's ordered `stop_sequence` is authoritative. A direct journey A→C is valid only when
the same trip serves A before C; UP/DOWN suffixes are preserved, so branches and opposite
directions are not merged. Repeated stop names and opposite-side stop IDs are kept distinct
and only collapsed when route context leaves one candidate.

Stop matching audit tiers: `exact` (raw name), `verified` (documented normalisation, curated
alias support, unique target), `ambiguous` (multiple equally plausible targets or weak
fuzzy hit), `unmatched`. “Verified” is deterministic rule-based verification, not per-row
human review of 2.5M records. `transport/aliases.json` contains 15 manually checked
spelling/abbreviation aliases (e.g. `Narouji Nagar → Nauroji Nagar`, `Bhajan Pura →
Bhajanpura`, `Neruhu Place → Nehru Place Terminal`); ambiguous cases with several real
targets (e.g. `Mahipal Pur`, `Peera Garhi`) are deliberately left ambiguous rather than
guessed. The first 13 aliases raised measured confident stop-pair coverage from 76.4% to
79.0%; the two later typo aliases were added for query matching and do not change that
measured build. Route matching preserves UP/DOWN and never guesses a new route.

## Historical ticket matching

Ticket issue time was calibrated on a 0.4% sample: 50% of the closest scheduled boarding
times were within 3.5 min, 90% within 16 min, 98% within 60 min. A documented
**±30-minute** tolerance is used for historical tickets; it is deliberately wider than the
commuter's ±2-minute rule so the preference does not silently discard history. Each ticket
is assigned to a feasible scheduled trip serving its stop pair in order.

Runs are inferred per (date, vehicle, route), sorted by ticket time, splitting on long gaps
(>90 min), time regressions, a trip change with >10 min gap (turnaround), or board-position
regression. Tickets with no vehicle number are not assigned to an individual run; they are
reported as excluded from individual loads (25.6% of passengers). An unobserved scheduled
departure is labelled `unobserved`, never “empty”.

## Loads and weighting

For each run, `L[k,s]` uses a difference array: `+p_i` at boarding position, `−p_i` at exit,
running sum in stop order, half-open `[b_i, a_i)`. This includes riders who boarded before
the commuter and those who board mid-ride. The time-weighted journey average
`Σ d_s L_s / Σ d_s` is used directly; segment travel times come from the GTFS stop times,
and shared-stretch weighting is therefore applied once, not pairwise. Busiest stretch and
minutes above the crowding threshold are reported per journey.

## Pooling

Fine-bin medians are shrunk toward the route median with strength `k`, where `k` is chosen
by the same earlier-week backtest alongside window length (`SHRINKAGE_CANDIDATES`). On this
feed the backtest selected a 14-day window with `k=600`; this removes the fine-bin MAE
penalty while retaining the RMSE reduction from pattern/time information.

“100 passengers / 3 buses” is implemented as `pooled_average`: a transparent total over
the same date, window, direction, stretch and eligible departures, divided by observed
departures when available, or by scheduled departures with an explicit uncertainty flag
when only GTFS supplies the denominator. Individual loads (e.g. 60/25/15) are preserved in
`individual_recorded_passengers` and in the stop-by-stop view; the interface does not replace
them with a mean.

## Pattern learning

Keys are `(pattern, weekday/weekend, 30-minute time bin at the segment)`. Each matched run
is one observation; stored median, p75, p90, run count and distinct-day count. Fallbacks:
60-minute bin → pattern/day-type without time → route-only. Selection used earlier weeks
only (train end 2026-10-21; backtests ending 10-07 and 10-14; 10% observation sample);
14 days won (MAE 0.755 vs 0.768 for 3 days and 0.777 for 7 days). Sparse results are shown
as `Limited data` when fewer than 3 runs support a segment.

## Capacity and output modes

Capacity table in `transport/capacity.py`: JBM's own Delhi CITYLIFE release states 35 seats
(URL recorded in code); a contemporary report says 36, so the mapping confidence is medium
and configuration-dependent. TCQSM Exhibit 5-14 (2nd ed., bus 1.01–1.25 passengers/seat =
comfortable standee band) supports `C = floor(1.25 S)` as a **planning benchmark**, not a
Delhi legal limit. `is_ac` is not used as a capacity proxy. An optional JSON map
(`transport/capacity_map.example.json`) accepts `{"<ticket vehicle number>": "<exact CAPACITY_TABLE vehicle_model>"}`;
only a real, independently sourced register should populate it. Because no fleet register links
ticket vehicle numbers to models and no independent coverage q exists, the app runs in
`recorded_only` mode: Lower / Typical / Higher recorded crowding against a documented
October train-period reference (p50 / p75 / p90 of recorded segment loads; on this feed
1 / 2 / 3 e-ticket passengers, so Lower ≤1, Typical ≤2, Higher >2), with sample support.
Seat/standing predictions are
implemented but deliberately disabled. Ranking across routes compares recorded demand on
a common basis only; it does not establish physical comfort.

## Limitations

- No live AVL: earlier departures are context only; recommendations use departures at or
  after arrival unless live evidence is supplied later.
- Ticket issue time is a proxy for boarding; ±30 min tolerance is a model choice, not truth.
- Missing destinations, ambiguous stop names and NULL vehicles reduce run-level coverage.
- Physical comfort, total onboard load, and whether an unobserved bus ran require onboard
  observations, AVL or operator data.

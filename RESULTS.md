# Actual validation results

Final test: train through 2026-10-21, untouched test 2026-10-22 … 2026-10-31. Window/shrinkage were chosen only on rolling folds inside Oct 1–21 (1–7 → 8–14, 1–14 → 15–21, 10% prediction sample, run-level splits). Chosen: 14 days, `k=250`. Full output: `output/validation.json`.

## Held-out segment error (recorded passengers per stretch)

| Version / method | n stretch observations | MAE | RMSE | Prediction coverage |
|---|---:|---:|---:|---:|
| Phase 1 before this change (pattern + time, k=600) | 2,383,514 | 0.459 | 1.025 | not reported in that format |
| Re-run baseline: route median only | 2,455,236 | **0.444** | 1.014 | 99.97% |
| Re-run current method: pattern + day type + time | 2,451,915 | **0.444** | **0.998** | 99.83% |
| New: stretch-aware pattern + day + local time + position | 2,397,851 | 0.450 | 1.011 | 97.63% |

The re-run rows are comparable with each other because they use the same rebuilt database, final test dates and run-level split. The Phase 1 row is shown for continuity but its n changed because the rebuild now distinguishes observed zero load from no evidence and excludes positions outside a run's board–alight span.

**Regression stated plainly:** the stretch-aware point estimate is worse than both re-run alternatives on MAE (+0.006 versus route median and current method) and worse than the current method on RMSE (+0.013); it is 0.003 better than route median on RMSE. Its benefit is positional and diagnostic: it answers which *part* of the ride carries the observed load, identifies ties, and labels missing/thin evidence instead of silently borrowing a whole-pattern average.

## By period (final test, MAE / RMSE)

| Period | Route median | Current pattern-time | Stretch-aware |
|---|---:|---:|---:|
| Morning peak (07–10) | 0.528 / 1.157 | **0.526 / 1.127** | 0.534 / 1.154 |
| Quiet day (10–17) | 0.361 / 0.845 | **0.362 / 0.841** | 0.368 / 0.845 |
| Evening peak (17–20) | 0.452 / 1.013 | **0.453 / 1.001** | 0.456 / 1.007 |
| Night/other | 0.427 / 1.013 | 0.428 / 1.002 | 0.432 / 1.007 |
| Weekday | 0.454 / 1.034 | **0.453 / 1.014** | 0.457 / 1.025 |
| Weekend | **0.416 / 0.957** | 0.418 / 0.952 | 0.428 / 0.967 |

Improvement is not uniform. The stretch-aware model is behind the current method on every aggregate period; the trade-off was accepted only because the positional answer is the task's priority and the MAE gap is small.

## Busiest-stretch identification (untouched final test)

| Metric | Result |
|---|---:|
| Held-out runs evaluated | 156,303 |
| Top-1 exact peak position | 81.67% |
| Tie-aware correct (predicted tied peak includes an actual tied peak) | 90.50% |
| Runs with an actual tied peak | 152,266 |
| Runs with a predicted tied peak | 105,270 |

Ties are deliberately reported rather than broken arbitrarily. Most observed runs have a flat recorded-load plateau, so tie-aware accuracy is the honest headline.

## Coverage and reconstruction evidence

- Tickets: 2,559,899; passenger_count total: 2,753,606.
- Confident stop pair (exact or verified both ends): 2,048,484 tickets = **80.02%** (was 79.0% before the alias pass measured in Phase 1).
- Passengers matched to a feasible scheduled trip: 1,538,808 = **55.89%**.
- Passengers in vehicle-identified runs: 796,520 = **28.93%**; no vehicle number: 726,242 = **26.38%**, excluded from individual runs.
- Ambiguous trip assignment: 14,878 tickets / 16,046 passengers kept separate and excluded from runs, not guessed.
- Reconstructed runs: **471,215**.
  - `unique_trip`: 455,684 runs, 734,168 passengers (26.66% of all passengers), evidence = one vehicle + route + direction + date + one feasible scheduled trip.
  - `vehicle_timeline`: 15,531 runs, 62,352 passengers (2.26%), evidence = same vehicle/route/direction/date with checked boarding order, ticket times and feasible trip sequence.
- Split reasons (runs / passengers): first ticket 305,323 / 485,771; time gap 78,488 / 142,706; different trip after turnaround 59,092 / 112,327; pattern change 14,759 / 25,103; impossible trip sequence 9,020 / 20,944; board-position regression 4,533 / 9,669.

The reconstruction rule adds 15,531 runs beyond the strict one-trip rule. It is reported separately; no missing vehicle or ambiguous assignment is folded into it.

## Automated checks

`python3 -m unittest discover -s tests -v` → **22 tests, all passing**, including: half-open overlap, inclusive 08:38–08:42 window, 60/25/15 pooling, missing capacity, opposite directions, repeated trips by one vehicle, aliases, the original three-option demo, same-hour different-stretch loads, midnight fallback, weekday/weekend midnight boundary, zero versus no evidence, busy corridor peak, sparse route labelled `No data`, all-nearby-departures-left, platform IDs, and post-October history label.

Browser/Python parity was checked by loading `docs/all.js` in Node with the exported `docs/network.json` and comparing route, trip, departure, platform IDs, weighted load, peak, high-demand minutes, confidence, support, days and segment methods for five queries: demo, busy corridor, sparse route, after-October date, and misspelled stop. No mismatches were found.

## What the ticket data supports

- Relative recorded demand by exact route pattern, direction, outgoing stop position, day type and local time.
- Comparison of scheduled alternatives on identical directed stretches where evidence exists.
- Identification of busiest stretches, tied peaks, and coverage/evidence gaps.

## What it does not support

- Total onboard passengers (ticket coverage q is unknown; no citywide multiplier applied).
- Physical seat availability, standing comfort or a cross-route physical comfort ranking.
- Whether an unobserved scheduled departure actually operated.

#!/usr/bin/env python3
"""Rolling date validation for route-only, pattern-time and stretch-aware models."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transport.common import mean, percentile  # noqa: E402
from transport.gtfs import load_network  # noqa: E402
from transport.model import CANDIDATE_WINDOWS, SHRINKAGE_CANDIDATES, HistoricalModel  # noqa: E402
from transport.store import put_meta  # noqa: E402

TRAIN_END = "2026-10-21"
TEST_START = "2026-10-22"
SELECTION_SAMPLE_STEP = 10
METHODS = ("route_only", "current_pattern_time", "improved_stretch")


def bucket(sec: int) -> str:
    h = (sec // 3600) % 24
    if 7 <= h < 10:
        return "morning_peak"
    if 17 <= h < 20:
        return "evening_peak"
    if 10 <= h < 17:
        return "quiet_day"
    return "night"


def error_stats(errors: list[float]) -> dict:
    if not errors:
        return {"n": 0, "mae": None, "rmse": None}
    return {"n": len(errors), "mae": round(mean(errors), 3),
            "rmse": round(math.sqrt(mean([e * e for e in errors])), 3)}


def run_side(observations, date_from: str | None = None, date_to: str | None = None) -> set[str]:
    by_run: dict[str, list[str]] = defaultdict(list)
    for o in observations:
        if date_from and o.date < date_from:
            continue
        if date_to and o.date > date_to:
            continue
        by_run[o.run_key].append(o.date)
    return {run for run, dates in by_run.items() if not date_from or min(dates) >= date_from}


def select_fold(observations, train_end: str, test_start: str | None = None,
                test_end: str | None = None):
    train_runs = run_side(observations, date_to=train_end)
    train = [o for o in observations if o.run_key in train_runs]
    if test_start is None:
        return train, []
    test_runs = run_side(observations, date_from=test_start, date_to=test_end) - train_runs
    test = [o for o in observations if o.run_key in test_runs]
    return train, test


def prediction_for(model: HistoricalModel, method: str, o):
    if method == "route_only":
        value = model.agg.route_medians.get(o.route_name)
        return (value, 0, 0, "route_median", None, []) if value is not None else None
    if method == "current_pattern_time":
        value, profile = model.predict_legacy(o.pattern_id, o.day_type, o.time_bin, o.route_name)
        if math.isnan(value):
            return None
        return value, profile.n_runs, profile.n_days, profile.fallback, None, []
    prediction = model.predict_stretch(o.pattern_id, o.day_type, o.position,
                                       o.scheduled_sec, o.route_name)
    if prediction.load is None or not prediction.evidence_based:
        return None
    return (prediction.load, prediction.n_runs, prediction.n_days, prediction.method,
            prediction.window_minutes, [round(v, 2) for v in prediction.observed_loads[:12]])


def evaluate_fold(conn, net, observations, train_end: str, test_start: str | None,
                  test_end: str | None, window_days: int, sample_step: int = 1,
                  model: HistoricalModel | None = None, shrinkage_k: float | None = None) -> dict:
    train, test = select_fold(observations, train_end, test_start, test_end)
    train_runs = {o.run_key for o in train}
    if sample_step > 1:
        test = test[::sample_step]
    model = model or HistoricalModel(conn, net)
    if shrinkage_k is not None:
        model.shrinkage_k = float(shrinkage_k)
    model.fit(train_end, window_days, train)
    reference_values = [o.load for o in train if o.date <= train_end]
    errors = {m: [] for m in METHODS}
    by_bucket_errors = {m: defaultdict(list) for m in METHODS}
    support = {m: [] for m in METHODS}
    predictions: dict[int, tuple] = {}
    for idx, o in enumerate(test):
        for method in METHODS:
            result = prediction_for(model, method, o)
            if result is None:
                continue
            value = result[0]
            if not math.isfinite(value):
                continue
            error = abs(value - o.load)
            errors[method].append(error)
            support[method].append(result[1])
            b = bucket(o.scheduled_sec)
            by_bucket_errors[method][b].append(error)
            by_bucket_errors[method]["weekend" if o.day_type == "weekend" else "weekday"].append(error)
            if method == "improved_stretch":
                predictions[idx] = result

    runs: dict[str, list[tuple[int, object]]] = defaultdict(list)
    for idx, o in enumerate(test):
        runs[o.run_key].append((idx, o))
    peak_runs = 0
    top1_correct = 0
    tie_aware_correct = 0
    actual_ties = 0
    predicted_ties = 0
    peak_examples = []
    for run_key, items in runs.items():
        actual_peak = max(o.load for _, o in items)
        actual_positions = sorted({o.position for _, o in items if abs(o.load - actual_peak) < 1e-9})
        predicted = [(idx, predictions[idx][0]) for idx, _ in items if idx in predictions]
        if not predicted:
            continue
        predicted_peak = max(value for _, value in predicted)
        predicted_positions = sorted({test[idx].position for idx, value in predicted
                                      if abs(value - predicted_peak) < 1e-9})
        peak_runs += 1
        actual_ties += int(len(actual_positions) > 1)
        predicted_ties += int(len(predicted_positions) > 1)
        top1_correct += int(bool(predicted_positions) and predicted_positions[0] in actual_positions)
        tie_aware_correct += int(bool(set(predicted_positions) & set(actual_positions)))
        if len(peak_examples) < 6:
            peak_examples.append({
                "run": run_key, "date": items[0][1].date, "route": items[0][1].route_name,
                "actual_peak_positions": actual_positions,
                "predicted_peak_positions": predicted_positions,
                "actual_peak_load": actual_peak, "predicted_peak_load": predicted_peak,
                "tie_aware_correct": bool(set(predicted_positions) & set(actual_positions)),
            })

    method_report = {}
    for method in METHODS:
        n = len(test)
        method_report[method] = {
            **error_stats(errors[method]),
            "prediction_coverage_pct": round(100 * len(errors[method]) / n, 2) if n else 0.0,
            "median_support": round(percentile(support[method], 50), 1) if support[method] else None,
            "error_by_period": {name: error_stats(vals)
                                for name, vals in sorted(by_bucket_errors[method].items())},
        }
    return {
        "train": {"through": train_end, "runs": len(train_runs), "observations": len(train)},
        "test": {"start": test_start, "end": test_end, "runs": len({o.run_key for o in test}),
                 "observations": len(test), "sample_step": sample_step},
        "window_days": window_days,
        "reference": {"n": len(reference_values),
                      "lower": percentile(reference_values, 50) if reference_values else None,
                      "typical": percentile(reference_values, 75) if reference_values else None,
                      "higher": percentile(reference_values, 90) if reference_values else None},
        "methods": method_report,
        "busiest_stretch_identification": {
            "runs_evaluated": peak_runs,
            "top1_correct": top1_correct,
            "top1_accuracy_pct": round(100 * top1_correct / peak_runs, 2) if peak_runs else None,
            "tie_aware_correct": tie_aware_correct,
            "tie_aware_accuracy_pct": round(100 * tie_aware_correct / peak_runs, 2) if peak_runs else None,
            "actual_tie_runs": actual_ties,
            "predicted_tie_runs": predicted_ties,
            "examples": peak_examples,
        },
    }


def rule_coverage(conn) -> dict:
    audit = {r[0]: int(r[1]) for r in conn.execute("SELECT key,CAST(value AS INTEGER) FROM audit")}
    total_pax = max(1, audit.get("passengers_total", 1))
    rules = []
    for rule, runs, pax in conn.execute(
        "SELECT reconstruction_rule,count(*),sum(total_pax) FROM run_info "
        "GROUP BY reconstruction_rule ORDER BY sum(total_pax) DESC"
    ):
        rules.append({"rule": rule, "runs": runs, "passengers": pax,
                      "pct_passengers": round(100 * pax / total_pax, 2)})
    splits = [{"reason": reason, "runs": runs, "passengers": pax}
              for reason, runs, pax in conn.execute(
                  "SELECT split_reason,count(*),sum(total_pax) FROM run_info "
                  "GROUP BY split_reason ORDER BY count(*) DESC")]
    return {"audit": audit, "by_reconstruction_rule": rules, "by_split_reason": splits}


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "transport.db"
    conn = sqlite3.connect(db)
    net = load_network(conn)
    model = HistoricalModel(conn, net)
    observations = model.all_observations()

    folds = [
        ("rolling_1", "2026-10-07", "2026-10-08", "2026-10-14"),
        ("rolling_2", "2026-10-14", "2026-10-15", "2026-10-21"),
    ]
    selection = {}
    for W in CANDIDATE_WINDOWS:
        for k in SHRINKAGE_CANDIDATES:
            scores = []
            for name, train_end, test_start, test_end in folds:
                folded = evaluate_fold(conn, net, observations, train_end, test_start, test_end,
                                       W, SELECTION_SAMPLE_STEP, model, k)
                mae = folded["methods"]["improved_stretch"]["mae"]
                if mae is not None:
                    scores.append(mae)
            selection[f"{W}|{k}"] = round(mean(scores), 3) if scores else None
    best_key = min((key for key, value in selection.items() if value is not None),
                   key=lambda key: selection[key])
    chosen, chosen_k = (int(part) for part in best_key.split("|"))
    selection_report = {
        "folds": [{"name": n, "train_through": a, "test": [b, c]} for n, a, b, c in folds],
        "sample_step": SELECTION_SAMPLE_STEP,
        "mean_stretch_mae_by_window_and_shrinkage": selection,
        "selected": chosen, "selected_shrinkage": chosen_k,
        "note": "Windows and shrinkage are chosen only from Oct 1-21; Oct 22-31 is untouched.",
    }

    rolling_results = [evaluate_fold(conn, net, observations, a, b, c, chosen,
                                     model=model, shrinkage_k=chosen_k)
                       for _, a, b, c in folds]
    final_result = evaluate_fold(conn, net, observations, TRAIN_END, TEST_START,
                                 "2026-10-31", chosen, model=model, shrinkage_k=chosen_k)

    put_meta(conn, "window_report", {
        "selected": chosen, "selected_shrinkage": chosen_k,
        "rolling_mae_by_window_and_shrinkage": selection,
        "note": "Chosen with rolling splits inside Oct 1-21.",
    })

    comparison = {}
    for method in METHODS:
        vals = [final_result["methods"][method]["mae"] for method in [method]
                if final_result["methods"][method]["mae"] is not None]
        comparison[method] = {"final_test_mae": vals[0] if vals else None}
    baseline = final_result["methods"]["route_only"]["mae"]
    stretch = final_result["methods"]["improved_stretch"]["mae"]
    if baseline is not None and stretch is not None:
        comparison["improved_stretch"]["mae_delta_vs_route_only"] = round(stretch - baseline, 3)
        comparison["improved_stretch"]["mae_improvement_pct"] = round(100 * (baseline - stretch) / baseline, 2)

    result = {
        "test_policy": {
            "tuning_folds": selection_report["folds"],
            "final_test": {"train_through": TRAIN_END, "test": [TEST_START, "2026-10-31"]},
            "run_level_split": True,
            "sample_step_for_window_selection": SELECTION_SAMPLE_STEP,
        },
        "window_selection": selection_report,
        "rolling_folds": rolling_results,
        "final_test": final_result,
        "method_comparison_final_test": comparison,
        "coverage": rule_coverage(conn),
        "supported_conclusions": [
            "Relative recorded passenger demand by exact route pattern, direction, stop position, day type and local time.",
            "Comparison of scheduled alternatives using medians from the same directed stretch where evidence exists.",
            "Identification of observed high-demand stretches and tied peaks, with support counts and service days.",
        ],
        "requires_independent_observation": [
            "Total onboard passengers (ticket coverage q is unknown).",
            "Physical seats, standing availability and true comfort.",
            "Whether an unobserved scheduled bus actually ran.",
        ],
    }
    out = ROOT / "output" / "validation.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "selected_window_days": chosen,
        "selected_shrinkage": chosen_k,
        "final_test": final_result["methods"],
        "busiest_stretch_identification": final_result["busiest_stretch_identification"],
    }, indent=2))
    print(f"Written {out}")


if __name__ == "__main__":
    main()

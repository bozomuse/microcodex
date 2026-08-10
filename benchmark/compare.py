#!/usr/bin/env python3
"""Compare two result directories produced by benchmark/run.py."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


METRICS = (
    "average_rss_kib",
    "peak_rss_kib",
    "post_average_rss_kib",
    "final_median_rss_kib",
    "post_turn_slope_kib",
    "wall_seconds",
    "user_cpu_seconds",
    "system_cpu_seconds",
)


def load_results(directory: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    path = directory.expanduser().resolve() / "summary.json"
    if not path.is_file():
        raise SystemExit(f"missing benchmark summary: {path}")
    document = json.loads(path.read_text())
    return document["metadata"], document["runs"]


def median(rows: list[dict[str, object]], scenario: str, metric: str) -> float | None:
    values = [
        float(row[metric])
        for row in rows
        if row["scenario"] == scenario and row.get(metric) is not None
    ]
    return statistics.median(values) if values else None


def format_value(metric: str, value: float | None) -> str:
    if value is None:
        return "n/a"
    suffix = " KiB" if metric.endswith("_kib") else " s" if metric.endswith("seconds") else ""
    return f"{value:,.2f}{suffix}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two MicroCodex benchmark runs.")
    parser.add_argument("baseline", type=Path, help="baseline result directory")
    parser.add_argument("candidate", type=Path, help="candidate result directory")
    parser.add_argument("--csv", type=Path, help="also write the comparison as CSV")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    baseline_metadata, baseline_rows = load_results(args.baseline)
    candidate_metadata, candidate_rows = load_results(args.candidate)
    for key in ("sample_ms", "response_kib", "turns", "settle_seconds"):
        if baseline_metadata.get(key) != candidate_metadata.get(key):
            raise SystemExit(
                f"benchmark settings differ for {key}: "
                f"{baseline_metadata.get(key)!r} != {candidate_metadata.get(key)!r}"
            )
    baseline_label = str(baseline_metadata["label"])
    candidate_label = str(candidate_metadata["label"])
    baseline_scenarios = {str(row["scenario"]) for row in baseline_rows}
    candidate_scenarios = {str(row["scenario"]) for row in candidate_rows}
    scenarios = sorted(baseline_scenarios & candidate_scenarios)
    if not scenarios:
        raise SystemExit("the result directories have no scenarios in common")

    comparison: list[dict[str, object]] = []
    for scenario in scenarios:
        for metric in METRICS:
            before = median(baseline_rows, scenario, metric)
            after = median(candidate_rows, scenario, metric)
            change = None
            if before not in (None, 0) and after is not None:
                change = (after - before) * 100 / before
            comparison.append(
                {
                    "scenario": scenario,
                    "metric": metric,
                    "baseline_median": before,
                    "candidate_median": after,
                    "change_percent": change,
                }
            )

    print(f"Baseline:  {baseline_label}")
    print(f"Candidate: {candidate_label}\n")
    print("| Scenario | Metric | Baseline median | Candidate median | Change |")
    print("|---|---|---:|---:|---:|")
    for row in comparison:
        metric = str(row["metric"])
        change = row["change_percent"]
        change_text = "n/a" if change is None else f"{float(change):+.2f}%"
        print(
            f"| {row['scenario']} | {metric} | "
            f"{format_value(metric, row['baseline_median'])} | "
            f"{format_value(metric, row['candidate_median'])} | {change_text} |"
        )

    if args.csv:
        path = args.csv.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=comparison[0].keys())
            writer.writeheader()
            writer.writerows(comparison)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

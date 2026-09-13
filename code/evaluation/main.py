#!/usr/bin/env python3
"""Evaluation workflow: predict, check the output contract, score the samples.

    python3 code/evaluation/main.py [OUTPUT_PATH] [--dataset DIR]

Sample rows are only reported; they never change predictions. The exit code
reflects the output contract check alone.
"""

import argparse
import csv
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as cli  # noqa: E402  (code/main.py, not this file)
from dataset_loader import find_dataset_root, load_dataset  # noqa: E402
from engine import recommend, write_predictions  # noqa: E402
from output_schema import parse_payment_plan  # noqa: E402
from recommendation_schema import validate_predictions_file  # noqa: E402
from validation import format_issues  # noqa: E402

SAMPLE_FIELDS = (
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
)
TOLERANCE = Decimal("0.02")


def same(field: str, ours: str, theirs: str) -> bool:
    if field == "amount_safe_to_pay":
        return abs(Decimal(ours or "0") - Decimal(theirs or "0")) <= TOLERANCE
    if field == "payment_plan":
        return parse_payment_plan(ours) == parse_payment_plan(theirs)
    return (ours or "").strip() == (theirs or "").strip()


def score_samples(dataset) -> tuple[Counter, int]:
    matched: Counter = Counter()
    with (dataset.root / "sample_requests.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        ours = recommend(dataset, dataset.sample_request_by_id[row["request_id"].strip()].request)
        matched.update(f for f in SAMPLE_FIELDS if same(f, ours[f], row[f]))
    return matched, len(rows)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", nargs="?", type=Path, default=cli.DEFAULT_OUTPUT)
    parser.add_argument("--dataset", type=Path, default=None, metavar="DIR")
    args = parser.parse_args(argv)
    dataset = load_dataset(args.dataset or find_dataset_root())

    rows = write_predictions(dataset, args.output)
    issues = validate_predictions_file(args.output, dataset)
    print(f"wrote {len(rows)} rows to {args.output}\n\nstatus x method")
    pairs = Counter((r["affordability_status"], r["recommended_payment_method"]) for r in rows)
    for (status, method), count in sorted(pairs.items()):
        print(f"  {status:<24}{method:<20}{count}")

    matched, total = score_samples(dataset)
    print("\nsample scoring (report only)")
    for field in SAMPLE_FIELDS:
        print(f"  {field:<32}{matched[field]}/{total}")

    if issues:
        print(format_issues(issues, limit=cli.DEFAULT_ISSUE_LIMIT), file=sys.stderr)
    print("\nFAIL output contract" if issues else "\nOK   output contract")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(run())

#!/usr/bin/env python3
"""Buy or Wait? — command-line entry point for the data foundation.

Current behaviour: load every participant-facing dataset, validate the schemas
and referential rules the problem statement depends on, and print a compact
summary. The affordability decision engine is not implemented yet, so no
predictions are written.

    python3 code/main.py               # dataset summary + contract validation
    python3 code/main.py --validate    # same, stated explicitly
    python3 code/main.py --check-output output.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataset_loader import Dataset, find_dataset_root, load_dataset  # noqa: E402
from output_schema import (  # noqa: E402
    REQUIRED_OUTPUT_COLUMNS,
    validate_output_file,
    validate_output_row_values,
)
from validation import DatasetError, ValidationIssue, data_line, format_issues  # noqa: E402

DEFAULT_ISSUE_LIMIT = 20


def _row(label: str, value: str) -> str:
    return f"  {label:<32}{value}"


def summarise(dataset: Dataset) -> str:
    """A compact, deterministic description of what was loaded and indexed."""
    options_per_request = [
        len(dataset.options_for(request.request_id)) for request in dataset.requests
    ]
    currencies = sorted({profile.home_currency for profile in dataset.profiles})
    lines = [
        f"dataset root: {dataset.root}",
        "",
        "files",
        _row("requests.csv", f"{len(dataset.requests)} evaluation requests"),
        _row("sample_requests.csv", f"{len(dataset.sample_requests)} solved examples"),
        _row(
            "financial_profiles.csv",
            f"{len(dataset.profiles)} profiles, currencies {'/'.join(currencies)}",
        ),
        _row(
            "financial_events.csv",
            f"{len(dataset.events)} events, "
            f"{len(dataset.events_missing_amount)} blank amounts kept as None",
        ),
        _row("exchange_rates.csv", f"{len(dataset.exchange_rates)} dated rates"),
        _row(
            "request_payment_options.csv",
            f"{len(dataset.payment_options)} options, "
            f"{min(options_per_request)}-{max(options_per_request)} per request",
        ),
        _row("messages.csv", f"{len(dataset.messages)} messages"),
        _row(
            "images.csv",
            f"{len(dataset.images)} images, "
            f"{sum(1 for image in dataset.images if image.exists)} files present",
        ),
        _row(
            "output.csv (template)",
            f"{len(dataset.output_template_columns)} columns, "
            f"{len(dataset.output_template_request_ids)} rows",
        ),
        "",
        "indexes",
        _row("profile by user_id", f"{len(dataset.profile_by_user)} users"),
        _row("requests by user_id", f"{len(dataset.requests_by_user)} users"),
        _row("events by user_id", f"{len(dataset.events_by_user)} users"),
        _row("events by event_id", f"{len(dataset.event_by_id)} events"),
        _row("options by request_id", f"{len(dataset.options_by_request)} requests"),
        _row(
            "messages by user/request/event",
            f"{len(dataset.messages_by_user)}/{len(dataset.messages_by_request)}"
            f"/{len(dataset.messages_by_event)}",
        ),
        _row(
            "images by user/request/event",
            f"{len(dataset.images_by_user)}/{len(dataset.images_by_request)}"
            f"/{len(dataset.images_by_event)}",
        ),
        _row("rates by date+pair", f"{len(dataset.rate_by_date_pair)} entries"),
    ]
    return "\n".join(lines)


def check_sample_outputs(dataset: Dataset) -> list[ValidationIssue]:
    """Run the output validator over the solved examples as a self-check."""
    path = dataset.root / "sample_requests.csv"
    issues: list[ValidationIssue] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            sample = dataset.sample_request_by_id.get((row.get("request_id") or "").strip())
            if sample is None:
                continue
            issues += validate_output_row_values(
                row,
                sample.request,
                source="dataset/sample_requests.csv",
                line=data_line(index),
            )
    return issues


def run_validation(dataset_root: Path | None, output_path: Path | None, issue_limit: int) -> int:
    try:
        dataset = load_dataset(dataset_root)
    except DatasetError as error:
        print("FAIL dataset could not be loaded", file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 1

    print(summarise(dataset))
    print()

    failures: list[tuple[str, list[ValidationIssue]]] = []

    dataset_issues = list(dataset.issues)
    print(f"{'FAIL' if dataset_issues else 'OK  '} schema and referential integrity")
    if dataset_issues:
        failures.append(("dataset", dataset_issues))

    template_issues = [
        issue for issue in dataset_issues if issue.source == "dataset/output.csv"
    ]
    contract_ok = (
        dataset.output_template_columns == REQUIRED_OUTPUT_COLUMNS and not template_issues
    )
    print(
        f"{'OK  ' if contract_ok else 'FAIL'} output contract: "
        f"{len(REQUIRED_OUTPUT_COLUMNS)} required columns, one row per request "
        f"({len(dataset.requests)} requests)"
    )

    sample_issues = check_sample_outputs(dataset)
    print(
        f"{'FAIL' if sample_issues else 'OK  '} solved examples satisfy the output value "
        f"contract ({len(dataset.sample_requests)} rows)"
    )
    if sample_issues:
        failures.append(("sample_requests", sample_issues))

    if output_path is not None:
        output_issues = validate_output_file(output_path, dataset.requests)
        print(
            f"{'FAIL' if output_issues else 'OK  '} {output_path} satisfies the output contract"
        )
        if output_issues:
            failures.append((str(output_path), output_issues))

    if not failures:
        print("\nno contract violations")
        return 0

    for label, issues in failures:
        print(f"\n{label}: {len(issues)} violation(s)", file=sys.stderr)
        print(format_issues(issues, limit=issue_limit), file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Load and validate the Buy or Wait? dataset. With no arguments this "
            "prints the dataset summary and exits non-zero on contract violations."
        ),
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="validate the dataset and print a compact summary (the default action)",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        metavar="DIR",
        help="dataset directory (default: the dataset/ folder next to this repository)",
    )
    parser.add_argument(
        "--check-output",
        type=Path,
        default=None,
        metavar="PATH",
        help="additionally validate a generated output.csv against the requests",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=DEFAULT_ISSUE_LIMIT,
        metavar="N",
        help=f"maximum violations to print per source (default: {DEFAULT_ISSUE_LIMIT})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset_root = args.dataset
    if dataset_root is None:
        try:
            dataset_root = find_dataset_root()
        except DatasetError as error:
            print(str(error), file=sys.stderr)
            return 1
    return run_validation(dataset_root, args.check_output, args.max_issues)


if __name__ == "__main__":
    raise SystemExit(main())

"""Diagnostic: the engine's rows next to the 25 public worked examples.

A microscope, not a test: no assertions, no thresholds, and no production
module imports it. Rules must not be tuned to raise these counts.

    python3 tests/sample_report.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import REAL_DATASET  # noqa: E402

from dataset_loader import load_dataset  # noqa: E402
from engine import recommend  # noqa: E402
from output_schema import parse_payment_plan  # noqa: E402

FIELDS = (
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
)
TOLERANCE = Decimal("0.02")


def _same(field: str, ours: str, theirs: str) -> bool:
    if field == "amount_safe_to_pay":
        return abs(Decimal(ours or "0") - Decimal(theirs or "0")) <= TOLERANCE
    if field == "payment_plan":
        return parse_payment_plan(ours) == parse_payment_plan(theirs)
    return (ours or "").strip() == (theirs or "").strip()


def report(dataset_root: Path = REAL_DATASET) -> str:
    dataset = load_dataset(dataset_root)
    matches = dict.fromkeys(FIELDS, 0)
    lines = []
    for sample in dataset.sample_requests:
        ours = recommend(dataset, sample.request)
        theirs = {
            "amount_safe_to_pay": str(sample.amount_safe_to_pay or ""),
            "affordability_status": sample.affordability_status or "",
            "recommended_payment_method": sample.recommended_payment_method or "",
            "payment_plan": sample.payment_plan or "",
            "earliest_date_for_full_payment": (
                sample.earliest_date_for_full_payment.isoformat()
                if sample.earliest_date_for_full_payment
                else ""
            ),
            "spending_changes_needed": sample.spending_changes_needed or "",
        }
        wrong = []
        for field in FIELDS:
            if _same(field, ours[field], theirs[field]):
                matches[field] += 1
            else:
                wrong.append(field)
        if wrong:
            lines.append(f"{sample.request_id}")
            for field in wrong:
                lines.append(f"  {field:<32} ours={ours[field]!r:<44} example={theirs[field]!r}")
            lines.append(f"  ours: {ours['decision_explanation']}")
    total = len(dataset.sample_requests)
    header = [f"{field:<32} {matches[field]}/{total}" for field in FIELDS]
    return "\n".join(header + [""] + lines)


if __name__ == "__main__":
    print(report())

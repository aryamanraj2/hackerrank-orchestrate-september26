"""Shared test helpers: import bootstrap and a tiny synthetic dataset builder."""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = REPO_ROOT / "code"
REAL_DATASET = REPO_ROOT / "dataset"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

# A one-pixel PNG, enough to make an image file "present" on disk.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

OUTPUT_HEADER = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)


def default_tables() -> dict[str, list[dict[str, str]]]:
    """A minimal dataset that satisfies every contract rule."""
    return {
        "financial_profiles": [
            {
                "user_id": "user_a",
                "home_currency": "ZAR",
                "current_available_balance": "1000",
                "minimum_balance_to_keep": "200",
                "financial_priorities": "education",
                "expense_categories_to_protect": "rent|groceries",
                "expense_categories_user_is_willing_to_reduce": "dining",
                "expense_categories_user_is_willing_to_stop": "",
                "payment_methods_user_will_consider": "full_payment|installments",
                "max_installment_months": "6",
            }
        ],
        "requests": [
            {
                "request_id": "request_a",
                "user_id": "user_a",
                "request_date": "2026-01-05",
                "request_type": "purchase",
                "requested_amount": "500",
                "desired_completion_date": "2026-02-05",
                "allows_partial_payment": "true",
                "request_text": "Can I afford this?",
            }
        ],
        "sample_requests": [
            {
                "request_id": "sample_a",
                "user_id": "user_a",
                "request_date": "2026-01-05",
                "request_type": "travel",
                "requested_amount": "400",
                "desired_completion_date": "2026-03-05",
                "allows_partial_payment": "false",
                "request_text": "Can I afford the trip?",
                "amount_safe_to_pay": "400",
                "affordability_status": "affordable_now",
                "recommended_payment_method": "full_payment",
                "payment_plan": "2026-01-05:400",
                "earliest_date_for_full_payment": "2026-01-05",
                "spending_changes_needed": "none",
                "decision_explanation": "Balance stays above the minimum.",
            }
        ],
        "financial_events": [
            {
                "event_id": "event_a",
                "user_id": "user_a",
                "event_type": "expense",
                "description": "Apartment rent",
                "category": "rent",
                "direction": "debit",
                "amount": "300",
                "currency": "ZAR",
                "event_date": "2025-12-02",
                "settlement_date": "2025-12-02",
                "status": "settled",
                "linked_event_id": "",
                "flexibility": "fixed",
                "minimum_allowed_amount": "",
            },
            {
                "event_id": "event_b",
                "user_id": "user_a",
                "event_type": "expense",
                "description": "Utility bill with amount in the linked image",
                "category": "utilities",
                "direction": "debit",
                "amount": "",
                "currency": "ZAR",
                "event_date": "2026-01-03",
                "settlement_date": "2026-01-03",
                "status": "settled",
                "linked_event_id": "event_a",
                "flexibility": "fixed",
                "minimum_allowed_amount": "",
            },
        ],
        "exchange_rates": [
            {
                "rate_date": "2026-01-05",
                "from_currency": "EUR",
                "to_currency": "ZAR",
                "rate": "20",
            }
        ],
        "request_payment_options": [
            {
                "payment_option_id": "payment_option_01",
                "request_id": "request_a",
                "payment_method": "full_payment",
                "payment_amount": "500",
                "number_of_payments": "1",
                "first_payment_date": "2026-01-05",
                "payment_frequency_days": "",
                "financing_fee": "0",
                "total_payable_amount": "500",
            },
            {
                "payment_option_id": "payment_option_02",
                "request_id": "request_a",
                "payment_method": "installments",
                "payment_amount": "175",
                "number_of_payments": "3",
                "first_payment_date": "2026-01-08",
                "payment_frequency_days": "30",
                "financing_fee": "25",
                "total_payable_amount": "525",
            },
        ],
        "messages": [
            {
                "message_id": "message_a",
                "user_id": "user_a",
                "request_id": "request_a",
                "related_event_id": "",
                "sent_at": "2026-01-04T09:30:00Z",
                "source_type": "employer",
                "message_text": "Your salary is confirmed.",
            }
        ],
        "images": [
            {
                "image_id": "image_a",
                "user_id": "user_a",
                "request_id": "request_a",
                "related_event_id": "event_b",
            }
        ],
    }


COLUMNS: Mapping[str, Sequence[str]] = {
    name: tuple(rows[0].keys()) for name, rows in default_tables().items()
}


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_dataset(
    base: Path,
    *,
    tables: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
    output_header: Sequence[str] = OUTPUT_HEADER,
    output_request_ids: Sequence[str] | None = None,
    image_files: Sequence[str] | None = None,
) -> Path:
    """Write a synthetic ``dataset/`` tree under ``base`` and return its path."""
    data = default_tables()
    for name, rows in (tables or {}).items():
        data[name] = [dict(row) for row in rows]

    dataset_dir = base / "dataset"
    for name, rows in data.items():
        columns = list(COLUMNS[name])
        for row in rows:
            for column in row:
                if column not in columns:
                    columns.append(column)
        write_csv(dataset_dir / f"{name}.csv", columns, rows)

    if output_request_ids is None:
        output_request_ids = [row["request_id"] for row in data["requests"]]
    write_csv(
        dataset_dir / "output.csv",
        output_header,
        [{output_header[0]: request_id} for request_id in output_request_ids],
    )

    images_dir = dataset_dir / "media" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    names = (
        image_files
        if image_files is not None
        else [row["image_id"] for row in data["images"]]
    )
    for image_id in names:
        (images_dir / f"{image_id}.png").write_bytes(PNG_BYTES)
    return dataset_dir

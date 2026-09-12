"""Reusable validator for the Buy or Wait? ``output.csv`` contract.

The column set, column order and one-row-per-request rule are checked here so
both the blank template and any generated predictions go through the same
code. Value checks are structural only: they enforce the allowed values and
plan formats from the problem statement, never a particular financial answer.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence

from validation import DatasetError, ValidationIssue, data_line

REQUIRED_OUTPUT_COLUMNS: tuple[str, ...] = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)

AFFORDABILITY_STATUSES = frozenset(
    {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
)

PAYMENT_METHODS = frozenset(
    {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
)

NO_PAYMENT_PLAN = "none"
NO_SPENDING_CHANGES = "none"
MAX_SPENDING_CHANGES = 3


# --------------------------------------------------------------------------
# Column and row-coverage contract
# --------------------------------------------------------------------------


def validate_output_columns(
    columns: Sequence[str], *, source: str = "output.csv"
) -> list[ValidationIssue]:
    """Require the eight required columns, in the required order."""
    found = tuple(column.strip() for column in columns)
    if found == REQUIRED_OUTPUT_COLUMNS:
        return []
    issues: list[ValidationIssue] = []
    missing = [column for column in REQUIRED_OUTPUT_COLUMNS if column not in found]
    unexpected = [column for column in found if column not in REQUIRED_OUTPUT_COLUMNS]
    if missing:
        issues.append(
            ValidationIssue(
                source, f"missing required column(s): {', '.join(missing)}", line=1
            )
        )
    if unexpected:
        issues.append(
            ValidationIssue(
                source, f"unexpected column(s): {', '.join(unexpected)}", line=1
            )
        )
    if not missing and not unexpected:
        issues.append(
            ValidationIssue(
                source,
                "columns are in the wrong order; expected "
                f"{', '.join(REQUIRED_OUTPUT_COLUMNS)} but found {', '.join(found)}",
                line=1,
            )
        )
    return issues


def validate_output_request_ids(
    found_ids: Sequence[str],
    expected_ids: Sequence[str],
    *,
    source: str = "output.csv",
    require_same_order: bool = True,
) -> list[ValidationIssue]:
    """Require exactly one row per evaluation request, in the dataset's order."""
    issues: list[ValidationIssue] = []
    seen: dict[str, int] = {}
    expected = set(expected_ids)
    for index, request_id in enumerate(found_ids):
        line = data_line(index)
        if request_id in seen:
            issues.append(
                ValidationIssue(
                    source,
                    f"duplicate row for request, first seen on line {seen[request_id]}",
                    line=line,
                    identifier=request_id,
                )
            )
            continue
        seen[request_id] = line
        if request_id not in expected:
            issues.append(
                ValidationIssue(
                    source,
                    "row is not a request_id present in dataset/requests.csv",
                    line=line,
                    identifier=request_id,
                )
            )
    for request_id in expected_ids:
        if request_id not in seen:
            issues.append(
                ValidationIssue(source, "no output row for this request", identifier=request_id)
            )
    if (
        require_same_order
        and not issues
        and tuple(found_ids) != tuple(expected_ids)
    ):
        issues.append(
            ValidationIssue(
                source,
                "rows are not in the same order as dataset/requests.csv",
            )
        )
    return issues


# --------------------------------------------------------------------------
# Field-level structural checks
# --------------------------------------------------------------------------


def _decimal(value: str) -> Decimal | None:
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_payment_plan(value: str) -> tuple[tuple[date, Decimal], ...]:
    """Parse ``YYYY-MM-DD:amount|...`` into dated payments.

    Returns an empty tuple for ``none``. Raises :class:`ValueError` when the
    format, a date or an amount is invalid.
    """
    text = (value or "").strip()
    if text == "" or text == NO_PAYMENT_PLAN:
        return ()
    payments: list[tuple[date, Decimal]] = []
    for entry in text.split("|"):
        part = entry.strip()
        if part.count(":") != 1:
            raise ValueError(f"payment_plan entry is not 'YYYY-MM-DD:amount': {part!r}")
        day, amount_text = part.split(":")
        try:
            when = date.fromisoformat(day.strip())
        except ValueError as exc:
            raise ValueError(f"payment_plan entry has an invalid date: {part!r}") from exc
        amount = _decimal(amount_text)
        if amount is None:
            raise ValueError(f"payment_plan entry has an invalid amount: {part!r}")
        payments.append((when, amount))
    return tuple(payments)


def parse_spending_changes(value: str) -> tuple[tuple[str, str, Decimal | None], ...]:
    """Parse ``stop:<event_id>`` / ``reduce_to:<event_id>:<amount>`` actions.

    Returns ``(action, event_id, new_amount)`` triples, empty for ``none``.
    """
    text = (value or "").strip()
    if text == "" or text == NO_SPENDING_CHANGES:
        return ()
    changes: list[tuple[str, str, Decimal | None]] = []
    for entry in text.split("|"):
        part = entry.strip()
        fields = part.split(":")
        if fields[0] == "stop" and len(fields) == 2 and fields[1]:
            changes.append(("stop", fields[1], None))
        elif fields[0] == "reduce_to" and len(fields) == 3 and fields[1]:
            amount = _decimal(fields[2])
            if amount is None:
                raise ValueError(f"reduce_to has an invalid amount: {part!r}")
            changes.append(("reduce_to", fields[1], amount))
        else:
            raise ValueError(
                f"spending change is not 'stop:<event_id>' or "
                f"'reduce_to:<event_id>:<amount>': {part!r}"
            )
    return tuple(changes)


def validate_output_row_values(
    row: Mapping[str, str],
    request,
    *,
    source: str = "output.csv",
    line: int | None = None,
) -> list[ValidationIssue]:
    """Check one prediction row against the structural output contract.

    ``request`` is a :class:`dataset_loader.Request`; it supplies the bounds
    (``requested_amount``, ``request_date``, ``desired_completion_date``,
    ``allows_partial_payment``) the contract is expressed against.
    """
    issues: list[ValidationIssue] = []
    request_id = (row.get("request_id") or "").strip()

    def add(message: str, column: str | None = None) -> None:
        issues.append(
            ValidationIssue(source, message, line=line, identifier=request_id, column=column)
        )

    amount = _decimal(row.get("amount_safe_to_pay", ""))
    if amount is None:
        add("amount_safe_to_pay is blank or not a number", "amount_safe_to_pay")
    elif amount < 0 or amount > request.requested_amount:
        add(
            f"amount_safe_to_pay {amount} is outside 0..{request.requested_amount}",
            "amount_safe_to_pay",
        )

    status = (row.get("affordability_status") or "").strip()
    if status not in AFFORDABILITY_STATUSES:
        add(f"affordability_status is not an allowed value: {status!r}", "affordability_status")

    method = (row.get("recommended_payment_method") or "").strip()
    if method not in PAYMENT_METHODS:
        add(
            f"recommended_payment_method is not an allowed value: {method!r}",
            "recommended_payment_method",
        )

    payments: tuple[tuple[date, Decimal], ...] = ()
    try:
        payments = parse_payment_plan(row.get("payment_plan", ""))
    except ValueError as exc:
        add(str(exc), "payment_plan")
    else:
        dates = [when for when, _ in payments]
        if dates != sorted(dates):
            add("payment_plan is not in chronological order", "payment_plan")

    earliest_text = (row.get("earliest_date_for_full_payment") or "").strip()
    earliest: date | None = None
    if earliest_text:
        try:
            earliest = date.fromisoformat(earliest_text)
        except ValueError:
            add(
                f"earliest_date_for_full_payment is not an ISO date: {earliest_text!r}",
                "earliest_date_for_full_payment",
            )
    if status == "affordable_now" and earliest != request.request_date:
        add(
            "affordable_now requires earliest_date_for_full_payment == "
            f"request_date ({request.request_date.isoformat()})",
            "earliest_date_for_full_payment",
        )

    if method == "partial_payment":
        if status != "affordable_with_plan":
            add(
                "partial_payment requires affordability_status 'affordable_with_plan'",
                "affordability_status",
            )
        if not request.allows_partial_payment:
            add(
                "partial_payment recommended but the request does not allow partial payment",
                "recommended_payment_method",
            )
        if len(payments) != 2:
            add(
                f"partial_payment requires exactly two payments, found {len(payments)}",
                "payment_plan",
            )
        else:
            (first_date, first_amount), (second_date, second_amount) = payments
            if first_date != request.request_date:
                add(
                    "first partial payment must fall on request_date "
                    f"({request.request_date.isoformat()})",
                    "payment_plan",
                )
            if amount is not None and first_amount != amount:
                add(
                    "first partial payment must equal amount_safe_to_pay", "payment_plan"
                )
            if first_amount + second_amount != request.requested_amount:
                add(
                    "partial payments must add up to requested_amount "
                    f"({request.requested_amount})",
                    "payment_plan",
                )
            if earliest is not None and second_date != earliest:
                add(
                    "second partial payment must fall on earliest_date_for_full_payment",
                    "payment_plan",
                )
            if second_date > request.desired_completion_date:
                add(
                    "second partial payment falls after desired_completion_date "
                    f"({request.desired_completion_date.isoformat()})",
                    "payment_plan",
                )
        if amount is not None and not (Decimal(0) < amount < request.requested_amount):
            add(
                "partial_payment requires 0 < amount_safe_to_pay < requested_amount",
                "amount_safe_to_pay",
            )

    try:
        changes = parse_spending_changes(row.get("spending_changes_needed", ""))
    except ValueError as exc:
        add(str(exc), "spending_changes_needed")
    else:
        if len(changes) > MAX_SPENDING_CHANGES:
            add(
                f"at most {MAX_SPENDING_CHANGES} spending changes are allowed, "
                f"found {len(changes)}",
                "spending_changes_needed",
            )
        stopped = {event_id for action, event_id, _ in changes if action == "stop"}
        reduced = {event_id for action, event_id, _ in changes if action == "reduce_to"}
        for event_id in sorted(stopped & reduced):
            add(
                f"event {event_id} is both stopped and reduced; the actions are "
                "mutually exclusive",
                "spending_changes_needed",
            )

    if not (row.get("decision_explanation") or "").strip():
        add("decision_explanation is blank", "decision_explanation")

    return issues


# --------------------------------------------------------------------------
# Whole-file validation
# --------------------------------------------------------------------------


def validate_output_rows(
    columns: Sequence[str],
    rows: Sequence[Mapping[str, str]],
    requests: Sequence,
    *,
    source: str = "output.csv",
    check_values: bool = True,
    require_same_order: bool = True,
) -> list[ValidationIssue]:
    """Validate columns, per-request coverage and (optionally) row values."""
    issues = validate_output_columns(columns, source=source)
    expected_ids = [request.request_id for request in requests]
    found_ids = [(row.get("request_id") or "").strip() for row in rows]
    issues += validate_output_request_ids(
        found_ids, expected_ids, source=source, require_same_order=require_same_order
    )
    if check_values:
        request_by_id = {request.request_id: request for request in requests}
        for index, row in enumerate(rows):
            request = request_by_id.get((row.get("request_id") or "").strip())
            if request is None:
                continue
            issues += validate_output_row_values(
                row, request, source=source, line=data_line(index)
            )
    return issues


def validate_output_file(
    path: Path | str,
    requests: Sequence,
    *,
    check_values: bool = True,
    require_same_order: bool = True,
    strict: bool = False,
) -> list[ValidationIssue]:
    """Validate an ``output.csv`` on disk against the evaluation requests."""
    file_path = Path(path)
    source = str(path)
    if not file_path.is_file():
        issues = [ValidationIssue(source, "file is missing")]
    else:
        with file_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns = tuple(reader.fieldnames or ())
            rows = [dict(row) for row in reader]
        issues = validate_output_rows(
            columns,
            rows,
            requests,
            source=source,
            check_values=check_values,
            require_same_order=require_same_order,
        )
    if strict and issues:
        raise DatasetError(issues, f"{source} does not satisfy the output contract")
    return issues

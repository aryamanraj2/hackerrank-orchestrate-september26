"""Semantic validation of a recommendation against the dataset it was made on.

:mod:`output_schema` checks a prediction row in isolation: columns, allowed
values, plan format, bounds. This layer adds the checks that need the user's
profile, the request's supplied payment options and the user's financial
events:

* the plan actually matches the recommended method — one full payment on
  `request_date` for ``full_payment``, one on `earliest_date_for_full_payment`
  for ``wait``, and a schedule identical to a supplied option for
  ``installments``, so no paid recommendation can carry an empty plan;
* the method is one the user accepts, within `max_installment_months`;
* spending changes target a live, flexible, permitted expense whose
  recurrence is supported by settled history (see :mod:`recurrence`).

It imports :mod:`dataset_loader`, which imports :mod:`output_schema`, so the
dependency runs one way only and the structural validator stays importable on
its own.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from dataset_loader import Dataset, FinancialEvent, FinancialProfile, PaymentOption, Request
from output_schema import (
    NO_PAYMENT_PLAN,
    parse_payment_plan,
    parse_spending_changes,
    validate_output_rows,
)
from recurrence import MIN_OCCURRENCES, recurrence_for_event
from validation import DatasetError, ValidationIssue, data_line

#: Methods that commit the user to paying now, so the user must accept them.
IMMEDIATE_METHODS = frozenset({"full_payment", "partial_payment", "installments"})

#: Waiting only makes sense when the user would take the later full payment.
WAIT_REQUIRES_METHOD = "full_payment"

#: A change can only alter a commitment that is still live.
CHANGEABLE_STATUSES = frozenset({"settled", "pending", "scheduled"})


def _matching_installment_options(
    options: Sequence[PaymentOption], payments: Sequence[tuple]
) -> list[PaymentOption]:
    return [
        option
        for option in options
        if option.payment_method == "installments"
        and option.schedule() == tuple(payments)
    ]


def _validate_single_full_payment(
    payments: Sequence[tuple],
    request: Request,
    expected_date,
    label: str,
    date_column: str,
    add,
) -> None:
    """Require exactly one payment of the whole request on ``expected_date``."""
    if len(payments) != 1:
        add(
            f"{label} requires exactly one payment of the full requested amount, "
            f"found {len(payments)}",
            "payment_plan",
        )
        return
    when, amount = payments[0]
    if amount != request.requested_amount:
        add(
            f"{label} must pay the full requested_amount "
            f"({request.requested_amount}), found {amount}",
            "payment_plan",
        )
    if expected_date is None:
        add(
            f"{label} requires a valid {date_column} to pay on",
            date_column,
        )
    elif when != expected_date:
        add(
            f"{label} must pay on {expected_date.isoformat()}, found {when.isoformat()}",
            "payment_plan",
        )


def _validate_installments(
    payments: Sequence[tuple],
    request: Request,
    profile: FinancialProfile,
    options: Sequence[PaymentOption],
    add,
) -> None:
    if not payments:
        add(
            "installments requires a non-empty payment plan matching a supplied "
            "installment option",
            "payment_plan",
        )
        return
    matches = _matching_installment_options(options, payments)
    if not matches:
        supplied = ", ".join(
            option.payment_option_id
            for option in options
            if option.payment_method == "installments"
        )
        add(
            "installment plan does not exactly match any supplied installment "
            f"option for {request.request_id} (options: {supplied or 'none'})",
            "payment_plan",
        )
        return
    option = matches[0]
    if profile.max_installment_months is None:
        add(
            "installments recommended but max_installment_months is blank, so the "
            "user will not consider installments",
            "recommended_payment_method",
        )
    elif option.number_of_payments > profile.max_installment_months:
        add(
            f"{option.payment_option_id} spreads the request over "
            f"{option.number_of_payments} payments, above the user's "
            f"max_installment_months of {profile.max_installment_months}",
            "payment_plan",
        )


def _validate_spending_change(
    action: str,
    event_id: str,
    new_amount: Decimal | None,
    request: Request,
    profile: FinancialProfile,
    dataset: Dataset,
    add,
) -> None:
    event: FinancialEvent | None = dataset.event_by_id.get(event_id)
    if event is None:
        add(
            f"spending change targets {event_id}, which is not in financial_events.csv",
            "spending_changes_needed",
        )
        return
    if event.user_id != request.user_id:
        add(
            f"spending change targets {event_id}, which belongs to {event.user_id} "
            f"and not to {request.user_id}",
            "spending_changes_needed",
        )
        return
    if event.direction != "debit":
        add(
            f"spending change targets {event_id}, a {event.direction} event; only "
            "debits can be stopped or reduced",
            "spending_changes_needed",
        )
    if event.status not in CHANGEABLE_STATUSES:
        add(
            f"{event_id} has status '{event.status}' and is not a live commitment "
            "that can be changed",
            "spending_changes_needed",
        )
    pattern = recurrence_for_event(
        event, dataset.events_for(event.user_id), as_of=request.request_date
    )
    if pattern is None:
        add(
            f"{event_id} is a one-off expense: category '{event.category}' has no "
            f"recurring debit history on or before {request.request_date.isoformat()} "
            f"(needs at least {MIN_OCCURRENCES} settled events with a consistent "
            "cadence)",
            "spending_changes_needed",
        )
    if event.category in profile.expense_categories_to_protect:
        add(
            f"{event_id} is in protected category '{event.category}'",
            "spending_changes_needed",
        )
    if action == "stop":
        if not event.is_stoppable:
            add(
                f"{event_id} has flexibility '{event.flexibility}' and cannot be stopped",
                "spending_changes_needed",
            )
        if event.category not in profile.expense_categories_user_is_willing_to_stop:
            add(
                f"the user will not stop category '{event.category}' "
                f"(willing: {'|'.join(profile.expense_categories_user_is_willing_to_stop) or 'none'})",
                "spending_changes_needed",
            )
        return

    if not event.is_reducible:
        add(
            f"{event_id} has flexibility '{event.flexibility}' and cannot be reduced",
            "spending_changes_needed",
        )
    if event.category not in profile.expense_categories_user_is_willing_to_reduce:
        add(
            f"the user will not reduce category '{event.category}' "
            f"(willing: {'|'.join(profile.expense_categories_user_is_willing_to_reduce) or 'none'})",
            "spending_changes_needed",
        )
    if new_amount is None:
        return
    if event.minimum_allowed_amount is not None and new_amount < event.minimum_allowed_amount:
        add(
            f"reduce_to {new_amount} for {event_id} is below its "
            f"minimum_allowed_amount of {event.minimum_allowed_amount}",
            "spending_changes_needed",
        )
    if event.amount is not None and new_amount >= event.amount:
        add(
            f"reduce_to {new_amount} for {event_id} is not a reduction from its "
            f"current amount of {event.amount}",
            "spending_changes_needed",
        )


def validate_recommendation(
    row: Mapping[str, str],
    request: Request,
    dataset: Dataset,
    *,
    source: str = "output.csv",
    line: int | None = None,
) -> list[ValidationIssue]:
    """Check one recommendation against the user's profile, options and events."""
    issues: list[ValidationIssue] = []
    request_id = (row.get("request_id") or "").strip()

    def add(message: str, column: str | None = None) -> None:
        issues.append(
            ValidationIssue(source, message, line=line, identifier=request_id, column=column)
        )

    profile = dataset.profile_by_user.get(request.user_id)
    if profile is None:
        add(f"no financial profile for {request.user_id}")
        return issues

    method = (row.get("recommended_payment_method") or "").strip()
    plan_text = (row.get("payment_plan") or "").strip()
    earliest_text = (row.get("earliest_date_for_full_payment") or "").strip()
    earliest: date | None = None
    if earliest_text:
        try:
            earliest = date.fromisoformat(earliest_text)
        except ValueError:
            earliest = None  # the structural validator reports the bad date

    plan_is_parsable = True
    try:
        payments = parse_payment_plan(plan_text)
    except ValueError:
        payments = ()  # the structural validator already reports the format error
        plan_is_parsable = False

    accepted = profile.payment_methods_user_will_consider
    if method in IMMEDIATE_METHODS and method not in accepted:
        add(
            f"{method} is not in the user's payment_methods_user_will_consider "
            f"({'|'.join(accepted) or 'none'})",
            "recommended_payment_method",
        )

    if method == "wait" and WAIT_REQUIRES_METHOD not in accepted:
        add(
            "wait requires the user to accept full_payment "
            f"({'|'.join(accepted) or 'none'})",
            "recommended_payment_method",
        )

    if plan_is_parsable:
        if method == "full_payment":
            _validate_single_full_payment(
                payments,
                request,
                request.request_date,
                "full_payment",
                "request_date",
                add,
            )
        elif method == "installments":
            _validate_installments(
                payments, request, profile, dataset.options_for(request.request_id), add
            )
        elif method == "wait":
            _validate_single_full_payment(
                payments,
                request,
                earliest,
                "wait",
                "earliest_date_for_full_payment",
                add,
            )
            if earliest is not None and earliest <= request.request_date:
                add(
                    "wait requires earliest_date_for_full_payment to be later than "
                    f"request_date ({request.request_date.isoformat()}), found "
                    f"{earliest.isoformat()}",
                    "earliest_date_for_full_payment",
                )

    if method == "not_recommended":
        if plan_text != NO_PAYMENT_PLAN:
            add(
                f"not_recommended requires payment_plan '{NO_PAYMENT_PLAN}', "
                f"found {plan_text!r}",
                "payment_plan",
            )
        if earliest_text:
            add(
                "not_recommended requires an empty earliest_date_for_full_payment, "
                f"found {earliest_text!r}",
                "earliest_date_for_full_payment",
            )

    try:
        changes = parse_spending_changes(row.get("spending_changes_needed", ""))
    except ValueError:
        changes = ()  # already reported structurally
    for action, event_id, new_amount in changes:
        _validate_spending_change(
            action, event_id, new_amount, request, profile, dataset, add
        )

    return issues


def validate_recommendations(
    columns: Sequence[str],
    rows: Sequence[Mapping[str, str]],
    dataset: Dataset,
    *,
    requests: Sequence[Request] | None = None,
    source: str = "output.csv",
    check_structure: bool = True,
    require_same_order: bool = True,
) -> list[ValidationIssue]:
    """Validate predictions structurally and then semantically."""
    target_requests = list(requests if requests is not None else dataset.requests)
    issues: list[ValidationIssue] = []
    if check_structure:
        issues += validate_output_rows(
            columns,
            rows,
            target_requests,
            source=source,
            check_values=True,
            require_same_order=require_same_order,
        )
    request_by_id = {request.request_id: request for request in target_requests}
    for index, row in enumerate(rows):
        request = request_by_id.get((row.get("request_id") or "").strip())
        if request is None:
            continue
        issues += validate_recommendation(
            row, request, dataset, source=source, line=data_line(index)
        )
    return issues


def validate_predictions_file(
    path: Path | str,
    dataset: Dataset,
    *,
    requests: Sequence[Request] | None = None,
    source: str | None = None,
    check_structure: bool = True,
    require_same_order: bool = True,
    strict: bool = False,
) -> list[ValidationIssue]:
    """Validate an output CSV on disk, structurally and semantically."""
    file_path = Path(path)
    label = source or str(path)
    if not file_path.is_file():
        issues = [ValidationIssue(label, "file is missing")]
    else:
        with file_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns = tuple(reader.fieldnames or ())
            rows = [dict(row) for row in reader]
        issues = validate_recommendations(
            columns,
            rows,
            dataset,
            requests=requests,
            source=label,
            check_structure=check_structure,
            require_same_order=require_same_order,
        )
    if strict and issues:
        raise DatasetError(issues, f"{label} does not satisfy the recommendation contract")
    return issues

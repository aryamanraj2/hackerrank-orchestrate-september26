"""Recommendation engine: turn a baseline forecast into one output row.

Every candidate plan is checked by :func:`simulate`, which books the plan's
payments as debits into the forecast ledger and walks every balance
checkpoint: the opening balance on the request date and the balance after
each movement, ordered by :func:`cashflow.same_day_order` (credits before
debits within a day). A plan is safe
only when no checkpoint falls below ``minimum_balance_to_keep``.

Spending changes are proposed only when no plan completes by the deadline
without them (see :func:`_plan_with_changes`).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Sequence

from cashflow import forecast_for_request, same_day_order
from output_schema import (
    MAX_SPENDING_CHANGES,
    NO_PAYMENT_PLAN,
    NO_SPENDING_CHANGES,
    REQUIRED_OUTPUT_COLUMNS,
)
from recommendation_schema import _validate_spending_change
from recurrence import recurrence_patterns

CENT = Decimal("0.01")

Payments = Sequence[tuple[date, Decimal]]


def simulate(forecast, payments: Payments) -> tuple[bool, Decimal, date]:
    """Apply ``payments`` to the baseline ledger and find its lowest checkpoint.

    Returns ``(safe, lowest_balance, lowest_date)``. Payments dated after the
    forecast horizon are still applied (after every ledger entry), which can
    only lower the balance, so the check stays conservative.
    """
    moves = [(entry.day, entry.amount) for entry in forecast.entries]
    moves += [(day, -amount) for day, amount in payments]
    # Stable sort keeps the ledger's own order among same-key movements.
    moves.sort(key=lambda move: same_day_order(*move))
    balance = lowest = forecast.opening_balance
    lowest_date = forecast.start_date
    for day, amount in moves:
        balance += amount
        if balance < lowest:
            lowest, lowest_date = balance, day
    return lowest >= forecast.minimum_balance_to_keep, lowest, lowest_date


def amount_safe_to_pay(forecast, requested: Decimal) -> Decimal:
    """Largest single payment on the request date that keeps every checkpoint safe.

    A payment on the request date lowers every checkpoint after it by the same
    amount, but not the opening balance or that day's credits booked before it.
    When the baseline is safe, a probe payment of ``requested`` puts the lowest
    checkpoint among the lowered ones, so the headroom is the probe's lowest
    balance plus ``requested`` less the floor, clamped to ``[0, requested]``
    and rounded down to the cent.
    """
    if not simulate(forecast, ())[0]:
        return Decimal("0.00")
    _, lowest, _ = simulate(forecast, ((forecast.start_date, requested),))
    room = lowest + requested - forecast.minimum_balance_to_keep
    room = min(max(room, Decimal(0)), requested)
    return room.quantize(CENT, rounding=ROUND_DOWN)


def earliest_full_payment_date(forecast, requested: Decimal) -> date | None:
    """First horizon day on which one payment of ``requested`` is safe."""
    day = forecast.start_date
    while day <= forecast.end_date:
        if simulate(forecast, ((day, requested),))[0]:
            return day
        day += timedelta(days=1)
    return None


@dataclass(frozen=True)
class Candidate:
    method: str
    status: str
    payments: tuple[tuple[date, Decimal], ...]
    total_cost: Decimal
    #: Final tie-breaker. Plans not taken from a supplied installment option use
    #: the request's full_payment option id.
    option_id: str
    lowest: Decimal
    lowest_date: date

    def rank_key(self):
        # Criteria 1 (completes by the deadline) and 2 (no spending changes)
        # hold for every candidate that reaches ranking, so they are omitted.
        return (self.total_cost, self.payments[0][0], len(self.payments), self.option_id)


def _candidates(forecast, request, profile, options, safe, earliest) -> list[Candidate]:
    requested = request.requested_amount
    start = request.request_date
    deadline = request.desired_completion_date
    accepted = profile.payment_methods_user_will_consider
    full_id = next(
        (o.payment_option_id for o in options if o.payment_method == "full_payment"),
        "",
    )
    plans: list[tuple[str, str, tuple, Decimal, str]] = []
    if "full_payment" in accepted:
        if earliest == start:
            plans.append(("full_payment", "affordable_now", ((start, requested),), requested, full_id))
        elif earliest is not None and earliest <= deadline:
            plans.append(("wait", "affordable_later", ((earliest, requested),), requested, full_id))
    if (
        request.allows_partial_payment
        and "partial_payment" in accepted
        and 0 < safe < requested
        and earliest is not None
        and earliest <= deadline
    ):
        payments = ((start, safe), (earliest, requested - safe))
        plans.append(("partial_payment", "affordable_with_plan", payments, requested, full_id))
    if "installments" in accepted and profile.max_installment_months is not None:
        for option in options:
            if option.payment_method != "installments":
                continue
            schedule = option.schedule()
            if (
                option.number_of_payments <= profile.max_installment_months
                and schedule[-1][0] <= deadline
            ):
                plans.append(
                    (
                        "installments",
                        "affordable_with_plan",
                        schedule,
                        option.total_payable_amount,
                        option.payment_option_id,
                    )
                )

    candidates = []
    for method, status, payments, cost, option_id in plans:
        ok, lowest, lowest_date = simulate(forecast, payments)
        if ok:
            candidates.append(
                Candidate(method, status, payments, cost, option_id, lowest, lowest_date)
            )
    return candidates


#: Methods a changed forecast may recommend. ``wait`` and ``partial_payment``
#: are dated by the earliest full-payment date, which stays computed on the
#: unchanged forecast, so they cannot be expressed consistently here.
CHANGE_METHODS = frozenset({"full_payment", "installments"})

@dataclass(frozen=True)
class SpendingChange:
    """One stop or reduce action on a recurring expense series."""

    event_id: str  # the series' latest settled occurrence
    category: str
    description: str
    new_amount: Decimal | None  # ``None`` stops the series
    saving: Decimal  # over the projected occurrences in the horizon

    @property
    def text(self) -> str:
        if self.new_amount is None:
            return f"stop:{self.event_id}"
        return f"reduce_to:{self.event_id}:{money(self.new_amount)}"


def _spending_actions(dataset, request, profile, forecast) -> list[SpendingChange]:
    """Every permitted action, one per recurring expense series, largest saving first.

    The cited event is the series' latest settled occurrence; an action is
    permitted exactly when the recommendation validator accepts it.
    """
    actions = []
    patterns = recurrence_patterns(dataset.events_for(request.user_id), as_of=request.request_date)
    for (category, direction, _), pattern in sorted(patterns.items()):
        event = dataset.event_by_id.get(pattern.event_ids[-1])
        projected = [
            -entry.amount
            for entry in forecast.entries
            if entry.source_kind == "recurrence" and entry.source_id == f"{category}/debit"
        ]
        # ponytail: home-currency expenses only (every flexible debit in the
        # dataset is); convert minimum_allowed_amount per date if that changes.
        if direction != "debit" or event is None or not projected or event.currency != forecast.home_currency:
            continue
        # Reduce is tried before stop: the smaller change wins when both are permitted.
        choices = [("stop", None)]
        if event.minimum_allowed_amount is not None and all(event.minimum_allowed_amount < amount for amount in projected):
            choices.insert(0, ("reduce_to", event.minimum_allowed_amount))
        for action, new_amount in choices:
            issues = []
            _validate_spending_change(
                action, event.event_id, new_amount, request, profile, dataset,
                lambda message, column=None: issues.append(message),
            )
            if not issues:
                saving = sum(amount - (new_amount or 0) for amount in projected)
                actions.append(SpendingChange(event.event_id, category, event.description, new_amount, saving))
                break
    return sorted(actions, key=lambda action: (-action.saving, action.event_id))


def _with_changes(forecast, changes: Sequence[SpendingChange]):
    """The forecast with each changed series' projected occurrences reduced or removed."""
    by_source = {f"{change.category}/debit": change for change in changes}
    entries, balance = [], forecast.opening_balance
    for entry in forecast.entries:
        change = by_source.get(entry.source_id) if entry.source_kind == "recurrence" else None
        if change is not None and change.new_amount is None:
            continue
        amount = entry.amount if change is None else -change.new_amount
        balance += amount
        entries.append(replace(entry, amount=amount, balance_after=balance))
    return replace(forecast, entries=tuple(entries))


def _plan_with_changes(dataset, request, profile, options, forecast):
    """The first greedy set of up to three changes that makes a plan work.

    Actions are added largest saving first; after each addition the normal
    candidate generation and ranking run on the changed forecast. Returns
    ``(changes sorted by event id, best candidate, changed forecast)`` or
    ``((), None, forecast)`` when no set of up to three is enough.
    """
    requested = request.requested_amount
    chosen: list[SpendingChange] = []
    for action in _spending_actions(dataset, request, profile, forecast)[:MAX_SPENDING_CHANGES]:
        chosen.append(action)
        changed = _with_changes(forecast, chosen)
        candidates = [
            candidate
            for candidate in _candidates(
                changed, request, profile, options,
                amount_safe_to_pay(changed, requested), earliest_full_payment_date(changed, requested),
            )
            if candidate.method in CHANGE_METHODS
        ]
        if candidates:
            return tuple(sorted(chosen, key=lambda change: change.event_id)), min(candidates, key=Candidate.rank_key), changed
    return (), None, forecast


def money(value: Decimal) -> str:
    """Plain decimal: whole amounts without decimals, otherwise two places."""
    value = value.quantize(CENT)
    return f"{value:.0f}" if value == value.to_integral_value() else f"{value:f}"


def _plan_text(payments: Payments) -> str:
    return "|".join(f"{day.isoformat()}:{money(amount)}" for day, amount in payments)


def _explain(candidate, forecast, request, safe, earliest, baseline, changes=()) -> str:
    cur = forecast.home_currency
    floor = f"{cur} {money(forecast.minimum_balance_to_keep)} minimum"
    requested = f"{cur} {money(request.requested_amount)}"
    if candidate is None:
        base_low, base_date = baseline
        if base_low < forecast.minimum_balance_to_keep:
            reason = (
                f"The projected balance already falls to {cur} {money(base_low)} on "
                f"{base_date.isoformat()}, below the {floor}"
            )
        elif earliest is None:
            reason = (
                f"Only {cur} {money(safe)} is safe today and the full {requested} "
                "does not become safe as one payment within the 90-day forecast"
            )
        elif earliest > request.desired_completion_date:
            reason = (
                f"Only {cur} {money(safe)} is safe today and the full amount is not "
                f"safe until {earliest.isoformat()}, after the desired date "
                f"{request.desired_completion_date.isoformat()}"
            )
        else:
            reason = (
                f"Only {cur} {money(safe)} is safe today and no payment method the "
                f"user accepts keeps the {floor} protected by "
                f"{request.desired_completion_date.isoformat()}"
            )
        return f"Do not proceed with the {requested} request. {reason}."

    first_day, first_amount = candidate.payments[0]
    if candidate.method == "full_payment":
        action = f"Pay {requested} in full today."
    elif candidate.method == "wait":
        action = (
            f"Wait until {first_day.isoformat()} and pay {requested} in full; "
            f"only {cur} {money(safe)} is safe today."
        )
    elif candidate.method == "partial_payment":
        second_day, second_amount = candidate.payments[1]
        action = (
            f"Pay {cur} {money(first_amount)} today and the remaining "
            f"{cur} {money(second_amount)} on {second_day.isoformat()}."
        )
    else:
        action = (
            f"Use {len(candidate.payments)} installments of {cur} {money(first_amount)} "
            f"starting {first_day.isoformat()} ({candidate.option_id})."
        )
    if changes:
        parts = [
            f"stop {change.description}"
            if change.new_amount is None
            else f"reduce {change.description} to {cur} {money(change.new_amount)}"
            for change in changes
        ]
        listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
        action = f"{listed[0].upper()}{listed[1:]}, then {action[0].lower()}{action[1:]}"
    return (
        f"{action} The lowest projected balance is {cur} {money(candidate.lowest)} on "
        f"{candidate.lowest_date.isoformat()}, keeping the {floor} protected."
    )


def recommend(dataset, request) -> dict[str, str]:
    """One output row for ``request``."""
    forecast = forecast_for_request(dataset, request)
    if not forecast.is_complete:
        shown = "; ".join(
            f"{blocker.source_id}: {blocker.reason}" for blocker in forecast.blockers[:2]
        )
        extra = len(forecast.blockers) - 2
        if extra > 0:
            shown += f"; and {extra} more"
        return {
            "request_id": request.request_id,
            "amount_safe_to_pay": "0",
            "affordability_status": "not_affordable",
            "recommended_payment_method": "not_recommended",
            "payment_plan": NO_PAYMENT_PLAN,
            "earliest_date_for_full_payment": "",
            "spending_changes_needed": NO_SPENDING_CHANGES,
            "decision_explanation": (
                "Not recommended because the forecast is incomplete so no payment "
                f"can be confirmed safe. Unresolved: {shown}."
            ),
        }

    profile = dataset.profile_by_user[request.user_id]
    options = dataset.options_for(request.request_id)
    requested = request.requested_amount
    safe = amount_safe_to_pay(forecast, requested)
    earliest = earliest_full_payment_date(forecast, requested)
    candidates = _candidates(forecast, request, profile, options, safe, earliest)
    best = min(candidates, key=Candidate.rank_key) if candidates else None
    changes, planned = (), forecast
    if best is None:
        changes, best, planned = _plan_with_changes(dataset, request, profile, options, forecast)
    explanation = _explain(
        best, planned, request, safe, earliest, simulate(forecast, ())[1:], changes
    )
    if best is None:
        # The contract leaves the earliest date blank for not_recommended.
        method, status, plan, earliest_text = "not_recommended", "not_affordable", NO_PAYMENT_PLAN, ""
    else:
        method, status, plan = best.method, best.status, _plan_text(best.payments)
        if changes:
            status = "affordable_with_plan"
        earliest_text = earliest.isoformat() if earliest else ""
    return {
        "request_id": request.request_id,
        "amount_safe_to_pay": money(safe),
        "affordability_status": status,
        "recommended_payment_method": method,
        "payment_plan": plan,
        "earliest_date_for_full_payment": earliest_text,
        "spending_changes_needed": "|".join(change.text for change in changes) or NO_SPENDING_CHANGES,
        "decision_explanation": explanation,
    }


def write_predictions(dataset, path: Path | str, requests=None) -> list[dict[str, str]]:
    """Write one row per request (dataset order) to ``path``; return the rows."""
    rows = [recommend(dataset, request) for request in (requests or dataset.requests)]
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REQUIRED_OUTPUT_COLUMNS)
        writer.writerows([row[column] for column in REQUIRED_OUTPUT_COLUMNS] for row in rows)
    return rows

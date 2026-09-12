"""Recommendation engine: turn a baseline forecast into one output row.

Every candidate plan is checked by :func:`simulate`, which books the plan's
payments as debits into the forecast ledger and walks every balance
checkpoint: the opening balance on the request date and the balance after
each movement, ordered by :func:`cashflow.same_day_order` (credits before
debits within a day). A plan is safe
only when no checkpoint falls below ``minimum_balance_to_keep``.

No spending changes are proposed yet; every row carries ``none``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Sequence

from cashflow import forecast_for_request, same_day_order
from output_schema import NO_PAYMENT_PLAN, NO_SPENDING_CHANGES, REQUIRED_OUTPUT_COLUMNS

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


def money(value: Decimal) -> str:
    """Plain decimal: whole amounts without decimals, otherwise two places."""
    value = value.quantize(CENT)
    return f"{value:.0f}" if value == value.to_integral_value() else f"{value:f}"


def _plan_text(payments: Payments) -> str:
    return "|".join(f"{day.isoformat()}:{money(amount)}" for day, amount in payments)


def _explain(candidate, forecast, request, safe, earliest, baseline) -> str:
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
    explanation = _explain(
        best, forecast, request, safe, earliest, simulate(forecast, ())[1:]
    )
    if best is None:
        # The contract leaves the earliest date blank for not_recommended.
        method, status, plan, earliest_text = "not_recommended", "not_affordable", NO_PAYMENT_PLAN, ""
    else:
        method, status, plan = best.method, best.status, _plan_text(best.payments)
        earliest_text = earliest.isoformat() if earliest else ""
    return {
        "request_id": request.request_id,
        "amount_safe_to_pay": money(safe),
        "affordability_status": status,
        "recommended_payment_method": method,
        "payment_plan": plan,
        "earliest_date_for_full_payment": earliest_text,
        "spending_changes_needed": NO_SPENDING_CHANGES,
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

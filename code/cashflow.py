"""Deterministic cash-flow forecasting for one user over a fixed horizon.

The forecast answers a single question: what does this user's balance do,
day by day, from the request date through the safety window, if nothing new is
decided? It books only cash that the dataset actually supports — settled money
that has not moved yet, reserved pending debits, scheduled commitments, and
recurring series that :mod:`recurrence` has already validated against history.

Every booking is evidence-bound: a recurring series is projected at the
amount :func:`recurrence.series_amount` derives from its history, income only
when it is confirmed or repeatedly evidenced, and anything unresolved (a blank amount, a missing exchange rate) is
surfaced as a blocker instead of being guessed at or silently treated as zero.

No recommendation, payment option or spending change is chosen here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from evidence_policy import (
    DATED_CREDIT_KINDS,
    FROM_DATE,
    IncomeFact,
    IncomeHold,
    fact_targets,
    income_facts,
    income_holds,
)
from image_evidence import IMAGE_AMOUNTS_PATH, apply_image_evidence
from recurrence import (
    RecurrencePattern,
    _anchored,
    conservative_amount,
    income_stream,
    recurrence_patterns,
    series_amount,
)

#: The safety window the forecast checks, in days from the request date.
#: Calibrated globally: reference behaviour ignores commitments on days 87-90
#: of the nominal 90-day period.
HORIZON_DAYS = 86

#: Statuses whose cash never moves, in either direction.
EXCLUDED_STATUSES = frozenset({"failed", "cancelled", "unrealized"})

#: Statuses that can still move cash on or after the request date.
CASH_STATUSES = frozenset({"settled", "pending", "scheduled"})

#: Only confirmed earned income counts. Refunds, investment sales and other
#: credit event types are not money the user can plan on.
COUNTED_CREDIT_EVENT_TYPES = frozenset({"income"})

#: Windfalls (bonuses, commissions, lottery proceeds) never count, even when
#: they are recorded as income.
UNCOUNTED_CREDIT_CATEGORIES = frozenset({"windfall"})


@dataclass(frozen=True)
class LedgerEntry:
    """One projected cash movement, in the user's home currency."""

    day: date
    #: Signed home-currency amount: negative for money out, positive for money in.
    amount: Decimal
    #: ``"event"`` for a supplied row, ``"recurrence"`` for a projected series.
    source_kind: str
    #: ``event_id`` or ``"<category>/<direction>"`` for a projected series.
    source_id: str
    category: str
    #: Why this cash state was booked on this date.
    rationale: str
    #: Projected balance once this entry has been applied.
    balance_after: Decimal

    @property
    def is_debit(self) -> bool:
        return self.amount < 0


@dataclass(frozen=True)
class ForecastBlocker:
    """A fact the forecast could not resolve from the data supplied."""

    source_id: str
    day: date | None
    reason: str


@dataclass(frozen=True)
class ForecastNote:
    """Something the forecast deliberately left out, and why.

    Unlike a :class:`ForecastBlocker` this is a resolved decision, not an
    unresolved fact: the cash was identified and then withheld on evidence, so
    the projection stays safe to act on.
    """

    source_id: str
    reason: str


@dataclass(frozen=True)
class Forecast:
    """A chronological projection plus everything a later phase needs."""

    user_id: str
    home_currency: str
    start_date: date
    end_date: date
    opening_balance: Decimal
    minimum_balance_to_keep: Decimal
    entries: tuple[LedgerEntry, ...]
    blockers: tuple[ForecastBlocker, ...]
    #: Cash that was found and then withheld on message evidence. Auditable,
    #: but never a reason to treat the forecast as incomplete: withholding
    #: income only ever makes the projection safer.
    notes: tuple[ForecastNote, ...] = ()

    @property
    def is_complete(self) -> bool:
        """False while any cash the horizon needs is still unresolved.

        An incomplete forecast is missing money it knows about: a recurring
        debit whose amount or exchange rate could not be established is simply
        absent from the ledger, so ``closing_balance``, ``minimum_balance``
        and ``balance_on`` are all upper bounds rather than projections. They
        must never be used to establish that a payment is affordable — resolve
        the blockers first, or treat the request as undecidable.
        """
        return not self.blockers

    @property
    def closing_balance(self) -> Decimal:
        return self.entries[-1].balance_after if self.entries else self.opening_balance

    @property
    def minimum_balance(self) -> Decimal:
        """Lowest balance reached anywhere in the horizon.

        Only an upper bound when :attr:`is_complete` is false.
        """
        return self.minimum_balance_from(self.start_date)

    @property
    def minimum_balance_date(self) -> date:
        """The first date the minimum balance is reached."""
        lowest = self.minimum_balance
        for entry in self.entries:
            if entry.balance_after == lowest:
                return entry.day
        return self.start_date

    def balance_on(self, day: date) -> Decimal:
        """Projected balance at the end of ``day``."""
        balance = self.opening_balance
        for entry in self.entries:
            if entry.day > day:
                break
            balance = entry.balance_after
        return balance

    def minimum_balance_from(self, day: date) -> Decimal:
        """Lowest balance reached from ``day`` to the end of the horizon."""
        lowest = self.balance_on(day)
        for entry in self.entries:
            if entry.day >= day:
                lowest = min(lowest, entry.balance_after)
        return lowest

    def entries_on(self, day: date) -> tuple[LedgerEntry, ...]:
        return tuple(entry for entry in self.entries if entry.day == day)

    def trace(self) -> str:
        """A compact human-readable ledger, one line per entry."""
        header = (
            f"{self.user_id}  {self.start_date} .. {self.end_date}  "
            f"opening {self.opening_balance} {self.home_currency}  "
            f"floor {self.minimum_balance_to_keep}"
        )
        if not self.is_complete:
            header += "  [INCOMPLETE]"
        lines = [header]
        for entry in self.entries:
            lines.append(
                f"  {entry.day}  {entry.amount:>14}  {entry.balance_after:>14}  "
                f"{entry.source_id:<24} {entry.rationale}"
            )
        lines.append(
            f"  minimum {self.minimum_balance} on {self.minimum_balance_date}"
            f"  ({len(self.blockers)} blocker(s))"
        )
        for blocker in self.blockers:
            day = str(blocker.day) if blocker.day else "-"
            lines.append(f"  ! {blocker.source_id:<24} {day:<12} {blocker.reason}")
        for note in self.notes:
            lines.append(f"  ~ {note.source_id:<24} {'-':<12} {note.reason}")
        return "\n".join(lines)


def same_day_order(day: date, amount: Decimal) -> tuple[date, bool]:
    """Sort key for cash movements: by day, and within a day credits first.

    Confirmed income settling on a day can fund a debit on that same day. The
    forecast ledger and the engine's payment simulation both sort with this
    key, so the rule lives in one place.
    """
    return day, amount < 0


def _counts_as_income(event) -> bool:
    return (
        event.event_type in COUNTED_CREDIT_EVENT_TYPES
        and event.category not in UNCOUNTED_CREDIT_CATEGORIES
    )


def _succeeded(latest, pattern, events: Sequence, start: date) -> bool:
    """Whether a new income stream in the same category replaced this series.

    A stream replaced by a new one in the same category (a new employer, a new
    client) is a change of payer, not lost income. The replacement must have
    settled after the series' last occurrence and on or before the request
    date, under a description that had never settled before; a description
    that was already paying alongside the series is a parallel stream.
    """

    def settled_income(event) -> bool:
        return event.status == "settled" and event.settlement_date is not None and _counts_as_income(event)

    return any(
        settled_income(event)
        and event.category == latest.category
        and event.description != latest.description
        and pattern.last_date < event.settlement_date <= start
        and not any(
            settled_income(other)
            and other.description == event.description
            and other.settlement_date <= pattern.last_date
            for other in events
        )
        for event in events
    )


def _booking_date(event, start: date) -> date | None:
    """When this event's cash moves in the forecast, or ``None`` if never.

    Cash that settled before or on the request date is already inside
    ``current_available_balance`` and is never rebooked. Cash that has not
    moved yet is reserved no later than the request date, so a pending debit
    that was due yesterday still reduces what is safe to spend today.
    """
    settlement = event.settlement_date
    if settlement is None:
        return None
    if event.status == "settled":
        return settlement if settlement > start else None
    return max(settlement, start)


def _convert(dataset, amount: Decimal, currency: str, home: str, on_date: date) -> Decimal | None:
    """Convert to the home currency using the supplied dated rate only."""
    rate = dataset.exchange_rate(on_date, currency, home)
    if rate is None:
        return None
    return amount * rate


def _event_entries(
    dataset,
    events: Iterable,
    *,
    home_currency: str,
    start: date,
    end: date,
) -> tuple[list[tuple[date, Decimal, str, str, str]], list[ForecastBlocker]]:
    """Book the supplied rows; return raw movements and unresolved blockers."""
    movements: list[tuple[date, Decimal, str, str, str]] = []
    blockers: list[ForecastBlocker] = []

    for event in sorted(events, key=lambda item: item.event_id):
        if event.status in EXCLUDED_STATUSES or event.status not in CASH_STATUSES:
            continue
        if event.direction not in {"debit", "credit"}:
            continue
        day = _booking_date(event, start)
        if day is None or day > end:
            continue
        if event.direction == "credit":
            # Pending credits are money in flight that may never arrive.
            if event.status == "pending" or not _counts_as_income(event):
                continue
        if event.amount is None:
            blockers.append(
                ForecastBlocker(
                    event.event_id,
                    day,
                    "amount is blank; resolve from the linked image before forecasting",
                )
            )
            continue
        converted = _convert(dataset, event.amount, event.currency, home_currency, day)
        if converted is None:
            blockers.append(
                ForecastBlocker(
                    event.event_id,
                    day,
                    f"no supplied {event.currency}->{home_currency} rate on {day}",
                )
            )
            continue
        signed = -converted if event.direction == "debit" else converted
        group = income_stream(event) or event.category
        movements.append((day, signed, "event", event.event_id, event.category, group))
    return movements, blockers


def _rationale_for_event(event, day: date) -> str:
    if event.status == "pending" and event.direction == "debit":
        return f"pending debit reserved (due {event.settlement_date})"
    if event.direction == "credit":
        return f"{event.status} income counted on its settlement date {event.settlement_date}"
    return f"{event.status} {event.direction} settling {day}"


def _amount_by_currency(
    pattern: RecurrencePattern, events_by_id
) -> dict[str, Decimal] | None:
    """The series' projected amount per currency, still unconverted.

    ``None`` when the series contains an occurrence whose amount is still
    blank: its figure cannot be established until that evidence is resolved,
    and guessing one could understate the commitment. The estimate is taken
    per currency before conversion, so each date converts one value per
    currency.
    """
    amounts: dict[str, list[Decimal]] = {}
    for event_id in pattern.event_ids:
        event = events_by_id.get(event_id)
        if event is None:
            continue
        if event.amount is None:
            return None
        amounts.setdefault(event.currency, []).append(event.amount)
    return {
        currency: series_amount(pattern.direction, values)
        for currency, values in amounts.items()
    }


def _projected_amount(
    dataset, pattern: RecurrencePattern, extremes, home_currency: str, day: date
) -> tuple[Decimal | None, str | None]:
    """The home-currency amount for one projected date.

    Every occurrence is valued at the rate supplied for the date it is
    projected on, never at a rate carried over from history. A mixed-currency
    history takes the safer of its per-currency figures once each has been
    converted for that same date. Returns ``(None, currency)`` when a needed rate is not supplied.
    """
    converted: list[Decimal] = []
    for currency, amount in sorted(extremes.items()):
        value = _convert(dataset, amount, currency, home_currency, day)
        if value is None:
            return None, currency
        converted.append(value)
    return conservative_amount(pattern.direction, converted), None


def _recurrence_entries(
    dataset,
    events: Sequence,
    *,
    home_currency: str,
    start: date,
    end: date,
    booked: Sequence[tuple[date, Decimal, str, str, str]],
    holds: Mapping[str, IncomeHold],
    facts: Sequence[IncomeFact] = (),
) -> tuple[
    list[tuple[date, Decimal, str, str, str]], list[ForecastBlocker], list[ForecastNote]
]:
    """Project every recurring series history already supports."""
    movements: list[tuple[date, Decimal, str, str, str]] = []
    blockers: list[ForecastBlocker] = []
    notes: list[ForecastNote] = []
    events_by_id = {event.event_id: event for event in events}
    patterns = recurrence_patterns(events, as_of=start)
    projected: list[tuple] = []
    credit_candidates: list[tuple] = []
    for fact in facts:
        if fact.kind == "unpriced_commitment":
            notes.append(ForecastNote(fact.message_id, f"{fact.message_id}: a new commitment states no amount; nothing booked"))
    dated = _dated_salary_credits(
        dataset, [fact for fact in facts if fact.kind in DATED_CREDIT_KINDS], events, patterns,
        home_currency=home_currency, start=start, end=end, notes=notes, blockers=blockers,
    )
    movements += dated
    facts_by_series = _facts_by_series(
        [fact for fact in facts if fact.kind not in DATED_CREDIT_KINDS | {"unpriced_commitment"}],
        patterns, events, events_by_id, notes,
    )
    income_ends = _final_payroll_ends(events)

    for (category, direction, stream), pattern in sorted(patterns.items()):
        source_id = f"{category}/{direction}" + (f"/{stream}" if stream else "")
        group = stream or category
        if direction == "credit":
            sample = events_by_id.get(pattern.event_ids[-1])
            if sample is None or not _counts_as_income(sample):
                continue
            # Income that already missed its next expected occurrence has
            # lapsed. Debits never lapse: an unseen bill is still owed.
            if not _succeeded(sample, pattern, events, start) and start > pattern.last_date + timedelta(
                days=pattern.cadence_days + pattern.cadence_days // 2
            ):
                notes.append(
                    ForecastNote(
                        source_id,
                        f"last occurrence {pattern.last_date} and the next expected one "
                        "never arrived; income not projected",
                    )
                )
                continue
            # Message evidence can say plainly that the next payout is not yet
            # money. Settled history keeps its place in the opening balance;
            # only the projection ahead is withheld.
            hold = _hold_for(holds, events, category, stream)
            if hold is not None:
                notes.append(ForecastNote(source_id, hold.reason))
                continue
        extremes = _amount_by_currency(pattern, events_by_id)
        if not extremes:
            blockers.append(
                ForecastBlocker(
                    source_id,
                    None,
                    "recurring series has an unresolved amount; projection withheld",
                )
            )
            continue
        window = max(1, pattern.cadence_days // 2)
        # An occurrence due on the request date is still owed. A settled row of
        # the series that day is already history (``as_of`` is inclusive), so
        # projection resumes after it and it is never booked twice.
        # ponytail: a same-day settled row the detector leaves out of the series
        # is not matched; add a same-series check if that shows up in data.
        days = list(pattern.occurrences_between(start, end))
        overrides: dict[date, tuple[Decimal, str, str]] = {}
        if direction == "credit":
            days, overrides, fact_notes = _apply_income_facts(
                facts_by_series.get(source_id, ()), days, source_id,
                start=start, end=end, window=window,
                history=lambda amount, currency, day, pattern=pattern, extremes=extremes: _history_above(
                    dataset, pattern, extremes, home_currency, amount, currency, day
                ),
            )
            notes += fact_notes
            ended = income_ends.get(category)
            if ended is not None:
                days = [day for day in days if day <= ended[0]]
                notes.append(
                    ForecastNote(source_id, f"final payroll {ended[1]} settled {ended[0]}; nothing projected after it")
                )
        candidates = [
            (day, source_id, category, group, window, pattern, extremes, direction, overrides.get(day))
            for day in days
        ]
        if direction == "credit":
            credit_candidates += candidates
            continue
        # A supplied row for the same series already books that occurrence; a
        # half-cadence window spots the overlap without matching individual
        # rows to projected dates.
        explicit = [
            day for day, _, _, _, _, entry_group in booked if entry_group == group
        ]
        projected += [
            candidate
            for candidate in candidates
            if not any(abs((candidate[0] - other).days) < window for other in explicit)
        ]

    # A booked credit is that cycle's pay, whatever description it carries: it
    # stands in for the nearest projected credit of the same category within
    # that projection's half-cadence window, and for no more than one.
    suppressed: set[int] = set()
    for day, _, _, event_id, category, group in sorted(
        [item for item in booked if item[1] > 0] + dated, key=lambda item: (item[0], item[3])
    ):
        matches = [
            (abs((candidate[0] - day).days), candidate[3] != group, candidate[0], candidate[1], index)
            for index, candidate in enumerate(credit_candidates)
            if index not in suppressed
            and candidate[2] == category
            and abs((candidate[0] - day).days) < candidate[4]
        ]
        if not matches:
            continue
        *_, index = min(matches)
        suppressed.add(index)
        match_day, match_source = credit_candidates[index][:2]
        notes.append(
            ForecastNote(
                match_source,
                f"occurrence on {match_day} not projected: booked credit {event_id} "
                f"on {day} is the same {category} income",
            )
        )
    projected += [
        candidate
        for index, candidate in enumerate(credit_candidates)
        if index not in suppressed
    ]

    blocked: set[tuple[str, str]] = set()
    for day, source_id, category, group, _, pattern, extremes, direction, override in projected:
        if override is not None:
            stated, currency, message_id = override
            amount = _convert(dataset, stated, currency, home_currency, day)
            if amount is None:
                blockers.append(
                    ForecastBlocker(
                        source_id,
                        day,
                        f"{message_id} states {currency} {stated} but no supplied "
                        f"{currency}->{home_currency} rate on {day}",
                    )
                )
                continue
            movements.append((day, amount, "recurrence", source_id, category, group))
            continue
        amount, missing_currency = _projected_amount(
            dataset, pattern, extremes, home_currency, day
        )
        if amount is None:
            if (source_id, missing_currency) not in blocked:
                blocked.add((source_id, missing_currency))
                blockers.append(
                    ForecastBlocker(
                        source_id,
                        day,
                        f"no supplied {missing_currency}->{home_currency} rate "
                        f"on projected date {day}",
                    )
                )
            continue
        signed = -amount if direction == "debit" else amount
        movements.append((day, signed, "recurrence", source_id, category, group))
    notes += _unprojected_income_notes(events, patterns, start=start)
    return movements, blockers, notes


def _stream_names(events, category: str) -> set[str]:
    return {
        income_stream(event)
        for event in events
        if event.category == category and _counts_as_income(event)
    }


def _hold_for(holds, events, category: str, stream: str):
    """The hold covering this series, if any.

    A series read as one merged category — the fallback when no single stream
    had enough history — is withheld as soon as any of the streams inside it
    is, since there is no way to tell the held money apart from the rest.
    """
    if stream:
        return holds.get(stream)
    covered = _stream_names(events, category) & set(holds)
    return holds[sorted(covered)[0]] if covered else None


def _facts_by_series(facts, patterns, events, events_by_id, notes) -> dict[str, list[IncomeFact]]:
    """Each projected income series' message facts, oldest first.

    A newer fact supersedes an older one of the same kind for the same series.
    Unmatched and ambiguous facts leave a note naming the message.
    """
    texts: dict[str, str] = {}
    for (category, direction, stream), pattern in sorted(patterns.items()):
        sample = events_by_id.get(pattern.event_ids[-1])
        if direction != "credit" or sample is None or not _counts_as_income(sample):
            continue
        label = f"{category}/credit" + (f"/{stream}" if stream else "")
        # A merged series answers to every stream inside it.
        texts[label] = stream or " | ".join(sorted(_stream_names(events, category)))
    latest: dict[tuple[str, str], IncomeFact] = {}
    for fact in facts:
        targets, note = fact_targets(fact, texts)
        if note:
            notes.append(ForecastNote(fact.message_id, note))
        for label in targets:
            latest[(label, fact.kind)] = fact
    by_series: dict[str, list[IncomeFact]] = {}
    for (label, _), fact in latest.items():
        by_series.setdefault(label, []).append(fact)
    return {
        label: sorted(items, key=lambda fact: (fact.sent_at, fact.message_id))
        for label, items in by_series.items()
    }


#: Facts that decide which dates a series pays on, before any amount is set.
DATE_FACT_KINDS = frozenset({"income_ended", "payday_moved"})

#: Facts that confirm pay rather than change it. A confirmation that disagrees
#: with settled history is not an amendment, so it may lower a projected
#: occurrence but never raise it above the series' own estimate.
CONFIRMATION_KINDS = frozenset(
    {"base_salary_confirmed", "regular_salary_confirmed", "remaining_salary_confirmed", "fx_salary_confirmed"}
)


def _history_above(dataset, pattern, extremes, home_currency, amount, currency, day) -> str | None:
    """The series' own estimate as ``"CUR B"`` when ``amount`` exceeds it, else ``None``.

    Compared in the stated currency when the series has it, otherwise both
    converted on ``day``. A missing rate is not decided here: the booking
    reports it as a blocker.
    """
    if currency in extremes:
        history = extremes[currency]
        return f"{currency} {history}" if amount > history else None
    stated = _convert(dataset, amount, currency, home_currency, day)
    history, _ = _projected_amount(dataset, pattern, extremes, home_currency, day)
    if stated is None or history is None or stated <= history:
        return None
    return f"{home_currency} {history}"


def _apply_income_facts(
    facts: Sequence[IncomeFact],
    days: list[date],
    source_id: str,
    *,
    start: date,
    end: date,
    window: int,
    history=lambda amount, currency, day: None,
) -> tuple[list[date], dict[date, tuple[Decimal, str, str]], list[ForecastNote]]:
    """Apply message facts to one income series' projected dates and amounts.

    Date facts settle which occurrences exist; amount facts then value them,
    oldest message first so a newer one wins where both reach an occurrence.
    Returns the dates, ``{date: (amount, currency, message_id)}`` overrides
    and one note per applied fact. Settled history is never touched: only
    dates already projected from ``start`` onward are in play.

    ``history(amount, currency, day)`` names the series' estimate when a stated
    amount exceeds it; a confirmation there keeps the history amount.
    """
    overrides: dict[date, tuple[Decimal, str, str]] = {}
    notes: list[ForecastNote] = []

    def note(fact: IncomeFact, text: str) -> None:
        notes.append(ForecastNote(source_id, f"{fact.message_id}: {text}"))

    for fact in facts:
        if fact.kind == "income_ended":
            days = [day for day in days if day < fact.sent_at]
            note(fact, f"income ended; nothing projected from {fact.sent_at}")
        elif fact.kind == "payday_moved" and days:
            moved = fact.effective_date
            days, months = [], 0
            while (day := _anchored(moved, months, moved.day)) <= end:
                if day >= start:
                    days.append(day)
                months += 1
            note(fact, f"payday moved; next occurrence {moved}, later ones on day {moved.day}")

    for fact in facts:
        if fact.kind in DATE_FACT_KINDS:
            continue
        stated = f"{fact.currency} {fact.amount}"
        if fact.kind == "one_off_extra":
            note(fact, f"one-off {stated} states no date; not counted")
            continue
        if fact.kind == "fx_salary_confirmed":
            confirmed = fact.effective_date
            if not start <= confirmed <= end:
                note(fact, f"confirmed salary on {confirmed} is outside the forecast; ignored")
                continue
            # The confirmed credit is that cycle's pay: it replaces the nearest
            # projected occurrence within half a cadence.
            near = [day for day in days if abs((day - confirmed).days) < window]
            replaces = bool(near)
            if near:
                days.remove(min(near, key=lambda day: (abs((day - confirmed).days), day)))
            days = sorted(set(days) | {confirmed})
            reached = [confirmed]
        elif fact.scope == FROM_DATE:
            reached = [day for day in days if day >= (fact.effective_date or start)]
        else:
            reached = days[:1]
        capped: dict[date, str] = {}
        if fact.kind in CONFIRMATION_KINDS and (fact.kind != "fx_salary_confirmed" or replaces):
            capped = {
                day: above
                for day in reached
                if (above := history(fact.amount, fact.currency, day)) is not None
            }
        for day in reached:
            if day not in capped:
                overrides[day] = (fact.amount, fact.currency, fact.message_id)
        for above in sorted(set(capped.values())):
            note(fact, f"confirms {stated}, above settled history {above}; history amount kept (safer)")
        applied = [day for day in reached if day not in capped]
        if applied or not reached:
            when = ", ".join(str(day) for day in applied) or "no projected occurrence"
            note(fact, f"{fact.kind} {stated} applied to {when}")
    return days, overrides, notes


#: The category a dated salary notice speaks for: the user's salary as a whole.
SALARY_CATEGORY = "salary"

def _dated_salary_credits(dataset, facts, events, patterns, *, home_currency, start, end, notes, blockers):
    """Monthly salary credits dated by a notice or by a supplied scheduled row.

    A first or resumed salary is booked on its stated date and continues at
    that amount on the same day of month through the horizon. A scheduled
    salary row in a category with no projectable income schedule continues the same way
    (the row itself is already booked). Each credit is that cycle's pay, so the
    caller lets it replace the nearest projected same-category credit, one for
    one. Not capped: a dated scheduled credit does not contradict history.
    """
    # (first day, amount, currency, source, months to skip)
    seeds = []
    for fact in facts:
        day = fact.effective_date
        if not start <= day <= end:
            where = "before the request date" if day < start else "after the forecast"
            notes.append(ForecastNote(fact.message_id, f"{fact.message_id}: {fact.kind} on {day} is {where}; not booked"))
            continue
        seeds.append((day, fact.amount, fact.currency, fact.message_id, 0))
    for event in events:
        if (
            event.status == "scheduled"
            and event.category == SALARY_CATEGORY
            and _counts_as_income(event)
            and event.amount is not None
            and event.settlement_date is not None
            and start <= event.settlement_date <= end
            and not any(key[:2] == (event.category, "credit") for key in patterns)
        ):
            seeds.append((event.settlement_date, event.amount, event.currency, event.event_id, 1))
    movements = []
    for first, amount, currency, source, skip in sorted(seeds, key=lambda seed: (seed[0], seed[3])):
        source_id = f"{SALARY_CATEGORY}/credit/{source}"
        months = skip
        while (day := _anchored(first, months, first.day)) <= end:
            converted = _convert(dataset, amount, currency, home_currency, day)
            if converted is None:
                blockers.append(ForecastBlocker(source_id, day, f"{source} states {currency} {amount} but no supplied {currency}->{home_currency} rate on {day}"))
            else:
                movements.append((day, converted, "message", source_id, SALARY_CATEGORY, SALARY_CATEGORY))
            months += 1
        notes.append(ForecastNote(source_id, f"{source}: dated salary {currency} {amount} from {first}, continued monthly"))
    return movements


FINAL_PAYROLL = re.compile(r"\bfinal\b", re.IGNORECASE)


def _final_payroll_ends(events) -> dict[str, tuple[date, str]]:
    """Per category, the settled final payroll that no later income follows."""
    income = [
        event for event in events
        if event.direction == "credit" and _counts_as_income(event) and event.settlement_date is not None
    ]
    ends: dict[str, tuple[date, str]] = {}
    for event in income:
        if event.status != "settled" or not FINAL_PAYROLL.search(event.description):
            continue
        if any(
            other.category == event.category
            and other.status in {"settled", "scheduled"}
            and other.settlement_date > event.settlement_date
            for other in income
        ):
            continue
        ends[event.category] = (event.settlement_date, event.event_id)
    return ends


#: A single settled credit is a one-off. Two or more in the same stream look
#: like a commitment, and declining to project one is worth recording.
MIN_AMBIGUOUS_OCCURRENCES = 2


def _unprojected_income_notes(events, patterns, *, start: date) -> list[ForecastNote]:
    """Record repeated income that no detected stream covers.

    A stream whose history is too short or too irregular to project is left
    out of the ledger, which is the safe result. It is not a silent one: an
    income stream that plainly repeats but could not be pinned to a schedule
    is exactly the fact a later decision should be able to see.
    """
    projected = {
        (pattern.category, pattern.stream)
        for pattern in patterns.values()
        if pattern.direction == "credit"
    }
    # A merged category already speaks for every stream inside it.
    merged = {category for category, stream in projected if stream == ""}
    counts: dict[tuple[str, str], int] = {}
    for event in events:
        if event.status != "settled" or not _counts_as_income(event):
            continue
        if event.settlement_date is None or event.settlement_date > start:
            continue
        key = (event.category, income_stream(event))
        if key in projected or event.category in merged:
            continue
        counts[key] = counts.get(key, 0) + 1
    return [
        ForecastNote(
            f"{category}/credit/{stream}" if stream else f"{category}/credit",
            f"{count} settled occurrences support no projectable schedule; "
            "this income is not counted",
        )
        for (category, stream), count in sorted(counts.items())
        if count >= MIN_AMBIGUOUS_OCCURRENCES
    ]


def build_forecast(
    dataset,
    user_id: str,
    start_date: date,
    *,
    horizon_days: int = HORIZON_DAYS,
    request=None,
    image_evidence: Path | None = IMAGE_AMOUNTS_PATH,
) -> Forecast:
    """Project ``user_id``'s balance from ``start_date`` over the horizon.

    ``image_evidence`` is the resolved image-amount artifact; blank amounts it
    validly resolves are filled in before anything is booked or projected.
    """
    profile = dataset.profile_by_user.get(user_id)
    if profile is None:
        raise KeyError(f"no financial profile for user {user_id!r}")

    events, evidence_notes = apply_image_evidence(
        dataset, dataset.events_for(user_id), user_id, image_evidence
    )
    end = start_date + timedelta(days=horizon_days)
    home = profile.home_currency

    movements, blockers = _event_entries(
        dataset, events, home_currency=home, start=start_date, end=end
    )
    holds = income_holds(dataset, user_id, as_of=start_date, request=request)
    projected, projection_blockers, notes = _recurrence_entries(
        dataset,
        events,
        home_currency=home,
        start=start_date,
        end=end,
        booked=movements,
        holds=holds,
        facts=income_facts(dataset, user_id, as_of=start_date, request=request),
    )
    blockers += projection_blockers
    notes += [ForecastNote(source_id, reason) for source_id, reason in evidence_notes]

    events_by_id = {event.event_id: event for event in events}
    ordered = sorted(
        movements + projected,
        key=lambda item: (*same_day_order(item[0], item[1]), item[3]),
    )

    entries: list[LedgerEntry] = []
    balance = profile.current_available_balance
    for day, amount, source_kind, source_id, category, _group in ordered:
        balance += amount
        event = events_by_id.get(source_id) if source_kind == "event" else None
        rationale = (
            _rationale_for_event(event, day)
            if event is not None
            else "dated salary credit" if source_kind == "message"
            else f"projected from a settled {category} series"
        )
        entries.append(
            LedgerEntry(
                day=day,
                amount=amount,
                source_kind=source_kind,
                source_id=source_id,
                category=category,
                rationale=rationale,
                balance_after=balance,
            )
        )

    return Forecast(
        user_id=user_id,
        home_currency=home,
        start_date=start_date,
        end_date=end,
        opening_balance=profile.current_available_balance,
        minimum_balance_to_keep=profile.minimum_balance_to_keep,
        entries=tuple(entries),
        blockers=tuple(sorted(blockers, key=lambda item: (item.source_id, item.reason))),
        notes=tuple(sorted(notes, key=lambda item: (item.source_id, item.reason))),
    )


def forecast_for_request(dataset, request, *, horizon_days: int = HORIZON_DAYS) -> Forecast:
    """The baseline forecast a request's decision is made against."""
    return build_forecast(
        dataset,
        request.user_id,
        request.request_date,
        horizon_days=horizon_days,
        request=request,
    )

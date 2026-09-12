"""Deterministic recurrence detection from historical financial events.

The problem statement allows a recurring commitment to be recognised only when
history supports it. This module answers that question from settled events
alone, dated by `settlement_date`: a pattern needs several matching debits (or
credits) in the same category with a consistent settlement cadence. Nothing
here looks forward, assumes a schedule, or trusts a single record.

The detected :class:`RecurrencePattern` carries the cadence and the typical
amount, so the forecast engine can project the same series it validated
against.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

#: Only settled records are evidence of a commitment that actually repeats.
#: Pending and scheduled rows are single future intentions; failed, cancelled
#: and unrealized rows never moved cash at all.
HISTORICAL_STATUSES = frozenset({"settled"})

#: A pair of dates is a coincidence; three give two intervals to compare.
MIN_OCCURRENCES = 3

#: Cadences outside this range are not treated as a recurring commitment.
MIN_CADENCE_DAYS = 5
MAX_CADENCE_DAYS = 400

#: Every interval must sit within max(4 days, 25% of the median cadence) of
#: the median, which accepts calendar-month drift (28-31) and rejects noise.
CADENCE_TOLERANCE_DAYS = 4
CADENCE_TOLERANCE_RATIO = Decimal("0.25")


@dataclass(frozen=True)
class RecurrencePattern:
    """A repeating series of same-category events in one user's history."""

    user_id: str
    category: str
    direction: str
    event_ids: tuple[str, ...]
    #: Settlement dates of the observed occurrences, ascending.
    dates: tuple[date, ...]
    cadence_days: int
    amounts: tuple[Decimal, ...]

    @property
    def occurrences(self) -> int:
        return len(self.dates)

    @property
    def first_date(self) -> date:
        return self.dates[0]

    @property
    def last_date(self) -> date:
        return self.dates[-1]

    @property
    def typical_amount(self) -> Decimal | None:
        """Median observed amount, or ``None`` when no amount is known.

        Descriptive only. A forecast must pick a conservative figure for
        protected or variable spending rather than assume the median.
        """
        if not self.amounts:
            return None
        return Decimal(statistics.median(sorted(self.amounts)))

    def next_occurrence_after(self, day: date) -> date:
        """The first projected occurrence strictly after ``day``."""
        projected = self.last_date
        while projected <= day:
            projected += timedelta(days=self.cadence_days)
        return projected

    def occurrences_between(self, start: date, end: date) -> tuple[date, ...]:
        """Projected occurrence dates in ``[start, end]``, cadence-spaced.

        Forward projection continues the observed series; it never invents a
        different rhythm or amount.
        """
        if end < start:
            return ()
        projected: list[date] = []
        current = self.next_occurrence_after(start - timedelta(days=1))
        while current <= end:
            projected.append(current)
            current += timedelta(days=self.cadence_days)
        return tuple(projected)


def _cadence_is_consistent(gaps: Sequence[int], median: float) -> bool:
    tolerance = max(
        Decimal(CADENCE_TOLERANCE_DAYS), Decimal(str(median)) * CADENCE_TOLERANCE_RATIO
    )
    return all(abs(Decimal(gap) - Decimal(str(median))) <= tolerance for gap in gaps)


def evidence_date(event) -> date | None:
    """When the event's cash actually moved, or ``None`` if it never did.

    Recurrence is a cash pattern, so settlement timing is the evidence — an
    event dated before a request but settling after it was still unknown when
    the request was made.
    """
    return event.settlement_date


def _historical(
    events: Iterable, *, as_of: date | None, statuses: frozenset[str]
) -> list:
    selected = [
        event
        for event in events
        if event.status in statuses
        and event.direction in {"debit", "credit"}
        and evidence_date(event) is not None
        and (as_of is None or evidence_date(event) <= as_of)
    ]
    return sorted(selected, key=lambda event: (evidence_date(event), event.event_id))


def detect_recurrence(
    events: Iterable,
    *,
    category: str,
    direction: str,
    as_of: date | None = None,
    statuses: frozenset[str] = HISTORICAL_STATUSES,
    min_occurrences: int = MIN_OCCURRENCES,
) -> RecurrencePattern | None:
    """Detect a recurring series for one category, or return ``None``.

    ``events`` should be one user's events; ``as_of`` restricts the evidence to
    history on or before that date, so a decision made on `request_date` never
    relies on events that had not happened yet.
    """
    series = [
        event
        for event in _historical(events, as_of=as_of, statuses=statuses)
        if event.category == category and event.direction == direction
    ]
    if len(series) < min_occurrences:
        return None

    dates = [evidence_date(event) for event in series]
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not gaps or any(gap <= 0 for gap in gaps):
        return None

    median = statistics.median(gaps)
    if not MIN_CADENCE_DAYS <= median <= MAX_CADENCE_DAYS:
        return None
    if not _cadence_is_consistent(gaps, median):
        return None

    return RecurrencePattern(
        user_id=series[0].user_id,
        category=category,
        direction=direction,
        event_ids=tuple(event.event_id for event in series),
        dates=tuple(dates),
        cadence_days=int(round(median)),
        amounts=tuple(event.amount for event in series if event.amount is not None),
    )


def recurrence_for_event(
    event,
    events: Iterable,
    *,
    as_of: date | None = None,
    statuses: frozenset[str] = HISTORICAL_STATUSES,
    min_occurrences: int = MIN_OCCURRENCES,
) -> RecurrencePattern | None:
    """Detect the recurring series ``event`` belongs to, if there is one."""
    return detect_recurrence(
        events,
        category=event.category,
        direction=event.direction,
        as_of=as_of,
        statuses=statuses,
        min_occurrences=min_occurrences,
    )


def recurrence_patterns(
    events: Iterable,
    *,
    as_of: date | None = None,
    statuses: frozenset[str] = HISTORICAL_STATUSES,
    min_occurrences: int = MIN_OCCURRENCES,
) -> Mapping[tuple[str, str], RecurrencePattern]:
    """Every recurring series in one user's history, keyed by category+direction."""
    history = _historical(events, as_of=as_of, statuses=statuses)
    keys = sorted({(event.category, event.direction) for event in history})
    patterns: dict[tuple[str, str], RecurrencePattern] = {}
    for category, direction in keys:
        pattern = detect_recurrence(
            history,
            category=category,
            direction=direction,
            as_of=as_of,
            statuses=statuses,
            min_occurrences=min_occurrences,
        )
        if pattern is not None:
            patterns[(category, direction)] = pattern
    return patterns

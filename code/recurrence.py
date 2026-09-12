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

import calendar
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


def conservative_amount(direction: str, amounts: Sequence[Decimal]) -> Decimal | None:
    """The safe figure to project: the largest debit, the smallest credit.

    A variable series must never flatter a forecast, so spending is taken at
    its observed high and income at its observed low. Callers that convert
    currencies first pass the converted amounts.
    """
    if not amounts:
        return None
    return max(amounts) if direction == "debit" else min(amounts)


@dataclass(frozen=True)
class RecurrencePattern:
    """A repeating series of same-category events in one user's history."""

    user_id: str
    category: str
    direction: str
    event_ids: tuple[str, ...]
    #: Settlement dates of the observed occurrences, ascending.
    dates: tuple[date, ...]
    #: Representative interval, for description and coarse comparisons.
    cadence_days: int
    #: The exact repeating gap cycle, rotated so ``cadence_cycle[0]`` is the
    #: gap that follows ``last_date``. A day-based monthly series is ``(30,)``;
    #: a semi-monthly one keeps both of its intervals. Ignored when
    #: ``month_day`` is set.
    cadence_cycle: tuple[int, ...]
    amounts: tuple[Decimal, ...]
    #: Day-of-month anchor when history settles on a calendar schedule (the
    #: 15th of consecutive months). ``31`` means "the last day of the month".
    #: Calendar projection keeps the real settlement day instead of drifting
    #: by a fixed number of days each month.
    month_day: int | None = None

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

    @property
    def conservative_amount(self) -> Decimal | None:
        """The safe figure to project, in the observed currency."""
        return conservative_amount(self.direction, self.amounts)

    def _projected_dates(self, until: date):
        """Yield occurrences after ``last_date`` up to ``until``, in phase."""
        if self.month_day is not None:
            current = self.last_date
            months = 0
            while current <= until:
                months += 1
                current = _anchored(self.last_date, months, self.month_day)
                yield current
            return
        current = self.last_date
        index = 0
        while current <= until:
            current += timedelta(days=self.cadence_cycle[index % len(self.cadence_cycle)])
            index += 1
            yield current

    def next_occurrence_after(self, day: date) -> date:
        """The first projected occurrence strictly after ``day``."""
        for projected in self._projected_dates(day):
            if projected > day:
                return projected
        raise AssertionError("unreachable: the cycle always advances")

    def occurrences_between(self, start: date, end: date) -> tuple[date, ...]:
        """Projected occurrence dates in ``[start, end]``.

        Forward projection continues the observed series in its own phase: a
        series paid on the 1st and the 23rd keeps alternating, rather than
        being flattened into one fabricated interval.
        """
        if end < start:
            return ()
        return tuple(
            day for day in self._projected_dates(end) if day <= end and day >= start
        )


#: A calendar-month gap, allowing for February and for a settlement nudged by
#: a weekend.
MIN_MONTH_GAP_DAYS = 26
MAX_MONTH_GAP_DAYS = 32


def _anchored(origin: date, months: int, month_day: int) -> date:
    """``origin`` advanced by ``months``, settled on its day-of-month anchor.

    The anchor is measured from the original month every time, so a series
    anchored to the 31st returns to the 31st after a short month instead of
    walking backwards. A month too short for the anchor settles on its last
    day, which is the conservative reading of "end of month".
    """
    month_index = origin.year * 12 + (origin.month - 1) + months
    year, month = divmod(month_index, 12)
    last_day = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(month_day, last_day))


def _month_anchor(dates: Sequence[date], gaps: Sequence[int]) -> int | None:
    """The day-of-month a series settles on, or ``None`` if it is not monthly.

    Evidence only: every gap must be one calendar month long, and every
    observation must fall on the anchor day clamped to its own month, so a
    series anchored to the 30th may settle on 28 February and nowhere else.
    A month-end series is the same rule with an anchor of 31. An irregular
    series never becomes a monthly one here.
    """
    if not gaps or any(
        not MIN_MONTH_GAP_DAYS <= gap <= MAX_MONTH_GAP_DAYS for gap in gaps
    ):
        return None
    anchor = max(day.day for day in dates)
    for day in dates:
        last_day = calendar.monthrange(day.year, day.month)[1]
        if day.day != min(anchor, last_day):
            return None
    return anchor


def _alternating_cycle(gaps: Sequence[int]) -> tuple[int, int] | None:
    """The exact two-gap cycle of a semi-monthly series, or ``None``.

    Pay on the 1st and the 23rd gives gaps that alternate (22, 9, 22, 9, ...).
    No single median describes that, but each phase is consistent on its own,
    so both intervals are kept and projected in turn.
    """
    phases = (gaps[0::2], gaps[1::2])
    if len(gaps) < 3 or not all(phases):
        return None
    medians = []
    for phase in phases:
        median = statistics.median(phase)
        if median < MIN_CADENCE_DAYS or not _cadence_is_consistent(phase, median):
            return None
        medians.append(int(round(median)))
    if sum(medians) > MAX_CADENCE_DAYS:
        return None
    return medians[0], medians[1]


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
    month_day = _month_anchor(dates, gaps)
    cycle: tuple[int, ...]
    if month_day is not None or (
        MIN_CADENCE_DAYS <= median <= MAX_CADENCE_DAYS and _cadence_is_consistent(gaps, median)
    ):
        cycle = (int(round(median)),)
    else:
        alternating = _alternating_cycle(gaps)
        if alternating is None:
            return None
        cycle = alternating
    # Rotate so the first entry is the gap that follows the last observation:
    # the series resumes in the phase it actually left off in.
    offset = len(gaps) % len(cycle)
    cycle = cycle[offset:] + cycle[:offset]

    return RecurrencePattern(
        user_id=series[0].user_id,
        category=category,
        direction=direction,
        event_ids=tuple(event.event_id for event in series),
        dates=tuple(dates),
        cadence_days=int(round(sum(cycle) / len(cycle))),
        cadence_cycle=cycle,
        month_day=month_day,
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

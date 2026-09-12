"""Deterministic recurrence detection from historical events only."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from helpers import REAL_DATASET

from dataset_loader import FinancialEvent, load_dataset
from recurrence import (
    MAX_CADENCE_DAYS,
    MIN_OCCURRENCES,
    detect_recurrence,
    recurrence_for_event,
    recurrence_patterns,
)


def make_event(
    event_id: str,
    day: str,
    *,
    category: str = "streaming",
    direction: str = "debit",
    amount: str | None = "20",
    status: str = "settled",
    settled_on: str | None = None,
) -> FinancialEvent:
    return FinancialEvent(
        event_id=event_id,
        user_id="user_a",
        event_type="subscription",
        description="Streaming subscription",
        category=category,
        direction=direction,
        amount=None if amount is None else Decimal(amount),
        currency="ZAR",
        event_date=date.fromisoformat(day),
        settlement_date=None if settled_on == "" else date.fromisoformat(settled_on or day),
        status=status,
        linked_event_id=None,
        flexibility="stoppable",
        minimum_allowed_amount=None,
        line=2,
    )


def series(days, **overrides) -> list[FinancialEvent]:
    return [
        make_event(f"event_{index}", day, **overrides) for index, day in enumerate(days, start=1)
    ]


class Detection(unittest.TestCase):
    def test_three_consistent_occurrences_are_recurring(self) -> None:
        pattern = detect_recurrence(
            series(["2025-10-12", "2025-11-12", "2025-12-12"]),
            category="streaming",
            direction="debit",
        )
        self.assertIsNotNone(pattern)
        self.assertEqual(pattern.occurrences, 3)
        self.assertEqual(pattern.cadence_days, 30)
        self.assertEqual(pattern.first_date, date(2025, 10, 12))
        self.assertEqual(pattern.last_date, date(2025, 12, 12))
        self.assertEqual(pattern.typical_amount, Decimal(20))

    def test_a_single_event_is_not_recurring(self) -> None:
        self.assertIsNone(
            detect_recurrence(
                series(["2025-12-12"]), category="streaming", direction="debit"
            )
        )

    def test_two_events_are_not_recurring(self) -> None:
        self.assertIsNone(
            detect_recurrence(
                series(["2025-11-12", "2025-12-12"]),
                category="streaming",
                direction="debit",
            )
        )
        self.assertEqual(MIN_OCCURRENCES, 3)

    def test_calendar_month_drift_is_accepted(self) -> None:
        pattern = detect_recurrence(
            series(["2025-11-12", "2025-12-12", "2026-01-12", "2026-02-09", "2026-03-12"]),
            category="streaming",
            direction="debit",
        )
        self.assertIsNotNone(pattern)
        self.assertIn(pattern.cadence_days, {30, 31})

    def test_inconsistent_cadence_is_not_recurring(self) -> None:
        self.assertIsNone(
            detect_recurrence(
                series(["2025-01-02", "2025-02-02", "2025-09-19"]),
                category="streaming",
                direction="debit",
            )
        )

    def test_cadence_outside_the_allowed_range(self) -> None:
        self.assertIsNone(
            detect_recurrence(
                series(["2025-12-10", "2025-12-11", "2025-12-12"]),
                category="streaming",
                direction="debit",
            ),
            "a one-day cadence is burst spending, not a commitment",
        )
        long_gap = MAX_CADENCE_DAYS + 30
        start = date(2020, 1, 1)
        days = [(start + timedelta(days=long_gap * i)).isoformat() for i in range(3)]
        self.assertIsNone(
            detect_recurrence(series(days), category="streaming", direction="debit")
        )

    def test_failed_and_cancelled_history_is_ignored(self) -> None:
        for status in ("failed", "cancelled", "pending", "scheduled", "unrealized"):
            self.assertIsNone(
                detect_recurrence(
                    series(
                        ["2025-10-12", "2025-11-12", "2025-12-12"], status=status
                    ),
                    category="streaming",
                    direction="debit",
                ),
                f"{status} rows are not settled history",
            )

    def test_mixed_history_counts_only_settled_rows(self) -> None:
        events = series(["2025-10-12", "2025-11-12"]) + series(
            ["2025-12-12"], status="cancelled"
        )
        self.assertIsNone(
            detect_recurrence(events, category="streaming", direction="debit")
        )

    def test_as_of_excludes_later_events(self) -> None:
        events = series(["2025-10-12", "2025-11-12", "2025-12-12"])
        self.assertIsNone(
            detect_recurrence(
                events,
                category="streaming",
                direction="debit",
                as_of=date(2025, 11, 30),
            ),
            "only two occurrences had happened by the cutoff",
        )
        self.assertIsNotNone(
            detect_recurrence(
                events,
                category="streaming",
                direction="debit",
                as_of=date(2025, 12, 12),
            )
        )

    def test_other_categories_and_directions_are_separate_series(self) -> None:
        events = series(["2025-10-12", "2025-11-12", "2025-12-12"])
        self.assertIsNone(
            detect_recurrence(events, category="dining", direction="debit")
        )
        self.assertIsNone(
            detect_recurrence(events, category="streaming", direction="credit")
        )

    def test_credit_series_is_detected_for_forecasting(self) -> None:
        pattern = detect_recurrence(
            series(
                ["2025-10-25", "2025-11-25", "2025-12-25"],
                category="salary",
                direction="credit",
                amount="2000",
            ),
            category="salary",
            direction="credit",
        )
        self.assertIsNotNone(pattern)
        self.assertEqual(pattern.direction, "credit")
        self.assertEqual(pattern.typical_amount, Decimal(2000))

    def test_missing_amounts_do_not_break_the_typical_amount(self) -> None:
        events = series(["2025-10-12", "2025-11-12"]) + series(
            ["2025-12-12"], amount=None
        )
        pattern = detect_recurrence(events, category="streaming", direction="debit")
        self.assertIsNotNone(pattern)
        self.assertEqual(pattern.occurrences, 3)
        self.assertEqual(len(pattern.amounts), 2)
        self.assertEqual(pattern.typical_amount, Decimal(20))

    def test_recurrence_for_event_uses_the_events_own_key(self) -> None:
        events = series(["2025-10-12", "2025-11-12", "2025-12-12"])
        self.assertIsNotNone(recurrence_for_event(events[-1], events))


class SettlementTiming(unittest.TestCase):
    """Evidence is dated by settlement, not by when the event was raised."""

    def test_a_later_settling_record_is_excluded(self) -> None:
        events = [
            make_event("event_1", "2025-10-12"),
            make_event("event_2", "2025-11-12"),
            # Raised before the cutoff, but the cash moves after it.
            make_event("event_3", "2025-12-10", settled_on="2025-12-20"),
        ]
        self.assertIsNone(
            detect_recurrence(
                events,
                category="streaming",
                direction="debit",
                as_of=date(2025, 12, 12),
            ),
            "a record settling after as_of was not yet known",
        )

    def test_a_record_settled_by_the_cutoff_is_included(self) -> None:
        events = [
            make_event("event_1", "2025-10-12"),
            make_event("event_2", "2025-11-12"),
            make_event("event_3", "2025-12-10", settled_on="2025-12-12"),
        ]
        pattern = detect_recurrence(
            events, category="streaming", direction="debit", as_of=date(2025, 12, 12)
        )
        self.assertIsNotNone(pattern)
        self.assertEqual(pattern.occurrences, 3)
        self.assertEqual(pattern.last_date, date(2025, 12, 12))

    def test_cadence_comes_from_settlement_dates(self) -> None:
        # Event dates are irregular; settlements land every 30 days.
        events = [
            make_event("event_1", "2025-10-02", settled_on="2025-10-12"),
            make_event("event_2", "2025-11-09", settled_on="2025-11-11"),
            make_event("event_3", "2025-12-05", settled_on="2025-12-11"),
        ]
        pattern = detect_recurrence(events, category="streaming", direction="debit")
        self.assertIsNotNone(pattern)
        self.assertEqual(pattern.cadence_days, 30)
        self.assertEqual(
            pattern.dates,
            (date(2025, 10, 12), date(2025, 11, 11), date(2025, 12, 11)),
        )

    def test_settlement_order_drives_the_series_order(self) -> None:
        events = [
            make_event("event_1", "2025-12-05", settled_on="2025-12-11"),
            make_event("event_2", "2025-10-02", settled_on="2025-10-12"),
            make_event("event_3", "2025-11-09", settled_on="2025-11-11"),
        ]
        pattern = detect_recurrence(events, category="streaming", direction="debit")
        self.assertEqual(pattern.dates, tuple(sorted(pattern.dates)))
        self.assertEqual(pattern.event_ids, ("event_2", "event_3", "event_1"))

    def test_a_record_that_never_settled_is_not_evidence(self) -> None:
        events = [
            make_event("event_1", "2025-10-12"),
            make_event("event_2", "2025-11-12"),
            make_event("event_3", "2025-12-12", settled_on=""),
        ]
        self.assertIsNone(
            detect_recurrence(events, category="streaming", direction="debit")
        )

    def test_projection_continues_from_the_last_settlement(self) -> None:
        events = [
            make_event("event_1", "2025-10-02", settled_on="2025-10-12"),
            make_event("event_2", "2025-11-09", settled_on="2025-11-11"),
            make_event("event_3", "2025-12-05", settled_on="2025-12-11"),
        ]
        pattern = detect_recurrence(events, category="streaming", direction="debit")
        self.assertEqual(
            pattern.next_occurrence_after(date(2025, 12, 11)), date(2026, 1, 10)
        )


class Projection(unittest.TestCase):
    def setUp(self) -> None:
        self.pattern = detect_recurrence(
            series(["2025-10-12", "2025-11-11", "2025-12-11"]),
            category="streaming",
            direction="debit",
        )
        self.assertEqual(self.pattern.cadence_days, 30)

    def test_next_occurrence_after_the_last_observed_date(self) -> None:
        self.assertEqual(
            self.pattern.next_occurrence_after(date(2025, 12, 11)), date(2026, 1, 10)
        )

    def test_next_occurrence_skips_forward_past_a_later_date(self) -> None:
        self.assertEqual(
            self.pattern.next_occurrence_after(date(2026, 2, 1)), date(2026, 2, 9)
        )

    def test_occurrences_between_projects_on_cadence(self) -> None:
        projected = self.pattern.occurrences_between(date(2025, 12, 12), date(2026, 3, 1))
        self.assertEqual(
            projected, (date(2026, 1, 10), date(2026, 2, 9))
        )

    def test_occurrences_between_with_an_inverted_window(self) -> None:
        self.assertEqual(
            self.pattern.occurrences_between(date(2026, 3, 1), date(2026, 1, 1)), ()
        )


class RealDatasetPatterns(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(REAL_DATASET)

    def test_users_have_detectable_recurring_commitments(self) -> None:
        request = self.dataset.requests[0]
        patterns = recurrence_patterns(
            self.dataset.events_for(request.user_id), as_of=request.request_date
        )
        self.assertTrue(patterns, "a user with months of history should have patterns")
        for (category, direction), pattern in patterns.items():
            self.assertEqual(pattern.category, category)
            self.assertEqual(pattern.direction, direction)
            self.assertGreaterEqual(pattern.occurrences, MIN_OCCURRENCES)
            self.assertLessEqual(pattern.last_date, request.request_date)
            self.assertGreaterEqual(pattern.cadence_days, 1)

    def test_spending_change_targets_in_the_samples_stay_recurring(self) -> None:
        # Every event the solved examples stop or reduce must still resolve to
        # a settlement-dated pattern established before its request date.
        import csv

        from output_schema import parse_spending_changes

        path = self.dataset.root / "sample_requests.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        checked = 0
        for row in rows:
            sample = self.dataset.sample_request_by_id[row["request_id"]]
            for _action, event_id, _amount in parse_spending_changes(
                row["spending_changes_needed"]
            ):
                event = self.dataset.event_by_id[event_id]
                pattern = recurrence_for_event(
                    event,
                    self.dataset.events_for(event.user_id),
                    as_of=sample.request.request_date,
                )
                self.assertIsNotNone(
                    pattern, f"{event_id} must stay a recurring target"
                )
                self.assertLessEqual(pattern.last_date, sample.request.request_date)
                self.assertIn(event.settlement_date, pattern.dates)
                checked += 1
        self.assertGreater(checked, 0, "the samples should contain spending changes")

    def test_evidence_dates_are_settlement_dates(self) -> None:
        request = self.dataset.requests[0]
        events = self.dataset.events_for(request.user_id)
        settlements = {
            event.settlement_date for event in events if event.settlement_date
        }
        for pattern in recurrence_patterns(events, as_of=request.request_date).values():
            self.assertTrue(set(pattern.dates) <= settlements)

    def test_patterns_are_deterministic(self) -> None:
        request = self.dataset.requests[0]
        events = self.dataset.events_for(request.user_id)
        first = recurrence_patterns(events, as_of=request.request_date)
        second = recurrence_patterns(events, as_of=request.request_date)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

"""Spending changes are proposed only when no plan meets the deadline without them."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import OUTPUT_HEADER, build_dataset, default_tables

from dataset_loader import load_dataset
from engine import recommend
from recommendation_schema import validate_recommendations


def monthly(prefix, category, amount, day, flexibility="stoppable", minimum=""):
    return [
        {
            **default_tables()["financial_events"][3],
            "event_id": f"{prefix}{month}",
            "description": f"{category} plan",
            "category": category,
            "amount": amount,
            "event_date": f"2025-{month}-{day}",
            "settlement_date": f"2025-{month}-{day}",
            "flexibility": flexibility,
            "minimum_allowed_amount": minimum,
        }
        for month in ("10", "11", "12")
    ]


def run(requested, *, extra_events=(), **profile):
    """Recommend for request_a (2026-01-05, deadline 2026-01-10, full payment only).

    The default ledger projects dining (4 x 102.50, reducible to 40, latest
    settled row event_c) and streaming (3 x 20, stoppable, latest event_d)
    against a 1000 balance and a 200 floor: 330 is safe today and nothing
    becomes safe later, since no income is projected.
    """
    tables = default_tables()
    tables["financial_profiles"][0].update(
        {"payment_methods_user_will_consider": "full_payment", "expense_categories_user_is_willing_to_stop": "streaming", **profile}
    )
    tables["requests"][0].update(
        requested_amount=str(requested), desired_completion_date="2026-01-10", allows_partial_payment="false"
    )
    tables["request_payment_options"][0]["payment_amount"] = str(requested)
    tables["request_payment_options"][0]["total_payable_amount"] = str(requested)
    tables["financial_events"] += list(extra_events)
    with tempfile.TemporaryDirectory() as tmp:
        build_dataset(Path(tmp), tables=tables)
        dataset = load_dataset(Path(tmp) / "dataset")
        request = dataset.request_by_id["request_a"]
        row = recommend(dataset, request)
        issues = validate_recommendations(OUTPUT_HEADER, [row], dataset, requests=[request])
        return row, issues


class SpendingChangeTests(unittest.TestCase):
    def assertValid(self, issues):
        self.assertEqual([issue.message for issue in issues], [])

    def test_no_change_when_an_unchanged_plan_meets_the_deadline(self):
        row, issues = run(300)
        self.assertEqual((row["affordability_status"], row["spending_changes_needed"]), ("affordable_now", "none"))
        self.assertValid(issues)

    def test_reduce_to_the_minimum_allowed_amount(self):
        row, issues = run(550)
        self.assertEqual(row["spending_changes_needed"], "reduce_to:event_c:40")
        self.assertEqual(row["affordability_status"], "affordable_with_plan")
        self.assertEqual(row["recommended_payment_method"], "full_payment")
        self.assertEqual(row["payment_plan"], "2026-01-05:550")
        self.assertTrue(row["decision_explanation"].startswith("Reduce Restaurant spending to ZAR 40, then pay"))
        self.assertValid(issues)

    def test_safe_amount_and_earliest_date_stay_on_the_unchanged_forecast(self):
        row, _ = run(550)
        self.assertEqual((row["amount_safe_to_pay"], row["earliest_date_for_full_payment"]), ("330", ""))

    def test_stop(self):
        row, issues = run(380, expense_categories_user_is_willing_to_reduce="")
        # Streaming's latest settled occurrence is the cited event.
        self.assertEqual(row["spending_changes_needed"], "stop:event_d")
        self.assertValid(issues)

    def test_reduce_is_preferred_when_both_are_permitted(self):
        row, issues = run(550, expense_categories_user_is_willing_to_stop="streaming|dining")
        self.assertEqual(row["spending_changes_needed"], "reduce_to:event_c:40")
        self.assertValid(issues)

    def test_protected_or_unpermitted_categories_are_excluded(self):
        for profile in ({"expense_categories_to_protect": "rent|dining"}, {"expense_categories_user_is_willing_to_reduce": "shopping"}):
            with self.subTest(profile=profile):
                row, issues = run(550, **profile)
                self.assertEqual((row["affordability_status"], row["spending_changes_needed"]), ("not_affordable", "none"))
                self.assertValid(issues)

    def test_greedy_adds_the_next_largest_saving_and_stops_when_sufficient(self):
        row, issues = run(620)
        self.assertEqual(row["spending_changes_needed"], "reduce_to:event_c:40|stop:event_d")
        self.assertTrue(row["decision_explanation"].startswith("Reduce Restaurant spending to ZAR 40 and stop Streaming subscription, then"))
        self.assertValid(issues)

    def test_at_most_three_changes(self):
        extra = monthly("event_gym", "gym", "15", "01") + monthly("event_music", "music", "10", "02")
        stop = "streaming|gym|music"
        row, issues = run(600, extra_events=extra, expense_categories_user_is_willing_to_stop=stop)
        self.assertEqual(row["spending_changes_needed"], "reduce_to:event_c:40|stop:event_d|stop:event_gym12")
        self.assertValid(issues)
        # Only all four together would be enough: fall back rather than exceed three.
        row, issues = run(625, extra_events=extra, expense_categories_user_is_willing_to_stop=stop)
        self.assertEqual((row["affordability_status"], row["spending_changes_needed"]), ("not_affordable", "none"))
        self.assertValid(issues)


if __name__ == "__main__":
    unittest.main()

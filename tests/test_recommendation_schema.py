"""Semantic recommendation checks against profiles, options and events."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Sequence

from helpers import REAL_DATASET, build_dataset, default_tables

from dataset_loader import load_dataset
from output_schema import REQUIRED_OUTPUT_COLUMNS
from recommendation_schema import (
    validate_predictions_file,
    validate_recommendation,
    validate_recommendations,
)
from validation import DatasetError

# The synthetic request: 500 ZAR, requested 2026-01-05, due 2026-02-05,
# partial payment allowed. The user accepts full_payment|installments with
# max_installment_months=6, will reduce dining and stop streaming.
SYNTHETIC_INSTALLMENT_PLAN = "2026-01-08:175|2026-02-07:175|2026-03-09:175"


def output_row(**overrides) -> dict[str, str]:
    row = {
        "request_id": "request_a",
        "amount_safe_to_pay": "500",
        "affordability_status": "affordable_now",
        "recommended_payment_method": "full_payment",
        "payment_plan": "2026-01-05:500",
        "earliest_date_for_full_payment": "2026-01-05",
        "spending_changes_needed": "none",
        "decision_explanation": "Balance stays above the minimum.",
    }
    row.update(overrides)
    return row


class SyntheticCase(unittest.TestCase):
    def load(self, tables: Mapping[str, Sequence[Mapping[str, str]]] | None = None):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = build_dataset(Path(tmpdir.name), tables=tables)
        return load_dataset(root)

    def check(self, row, dataset=None):
        dataset = dataset or self.load()
        request = dataset.request_by_id["request_a"]
        return validate_recommendation(row, request, dataset, source="output.csv", line=2)

    def assert_rejected(self, row, contains: str, dataset=None) -> None:
        issues = self.check(row, dataset)
        self.assertTrue(
            any(contains in issue.message for issue in issues),
            f"expected a message containing {contains!r}; got {[str(i) for i in issues]}",
        )

    def assert_accepted(self, row, dataset=None) -> None:
        issues = self.check(row, dataset)
        self.assertEqual([str(issue) for issue in issues], [])


class AcceptedMethods(SyntheticCase):
    def test_full_payment_the_user_accepts(self) -> None:
        self.assert_accepted(output_row())

    def test_method_outside_the_users_accepted_methods(self) -> None:
        self.assert_rejected(
            output_row(
                amount_safe_to_pay="200",
                affordability_status="affordable_with_plan",
                recommended_payment_method="partial_payment",
                payment_plan="2026-01-05:200|2026-01-20:300",
                earliest_date_for_full_payment="2026-01-20",
            ),
            "partial_payment is not in the user's payment_methods_user_will_consider",
        )

    def test_wait_requires_the_user_to_accept_full_payment(self) -> None:
        tables = default_tables()
        tables["financial_profiles"][0]["payment_methods_user_will_consider"] = "installments"
        self.assert_rejected(
            output_row(
                amount_safe_to_pay="0",
                affordability_status="affordable_later",
                recommended_payment_method="wait",
                payment_plan="2026-01-25:500",
                earliest_date_for_full_payment="2026-01-25",
            ),
            "wait requires the user to accept full_payment",
            dataset=self.load(tables),
        )


class InstallmentPlans(SyntheticCase):
    def _installment_row(self, **overrides) -> dict[str, str]:
        row = output_row(
            amount_safe_to_pay="500",
            affordability_status="affordable_with_plan",
            recommended_payment_method="installments",
            payment_plan=SYNTHETIC_INSTALLMENT_PLAN,
            earliest_date_for_full_payment="2026-01-05",
        )
        row.update(overrides)
        return row

    def test_plan_matching_a_supplied_option(self) -> None:
        self.assert_accepted(self._installment_row())

    def test_arbitrary_schedule_is_rejected(self) -> None:
        self.assert_rejected(
            self._installment_row(payment_plan="2026-01-08:250|2026-02-08:250"),
            "installment plan does not exactly match any supplied installment option",
        )

    def test_shifted_date_is_rejected(self) -> None:
        self.assert_rejected(
            self._installment_row(payment_plan="2026-01-09:175|2026-02-07:175|2026-03-09:175"),
            "does not exactly match any supplied installment option",
        )

    def test_changed_amount_is_rejected(self) -> None:
        self.assert_rejected(
            self._installment_row(payment_plan="2026-01-08:175|2026-02-07:175|2026-03-09:174"),
            "does not exactly match any supplied installment option",
        )

    def test_truncated_schedule_is_rejected(self) -> None:
        self.assert_rejected(
            self._installment_row(payment_plan="2026-01-08:175|2026-02-07:175"),
            "does not exactly match any supplied installment option",
        )

    def test_blank_max_installment_months_forbids_installments(self) -> None:
        tables = default_tables()
        tables["financial_profiles"][0]["max_installment_months"] = ""
        self.assert_rejected(
            self._installment_row(),
            "max_installment_months is blank",
            dataset=self.load(tables),
        )

    def test_too_many_payments_for_max_installment_months(self) -> None:
        tables = default_tables()
        tables["financial_profiles"][0]["max_installment_months"] = "2"
        self.assert_rejected(
            self._installment_row(),
            "above the user's max_installment_months of 2",
            dataset=self.load(tables),
        )


class PlanShapes(SyntheticCase):
    def test_full_payment_pays_the_whole_request_on_request_date(self) -> None:
        self.assert_accepted(output_row())

    def test_full_payment_with_no_plan(self) -> None:
        self.assert_rejected(
            output_row(payment_plan="none"),
            "full_payment requires exactly one payment of the full requested amount, found 0",
        )

    def test_full_payment_on_the_wrong_date(self) -> None:
        self.assert_rejected(
            output_row(payment_plan="2026-01-06:500"),
            "full_payment must pay on 2026-01-05, found 2026-01-06",
        )

    def test_full_payment_for_less_than_the_request(self) -> None:
        self.assert_rejected(
            output_row(payment_plan="2026-01-05:400"),
            "full_payment must pay the full requested_amount (500), found 400",
        )

    def test_full_payment_split_across_two_dates(self) -> None:
        self.assert_rejected(
            output_row(payment_plan="2026-01-05:250|2026-02-05:250"),
            "full_payment requires exactly one payment of the full requested amount, found 2",
        )

    def test_installments_with_no_plan(self) -> None:
        self.assert_rejected(
            output_row(
                affordability_status="affordable_with_plan",
                recommended_payment_method="installments",
                payment_plan="none",
            ),
            "installments requires a non-empty payment plan matching a supplied "
            "installment option",
        )

    def test_wait_pays_the_whole_request_on_the_earliest_date(self) -> None:
        self.assert_accepted(
            output_row(
                amount_safe_to_pay="0",
                affordability_status="affordable_later",
                recommended_payment_method="wait",
                payment_plan="2026-01-25:500",
                earliest_date_for_full_payment="2026-01-25",
            )
        )

    def test_wait_with_no_plan(self) -> None:
        self.assert_rejected(
            output_row(
                amount_safe_to_pay="0",
                affordability_status="affordable_later",
                recommended_payment_method="wait",
                payment_plan="none",
                earliest_date_for_full_payment="2026-01-25",
            ),
            "wait requires exactly one payment of the full requested amount, found 0",
        )

    def test_wait_without_an_earliest_date(self) -> None:
        issues = self.check(
            output_row(
                amount_safe_to_pay="0",
                affordability_status="affordable_later",
                recommended_payment_method="wait",
                payment_plan="2026-01-25:500",
                earliest_date_for_full_payment="",
            )
        )
        self.assertTrue(
            any("wait requires a valid earliest_date_for_full_payment" in issue.message
                for issue in issues),
            [str(issue) for issue in issues],
        )

    def test_wait_paying_on_a_date_other_than_the_earliest_date(self) -> None:
        self.assert_rejected(
            output_row(
                amount_safe_to_pay="0",
                affordability_status="affordable_later",
                recommended_payment_method="wait",
                payment_plan="2026-01-30:500",
                earliest_date_for_full_payment="2026-01-25",
            ),
            "wait must pay on 2026-01-25, found 2026-01-30",
        )

    def test_wait_paying_less_than_the_request(self) -> None:
        self.assert_rejected(
            output_row(
                amount_safe_to_pay="0",
                affordability_status="affordable_later",
                recommended_payment_method="wait",
                payment_plan="2026-01-25:300",
                earliest_date_for_full_payment="2026-01-25",
            ),
            "wait must pay the full requested_amount (500), found 300",
        )

    def test_wait_must_be_later_than_request_date(self) -> None:
        for day in ("2026-01-05", "2026-01-01"):
            self.assert_rejected(
                output_row(
                    amount_safe_to_pay="0",
                    affordability_status="affordable_later",
                    recommended_payment_method="wait",
                    payment_plan=f"{day}:500",
                    earliest_date_for_full_payment=day,
                ),
                "wait requires earliest_date_for_full_payment to be later than "
                "request_date (2026-01-05)",
            )


class NotRecommendedRows(SyntheticCase):
    def _row(self, **overrides) -> dict[str, str]:
        row = output_row(
            amount_safe_to_pay="0",
            affordability_status="not_affordable",
            recommended_payment_method="not_recommended",
            payment_plan="none",
            earliest_date_for_full_payment="",
        )
        row.update(overrides)
        return row

    def test_clean_not_recommended_row(self) -> None:
        self.assert_accepted(self._row())

    def test_payment_plan_must_be_none(self) -> None:
        self.assert_rejected(
            self._row(payment_plan="2026-01-05:500"),
            "not_recommended requires payment_plan 'none'",
        )

    def test_full_payment_date_must_be_empty(self) -> None:
        self.assert_rejected(
            self._row(earliest_date_for_full_payment="2026-01-20"),
            "not_recommended requires an empty earliest_date_for_full_payment",
        )


class SpendingChanges(SyntheticCase):
    def _row(self, changes: str) -> dict[str, str]:
        return output_row(
            affordability_status="affordable_with_plan", spending_changes_needed=changes
        )

    def test_eligible_stop_and_reduce(self) -> None:
        # event_d (streaming, monthly) and event_c (dining, every 21 days) both
        # have repeated settled history before the request date.
        self.assert_accepted(self._row("stop:event_d|reduce_to:event_c:40"))

    def test_one_off_flexible_expense_cannot_be_stopped(self) -> None:
        # event_f is flexible and in a category the user will stop, but it has
        # happened exactly once, so it is not a recurring commitment.
        self.assert_rejected(
            self._row("stop:event_f"),
            "event_f is a one-off expense: category 'shopping' has no recurring "
            "debit history on or before 2026-01-05",
        )

    def test_one_off_flexible_expense_cannot_be_reduced(self) -> None:
        self.assert_rejected(
            self._row("reduce_to:event_f:30"),
            "event_f is a one-off expense",
        )

    def test_recurrence_evidence_must_predate_the_request(self) -> None:
        tables = default_tables()
        for row in tables["financial_events"]:
            if row["event_id"] in {"event_c1", "event_c2", "event_c3"}:
                row["event_date"] = "2026-06-10"
                row["settlement_date"] = "2026-06-10"
        self.assert_rejected(
            self._row("reduce_to:event_c:40"),
            "has no recurring debit history on or before 2026-01-05",
            dataset=self.load(tables),
        )

    def test_cancelled_history_does_not_establish_recurrence(self) -> None:
        tables = default_tables()
        for row in tables["financial_events"]:
            if row["event_id"] in {"event_d1", "event_d2", "event_d3"}:
                row["status"] = "cancelled"
        self.assert_rejected(
            self._row("stop:event_d"),
            "is a one-off expense",
            dataset=self.load(tables),
        )

    def test_cancelled_target_event_cannot_be_changed(self) -> None:
        tables = default_tables()
        for row in tables["financial_events"]:
            if row["event_id"] == "event_d":
                row["status"] = "cancelled"
        self.assert_rejected(
            self._row("stop:event_d"),
            "has status 'cancelled' and is not a live commitment",
            dataset=self.load(tables),
        )

    def test_unknown_event(self) -> None:
        self.assert_rejected(
            self._row("stop:event_zzz"), "which is not in financial_events.csv"
        )

    def test_event_belonging_to_another_user(self) -> None:
        tables = default_tables()
        other = dict(tables["financial_events"][3])
        other["event_id"] = "event_other"
        other["user_id"] = "user_b"
        tables["financial_events"].append(other)
        profile = dict(tables["financial_profiles"][0])
        profile["user_id"] = "user_b"
        tables["financial_profiles"].append(profile)
        self.assert_rejected(
            self._row("stop:event_other"),
            "belongs to user_b and not to user_a",
            dataset=self.load(tables),
        )

    def test_credit_event_cannot_be_changed(self) -> None:
        self.assert_rejected(
            self._row("stop:event_e"), "a credit event; only debits can be stopped"
        )

    def test_fixed_event_cannot_be_stopped(self) -> None:
        self.assert_rejected(
            self._row("stop:event_a"), "flexibility 'fixed' and cannot be stopped"
        )

    def test_protected_category_is_rejected(self) -> None:
        self.assert_rejected(self._row("stop:event_a"), "protected category 'rent'")

    def test_category_the_user_will_not_stop(self) -> None:
        self.assert_rejected(
            self._row("stop:event_c"), "the user will not stop category 'dining'"
        )

    def test_category_the_user_will_not_reduce(self) -> None:
        self.assert_rejected(
            self._row("reduce_to:event_d:10"),
            "the user will not reduce category 'streaming'",
        )

    def test_reduction_below_minimum_allowed_amount(self) -> None:
        self.assert_rejected(
            self._row("reduce_to:event_c:10"),
            "below its minimum_allowed_amount of 40",
        )

    def test_reduction_that_does_not_reduce(self) -> None:
        self.assert_rejected(
            self._row("reduce_to:event_c:100"),
            "is not a reduction from its current amount of 100",
        )


class WholeFile(SyntheticCase):
    def _write(self, path: Path, rows) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(REQUIRED_OUTPUT_COLUMNS))
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def test_valid_predictions_file(self) -> None:
        dataset = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.csv"
            self._write(path, [output_row()])
            self.assertEqual(validate_predictions_file(path, dataset), [])

    def test_semantic_failure_is_reported_with_line_and_request(self) -> None:
        dataset = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.csv"
            self._write(
                path,
                [
                    output_row(
                        affordability_status="affordable_with_plan",
                        recommended_payment_method="installments",
                        payment_plan="2026-01-08:250|2026-02-08:250",
                    )
                ],
            )
            issues = validate_predictions_file(path, dataset)
            self.assertTrue(issues)
            self.assertEqual(issues[0].line, 2)
            self.assertEqual(issues[0].identifier, "request_a")
            with self.assertRaises(DatasetError):
                validate_predictions_file(path, dataset, strict=True)

    def test_structural_and_semantic_issues_are_both_reported(self) -> None:
        dataset = self.load()
        issues = validate_recommendations(
            REQUIRED_OUTPUT_COLUMNS,
            [
                output_row(
                    amount_safe_to_pay="900",
                    affordability_status="affordable_with_plan",
                    recommended_payment_method="installments",
                    payment_plan="2026-01-08:250|2026-02-08:250",
                )
            ],
            dataset,
        )
        messages = " ".join(issue.message for issue in issues)
        self.assertIn("outside 0..500", messages)
        self.assertIn("does not exactly match any supplied installment option", messages)

    def test_missing_file(self) -> None:
        dataset = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            issues = validate_predictions_file(Path(tmp) / "nope.csv", dataset)
            self.assertIn("file is missing", issues[0].message)


class SolvedExamplesStillPass(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(REAL_DATASET)

    def test_all_solved_samples_pass_the_semantic_contract(self) -> None:
        path = self.dataset.root / "sample_requests.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), len(self.dataset.sample_requests))
        self.assertEqual(len(rows), 25)
        for index, row in enumerate(rows):
            sample = self.dataset.sample_request_by_id[row["request_id"]]
            issues = validate_recommendation(
                row,
                sample.request,
                self.dataset,
                source="dataset/sample_requests.csv",
                line=index + 2,
            )
            self.assertEqual([str(issue) for issue in issues], [])

    def test_every_recommended_method_appears_in_the_samples(self) -> None:
        methods = {
            sample.recommended_payment_method for sample in self.dataset.sample_requests
        }
        self.assertTrue(
            {"full_payment", "installments", "partial_payment", "wait", "not_recommended"}
            <= methods,
            methods,
        )


if __name__ == "__main__":
    unittest.main()

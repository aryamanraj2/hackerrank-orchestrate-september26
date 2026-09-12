"""The reusable output-schema validator."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from helpers import OUTPUT_HEADER, REAL_DATASET

from dataset_loader import Request, load_dataset
from output_schema import (
    REQUIRED_OUTPUT_COLUMNS,
    parse_payment_plan,
    parse_spending_changes,
    validate_output_columns,
    validate_output_file,
    validate_output_request_ids,
    validate_output_row_values,
    validate_output_rows,
)
from validation import DatasetError


def make_request(**overrides) -> Request:
    base = dict(
        request_id="request_a",
        user_id="user_a",
        request_date=date(2026, 1, 5),
        request_type="purchase",
        requested_amount=Decimal(500),
        desired_completion_date=date(2026, 2, 5),
        allows_partial_payment=True,
        request_text="Can I afford this?",
        line=2,
    )
    base.update(overrides)
    return Request(**base)


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


class Columns(unittest.TestCase):
    def test_required_columns_are_the_eight_contract_columns(self) -> None:
        self.assertEqual(REQUIRED_OUTPUT_COLUMNS, OUTPUT_HEADER)
        self.assertEqual(len(REQUIRED_OUTPUT_COLUMNS), 8)

    def test_exact_header_passes(self) -> None:
        self.assertEqual(validate_output_columns(REQUIRED_OUTPUT_COLUMNS), [])

    def test_missing_column(self) -> None:
        issues = validate_output_columns(REQUIRED_OUTPUT_COLUMNS[:-1])
        self.assertEqual(len(issues), 1)
        self.assertIn("missing required column(s): decision_explanation", issues[0].message)

    def test_unexpected_column(self) -> None:
        issues = validate_output_columns(REQUIRED_OUTPUT_COLUMNS + ("confidence",))
        self.assertIn("unexpected column(s): confidence", issues[0].message)

    def test_wrong_order(self) -> None:
        swapped = (REQUIRED_OUTPUT_COLUMNS[1], REQUIRED_OUTPUT_COLUMNS[0]) + REQUIRED_OUTPUT_COLUMNS[2:]
        issues = validate_output_columns(swapped)
        self.assertIn("wrong order", issues[0].message)


class RequestCoverage(unittest.TestCase):
    def test_one_row_per_request(self) -> None:
        self.assertEqual(validate_output_request_ids(["r1", "r2"], ["r1", "r2"]), [])

    def test_missing_row(self) -> None:
        issues = validate_output_request_ids(["r1"], ["r1", "r2"])
        self.assertEqual([issue.identifier for issue in issues], ["r2"])
        self.assertIn("no output row", issues[0].message)

    def test_duplicate_row(self) -> None:
        issues = validate_output_request_ids(["r1", "r1"], ["r1"])
        self.assertIn("duplicate row", issues[0].message)
        self.assertEqual(issues[0].line, 3)

    def test_unknown_request(self) -> None:
        issues = validate_output_request_ids(["r1", "r9"], ["r1"])
        self.assertTrue(any("not a request_id present" in issue.message for issue in issues))

    def test_out_of_order_rows(self) -> None:
        issues = validate_output_request_ids(["r2", "r1"], ["r1", "r2"])
        self.assertIn("not in the same order", issues[0].message)
        self.assertEqual(validate_output_request_ids(["r2", "r1"], ["r1", "r2"], require_same_order=False), [])


class PlanParsing(unittest.TestCase):
    def test_none_is_an_empty_plan(self) -> None:
        self.assertEqual(parse_payment_plan("none"), ())
        self.assertEqual(parse_payment_plan(""), ())

    def test_multiple_payments(self) -> None:
        plan = parse_payment_plan("2026-01-05:100|2026-02-05:200.50")
        self.assertEqual(
            plan, ((date(2026, 1, 5), Decimal("100")), (date(2026, 2, 5), Decimal("200.50")))
        )

    def test_bad_entries_raise(self) -> None:
        for text in (
            "2026-01-05",
            "05-01-2026:100",
            "2026-01-05:many",
            "2026-01-05:1:2",
            "2026-01-05:NaN",
            "2026-01-05:Infinity",
            "2026-01-05:0",
            "2026-01-05:-1",
        ):
            with self.assertRaises(ValueError, msg=text):
                parse_payment_plan(text)

    def test_spending_changes(self) -> None:
        self.assertEqual(parse_spending_changes("none"), ())
        self.assertEqual(
            parse_spending_changes("stop:event_14|reduce_to:event_21:100"),
            (("stop", "event_14", None), ("reduce_to", "event_21", Decimal(100))),
        )
        for text in (
            "stop",
            "pause:event_1",
            "reduce_to:event_1",
            "reduce_to:event_1:x",
            "reduce_to:event_1:NaN",
            "reduce_to:event_1:-5",
        ):
            with self.assertRaises(ValueError, msg=text):
                parse_spending_changes(text)


class RowValues(unittest.TestCase):
    def test_valid_row(self) -> None:
        self.assertEqual(validate_output_row_values(output_row(), make_request()), [])

    def test_amount_out_of_bounds(self) -> None:
        issues = validate_output_row_values(
            output_row(amount_safe_to_pay="600"), make_request()
        )
        self.assertIn("outside 0..500", issues[0].message)

    def test_blank_amount_is_rejected(self) -> None:
        issues = validate_output_row_values(output_row(amount_safe_to_pay=""), make_request())
        self.assertIn("blank, non-numeric or not finite", issues[0].message)

    def test_non_finite_amount_is_rejected(self) -> None:
        for text in ("NaN", "Infinity", "-Infinity", "inf"):
            issues = validate_output_row_values(
                output_row(amount_safe_to_pay=text), make_request()
            )
            self.assertTrue(
                any("not finite" in issue.message for issue in issues),
                f"{text} should be rejected: {[str(i) for i in issues]}",
            )

    def test_non_positive_plan_amount_is_rejected(self) -> None:
        for text in ("2026-01-05:0", "2026-01-05:-500"):
            issues = validate_output_row_values(
                output_row(payment_plan=text), make_request()
            )
            self.assertTrue(
                any("strictly positive" in issue.message for issue in issues),
                f"{text} should be rejected: {[str(i) for i in issues]}",
            )

    def test_non_finite_plan_amount_is_rejected(self) -> None:
        issues = validate_output_row_values(
            output_row(payment_plan="2026-01-05:NaN"), make_request()
        )
        self.assertTrue(any("non-finite" in issue.message for issue in issues))

    def test_partial_payment_requires_an_earliest_date(self) -> None:
        issues = validate_output_row_values(
            output_row(
                amount_safe_to_pay="200",
                affordability_status="affordable_with_plan",
                recommended_payment_method="partial_payment",
                payment_plan="2026-01-05:200|2026-01-20:300",
                earliest_date_for_full_payment="",
            ),
            make_request(),
        )
        messages = " ".join(issue.message for issue in issues)
        self.assertIn("partial_payment requires a valid earliest_date_for_full_payment", messages)
        self.assertIn("second partial payment must fall on", messages)

    def test_partial_payment_second_date_must_equal_earliest_date(self) -> None:
        issues = validate_output_row_values(
            output_row(
                amount_safe_to_pay="200",
                affordability_status="affordable_with_plan",
                recommended_payment_method="partial_payment",
                payment_plan="2026-01-05:200|2026-01-25:300",
                earliest_date_for_full_payment="2026-01-20",
            ),
            make_request(),
        )
        self.assertTrue(
            any("earliest_date_for_full_payment (2026-01-20)" in issue.message for issue in issues)
        )

    def test_unknown_status_and_method(self) -> None:
        issues = validate_output_row_values(
            output_row(affordability_status="maybe", recommended_payment_method="cash"),
            make_request(),
        )
        messages = " ".join(issue.message for issue in issues)
        self.assertIn("affordability_status is not an allowed value", messages)
        self.assertIn("recommended_payment_method is not an allowed value", messages)

    def test_affordable_now_requires_request_date(self) -> None:
        issues = validate_output_row_values(
            output_row(earliest_date_for_full_payment="2026-01-09"), make_request()
        )
        self.assertIn("affordable_now requires", issues[0].message)

    def test_plan_must_be_chronological(self) -> None:
        issues = validate_output_row_values(
            output_row(
                affordability_status="affordable_with_plan",
                recommended_payment_method="installments",
                payment_plan="2026-02-05:250|2026-01-05:250",
                earliest_date_for_full_payment="2026-01-05",
            ),
            make_request(),
        )
        self.assertTrue(any("chronological" in issue.message for issue in issues))

    def test_valid_partial_payment(self) -> None:
        issues = validate_output_row_values(
            output_row(
                amount_safe_to_pay="200",
                affordability_status="affordable_with_plan",
                recommended_payment_method="partial_payment",
                payment_plan="2026-01-05:200|2026-01-20:300",
                earliest_date_for_full_payment="2026-01-20",
            ),
            make_request(),
        )
        self.assertEqual([str(issue) for issue in issues], [])

    def test_partial_payment_must_sum_to_requested_amount(self) -> None:
        issues = validate_output_row_values(
            output_row(
                amount_safe_to_pay="200",
                affordability_status="affordable_with_plan",
                recommended_payment_method="partial_payment",
                payment_plan="2026-01-05:200|2026-01-20:250",
                earliest_date_for_full_payment="2026-01-20",
            ),
            make_request(),
        )
        self.assertTrue(any("add up to requested_amount" in issue.message for issue in issues))

    def test_partial_payment_after_completion_date(self) -> None:
        issues = validate_output_row_values(
            output_row(
                amount_safe_to_pay="200",
                affordability_status="affordable_with_plan",
                recommended_payment_method="partial_payment",
                payment_plan="2026-01-05:200|2026-03-20:300",
                earliest_date_for_full_payment="2026-03-20",
            ),
            make_request(),
        )
        self.assertTrue(
            any("after desired_completion_date" in issue.message for issue in issues)
        )

    def test_partial_payment_needs_permission_and_plan_status(self) -> None:
        issues = validate_output_row_values(
            output_row(
                amount_safe_to_pay="200",
                affordability_status="affordable_later",
                recommended_payment_method="partial_payment",
                payment_plan="2026-01-05:200|2026-01-20:300",
                earliest_date_for_full_payment="2026-01-20",
            ),
            make_request(allows_partial_payment=False),
        )
        messages = " ".join(issue.message for issue in issues)
        self.assertIn("does not allow partial payment", messages)
        self.assertIn("affordable_with_plan", messages)

    def test_spending_changes_limits(self) -> None:
        issues = validate_output_row_values(
            output_row(
                spending_changes_needed="stop:e1|stop:e2|stop:e3|stop:e4",
            ),
            make_request(),
        )
        self.assertIn("at most 3 spending changes", issues[0].message)

    def test_stop_and_reduce_are_mutually_exclusive(self) -> None:
        issues = validate_output_row_values(
            output_row(spending_changes_needed="stop:e1|reduce_to:e1:50"), make_request()
        )
        self.assertIn("mutually exclusive", issues[0].message)

    def test_blank_explanation(self) -> None:
        issues = validate_output_row_values(
            output_row(decision_explanation="  "), make_request()
        )
        self.assertIn("decision_explanation is blank", issues[0].message)

    def test_issue_string_names_file_row_and_request(self) -> None:
        issues = validate_output_row_values(
            output_row(amount_safe_to_pay="600"),
            make_request(),
            source="output.csv",
            line=7,
        )
        self.assertIn("output.csv:7", str(issues[0]))
        self.assertIn("request_a", str(issues[0]))


class WholeFile(unittest.TestCase):
    def _write(self, path: Path, header, rows) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(header))
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def test_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.csv"
            self._write(path, REQUIRED_OUTPUT_COLUMNS, [output_row()])
            self.assertEqual(validate_output_file(path, [make_request()]), [])

    def test_blank_template_passes_coverage_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.csv"
            self._write(
                path,
                REQUIRED_OUTPUT_COLUMNS,
                [{"request_id": "request_a"}],
            )
            self.assertEqual(
                validate_output_file(path, [make_request()], check_values=False), []
            )
            self.assertTrue(validate_output_file(path, [make_request()]))

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issues = validate_output_file(Path(tmp) / "nope.csv", [make_request()])
            self.assertIn("file is missing", issues[0].message)

    def test_strict_mode_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.csv"
            self._write(path, REQUIRED_OUTPUT_COLUMNS, [output_row(amount_safe_to_pay="900")])
            with self.assertRaises(DatasetError):
                validate_output_file(path, [make_request()], strict=True)

    def test_rows_validator_skips_values_for_unknown_requests(self) -> None:
        issues = validate_output_rows(
            REQUIRED_OUTPUT_COLUMNS,
            [output_row(request_id="request_zzz")],
            [make_request()],
        )
        sources = {issue.message for issue in issues}
        self.assertTrue(any("not a request_id present" in message for message in sources))


class RealTemplateAndSamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(REAL_DATASET)

    def test_shipped_template_matches_the_contract(self) -> None:
        self.assertEqual(self.dataset.output_template_columns, REQUIRED_OUTPUT_COLUMNS)
        self.assertEqual(
            validate_output_file(
                self.dataset.root / "output.csv", self.dataset.requests, check_values=False
            ),
            [],
        )

    def test_solved_examples_satisfy_the_value_contract(self) -> None:
        path = self.dataset.root / "sample_requests.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        for index, row in enumerate(rows):
            sample = self.dataset.sample_request_by_id[row["request_id"]]
            issues = validate_output_row_values(
                row,
                sample.request,
                source="dataset/sample_requests.csv",
                line=index + 2,
            )
            self.assertEqual([str(issue) for issue in issues], [])


if __name__ == "__main__":
    unittest.main()

"""Referential-integrity and schema failures produce actionable messages."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Sequence

from helpers import OUTPUT_HEADER, build_dataset, default_tables

from dataset_loader import load_dataset
from validation import DatasetError


class DatasetValidationCase(unittest.TestCase):
    def load_with(
        self,
        tables: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
        **build_kwargs,
    ):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = build_dataset(Path(self.tmpdir.name), tables=tables, **build_kwargs)
        return load_dataset(root)

    def assert_issue(self, dataset, *, source: str, identifier: str, contains: str) -> None:
        matches = [
            issue
            for issue in dataset.issues
            if issue.source == source and issue.identifier == identifier
        ]
        self.assertTrue(
            matches,
            f"expected an issue on {source} for {identifier}; got "
            f"{[str(i) for i in dataset.issues]}",
        )
        self.assertTrue(
            any(contains in issue.message for issue in matches),
            f"expected a message containing {contains!r}; got "
            f"{[i.message for i in matches]}",
        )
        for issue in matches:
            self.assertIsNotNone(issue.line, "every row-level issue names its CSV line")
            self.assertIn(source, str(issue))
            self.assertIn(identifier, str(issue))


class ValidBaseline(DatasetValidationCase):
    def test_minimal_dataset_is_clean(self) -> None:
        dataset = self.load_with()
        self.assertEqual([str(issue) for issue in dataset.issues], [])
        self.assertEqual(len(dataset.requests), 1)
        self.assertIsNone(dataset.event_by_id["event_b"].amount)


class MissingReferences(DatasetValidationCase):
    def test_request_without_a_profile(self) -> None:
        tables = default_tables()
        tables["requests"][0]["user_id"] = "user_ghost"
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="requests.csv",
            identifier="request_a",
            contains="'user_ghost' which is not present in financial_profiles.csv",
        )

    def test_event_with_unknown_linked_event(self) -> None:
        tables = default_tables()
        tables["financial_events"][1]["linked_event_id"] = "event_zzz"
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="financial_events.csv",
            identifier="event_b",
            contains="'event_zzz' which is not present in financial_events.csv",
        )

    def test_option_for_unknown_request(self) -> None:
        tables = default_tables()
        tables["request_payment_options"][1]["request_id"] = "request_zzz"
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="request_payment_options.csv",
            identifier="payment_option_02",
            contains="'request_zzz' which is not present",
        )

    def test_message_pointing_at_a_missing_event(self) -> None:
        tables = default_tables()
        tables["messages"][0]["related_event_id"] = "event_zzz"
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="messages.csv",
            identifier="message_a",
            contains="not present in financial_events.csv",
        )

    def test_image_pointing_at_a_missing_request(self) -> None:
        tables = default_tables()
        tables["images"][0]["request_id"] = "request_zzz"
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="images.csv",
            identifier="image_a",
            contains="'request_zzz' which is not present",
        )


class BlankAmountsNeedEvidence(DatasetValidationCase):
    def test_blank_amount_without_a_linked_image(self) -> None:
        tables = default_tables()
        tables["images"] = []
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="financial_events.csv",
            identifier="event_b",
            contains="amount is blank and no images.csv row links to this event",
        )

    def test_blank_amount_whose_image_file_is_absent(self) -> None:
        dataset = self.load_with(image_files=[])
        self.assert_issue(
            dataset,
            source="images.csv",
            identifier="image_a",
            contains="image file is missing for blank amount on event event_b",
        )


class StructuralRules(DatasetValidationCase):
    def test_duplicate_identifier(self) -> None:
        tables = default_tables()
        tables["financial_events"].append(dict(tables["financial_events"][0]))
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="financial_events.csv",
            identifier="event_a",
            contains="duplicate identifier",
        )

    def test_too_few_payment_options(self) -> None:
        tables = default_tables()
        tables["request_payment_options"] = tables["request_payment_options"][:1]
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="request_payment_options.csv",
            identifier="request_a",
            contains="request has 1 payment option(s)",
        )

    def test_missing_settlement_date_on_a_cash_event(self) -> None:
        tables = default_tables()
        tables["financial_events"][0]["settlement_date"] = ""
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="financial_events.csv",
            identifier="event_a",
            contains="settlement_date is blank for status 'settled'",
        )

    def test_unrealized_valuation_may_omit_settlement_date(self) -> None:
        tables = default_tables()
        tables["financial_events"][0].update(
            {
                "event_type": "investment_valuation",
                "direction": "non_cash",
                "status": "unrealized",
                "settlement_date": "",
            }
        )
        dataset = self.load_with(tables)
        self.assertEqual([str(issue) for issue in dataset.issues], [])

    def test_completion_date_before_request_date(self) -> None:
        tables = default_tables()
        tables["requests"][0]["desired_completion_date"] = "2025-12-01"
        dataset = self.load_with(tables)
        self.assert_issue(
            dataset,
            source="requests.csv",
            identifier="request_a",
            contains="desired_completion_date is before request_date",
        )

    def test_output_template_must_match_the_requests(self) -> None:
        dataset = self.load_with(output_request_ids=[])
        self.assertTrue(
            any(
                issue.source == "dataset/output.csv"
                and issue.identifier == "request_a"
                and "no output row" in issue.message
                for issue in dataset.issues
            ),
            [str(issue) for issue in dataset.issues],
        )

    def test_output_template_column_drift_is_reported(self) -> None:
        dataset = self.load_with(output_header=OUTPUT_HEADER[:-1])
        self.assertTrue(
            any(
                issue.source == "dataset/output.csv" and "missing required column" in issue.message
                for issue in dataset.issues
            ),
            [str(issue) for issue in dataset.issues],
        )

    def test_strict_mode_raises_with_every_issue(self) -> None:
        tables = default_tables()
        tables["requests"][0]["user_id"] = "user_ghost"
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = build_dataset(Path(tmpdir.name), tables=tables)
        with self.assertRaises(DatasetError) as ctx:
            load_dataset(root, strict=True)
        self.assertTrue(ctx.exception.issues)
        self.assertIn("requests.csv", str(ctx.exception))
        self.assertIn("request_a", str(ctx.exception))


class UnparsableRowsFailFast(DatasetValidationCase):
    def _expect_error(self, tables) -> DatasetError:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = build_dataset(Path(tmpdir.name), tables=tables)
        with self.assertRaises(DatasetError) as ctx:
            load_dataset(root)
        return ctx.exception

    def test_non_numeric_amount_names_file_row_and_identifier(self) -> None:
        tables = default_tables()
        tables["requests"][0]["requested_amount"] = "about 500"
        error = self._expect_error(tables)
        issue = error.issues[0]
        self.assertEqual(issue.source, "dataset/requests.csv")
        self.assertEqual(issue.identifier, "request_a")
        self.assertEqual(issue.line, 2)
        self.assertIn("requested_amount", issue.message)

    def test_unknown_request_type(self) -> None:
        tables = default_tables()
        tables["requests"][0]["request_type"] = "yacht"
        error = self._expect_error(tables)
        self.assertIn("request_type", str(error))
        self.assertIn("request_a", str(error))

    def test_unknown_payment_method(self) -> None:
        tables = default_tables()
        tables["request_payment_options"][0]["payment_method"] = "barter"
        error = self._expect_error(tables)
        self.assertIn("payment_method", str(error))
        self.assertIn("payment_option_01", str(error))

    def test_missing_column_is_reported_on_the_header_line(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = build_dataset(Path(tmpdir.name))
        target = root / "requests.csv"
        lines = target.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("requested_amount,", "")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(DatasetError) as ctx:
            load_dataset(root)
        issue = ctx.exception.issues[0]
        self.assertEqual(issue.line, 1)
        self.assertIn("missing required column(s): requested_amount", issue.message)

    def test_missing_file_is_reported(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = build_dataset(Path(tmpdir.name))
        (root / "messages.csv").unlink()
        with self.assertRaises(DatasetError) as ctx:
            load_dataset(root)
        self.assertIn("dataset/messages.csv", str(ctx.exception))
        self.assertIn("file is missing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

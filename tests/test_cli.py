"""The `code/main.py` validation CLI."""

from __future__ import annotations

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path

from helpers import REAL_DATASET, build_dataset, default_tables

import main
from output_schema import REQUIRED_OUTPUT_COLUMNS


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class ValidateRealDataset(unittest.TestCase):
    def test_default_invocation_validates_and_succeeds(self) -> None:
        code, out, err = run_cli([])
        self.assertEqual(code, 0, err)
        self.assertIn("no contract violations", out)

    def test_validate_flag_reports_the_request_count_and_contract(self) -> None:
        code, out, err = run_cli(["--validate", "--dataset", str(REAL_DATASET)])
        self.assertEqual(code, 0, err)
        self.assertIn("250 evaluation requests", out)
        self.assertIn(
            f"output contract: {len(REQUIRED_OUTPUT_COLUMNS)} required columns, "
            "one row per request (250 requests)",
            out,
        )
        self.assertIn("schema and referential integrity", out)
        self.assertIn("indexes", out)

    def test_summary_states_that_blank_amounts_stay_none(self) -> None:
        _, out, _ = run_cli(["--dataset", str(REAL_DATASET)])
        self.assertIn("blank amounts kept as None", out)


class ValidateBrokenDataset(unittest.TestCase):
    def test_referential_failure_exits_non_zero(self) -> None:
        tables = default_tables()
        tables["requests"][0]["user_id"] = "user_ghost"
        with tempfile.TemporaryDirectory() as tmp:
            root = build_dataset(Path(tmp), tables=tables)
            code, out, err = run_cli(["--validate", "--dataset", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("FAIL schema and referential integrity", out)
        self.assertIn("user_ghost", err)

    def test_unparsable_dataset_exits_non_zero(self) -> None:
        tables = default_tables()
        tables["requests"][0]["requested_amount"] = "about 500"
        with tempfile.TemporaryDirectory() as tmp:
            root = build_dataset(Path(tmp), tables=tables)
            code, _, err = run_cli(["--dataset", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("dataset could not be loaded", err)
        self.assertIn("requested_amount", err)

    def test_missing_dataset_directory_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run_cli(["--dataset", str(Path(tmp) / "dataset")])
        self.assertEqual(code, 1)
        self.assertIn("file is missing", err)


class CheckGeneratedOutput(unittest.TestCase):
    def _write_output(self, path: Path, rows) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(REQUIRED_OUTPUT_COLUMNS))
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def test_incomplete_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.csv"
            self._write_output(path, [])
            code, out, err = run_cli(
                ["--dataset", str(REAL_DATASET), "--check-output", str(path), "--max-issues", "3"]
            )
        self.assertEqual(code, 1)
        self.assertIn("satisfies the output contract", out)
        self.assertIn("no output row for this request", err)
        self.assertIn("and 247 more", err)


if __name__ == "__main__":
    unittest.main()

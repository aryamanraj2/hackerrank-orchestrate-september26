"""Field-level parsing: blanks stay None, bad values fail loudly."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

import helpers  # noqa: F401  (bootstraps sys.path)

from dataset_loader import (
    parse_bool,
    parse_date,
    parse_datetime,
    parse_decimal,
    parse_list,
    parse_optional_date,
    parse_optional_decimal,
    parse_optional_int,
    parse_optional_text,
    parse_required_text,
)


class ParseOptionalValues(unittest.TestCase):
    def test_blank_numeric_is_none_not_zero(self) -> None:
        for blank in ("", "   ", None):
            self.assertIsNone(parse_optional_decimal(blank, "amount"))
            self.assertIsNone(parse_optional_int(blank, "payment_frequency_days"))

    def test_blank_date_and_text_are_none(self) -> None:
        self.assertIsNone(parse_optional_date("", "settlement_date"))
        self.assertIsNone(parse_optional_text("  "))

    def test_zero_is_preserved_as_zero(self) -> None:
        self.assertEqual(parse_optional_decimal("0", "financing_fee"), Decimal(0))

    def test_empty_list_field(self) -> None:
        self.assertEqual(parse_list(""), ())
        self.assertEqual(parse_list("rent|groceries"), ("rent", "groceries"))
        self.assertEqual(parse_list(" rent | groceries "), ("rent", "groceries"))


class ParseRequiredValues(unittest.TestCase):
    def test_decimal_keeps_exact_precision(self) -> None:
        self.assertEqual(parse_decimal("1475.46", "amount"), Decimal("1475.46"))

    def test_dates_and_timestamps(self) -> None:
        self.assertEqual(parse_date("2026-01-05", "request_date"), date(2026, 1, 5))
        self.assertEqual(
            parse_datetime("2025-07-29T09:30:00Z", "sent_at"),
            datetime(2025, 7, 29, 9, 30, tzinfo=timezone.utc),
        )

    def test_booleans(self) -> None:
        self.assertTrue(parse_bool("true", "allows_partial_payment"))
        self.assertFalse(parse_bool("FALSE", "allows_partial_payment"))

    def test_invalid_values_name_the_column(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_decimal("n/a", "requested_amount")
        self.assertIn("requested_amount", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            parse_date("05/01/2026", "request_date")
        self.assertIn("request_date", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            parse_bool("maybe", "allows_partial_payment")
        self.assertIn("allows_partial_payment", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            parse_required_text("", "user_id")
        self.assertIn("user_id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

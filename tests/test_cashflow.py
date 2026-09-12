"""Cash-flow forecasting: what gets booked, when, and what stays unresolved."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from helpers import build_dataset, default_tables

from cashflow import HORIZON_DAYS, build_forecast, forecast_for_request
from dataset_loader import load_dataset
from recurrence import conservative_amount

REQUEST_DATE = date(2026, 1, 5)


def event(**overrides) -> dict[str, str]:
    """One financial-event row; only the differences need spelling out."""
    row = {
        "event_id": "event_x",
        "user_id": "user_a",
        "event_type": "expense",
        "description": "Test event",
        "category": "misc",
        "direction": "debit",
        "amount": "100",
        "currency": "ZAR",
        "event_date": "2026-01-10",
        "settlement_date": "2026-01-10",
        "status": "settled",
        "linked_event_id": "",
        "flexibility": "fixed",
        "minimum_allowed_amount": "",
    }
    row.update({key: str(value) for key, value in overrides.items()})
    return row


class ForecastTestCase(unittest.TestCase):
    """Builds a synthetic dataset per test and forecasts ``user_a``."""

    def forecast(self, *extra_events, tables=None, images=None):
        data = default_tables()
        rows = list(data["financial_events"]) + [dict(row) for row in extra_events]
        overrides = {"financial_events": rows}
        overrides.update(tables or {})
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(
                Path(tmp),
                tables=overrides,
                image_files=images,
            )
            dataset = load_dataset(Path(tmp) / "dataset")
            return build_forecast(dataset, "user_a", REQUEST_DATE)

    def entries_for(self, forecast, source_id):
        return [entry for entry in forecast.entries if entry.source_id == source_id]


class HorizonTests(ForecastTestCase):
    def test_opening_balance_and_horizon(self):
        forecast = self.forecast()
        self.assertEqual(forecast.opening_balance, Decimal("1000"))
        self.assertEqual(forecast.minimum_balance_to_keep, Decimal("200"))
        self.assertEqual(forecast.start_date, REQUEST_DATE)
        self.assertEqual(forecast.end_date, REQUEST_DATE + timedelta(days=HORIZON_DAYS))

    def test_running_balance_is_consistent(self):
        forecast = self.forecast(event(event_id="event_z", amount="50"))
        balance = forecast.opening_balance
        for entry in forecast.entries:
            balance += entry.amount
            self.assertEqual(entry.balance_after, balance)
        self.assertEqual(forecast.closing_balance, balance)

    def test_entries_are_chronological_with_debits_first(self):
        forecast = self.forecast(
            event(event_id="event_in", direction="credit", event_type="income",
                  category="salary", amount="500", status="scheduled"),
            event(event_id="event_out", amount="70"),
        )
        days = [entry.day for entry in forecast.entries]
        self.assertEqual(days, sorted(days))
        same_day = forecast.entries_on(date(2026, 1, 10))
        self.assertEqual([entry.source_id for entry in same_day], ["event_out", "event_in"])

    def test_events_beyond_the_horizon_are_not_booked(self):
        beyond = REQUEST_DATE + timedelta(days=HORIZON_DAYS + 1)
        forecast = self.forecast(
            event(event_id="event_far", status="scheduled",
                  event_date=beyond, settlement_date=beyond)
        )
        self.assertEqual(self.entries_for(forecast, "event_far"), [])


class CashStateTests(ForecastTestCase):
    def test_pending_debit_is_reserved(self):
        forecast = self.forecast(
            event(event_id="event_pending", status="pending", amount="150")
        )
        [entry] = self.entries_for(forecast, "event_pending")
        self.assertEqual(entry.amount, Decimal("-150"))
        self.assertEqual(entry.day, date(2026, 1, 10))
        self.assertIn("pending debit reserved", entry.rationale)

    def test_overdue_pending_debit_is_reserved_on_the_request_date(self):
        forecast = self.forecast(
            event(event_id="event_late", status="pending", amount="150",
                  event_date="2026-01-02", settlement_date="2026-01-02")
        )
        [entry] = self.entries_for(forecast, "event_late")
        self.assertEqual(entry.day, REQUEST_DATE)

    def test_pending_credit_is_ignored(self):
        forecast = self.forecast(
            event(event_id="event_hope", status="pending", direction="credit",
                  event_type="income", category="salary", amount="5000")
        )
        self.assertEqual(self.entries_for(forecast, "event_hope"), [])

    def test_scheduled_salary_counts_on_its_settlement_date(self):
        forecast = self.forecast(
            event(event_id="event_pay", status="scheduled", direction="credit",
                  event_type="income", category="salary", amount="2000",
                  event_date="2026-01-20", settlement_date="2026-01-25")
        )
        [entry] = self.entries_for(forecast, "event_pay")
        self.assertEqual(entry.amount, Decimal("2000"))
        self.assertEqual(entry.day, date(2026, 1, 25))

    def test_windfall_and_refund_credits_are_never_counted(self):
        forecast = self.forecast(
            event(event_id="event_bonus", status="scheduled", direction="credit",
                  event_type="income", category="windfall", amount="9000"),
            event(event_id="event_refund", status="scheduled", direction="credit",
                  event_type="refund", category="shopping", amount="800"),
        )
        self.assertEqual(self.entries_for(forecast, "event_bonus"), [])
        self.assertEqual(self.entries_for(forecast, "event_refund"), [])

    def test_cancelled_and_failed_events_are_excluded(self):
        forecast = self.forecast(
            event(event_id="event_cancelled", status="cancelled", amount="400"),
            event(event_id="event_failed", status="failed", amount="400"),
        )
        self.assertEqual(self.entries_for(forecast, "event_cancelled"), [])
        self.assertEqual(self.entries_for(forecast, "event_failed"), [])

    def test_unrealized_investment_value_is_not_cash(self):
        forecast = self.forecast(
            event(event_id="event_gain", status="unrealized", direction="non_cash",
                  event_type="investment_purchase", category="investment",
                  amount="10000", settlement_date="")
        )
        self.assertEqual(self.entries_for(forecast, "event_gain"), [])

    def test_settled_cash_before_the_request_date_is_not_rebooked(self):
        forecast = self.forecast(
            event(event_id="event_past", event_date="2026-01-02",
                  settlement_date="2026-01-02", amount="900")
        )
        self.assertEqual(self.entries_for(forecast, "event_past"), [])

    def test_settled_event_on_the_request_date_is_already_in_the_balance(self):
        forecast = self.forecast(
            event(event_id="event_today", event_date="2026-01-05",
                  settlement_date="2026-01-05", amount="900")
        )
        self.assertEqual(self.entries_for(forecast, "event_today"), [])

    def test_settled_event_settling_after_the_request_date_is_booked(self):
        forecast = self.forecast(
            event(event_id="event_lagging", event_date="2026-01-04",
                  settlement_date="2026-01-09", amount="250")
        )
        [entry] = self.entries_for(forecast, "event_lagging")
        self.assertEqual(entry.day, date(2026, 1, 9))
        self.assertEqual(entry.amount, Decimal("-250"))


class CurrencyTests(ForecastTestCase):
    def test_foreign_debit_uses_the_supplied_settlement_date_rate(self):
        rates = [
            {"rate_date": "2026-01-05", "from_currency": "EUR",
             "to_currency": "ZAR", "rate": "20"},
            {"rate_date": "2026-01-10", "from_currency": "EUR",
             "to_currency": "ZAR", "rate": "21"},
        ]
        forecast = self.forecast(
            event(event_id="event_eur", status="scheduled", currency="EUR", amount="10"),
            tables={"exchange_rates": rates},
        )
        [entry] = self.entries_for(forecast, "event_eur")
        self.assertEqual(entry.amount, Decimal("-210"))

    def test_missing_rate_blocks_instead_of_guessing_an_inverse(self):
        forecast = self.forecast(
            event(event_id="event_usd", status="scheduled", currency="USD", amount="10")
        )
        self.assertEqual(self.entries_for(forecast, "event_usd"), [])
        [blocker] = [b for b in forecast.blockers if b.source_id == "event_usd"]
        self.assertIn("USD->ZAR", blocker.reason)


class BlockerTests(ForecastTestCase):
    def test_blank_amount_is_an_explicit_blocker_not_a_zero(self):
        forecast = self.forecast(
            event(event_id="event_img", status="pending", amount="",
                  category="utilities")
        )
        self.assertEqual(self.entries_for(forecast, "event_img"), [])
        [blocker] = [b for b in forecast.blockers if b.source_id == "event_img"]
        self.assertEqual(blocker.day, date(2026, 1, 10))
        self.assertIn("blank", blocker.reason)

    def test_forecast_is_still_usable_alongside_blockers(self):
        forecast = self.forecast(
            event(event_id="event_img", status="pending", amount=""),
            event(event_id="event_known", status="pending", amount="60"),
        )
        self.assertTrue(forecast.blockers)
        self.assertTrue(self.entries_for(forecast, "event_known"))


class RecurrenceProjectionTests(ForecastTestCase):
    def test_recurring_expense_is_projected_at_its_cadence(self):
        forecast = self.forecast()
        streaming = self.entries_for(forecast, "streaming/debit")
        self.assertTrue(streaming)
        self.assertTrue(all(entry.amount == Decimal("-20") for entry in streaming))
        days = [entry.day for entry in streaming]
        self.assertEqual(days, sorted(days))
        self.assertTrue(all(entry.day > REQUEST_DATE for entry in streaming))
        self.assertTrue(all(entry.day <= forecast.end_date for entry in streaming))

    def test_variable_expense_projects_the_highest_observed_amount(self):
        forecast = self.forecast()
        dining = self.entries_for(forecast, "dining/debit")
        self.assertTrue(dining)
        # Observed dining amounts are 110, 105, 100 and 95.
        self.assertTrue(all(entry.amount == Decimal("-110") for entry in dining))

    def test_one_off_spending_is_not_projected(self):
        forecast = self.forecast()
        self.assertEqual(self.entries_for(forecast, "shopping/debit"), [])

    def test_supplied_row_is_not_double_counted_with_its_series(self):
        # A scheduled streaming charge on a date the series would also project.
        forecast = self.forecast(
            event(event_id="event_stream_next", status="scheduled", amount="20",
                  category="streaming", event_type="subscription",
                  event_date="2026-01-12", settlement_date="2026-01-12")
        )
        january = [
            entry
            for entry in forecast.entries
            if entry.category == "streaming" and entry.day < date(2026, 1, 25)
        ]
        self.assertEqual(len(january), 1)
        self.assertEqual(january[0].source_id, "event_stream_next")

    def test_conservative_amount_picks_against_the_user(self):
        amounts = (Decimal("10"), Decimal("30"), Decimal("20"))
        self.assertEqual(conservative_amount("debit", amounts), Decimal("30"))
        self.assertEqual(conservative_amount("credit", amounts), Decimal("10"))
        self.assertIsNone(conservative_amount("debit", ()))


class ProjectedCurrencyTests(ForecastTestCase):
    """A projected occurrence is valued at the rate supplied for its own date."""

    #: Gaps of exactly 30 days, so the series projects on 2026-01-10,
    #: 2026-02-09 and 2026-03-11.
    GYM = ("2025-10-12", "2025-11-11", "2025-12-11")
    PROJECTED = (date(2026, 1, 10), date(2026, 2, 9), date(2026, 3, 11))

    def gym_series(self, amounts, currency="EUR"):
        return [
            event(
                event_id=f"event_gym_{index}",
                category="gym",
                event_type="subscription",
                currency=currency[index] if isinstance(currency, tuple) else currency,
                amount=amount,
                event_date=day,
                settlement_date=day,
            )
            for index, (day, amount) in enumerate(zip(self.GYM, amounts))
        ]

    def rate(self, day, value, pair=("EUR", "ZAR")):
        return {
            "rate_date": str(day),
            "from_currency": pair[0],
            "to_currency": pair[1],
            "rate": str(value),
        }

    def test_projection_uses_the_rate_of_the_projected_date(self):
        rates = [
            # A historical rate that must never be carried into the future.
            self.rate("2025-12-11", "5"),
            self.rate(self.PROJECTED[0], "20"),
            self.rate(self.PROJECTED[1], "25"),
            self.rate(self.PROJECTED[2], "30"),
        ]
        forecast = self.forecast(
            *self.gym_series(["10", "10", "10"]), tables={"exchange_rates": rates}
        )
        booked = {
            entry.day: entry.amount for entry in self.entries_for(forecast, "gym/debit")
        }
        self.assertEqual(
            booked,
            {
                self.PROJECTED[0]: Decimal("-200"),
                self.PROJECTED[1]: Decimal("-250"),
                self.PROJECTED[2]: Decimal("-300"),
            },
        )

    def test_mixed_currency_history_is_compared_at_the_projected_date(self):
        rates = [self.rate(self.PROJECTED[0], "20"), self.rate(self.PROJECTED[1], "25")]
        forecast = self.forecast(
            *self.gym_series(["10", "12", "250"], currency=("EUR", "EUR", "ZAR")),
            tables={"exchange_rates": rates},
        )
        booked = {
            entry.day: entry.amount for entry in self.entries_for(forecast, "gym/debit")
        }
        # At 20 the 250 ZAR occurrence is the larger debit; at 25 the 12 EUR one is.
        self.assertEqual(booked[self.PROJECTED[0]], Decimal("-250"))
        self.assertEqual(booked[self.PROJECTED[1]], Decimal("-300"))

    def test_missing_projected_rate_blocks_that_series(self):
        rates = [self.rate(self.PROJECTED[0], "20")]
        forecast = self.forecast(
            *self.gym_series(["10", "10", "10"]), tables={"exchange_rates": rates}
        )
        days = [entry.day for entry in self.entries_for(forecast, "gym/debit")]
        self.assertEqual(days, [self.PROJECTED[0]])
        blockers = [b for b in forecast.blockers if b.source_id == "gym/debit"]
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0].day, self.PROJECTED[1])
        self.assertIn("EUR->ZAR", blockers[0].reason)

    def test_blank_amount_in_a_series_withholds_the_whole_projection(self):
        rows = [
            row
            for row in default_tables()["financial_events"]
            if row["category"] != "streaming"
        ]
        blank = self.gym_series(["10", "10", ""], currency="ZAR")
        forecast = self.forecast(tables={"financial_events": rows + blank})
        self.assertEqual(self.entries_for(forecast, "gym/debit"), [])
        [blocker] = [b for b in forecast.blockers if b.source_id == "gym/debit"]
        self.assertIsNone(blocker.day)
        self.assertIn("unresolved amount", blocker.reason)


class CalendarProjectionTests(ForecastTestCase):
    """A monthly foreign salary is converted on its real settlement day."""

    #: Settles on the 15th of consecutive months.
    MONTHLY = ("2025-10-15", "2025-11-15", "2025-12-15")

    def salary_series(self, currency="EUR", amount="1000"):
        return [
            event(
                event_id=f"event_pay_{index}",
                event_type="income",
                category="salary",
                direction="credit",
                currency=currency,
                amount=amount,
                event_date=day,
                settlement_date=day,
            )
            for index, day in enumerate(self.MONTHLY)
        ]

    def test_monthly_series_books_on_the_calendar_day_with_its_rate(self):
        rates = [
            {"rate_date": day, "from_currency": "EUR", "to_currency": "ZAR", "rate": "20"}
            for day in ("2026-01-15", "2026-02-15", "2026-03-15", "2026-04-15")
        ]
        rows = [
            row
            for row in default_tables()["financial_events"]
            if row["category"] != "salary"
        ]
        forecast = self.forecast(
            tables={
                "exchange_rates": rates,
                "financial_events": rows + self.salary_series(),
            }
        )
        entries = self.entries_for(forecast, "salary/credit")
        self.assertTrue(entries)
        self.assertTrue(all(entry.day.day == 15 for entry in entries))
        self.assertTrue(all(entry.amount == Decimal("20000") for entry in entries))
        # No drift means no artificial missing-rate blocker.
        self.assertEqual([b for b in forecast.blockers if b.source_id == "salary/credit"], [])
        self.assertTrue(forecast.is_complete)


class CompletenessTests(ForecastTestCase):
    def test_a_clean_forecast_is_complete(self):
        forecast = self.forecast()
        self.assertEqual(forecast.blockers, ())
        self.assertTrue(forecast.is_complete)

    def test_a_missing_rate_leaves_the_forecast_incomplete(self):
        # A recurring EUR debit with no rate on its projected dates: the debit
        # is absent from the ledger, so the balance is only an upper bound.
        recurring = [
            event(
                event_id=f"event_eur_{index}",
                category="gym",
                event_type="subscription",
                currency="EUR",
                amount="10",
                event_date=day,
                settlement_date=day,
            )
            for index, day in enumerate(("2025-10-12", "2025-11-11", "2025-12-11"))
        ]
        forecast = self.forecast(*recurring, tables={"exchange_rates": []})
        self.assertTrue(forecast.blockers)
        self.assertFalse(forecast.is_complete)
        self.assertEqual(self.entries_for(forecast, "gym/debit"), [])
        # The unbooked debit is real money; an incomplete minimum proves nothing.
        self.assertIn("INCOMPLETE", forecast.trace())

    def test_a_blank_recurring_amount_leaves_the_forecast_incomplete(self):
        rows = [
            row
            for row in default_tables()["financial_events"]
            if row["category"] != "streaming"
        ]
        blank = [
            event(event_id="event_gym_1", category="gym", amount="10",
                  event_date="2025-10-12", settlement_date="2025-10-12"),
            event(event_id="event_gym_2", category="gym", amount="10",
                  event_date="2025-11-11", settlement_date="2025-11-11"),
            event(event_id="event_gym_3", category="gym", amount="",
                  event_date="2025-12-11", settlement_date="2025-12-11"),
        ]
        forecast = self.forecast(tables={"financial_events": rows + blank})
        self.assertFalse(forecast.is_complete)
        self.assertEqual(self.entries_for(forecast, "gym/debit"), [])


class MinimumBalanceTests(ForecastTestCase):
    def test_minimum_balance_and_date(self):
        forecast = self.forecast(
            event(event_id="event_big", status="scheduled", amount="800",
                  event_date="2026-01-20", settlement_date="2026-01-20")
        )
        self.assertEqual(
            forecast.minimum_balance, min(e.balance_after for e in forecast.entries)
        )
        self.assertLessEqual(forecast.minimum_balance_date, forecast.end_date)

    def test_balance_on_and_minimum_from_a_later_date(self):
        forecast = self.forecast(
            event(event_id="event_out", status="scheduled", amount="100",
                  event_date="2026-02-01", settlement_date="2026-02-01")
        )
        before = forecast.balance_on(date(2026, 1, 31))
        after = forecast.balance_on(date(2026, 2, 1))
        self.assertEqual(after, before - Decimal("100"))
        self.assertLessEqual(forecast.minimum_balance_from(date(2026, 2, 2)), after)
        self.assertEqual(
            forecast.balance_on(REQUEST_DATE - timedelta(days=1)),
            forecast.opening_balance,
        )


class RealDatasetTests(unittest.TestCase):
    def test_every_evaluation_request_forecasts_deterministically(self):
        dataset = load_dataset()
        for request in dataset.requests[:25]:
            first = forecast_for_request(dataset, request)
            second = forecast_for_request(dataset, request)
            self.assertEqual(first.entries, second.entries)
            self.assertEqual(first.blockers, second.blockers)
            self.assertTrue(
                all(entry.day <= first.end_date for entry in first.entries)
            )
            self.assertTrue(first.trace())


if __name__ == "__main__":
    unittest.main()

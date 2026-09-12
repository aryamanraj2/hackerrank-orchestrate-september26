"""Cash-flow forecasting: what gets booked, when, and what stays unresolved."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from helpers import REAL_DATASET, build_dataset, default_tables

from cashflow import HORIZON_DAYS, build_forecast, forecast_for_request
from dataset_loader import load_dataset
from recurrence import conservative_amount, series_amount

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

    def test_entries_are_chronological_with_credits_first(self):
        forecast = self.forecast(
            event(event_id="event_out", amount="70"),
            event(event_id="event_in", direction="credit", event_type="income",
                  category="salary", amount="500", status="scheduled"),
        )
        days = [entry.day for entry in forecast.entries]
        self.assertEqual(days, sorted(days))
        same_day = forecast.entries_on(date(2026, 1, 10))
        self.assertEqual([entry.source_id for entry in same_day], ["event_in", "event_out"])

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

    def test_variable_expense_projects_the_mean_observed_amount(self):
        forecast = self.forecast()
        dining = self.entries_for(forecast, "dining/debit")
        self.assertTrue(dining)
        # Observed dining amounts are 110, 105, 100 and 95.
        self.assertTrue(all(entry.amount == Decimal("-102.50") for entry in dining))

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

    def test_series_amount_policy(self):
        amounts = (Decimal("10"), Decimal("30"), Decimal("20.01"), Decimal("20"))
        # Debits: mean 80.01 / 4 = 20.0025, half-up to the cent.
        self.assertEqual(series_amount("debit", amounts), Decimal("20.00"))
        self.assertEqual(series_amount("debit", amounts[:3]), Decimal("20.00"))
        self.assertEqual(series_amount("debit", (Decimal("0.01"), Decimal("0.02"))), Decimal("0.02"))
        # Credits: median, even count averages the middle two (20 and 20.01).
        self.assertEqual(series_amount("credit", amounts), Decimal("20.01"))
        self.assertIsNone(series_amount("debit", ()))

    def test_identical_amount_series_is_unchanged(self):
        same = (Decimal("5148.125"),) * 4
        self.assertEqual(series_amount("debit", same), Decimal("5148.125"))
        self.assertEqual(series_amount("credit", same), Decimal("5148.125"))

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
        # EUR is estimated before conversion (mean of 10 and 12 = 11 EUR): at 20
        # the 250 ZAR figure is the larger debit; at 25 the 11 EUR one is.
        self.assertEqual(booked[self.PROJECTED[0]], Decimal("-250"))
        self.assertEqual(booked[self.PROJECTED[1]], Decimal("-275"))

    def test_foreign_series_is_estimated_before_conversion(self):
        rates = [self.rate(day, "20") for day in self.PROJECTED]
        forecast = self.forecast(
            *self.gym_series(["10", "10", "11"]), tables={"exchange_rates": rates}
        )
        # mean(10, 10, 11) = 10.33 EUR, then x20. Converting first would give 206.67.
        amounts = {entry.amount for entry in self.entries_for(forecast, "gym/debit")}
        self.assertEqual(amounts, {Decimal("-206.60")})

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
                description="Monthly salary",
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
        entries = self.entries_for(forecast, "salary/credit/Monthly salary")
        self.assertTrue(entries)
        self.assertTrue(all(entry.day.day == 15 for entry in entries))
        self.assertTrue(all(entry.amount == Decimal("20000") for entry in entries))
        # No drift means no artificial missing-rate blocker.
        self.assertEqual(
            [b for b in forecast.blockers if b.source_id.startswith("salary/credit")], []
        )
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


class IncomeHoldTests(ForecastTestCase):
    """An explicitly unconfirmed payout stops being projected, and says so."""

    #: A weekly gig stream: three settled payouts, cadence supported by history.
    PAYOUTS = ("2025-12-15", "2025-12-22", "2025-12-29")

    PENDING_NOTICE = (
        "The next payout is still pending. The weekly earnings shown in the app "
        "can change until the payout is closed. The balance isn't withdrawable "
        "until the payout shows as completed."
    )

    def payout_events(self):
        return [
            event(
                event_id=f"event_p{index}",
                event_type="income",
                description="Platform payout",
                category="gig",
                direction="credit",
                amount="300",
                event_date=day,
                settlement_date=day,
            )
            for index, day in enumerate(self.PAYOUTS, start=1)
        ]

    def notice(self, text):
        return [
            {
                "message_id": "message_hold",
                "user_id": "user_a",
                "request_id": "request_a",
                "related_event_id": "",
                "sent_at": "2026-01-04T09:30:00Z",
                "source_type": "service_provider",
                "message_text": text,
            }
        ]

    def forecast_with(self, messages):
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(
                Path(tmp),
                tables={
                    "financial_events": list(default_tables()["financial_events"])
                    + self.payout_events(),
                    "messages": messages,
                },
            )
            dataset = load_dataset(Path(tmp) / "dataset")
            return forecast_for_request(dataset, dataset.request_by_id["request_a"])

    def gig_entries(self, forecast):
        return [entry for entry in forecast.entries if entry.category == "gig"]

    def test_the_same_history_is_forecast_without_a_notice(self) -> None:
        forecast = self.forecast_with([])
        self.assertTrue(self.gig_entries(forecast))
        self.assertTrue(all(entry.amount > 0 for entry in self.gig_entries(forecast)))
        self.assertEqual(forecast.notes, ())

    def test_an_unconfirmed_payout_withholds_future_credits(self) -> None:
        forecast = self.forecast_with(self.notice(self.PENDING_NOTICE))
        self.assertEqual(self.gig_entries(forecast), [])

    def test_the_hold_is_explained_in_the_trace(self) -> None:
        forecast = self.forecast_with(self.notice(self.PENDING_NOTICE))
        self.assertEqual(len(forecast.notes), 1)
        note = forecast.notes[0]
        self.assertEqual(note.source_id, "gig/credit/Platform payout")
        self.assertIn("message_hold", note.reason)
        self.assertIn("message_hold", forecast.trace())

    def test_a_hold_never_makes_the_forecast_incomplete(self) -> None:
        # Withheld income is a resolved decision, not missing evidence: the
        # projection is safe to act on, merely poorer.
        forecast = self.forecast_with(self.notice(self.PENDING_NOTICE))
        self.assertTrue(forecast.is_complete)
        self.assertEqual(forecast.blockers, ())

    def test_settled_history_is_never_unspent(self) -> None:
        held = self.forecast_with(self.notice(self.PENDING_NOTICE))
        free = self.forecast_with([])
        self.assertEqual(held.opening_balance, free.opening_balance)
        # Holding the stream can only lower the projection, never raise it.
        self.assertLess(held.closing_balance, free.closing_balance)

    def test_a_confirmation_of_the_same_stream_leaves_it_forecast(self) -> None:
        forecast = self.forecast_with(
            self.notice("The payout has been released and is now withdrawable.")
        )
        self.assertTrue(self.gig_entries(forecast))
        self.assertEqual(forecast.notes, ())

    def test_a_base_salary_confirmation_does_not_release_a_payout_hold(self) -> None:
        # Two clauses, two streams: the fixed one is confirmed, the variable
        # one is not. Confirming the first says nothing about the second.
        forecast = self.forecast_with(
            self.notice(
                "Your confirmed monthly salary is unchanged. " + self.PENDING_NOTICE
            )
        )
        self.assertEqual(self.gig_entries(forecast), [])

    def test_expenses_are_untouched_by_an_income_hold(self) -> None:
        held = self.forecast_with(self.notice(self.PENDING_NOTICE))
        free = self.forecast_with([])
        debits = lambda forecast: [
            (entry.day, entry.amount) for entry in forecast.entries if entry.is_debit
        ]
        self.assertEqual(debits(held), debits(free))


class IncomeStreamTests(ForecastTestCase):
    """Two pay sources in one category are two commitments, not one."""

    BASE = ("2025-10-15", "2025-11-15", "2025-12-15")
    COMMISSION = ("2025-10-24", "2025-11-24", "2025-12-24")

    def income(self, event_id, day, description, amount):
        return event(
            event_id=event_id,
            event_type="income",
            description=description,
            category="salary",
            direction="credit",
            amount=amount,
            event_date=day,
            settlement_date=day,
        )

    def two_streams(self):
        rows = [
            self.income(f"event_base_{index}", day, "Base salary", "2000")
            for index, day in enumerate(self.BASE, start=1)
        ]
        # A commission varies; it is projected at its observed median.
        for index, (day, amount) in enumerate(
            zip(self.COMMISSION, ("900", "400", "700")), start=1
        ):
            rows.append(
                self.income(f"event_comm_{index}", day, "Sales commission", amount)
            )
        return rows

    def forecast_streams(self, messages=None):
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(
                Path(tmp),
                tables={
                    "financial_events": [
                        row
                        for row in default_tables()["financial_events"]
                        if row["category"] != "salary"
                    ]
                    + self.two_streams(),
                    "messages": messages if messages is not None else [],
                },
            )
            dataset = load_dataset(Path(tmp) / "dataset")
            return forecast_for_request(dataset, dataset.request_by_id["request_a"])

    def notice(self, text):
        return [
            {
                "message_id": "message_split",
                "user_id": "user_a",
                "request_id": "request_a",
                "related_event_id": "",
                "sent_at": "2026-01-04T09:30:00Z",
                "source_type": "employer",
                "message_text": text,
            }
        ]

    def test_interleaved_streams_become_two_patterns(self) -> None:
        forecast = self.forecast_streams()
        base = self.entries_for(forecast, "salary/credit/Base salary")
        commission = self.entries_for(forecast, "salary/credit/Sales commission")
        self.assertTrue(base)
        self.assertTrue(commission)
        self.assertTrue(all(entry.day.day == 15 for entry in base))
        self.assertTrue(all(entry.day.day == 24 for entry in commission))

    def test_each_stream_is_valued_on_its_own_history(self) -> None:
        forecast = self.forecast_streams()
        base = self.entries_for(forecast, "salary/credit/Base salary")
        commission = self.entries_for(forecast, "salary/credit/Sales commission")
        self.assertTrue(all(entry.amount == Decimal("2000") for entry in base))
        # The stream's own median (900, 400, 700), not the base.
        self.assertTrue(all(entry.amount == Decimal("700") for entry in commission))

    def test_a_hold_on_the_commission_leaves_the_base_forecast(self) -> None:
        forecast = self.forecast_streams(
            self.notice(
                "Your confirmed base salary is unchanged. The commission payout is "
                "still pending and is not yet withdrawable."
            )
        )
        self.assertTrue(self.entries_for(forecast, "salary/credit/Base salary"))
        self.assertEqual(
            self.entries_for(forecast, "salary/credit/Sales commission"), []
        )
        [note] = forecast.notes
        self.assertEqual(note.source_id, "salary/credit/Sales commission")

    def test_an_unprojectable_repeated_stream_is_recorded_not_counted(self) -> None:
        # Two occurrences, three months apart: real money, no schedule anyone
        # can stand behind. It stays out of the ledger and is written down.
        extra = [
            self.income("event_odd_1", "2025-09-02", "Ad-hoc retainer", "500"),
            self.income("event_odd_2", "2025-12-19", "Ad-hoc retainer", "500"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(
                Path(tmp),
                tables={
                    "financial_events": [
                        row
                        for row in default_tables()["financial_events"]
                        if row["category"] != "salary"
                    ]
                    + self.two_streams()
                    + extra
                },
            )
            dataset = load_dataset(Path(tmp) / "dataset")
            forecast = forecast_for_request(
                dataset, dataset.request_by_id["request_a"]
            )
        self.assertEqual(
            self.entries_for(forecast, "salary/credit/Ad-hoc retainer"), []
        )
        [note] = forecast.notes
        self.assertEqual(note.source_id, "salary/credit/Ad-hoc retainer")
        self.assertIn("no projectable schedule", note.reason)

    def test_a_single_one_off_credit_is_not_worth_a_note(self) -> None:
        extra = [self.income("event_odd_1", "2025-09-02", "Gift received", "500")]
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(
                Path(tmp),
                tables={
                    "financial_events": [
                        row
                        for row in default_tables()["financial_events"]
                        if row["category"] != "salary"
                    ]
                    + self.two_streams()
                    + extra
                },
            )
            dataset = load_dataset(Path(tmp) / "dataset")
            forecast = forecast_for_request(
                dataset, dataset.request_by_id["request_a"]
            )
        self.assertEqual(forecast.notes, ())


class MergedIncomeFallbackTests(ForecastTestCase):
    """Fine-grained descriptions must not erase income the category repeats."""

    DAYS = ("2025-10-15", "2025-11-15", "2025-12-15")

    def varied_income(self):
        # Freelance work arrives under a new description every month, so no
        # single stream reaches the evidence threshold.
        return [
            event(
                event_id=f"event_gig_{index}",
                event_type="income",
                description=f"Project {index} payment",
                category="salary",
                direction="credit",
                amount=amount,
                event_date=day,
                settlement_date=day,
            )
            for index, (day, amount) in enumerate(
                zip(self.DAYS, ("1500", "900", "1200")), start=1
            )
        ]

    def forecast_varied(self, messages=None):
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(
                Path(tmp),
                tables={
                    "financial_events": [
                        row
                        for row in default_tables()["financial_events"]
                        if row["category"] != "salary"
                    ]
                    + self.varied_income(),
                    "messages": messages if messages is not None else [],
                },
            )
            dataset = load_dataset(Path(tmp) / "dataset")
            return forecast_for_request(dataset, dataset.request_by_id["request_a"])

    def test_a_category_with_no_single_stream_is_read_as_one_series(self) -> None:
        forecast = self.forecast_varied()
        entries = self.entries_for(forecast, "salary/credit")
        self.assertTrue(entries)
        # Valued at the median credit across the whole category (1500, 900, 1200).
        self.assertTrue(all(entry.amount == Decimal("1200") for entry in entries))

    def test_the_merged_series_leaves_no_ambiguity_notes(self) -> None:
        self.assertEqual(self.forecast_varied().notes, ())

    def test_a_hold_on_any_stream_withholds_the_merged_series(self) -> None:
        forecast = self.forecast_varied(
            [
                {
                    "message_id": "message_merge",
                    "user_id": "user_a",
                    "request_id": "request_a",
                    "related_event_id": "event_gig_2",
                    "sent_at": "2026-01-04T09:30:00Z",
                    "source_type": "service_provider",
                    "message_text": (
                        "The payout is still pending and is not yet withdrawable."
                    ),
                }
            ]
        )
        self.assertEqual(self.entries_for(forecast, "salary/credit"), [])
        self.assertTrue(forecast.notes)


class RealDatasetIncomeStreams(unittest.TestCase):
    """A read-only look at how the supplied data splits into streams."""

    @classmethod
    def setUpClass(cls) -> None:
        if not REAL_DATASET.is_dir():
            raise unittest.SkipTest("no dataset/ in this checkout")
        cls.dataset = load_dataset(REAL_DATASET)

    def test_a_user_with_two_pay_sources_projects_them_separately(self) -> None:
        # request_11's user is paid a fixed monthly base and a variable
        # commission, both filed under salary. Whatever the right decision for
        # that request turns out to be, the two must not be averaged into one
        # stream, and each must be identifiable in the trace.
        sample = self.dataset.sample_request_by_id.get("request_11")
        if sample is None:
            self.skipTest("request_11 is not in this dataset")
        forecast = forecast_for_request(self.dataset, sample.request)
        credit_sources = {
            entry.source_id for entry in forecast.entries if entry.amount > 0
        }
        withheld = {note.source_id for note in forecast.notes}
        identified = credit_sources | withheld
        self.assertGreater(len(identified), 1, forecast.trace())
        for source_id in identified:
            self.assertTrue(source_id.startswith("salary/credit/"), source_id)
        # Every projected occurrence of one stream carries that stream's own
        # amount rather than a figure blended across both.
        for source_id in credit_sources:
            amounts = {
                entry.amount
                for entry in forecast.entries
                if entry.source_id == source_id
            }
            self.assertEqual(len(amounts), 1, source_id)


class LedgerBoundaryTests(ForecastTestCase):
    """Occurrences due on the request date, and one booked pay per cycle."""

    def monthly(self, prefix, day_of_month=5, **overrides):
        """Four settled monthly rows, Sep-Dec 2025, so the next is due 2026-01-05."""
        return [
            event(event_id=f"{prefix}_{month}", event_date=f"2025-{month:02d}-{day_of_month:02d}",
                  settlement_date=f"2025-{month:02d}-{day_of_month:02d}", **overrides)
            for month in (9, 10, 11, 12)
        ]

    def salary(self, **overrides):
        fields = dict(direction="credit", event_type="income", category="salary",
                      description="Primary salary", amount="500")
        fields.update(overrides)
        return self.monthly("event_pay", **fields)

    def scheduled_pay(self, event_id, day):
        return event(event_id=event_id, direction="credit", event_type="income",
                     category="salary", description="Next confirmed salary", amount="500",
                     status="scheduled", event_date=day, settlement_date=day)

    def only(self, *rows):
        return self.forecast(tables={"financial_events": list(rows), "images": []})

    def test_debit_due_on_request_date_is_reserved(self):
        forecast = self.only(*self.monthly("event_gym", category="gym", amount="60"))
        days = [entry.day for entry in self.entries_for(forecast, "gym/debit")]
        self.assertEqual(days[0], REQUEST_DATE)

    def test_settled_row_on_request_date_is_not_projected_again(self):
        rows = self.monthly("event_gym", category="gym", amount="60")
        rows.append(event(event_id="event_gym_today", category="gym", amount="60",
                          event_date="2026-01-05", settlement_date="2026-01-05"))
        forecast = self.only(*rows)
        self.assertEqual(forecast.entries_on(REQUEST_DATE), ())
        self.assertEqual(self.entries_for(forecast, "gym/debit")[0].day, date(2026, 2, 5))

    def test_salary_due_on_request_date_funds_a_payment_once(self):
        from engine import amount_safe_to_pay, simulate

        forecast = self.only(*self.salary())
        self.assertEqual(len(forecast.entries_on(REQUEST_DATE)), 1)
        # Opening 1000 + one 500 salary - floor 200.
        self.assertEqual(amount_safe_to_pay(forecast, Decimal("5000")), Decimal("1300"))
        self.assertFalse(simulate(forecast, [(REQUEST_DATE, Decimal("1300.01"))])[0])

    def test_booked_pay_replaces_a_differently_named_projection(self):
        forecast = self.only(*self.salary(), self.scheduled_pay("event_next", "2026-01-05"))
        self.assertEqual([e.source_id for e in forecast.entries_on(REQUEST_DATE)], ["event_next"])
        [note] = forecast.notes
        self.assertEqual(note.source_id, "salary/credit/Primary salary")
        self.assertIn("event_next", note.reason)
        self.assertIn("2026-01-05", note.reason)

    def test_booked_pay_outside_the_window_suppresses_nothing(self):
        forecast = self.only(*self.salary(), self.scheduled_pay("event_next", "2026-01-20"))
        projected = [e.day for e in self.entries_for(forecast, "salary/credit/Primary salary")]
        self.assertEqual(projected[:2], [date(2026, 1, 5), date(2026, 2, 5)])
        self.assertEqual(forecast.notes, ())

    def test_each_booked_credit_suppresses_at_most_one_occurrence(self):
        forecast = self.only(
            *self.salary(),
            self.scheduled_pay("event_a1", "2026-01-05"),
            self.scheduled_pay("event_a2", "2026-01-06"),
            self.scheduled_pay("event_b", "2026-02-05"),
        )
        projected = [e.day for e in self.entries_for(forecast, "salary/credit/Primary salary")]
        self.assertEqual(projected, [date(2026, 3, 5), date(2026, 4, 5)])
        self.assertEqual(len(forecast.notes), 2)
        self.assertEqual(len([e for e in forecast.entries if e.source_kind == "event"]), 3)

    def test_debit_overlap_window_is_unchanged(self):
        rows = self.monthly("event_gym", category="gym", amount="60")
        rows.append(event(event_id="event_gym_bill", category="gym", amount="75",
                          status="pending", event_date="2026-02-01", settlement_date="2026-02-08"))
        forecast = self.only(*rows)
        days = [entry.day for entry in self.entries_for(forecast, "gym/debit")]
        self.assertIn(REQUEST_DATE, days)
        self.assertNotIn(date(2026, 2, 5), days)
        self.assertEqual(forecast.notes, ())


if __name__ == "__main__":
    unittest.main()

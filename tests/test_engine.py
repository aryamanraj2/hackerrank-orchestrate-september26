"""The recommendation engine: payment simulation, eligibility and ranking."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from helpers import REAL_DATASET, build_dataset, default_tables

import main
from cashflow import Forecast, LedgerEntry
from dataset_loader import load_dataset
from engine import (
    Candidate,
    amount_safe_to_pay,
    earliest_full_payment_date,
    recommend,
    simulate,
)
from output_schema import REQUIRED_OUTPUT_COLUMNS, validate_output_row_values
from recommendation_schema import validate_predictions_file, validate_recommendation

START = date(2026, 1, 5)


def ledger(opening, *moves, floor="200"):
    """A hand-built forecast; ``moves`` are ``(day_offset, signed_amount)``."""
    balance = Decimal(opening)
    entries = []
    for offset, amount in moves:
        balance += Decimal(amount)
        entries.append(
            LedgerEntry(START + timedelta(days=offset), Decimal(amount), "event",
                        f"e{len(entries)}", "misc", "test", balance)
        )
    return Forecast("user_a", "ZAR", START, START + timedelta(days=90), Decimal(opening),
                    Decimal(floor), tuple(entries), ())


def event(event_id, direction, amount, day, status, **overrides):
    row = {
        "event_id": event_id,
        "user_id": "user_a",
        "event_type": "income" if direction == "credit" else "expense",
        "description": event_id,
        "category": "salary" if direction == "credit" else "bills",
        "direction": direction,
        "amount": amount,
        "currency": "ZAR",
        "event_date": day,
        "settlement_date": day,
        "status": status,
        "linked_event_id": "",
        "flexibility": "fixed",
        "minimum_allowed_amount": "",
    }
    row.update(overrides)
    return row


def option(option_id, method, amount, count, first, every="", fee="0", total=None):
    return {
        "payment_option_id": option_id,
        "request_id": "request_a",
        "payment_method": method,
        "payment_amount": amount,
        "number_of_payments": str(count),
        "first_payment_date": first,
        "payment_frequency_days": every,
        "financing_fee": fee,
        "total_payable_amount": total or str(Decimal(amount) * count),
    }


FULL = option("payment_option_01", "full_payment", "500", 1, "2026-01-05")
# 3 x 175 = 525 on 01-08, 01-18, 01-28: inside the 02-05 deadline.
FEE_PLAN = option("payment_option_02", "installments", "175", 3, "2026-01-08", "10", "25", "525")


def run(events=(), options=(FULL, FEE_PLAN), profile=None, request=None):
    """Recommend request_a on a synthetic dataset and validate the row."""
    tables = default_tables()
    tables["financial_profiles"][0].update(profile or {})
    tables["requests"][0].update(request or {})
    tables["financial_events"] = [dict(row) for row in events]
    tables["request_payment_options"] = [dict(row) for row in options]
    tables["images"] = []
    tables["messages"] = []
    with tempfile.TemporaryDirectory() as tmp:
        dataset = load_dataset(build_dataset(Path(tmp), tables=tables))
        req = dataset.requests[0]
        row = recommend(dataset, req)
        issues = validate_output_row_values(row, req) + validate_recommendation(row, req, dataset)
    assert not issues, issues
    return row


class SimulateTests(unittest.TestCase):
    def test_same_day_credit_funds_a_payment(self):
        forecast = ledger("1000", (3, "-100"), (3, "500"))
        day = START + timedelta(days=3)
        safe, lowest, lowest_day = simulate(forecast, [(day, Decimal("750"))])
        self.assertTrue(safe)
        self.assertEqual((lowest, lowest_day), (Decimal("650"), day))
        self.assertFalse(simulate(forecast, [(day, Decimal("1201"))])[0])

    def test_next_day_credit_cannot_fund_a_payment(self):
        forecast = ledger("1000", (4, "500"))
        safe, lowest, lowest_day = simulate(forecast, [(START + timedelta(days=3), Decimal("900"))])
        self.assertFalse(safe)
        self.assertEqual((lowest, lowest_day), (Decimal("100"), START + timedelta(days=3)))

    def test_opening_balance_is_a_checkpoint(self):
        # A same-day credit lifts the end-of-day balance, but the opening is below the floor.
        forecast = ledger("150", (0, "1000"))
        self.assertEqual(simulate(forecast, ()), (False, Decimal("150"), START))
        self.assertEqual(amount_safe_to_pay(forecast, Decimal("500")), Decimal("0"))

    def test_safe_amount_can_use_request_day_income(self):
        # The opening balance is the lowest checkpoint but does not limit the payment.
        forecast = ledger("300", (0, "1000"), (20, "-400"))
        self.assertEqual(amount_safe_to_pay(forecast, Decimal("2000")), Decimal("700.00"))
        self.assertTrue(simulate(forecast, [(START, Decimal("700"))])[0])
        self.assertFalse(simulate(forecast, [(START, Decimal("700.01"))])[0])

    def test_payments_apply_cumulatively(self):
        forecast = ledger("1000")
        day = START + timedelta(days=1)
        self.assertTrue(simulate(forecast, [(day, Decimal("400"))])[0])
        self.assertFalse(simulate(forecast, [(day, Decimal("400")), (day, Decimal("401"))])[0])

    def test_safe_amount_rounds_down_to_the_cent(self):
        forecast = ledger("1000", (10, "-0.005"))
        self.assertEqual(amount_safe_to_pay(forecast, Decimal("900")), Decimal("799.99"))

    def test_earliest_date_can_land_on_a_salary_day(self):
        forecast = ledger("600", (10, "1000"))
        self.assertEqual(
            earliest_full_payment_date(forecast, Decimal("500")), START + timedelta(days=10)
        )
        self.assertFalse(simulate(forecast, [(START + timedelta(days=9), Decimal("500"))])[0])
        self.assertIsNone(earliest_full_payment_date(ledger("600"), Decimal("500")))


class SafeAmountAndDateTests(unittest.TestCase):
    def test_safe_amount_limited_by_a_later_pending_debit(self):
        row = run([event("event_p", "debit", "600", "2026-02-01", "pending")])
        self.assertEqual(row["amount_safe_to_pay"], "200")

    def test_earliest_date_on_salary_settlement(self):
        salary = event("event_s", "credit", "1000", "2026-01-20", "scheduled")
        row = run([salary], profile={"current_available_balance": "600"})
        self.assertEqual(row["earliest_date_for_full_payment"], "2026-01-20")
        self.assertEqual(row["recommended_payment_method"], "wait")
        self.assertEqual(row["affordability_status"], "affordable_later")
        self.assertEqual(row["payment_plan"], "2026-01-20:500")


class EligibilityTests(unittest.TestCase):
    SALARY = event("event_s", "credit", "1000", "2026-01-20", "scheduled")

    def test_status_follows_chosen_method_when_full_is_not_accepted(self):
        row = run(profile={"payment_methods_user_will_consider": "installments"})
        self.assertEqual(row["recommended_payment_method"], "installments")
        self.assertEqual(row["affordability_status"], "affordable_with_plan")
        self.assertEqual(row["amount_safe_to_pay"], "500")
        self.assertEqual(row["earliest_date_for_full_payment"], "2026-01-05")

    def test_partial_payment_chosen_over_wait(self):
        row = run(
            [self.SALARY],
            profile={"current_available_balance": "600",
                     "payment_methods_user_will_consider": "full_payment|partial_payment"},
        )
        self.assertEqual(row["recommended_payment_method"], "partial_payment")
        self.assertEqual(row["payment_plan"], "2026-01-05:400|2026-01-20:100")

    def test_partial_rejected_when_request_disallows_it(self):
        row = run(
            [self.SALARY],
            profile={"current_available_balance": "600",
                     "payment_methods_user_will_consider": "full_payment|partial_payment"},
            request={"allows_partial_payment": "false"},
        )
        self.assertEqual(row["recommended_payment_method"], "wait")

    def test_partial_rejected_when_nothing_is_safe_today(self):
        row = run(
            [self.SALARY],
            profile={"current_available_balance": "200",
                     "payment_methods_user_will_consider": "partial_payment"},
        )
        self.assertEqual(row["amount_safe_to_pay"], "0")
        self.assertEqual(row["recommended_payment_method"], "not_recommended")

    def test_partial_rejected_when_everything_is_safe_today(self):
        row = run(profile={"payment_methods_user_will_consider": "partial_payment"})
        self.assertEqual(row["amount_safe_to_pay"], "500")
        self.assertEqual(row["recommended_payment_method"], "not_recommended")

    def test_installments_rejected_above_max_months(self):
        row = run(profile={"payment_methods_user_will_consider": "installments",
                           "max_installment_months": "2"})
        self.assertEqual(row["recommended_payment_method"], "not_recommended")

    def test_installments_rejected_when_max_months_blank(self):
        row = run(profile={"payment_methods_user_will_consider": "installments",
                           "max_installment_months": ""})
        self.assertEqual(row["recommended_payment_method"], "not_recommended")

    def test_installments_rejected_after_deadline(self):
        late = option("payment_option_02", "installments", "175", 3, "2026-01-08", "30", "25", "525")
        row = run(options=(FULL, late),
                  profile={"payment_methods_user_will_consider": "installments"})
        self.assertEqual(row["recommended_payment_method"], "not_recommended")

    def test_installments_rejected_on_cumulative_breach(self):
        # Each 300 fits the 800 headroom alone; the third one breaches it.
        plan = option("payment_option_02", "installments", "300", 3, "2026-01-06", "10")
        row = run(options=(FULL, plan),
                  profile={"payment_methods_user_will_consider": "installments"},
                  request={"requested_amount": "900"})
        self.assertEqual(row["recommended_payment_method"], "not_recommended")
        self.assertEqual(row["affordability_status"], "not_affordable")
        self.assertEqual(row["earliest_date_for_full_payment"], "")


class RankingTests(unittest.TestCase):
    def test_full_payment_beats_wait(self):
        pay = (Decimal("500"),)
        full = Candidate("full_payment", "affordable_now", ((START,) + pay,), Decimal("500"),
                         "payment_option_01", Decimal("0"), START)
        wait = Candidate("wait", "affordable_later", ((START + timedelta(days=5),) + pay,),
                         Decimal("500"), "payment_option_01", Decimal("0"), START)
        self.assertIs(min([wait, full], key=Candidate.rank_key), full)

    def test_no_fee_installment_beats_fee_installment(self):
        free = option("payment_option_03", "installments", "166.67", 3, "2026-01-08", "10", "0", "500")
        row = run(options=(FULL, FEE_PLAN, free),
                  profile={"payment_methods_user_will_consider": "installments"})
        self.assertEqual(row["payment_plan"], "2026-01-08:166.67|2026-01-18:166.67|2026-01-28:166.67")

    def test_identical_plans_break_tie_on_lowest_option_id(self):
        twin = option("payment_option_03", "installments", "175", 3, "2026-01-08", "10", "25", "525")
        row = run(options=(FULL, twin, FEE_PLAN),
                  profile={"payment_methods_user_will_consider": "installments"})
        self.assertIn("payment_option_02", row["decision_explanation"])


class IncompleteForecastTests(unittest.TestCase):
    def test_blank_amount_gives_the_conservative_row(self):
        row = run([event("event_x", "debit", "", "2026-01-20", "pending")])
        self.assertEqual(
            {k: row[k] for k in REQUIRED_OUTPUT_COLUMNS[1:7]},
            {
                "amount_safe_to_pay": "0",
                "affordability_status": "not_affordable",
                "recommended_payment_method": "not_recommended",
                "payment_plan": "none",
                "earliest_date_for_full_payment": "",
                "spending_changes_needed": "none",
            },
        )
        self.assertIn("event_x", row["decision_explanation"])
        self.assertIn("blank", row["decision_explanation"])


@unittest.skipUnless(REAL_DATASET.is_dir(), "real dataset not present")
class PredictRealDatasetTests(unittest.TestCase):
    def test_predict_writes_a_valid_row_per_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(["--predict", str(path)]), 0)
            dataset = load_dataset(REAL_DATASET)
            self.assertEqual(validate_predictions_file(path, dataset), [])


if __name__ == "__main__":
    unittest.main()

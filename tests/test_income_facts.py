"""Payroll notices change projected income amounts, paydays and ended streams."""

from __future__ import annotations

import collections
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from helpers import REAL_DATASET, build_dataset, default_tables

from cashflow import forecast_for_request
from dataset_loader import load_dataset
from evidence_policy import parse_income_facts

HISTORY = ("2025-09-15", "2025-10-15", "2025-11-15", "2025-12-15")
PROJECTED = (date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15))


def income(event_id, day, description, amount="2000", currency="ZAR"):
    row = dict(default_tables()["financial_events"][4])  # the salary credit row
    row.update(
        event_id=event_id,
        description=description,
        amount=amount,
        currency=currency,
        event_date=day,
        settlement_date=day,
    )
    return row


def series(prefix, description, days=HISTORY, **kwargs):
    return [income(f"{prefix}{i}", day, description, **kwargs) for i, day in enumerate(days)]


def notice(text, message_id="message_n", sent_at="2026-01-04T09:30:00Z", **overrides):
    row = {
        "message_id": message_id,
        "user_id": "user_a",
        "request_id": "request_a",
        "related_event_id": "",
        "sent_at": sent_at,
        "source_type": "employer",
        "message_text": f"A note from Acme Payroll. {text} Payroll ref EMP-0001.",
    }
    row.update(overrides)
    return row


def forecast(messages, incomes=None, rates=None):
    events = [
        row for row in default_tables()["financial_events"] if row["direction"] != "credit"
    ] + (incomes if incomes is not None else series("event_p", "Payroll credit"))
    tables = {"messages": messages, "financial_events": events}
    if rates is not None:
        tables["exchange_rates"] = rates
    with tempfile.TemporaryDirectory() as tmp:
        build_dataset(Path(tmp), tables=tables)
        dataset = load_dataset(Path(tmp) / "dataset")
        return forecast_for_request(dataset, dataset.request_by_id["request_a"])


def credits(result, stream=""):
    return [
        (entry.day, entry.amount)
        for entry in result.entries
        if entry.amount > 0 and entry.source_id.startswith("salary/credit") and stream in entry.source_id
    ]


def normal(*amounts, days=PROJECTED):
    return list(zip(days, (Decimal(amount) for amount in amounts)))


def cited(result, message_id="message_n"):
    return [note.reason for note in result.notes if note.reason.startswith(f"{message_id}:")]


class AmountTemplateTests(unittest.TestCase):
    def test_baseline_projects_three_paydays(self):
        self.assertEqual(credits(forecast([])), normal(2000, 2000, 2000))

    def test_salary_increase_applies_from_its_effective_date(self):
        for text in (
            "Your monthly salary has increased to ZAR 2600. The change applies from 2026-02-15.",
            "Gaji bulanan Anda naik menjadi ZAR 2600. Perubahan ini berlaku mulai 2026-02-15.",
        ):
            with self.subTest(text=text):
                result = forecast([notice(text)])
                self.assertEqual(credits(result), normal(2000, 2600, 2600))
                self.assertTrue(cited(result))

    def test_next_salary_reduced_changes_only_the_next_payday(self):
        result = forecast([notice("Your next salary is reduced to ZAR 1500. The adjustment is due to approved unpaid leave.")])
        self.assertEqual(credits(result), normal(1500, 2000, 2000))

    def test_temporary_pay_changes_only_the_next_payday(self):
        for text in (
            "Your temporary monthly pay is ZAR 1400. The reduced amount continues for the next payroll.",
            "Gaji bulanan sementara Anda adalah ZAR 1400. Jumlah yang lebih rendah masih berlaku untuk penggajian berikutnya.",
        ):
            with self.subTest(text=text):
                self.assertEqual(credits(forecast([notice(text)])), normal(1400, 2000, 2000))

    def test_regular_salary_applies_and_undated_extra_is_only_noted(self):
        for text in (
            "Your regular salary for the next payroll is ZAR 1900. The same payroll includes a one-time arrears adjustment of ZAR 800.",
            "Gaji rutin Anda untuk penggajian berikutnya adalah ZAR 1900. Penggajian yang sama mencakup penyesuaian tunggakan satu kali sebesar ZAR 800.",
        ):
            with self.subTest(text=text):
                result = forecast([notice(text)])
                self.assertEqual(credits(result), normal(1900, 1900, 1900))
                self.assertTrue(any("states no date; not counted" in reason for reason in cited(result)))

    def test_base_salary_confirmed_above_history_keeps_history_and_the_commission_hold(self):
        incomes = series("event_b", "Base salary") + series(
            "event_c", "Sales commission", days=("2025-09-24", "2025-10-24", "2025-11-24", "2025-12-24"), amount="500"
        )
        for text in (
            "Your confirmed base salary is ZAR 3000. The commission shown for open deals is still pending approval. "
            "Open deals will stay out of the payout until the commission is marked as earned.",
            "Gaji pokok yang dikonfirmasi adalah ZAR 3000. Komisi dari transaksi yang masih berjalan belum disetujui. "
            "Transaksi yang masih berjalan tidak masuk pembayaran sampai komisinya dinyatakan diperoleh.",
        ):
            with self.subTest(text=text):
                result = forecast([notice(text)], incomes=incomes)
                self.assertEqual(credits(result, "Base salary"), normal(2000, 2000, 2000))
                self.assertEqual(credits(result, "Sales commission"), [])
                self.assertIn(
                    "message_n: confirms ZAR 3000, above settled history ZAR 2000; history amount kept (safer)",
                    cited(result),
                )

    def test_confirmed_base_below_history_projects_the_stated_amount(self):
        incomes = series("event_b", "Base salary")
        result = forecast([notice("Your confirmed base salary is ZAR 1800.")], incomes=incomes)
        self.assertEqual(credits(result, "Base salary"), normal(1800, 1800, 1800))
        self.assertFalse(any("history amount kept" in reason for reason in cited(result)))

    def test_salary_increase_above_history_still_applies(self):
        # An explicit change is an amendment, not a confirmation: no cap.
        result = forecast([notice("Your monthly salary has increased to ZAR 5000. The change applies from 2026-01-15.")])
        self.assertEqual(credits(result), normal(5000, 5000, 5000))


class FxSalaryTests(unittest.TestCase):
    RATES = [
        {"rate_date": day, "from_currency": "EUR", "to_currency": "ZAR", "rate": "20"}
        for day in ("2026-01-16", "2026-02-15", "2026-03-15")
    ]

    def eur_forecast(self, text, rates):
        incomes = series("event_x", "International employer payroll", amount="100", currency="EUR")
        return forecast([notice(text)], incomes=incomes, rates=rates)

    def test_confirmed_salary_is_booked_on_its_date_at_that_dates_rate(self):
        for text in (
            "Your salary of EUR 90 is confirmed for 2026-01-16. The receiving bank will convert it using the rate applied on the settlement date.",
            "Gaji sebesar EUR 90 dikonfirmasi untuk 2026-01-16. Bank penerima akan mengonversinya dengan kurs pada tanggal penyelesaian.",
        ):
            with self.subTest(text=text):
                result = self.eur_forecast(text, self.RATES)
                self.assertEqual(
                    credits(result),
                    normal(1800, 2000, 2000, days=(date(2026, 1, 16),) + PROJECTED[1:]),
                )
                self.assertTrue(result.is_complete)

    def test_confirmed_salary_above_history_keeps_the_history_amount_on_its_date(self):
        result = self.eur_forecast("Your salary of EUR 110 is confirmed for 2026-01-16.", self.RATES)
        self.assertEqual(credits(result)[0], (date(2026, 1, 16), Decimal(2000)))
        self.assertTrue(any("above settled history EUR 100" in reason for reason in cited(result)))

    def test_confirmed_salary_with_no_payday_to_replace_books_the_stated_amount(self):
        rates = self.RATES + [{"rate_date": "2026-04-01", "from_currency": "EUR", "to_currency": "ZAR", "rate": "20"}]
        result = self.eur_forecast("Your salary of EUR 110 is confirmed for 2026-04-01.", rates)
        self.assertIn((date(2026, 4, 1), Decimal(2200)), credits(result))

    def test_a_missing_rate_is_a_blocker_not_a_guess(self):
        result = self.eur_forecast("Your salary of EUR 90 is confirmed for 2026-01-16.", self.RATES[1:])
        self.assertFalse(result.is_complete)
        self.assertTrue(any("message_n" in blocker.reason for blocker in result.blockers))
        self.assertNotIn(date(2026, 1, 16), [day for day, _ in credits(result)])


class DateAndEndTemplateTests(unittest.TestCase):
    def test_payday_move_keeps_the_new_day_of_month(self):
        for text in (
            "Your confirmed salary is now expected on 2026-01-23. This replaces the payroll date shown in the earlier update. "
            "Please use the revised date for anything you normally pay around payday.",
            "Gaji yang sudah dikonfirmasi kini diperkirakan masuk pada 2026-01-23. Gunakan tanggal terbaru ini untuk pembayaran yang biasanya dilakukan saat gajian.",
        ):
            with self.subTest(text=text):
                days = (date(2026, 1, 23), date(2026, 2, 23), date(2026, 3, 23))
                self.assertEqual(credits(forecast([notice(text)])), normal(2000, 2000, 2000, days=days))

    def test_seasonal_contract_end_stops_the_merged_series(self):
        incomes = series("event_s", "Seasonal contract payment", days=HISTORY[:2]) + series(
            "event_t", "Temporary assignment pay", days=HISTORY[2:]
        )
        self.assertTrue(credits(forecast([], incomes=incomes)))
        for text in ("The current seasonal contract has ended.", "Kontrak musiman saat ini telah berakhir."):
            with self.subTest(text=text):
                result = forecast([notice(text)], incomes=incomes)
                self.assertEqual(credits(result), [])
                self.assertTrue(cited(result))

    def test_employment_end_stops_salary_and_leaves_history_alone(self):
        baseline = forecast([])
        for text in ("Your employment has ended.", "Hubungan kerja Anda telah berakhir."):
            with self.subTest(text=text):
                result = forecast([notice(text)])
                self.assertEqual(credits(result), [])
                self.assertEqual(result.opening_balance, baseline.opening_balance)
                debits = lambda f: [(e.day, e.amount, e.source_id) for e in f.entries if e.amount < 0]
                self.assertEqual(debits(result), debits(baseline))

    def test_household_income_end_stops_the_other_stream_not_the_salary(self):
        # Regression: this notice used to read only as a confirmation of the
        # household salary, and the ended income kept being projected.
        incomes = series("event_h", "Primary household salary") + series(
            "event_i", "Second household income", days=("2025-09-20", "2025-10-20", "2025-11-20", "2025-12-20"), amount="700"
        )
        for text in (
            "One household employment record has ended. The remaining confirmed monthly salary is ZAR 2500. "
            "Any income that has ended should be removed from future estimates.",
            "Salah satu sumber pendapatan kerja rumah tangga telah berakhir. Sisa gaji bulanan yang dikonfirmasi adalah ZAR 2500. "
            "Pendapatan yang sudah berakhir harus dikeluarkan dari perkiraan berikutnya.",
        ):
            with self.subTest(text=text):
                result = forecast([notice(text)], incomes=incomes)
                self.assertEqual(credits(result, "Second household income"), [])
                self.assertEqual(credits(result, "Primary household salary"), normal(2000, 2000, 2000))
                self.assertTrue(any("history amount kept" in reason for reason in cited(result)))


class ApplicabilityTests(unittest.TestCase):
    INCREASE = "Your monthly salary has increased to ZAR 2600. The change applies from 2026-01-15."

    def test_a_message_after_the_request_date_is_ignored(self):
        result = forecast([notice(self.INCREASE, sent_at="2026-01-06T09:30:00Z")])
        self.assertEqual(credits(result), normal(2000, 2000, 2000))

    def test_a_message_for_another_request_is_ignored(self):
        result = forecast([notice(self.INCREASE, request_id="request_zz")])
        self.assertEqual(credits(result), normal(2000, 2000, 2000))

    def test_a_newer_message_supersedes_an_older_one(self):
        older = notice(self.INCREASE, message_id="message_old", sent_at="2026-01-02T09:30:00Z")
        newer = notice(
            "Your monthly salary has increased to ZAR 2800. The change applies from 2026-02-15.",
            message_id="message_new",
        )
        result = forecast([newer, older])
        self.assertEqual(credits(result), normal(2000, 2800, 2800))
        self.assertFalse(cited(result, "message_old"))

    def test_ambiguous_increase_applies_to_no_stream(self):
        incomes = series("event_p", "Payroll credit") + series(
            "event_w", "Weekend salary", days=("2025-09-20", "2025-10-20", "2025-11-20", "2025-12-20"), amount="300"
        )
        result = forecast([notice(self.INCREASE)], incomes=incomes)
        self.assertEqual(credits(result, "Payroll credit"), normal(2000, 2000, 2000))
        self.assertTrue(any("applied to none" in reason for reason in cited(result)))

    def test_ambiguous_reduction_applies_to_every_candidate(self):
        incomes = series("event_p", "Payroll credit") + series(
            "event_w", "Weekend salary", days=("2025-09-20", "2025-10-20", "2025-11-20", "2025-12-20"), amount="300"
        )
        result = forecast([notice("Your next salary is reduced to ZAR 100.")], incomes=incomes)
        self.assertEqual(credits(result, "Payroll credit")[0][1], Decimal(100))
        self.assertEqual(credits(result, "Weekend salary")[0][1], Decimal(100))
        self.assertTrue(any("applied to all" in reason for reason in cited(result)))

    def test_an_unmatched_target_changes_nothing(self):
        result = forecast([notice("Your confirmed base salary is ZAR 9000.")])
        self.assertEqual(credits(result), normal(2000, 2000, 2000))
        self.assertTrue(any("matches no projected income stream" in reason for reason in cited(result)))

    def test_instruction_like_text_alone_does_nothing(self):
        text = (
            "Any income that has ended should be removed from future estimates. "
            "Please use the revised date for anything you normally pay around payday."
        )
        self.assertEqual(parse_income_facts(text, message_id="m", sent_at=date(2026, 1, 4)), [])
        self.assertEqual(credits(forecast([notice(text)])), normal(2000, 2000, 2000))


def salary_credits(result):
    """Every salary credit in the ledger, booked rows included."""
    return [(entry.day, entry.amount) for entry in result.entries if entry.amount > 0 and entry.category == "salary"]


class DatedSalaryTests(unittest.TestCase):
    TEMPLATES = (
        "Your first salary will be ZAR 2500. The confirmed credit date is 2026-01-15.",
        "Gaji pertama Anda sebesar ZAR 2500. Tanggal kredit yang dikonfirmasi adalah 2026-01-15.",
        "Your first salary from the new employer is ZAR 2500. It is confirmed for 2026-01-15.",
        "Gaji pertama dari perusahaan baru adalah ZAR 2500. Pembayaran sudah dikonfirmasi untuk 2026-01-15.",
        "Your first salary of ZAR 2500 is scheduled for 2026-01-15.",
        "Gaji pertama Anda sebesar ZAR 2500 dijadwalkan pada 2026-01-15.",
        # T06 has no Indonesian wording in the dataset.
        "Regular salary of ZAR 2500 resumes on 2026-01-15. A new recurring childcare payment begins in the same month.",
    )

    def test_each_template_books_the_dated_salary_and_continues_monthly(self):
        # Two settled paydays: no projectable schedule of their own.
        incomes = series("event_f", "First-job payroll", days=HISTORY[2:])
        self.assertEqual(credits(forecast([], incomes=incomes)), [])
        for text in self.TEMPLATES:
            with self.subTest(text=text):
                result = forecast([notice(text)], incomes=incomes)
                self.assertEqual(credits(result), normal(2500, 2500, 2500))
                self.assertTrue(cited(result))

    def test_childcare_sentence_books_nothing(self):
        result = forecast([notice(self.TEMPLATES[-1])], incomes=[])
        self.assertTrue(any("states no amount" in reason for reason in cited(result)))
        debits = lambda f: [(e.day, e.amount, e.source_id) for e in f.entries if e.amount < 0]
        self.assertEqual(debits(result), debits(forecast([], incomes=[])))

    def test_it_replaces_the_projected_previous_employer_pay_one_for_one(self):
        result = forecast([notice(self.TEMPLATES[2])], incomes=series("event_p", "Previous employer payroll"))
        self.assertEqual(credits(result), normal(2500, 2500, 2500))

    def test_a_date_before_the_request_is_only_noted(self):
        text = "Your first salary will be ZAR 2500. The confirmed credit date is 2026-01-02."
        result = forecast([notice(text, sent_at="2026-01-01T09:30:00Z")], incomes=[])
        self.assertEqual(credits(result), [])
        self.assertTrue(any("before the request date" in reason for reason in cited(result)))

    def test_a_missing_rate_is_a_blocker_naming_the_message(self):
        result = forecast([notice("Your first salary will be EUR 100. The confirmed credit date is 2026-01-15.")], incomes=[])
        self.assertFalse(result.is_complete)
        self.assertTrue(all("message_n" in blocker.reason for blocker in result.blockers))
        self.assertEqual(credits(result), [])

    def test_a_scheduled_salary_without_a_schedule_continues_monthly(self):
        incomes = [income("event_r", "2025-12-15", "Prorated first salary", amount="900")]
        scheduled = income("event_n", "2026-01-15", "Next confirmed salary", amount="2300")
        scheduled["status"] = "scheduled"
        result = forecast([], incomes=incomes + [scheduled])
        self.assertEqual(salary_credits(result), normal(2300, 2300, 2300))


class FinalPayrollTests(unittest.TestCase):
    # The final payroll follows an on-time series (last payday 2025-12-15), so
    # the lapse rule does not already hide the stream.
    FINAL = income("event_z", "2026-01-02", "Final employer payroll")

    def history(self):
        return series("event_p", "Payroll credit") + [dict(self.FINAL)]

    def test_final_payroll_ends_later_projections(self):
        baseline = forecast([], incomes=series("event_p", "Payroll credit"))
        result = forecast([], incomes=self.history())
        self.assertEqual(credits(result), [])
        self.assertTrue(any("event_z" in note.reason for note in result.notes))
        self.assertEqual(result.opening_balance, baseline.opening_balance)

    def test_a_later_scheduled_credit_keeps_the_projection(self):
        later = income("event_l", "2026-02-15", "Payroll credit")
        later["status"] = "scheduled"
        result = forecast([], incomes=self.history() + [later])
        self.assertTrue(credits(result))
        self.assertFalse(any("event_z" in note.reason for note in result.notes))

    def test_a_later_settled_credit_keeps_the_projection(self):
        later = income("event_l", "2026-01-04", "Payroll credit")
        result = forecast([], incomes=self.history() + [later])
        self.assertFalse(any("event_z" in note.reason for note in result.notes))

    def test_final_payroll_and_employment_ended_notice_agree(self):
        result = forecast([notice("Your employment has ended.")], incomes=self.history())
        self.assertEqual(credits(result), [])


class WindowAndLapseTests(unittest.TestCase):
    def debit_series(self, category, days, amount="30"):
        row = default_tables()["financial_events"][3]  # a settled monthly subscription
        return [
            {**row, "event_id": f"event_{category}{i}", "category": category, "amount": amount,
             "event_date": day, "settlement_date": day}
            for i, day in enumerate(days)
        ]

    def test_a_lapsed_credit_is_not_projected_and_noted(self):
        # Last payday 2025-11-15 on a monthly cadence: the 2025-12-15 payday
        # never arrived, so nothing is projected from the 2026-01-05 request.
        result = forecast([], incomes=series("event_p", "Payroll credit", days=HISTORY[:3]))
        self.assertEqual(credits(result), [])
        self.assertIn(
            "last occurrence 2025-11-15 and the next expected one never arrived; income not projected",
            [note.reason for note in result.notes if note.source_id == "salary/credit/Payroll credit"],
        )

    def test_a_credit_replaced_by_a_new_stream_is_still_projected(self):
        # The old payroll stopped after 2025-11-15, but a new payroll in the
        # same category first settled on 2025-12-20: a change of payer.
        incomes = series("event_p", "Previous employer payroll", days=HISTORY[:3]) + [
            income("event_n", "2025-12-20", "New employer payroll")
        ]
        result = forecast([], incomes=incomes)
        self.assertEqual(credits(result), normal(2000, 2000, 2000))
        self.assertFalse(any("never arrived" in note.reason for note in result.notes))

    def test_a_parallel_stream_is_not_a_successor(self):
        # The other description already settled before the lapsed series'
        # last payday, so it ran alongside it and replaced nothing.
        incomes = series("event_p", "Payroll credit", days=HISTORY[:3]) + [
            income("event_s1", "2025-10-01", "Side payroll"),
            income("event_s2", "2025-12-20", "Side payroll"),
        ]
        result = forecast([], incomes=incomes)
        self.assertEqual(credits(result), [])
        self.assertIn(
            "last occurrence 2025-11-15 and the next expected one never arrived; income not projected",
            [note.reason for note in result.notes if note.source_id == "salary/credit/Payroll credit"],
        )

    def test_an_on_time_credit_is_still_projected(self):
        result = forecast([], incomes=series("event_p", "Payroll credit"))
        self.assertEqual(credits(result), normal(2000, 2000, 2000))
        self.assertFalse(any("never arrived" in note.reason for note in result.notes))

    def test_debits_never_lapse(self):
        rows = self.debit_series("gym", ("2025-08-20", "2025-09-20", "2025-10-20"))
        result = forecast([], incomes=rows)
        self.assertEqual(
            [entry.day for entry in result.entries if entry.source_id == "gym/debit"],
            [date(2026, 1, 20), date(2026, 2, 20), date(2026, 3, 20)],
        )

    def test_the_window_ends_on_day_86(self):
        rows = self.debit_series("gym", ("2025-10-01", "2025-11-01", "2025-12-01")) + self.debit_series(
            "club", ("2025-10-02", "2025-11-02", "2025-12-02")
        )
        result = forecast([], incomes=rows)
        self.assertEqual(result.end_date, date(2026, 4, 1))
        days = lambda source: [entry.day for entry in result.entries if entry.source_id == source]
        self.assertEqual(days("gym/debit")[-1], date(2026, 4, 1))  # day 86 is checked
        self.assertEqual(days("club/debit")[-1], date(2026, 3, 2))  # day 87 is not


class RealDatasetCoverageTests(unittest.TestCase):
    def test_every_in_scope_template_message_is_matched(self):
        if not REAL_DATASET.is_dir():
            self.skipTest("real dataset not present")
        dataset = load_dataset(REAL_DATASET)
        counts = collections.Counter(
            fact.kind
            for messages in dataset.messages_by_user.values()
            for message in messages
            for fact in parse_income_facts(message.message_text, message_id=message.message_id, sent_at=message.sent_at.date())
        )
        self.assertEqual(
            counts,
            {
                "salary_increase": 9,
                "base_salary_confirmed": 9,
                "next_salary_reduced": 10,
                "temporary_pay": 10,
                "regular_salary_confirmed": 8,
                "one_off_extra": 8,
                "fx_salary_confirmed": 7,
                "payday_moved": 7,
                "income_ended": 20,
                "remaining_salary_confirmed": 7,
                "first_income": 27,
                "income_resumes": 8,
                "unpriced_commitment": 8,
            },
        )


if __name__ == "__main__":
    unittest.main()

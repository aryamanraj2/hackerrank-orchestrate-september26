"""Image amount overlay: only validated evidence fills a blank amount."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from helpers import PNG_BYTES, build_dataset, default_tables

from cashflow import build_forecast
from dataset_loader import load_dataset
from image_evidence import ImageEvidenceError
from test_cashflow import REQUEST_DATE, event

PNG_SHA = hashlib.sha256(PNG_BYTES).hexdigest()


def record(**overrides):
    row = {
        "image_id": "image_x",
        "event_id": "event_x",
        "image_sha256": PNG_SHA,
        "status": "resolved",
        "amount": "150.50",
        "currency": "ZAR",
        "field_label": "Amount due",
        "selection_rationale": "the total payable",
        "reason": None,
    }
    row.update(overrides)
    return row


class ImageEvidenceTestCase(unittest.TestCase):
    def forecast(self, records=None, *, events=None, images=None, raw=None):
        """Forecast ``user_a`` with a pending blank ``event_x`` linked to ``image_x``."""
        data = default_tables()
        rows = events if events is not None else data["financial_events"] + [
            event(event_id="event_x", status="pending", amount="", category="utilities")
        ]
        image_rows = data["images"] + [
            {"image_id": "image_x", "user_id": "user_a", "request_id": "",
             "related_event_id": "event_x"},
            {"image_id": "image_other", "user_id": "user_a", "request_id": "",
             "related_event_id": "event_a"},
        ] + list(images or [])
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(
                Path(tmp), tables={"financial_events": rows, "images": image_rows}
            )
            artifact = Path(tmp) / "image_amounts.json"
            if raw is not None:
                artifact.write_text(raw, encoding="utf-8")
            elif records is not None:
                artifact.write_text(
                    json.dumps({"prompt_version": "t", "model": "t", "records": records}),
                    encoding="utf-8",
                )
            dataset = load_dataset(Path(tmp) / "dataset")
            return build_forecast(dataset, "user_a", REQUEST_DATE, image_evidence=artifact)

    def blockers_for(self, forecast, source_id):
        return [b for b in forecast.blockers if b.source_id == source_id]


class AppliedTests(ImageEvidenceTestCase):
    def test_valid_record_fills_the_blank_and_clears_the_blocker(self):
        forecast = self.forecast([record()])
        self.assertEqual(self.blockers_for(forecast, "event_x"), [])
        [entry] = [e for e in forecast.entries if e.source_id == "event_x"]
        self.assertEqual(entry.amount, Decimal("-150.50"))
        self.assertEqual(entry.day, date(2026, 1, 10))
        [note] = [n for n in forecast.notes if n.source_id == "event_x"]
        self.assertIn("image_x", note.reason)
        self.assertIn("Amount due", note.reason)

    def test_missing_artifact_matches_current_behaviour(self):
        without = self.forecast()
        with tempfile.TemporaryDirectory() as tmp:
            build_dataset(Path(tmp))
            dataset = load_dataset(Path(tmp) / "dataset")
            baseline = build_forecast(dataset, "user_a", REQUEST_DATE, image_evidence=None)
            missing = build_forecast(
                dataset, "user_a", REQUEST_DATE, image_evidence=Path(tmp) / "absent.json"
            )
        self.assertEqual(baseline, missing)
        self.assertTrue(self.blockers_for(without, "event_x"))
        self.assertEqual(without.notes, ())

    def test_malformed_json_raises_a_clear_error(self):
        with self.assertRaisesRegex(ImageEvidenceError, "not valid JSON"):
            self.forecast(raw="{not json")
        with self.assertRaisesRegex(ImageEvidenceError, "records"):
            self.forecast(raw='{"records": "nope"}')

    def test_resolved_blank_in_a_recurring_series_lifts_the_series_blocker(self):
        rows = [
            row for row in default_tables()["financial_events"]
            if row["category"] != "streaming"
        ] + [
            event(event_id="event_gym_1", category="gym", amount="10",
                  event_date="2025-10-12", settlement_date="2025-10-12"),
            event(event_id="event_gym_2", category="gym", amount="10",
                  event_date="2025-11-11", settlement_date="2025-11-11"),
            event(event_id="event_x", category="gym", amount="",
                  event_date="2025-12-11", settlement_date="2025-12-11"),
        ]
        blocked = self.forecast([record(status="unresolved", amount=None, reason="blurry")],
                                events=rows)
        [blocker] = self.blockers_for(blocked, "gym/debit")
        self.assertIn("unresolved amount", blocker.reason)

        resolved = self.forecast([record(amount="12")], events=rows)
        self.assertEqual(self.blockers_for(resolved, "gym/debit"), [])
        booked = [e.amount for e in resolved.entries if e.source_id == "gym/debit"]
        self.assertTrue(booked)
        self.assertTrue(all(amount == Decimal("-12") for amount in booked))


class RejectionTests(ImageEvidenceTestCase):
    def assertRejected(self, records, why, source_id="event_x"):
        forecast = self.forecast(records)
        [blocker] = self.blockers_for(forecast, "event_x")
        self.assertIn("blank", blocker.reason)
        self.assertEqual([e for e in forecast.entries if e.source_id == "event_x"], [])
        notes = [n.reason for n in forecast.notes if n.source_id == source_id]
        self.assertTrue(notes)
        self.assertTrue(all(why in reason for reason in notes), notes)

    def test_unresolved_status(self):
        self.assertRejected(
            [record(status="unresolved", amount=None, reason="amount cropped")],
            "amount cropped",
        )

    def test_unknown_status(self):
        self.assertRejected([record(status="guessed")], "status")

    def test_event_with_a_supplied_amount(self):
        self.assertRejected(
            [record(event_id="event_a", image_id="image_other", amount="999")],
            "already has a supplied amount",
            source_id="event_a",
        )

    def test_image_not_linked_to_the_event(self):
        self.assertRejected([record(image_id="image_other")], "does not link")

    def test_sha256_mismatch(self):
        self.assertRejected([record(image_sha256="0" * 64)], "image_sha256")

    def test_currency_mismatch(self):
        self.assertRejected([record(currency="EUR")], "currency")

    def test_bad_amounts(self):
        for amount in ("0", "0.00", "-5", "abc", "1,00,000", "1e3", " 12", 150.5, None, "١٢"):
            with self.subTest(amount=amount):
                self.assertRejected([record(amount=amount)], "rejected: amount")

    def test_duplicate_records_for_one_event_are_both_rejected(self):
        forecast = self.forecast([record(amount="100"), record(amount="100")])
        self.assertTrue(self.blockers_for(forecast, "event_x"))
        notes = [n.reason for n in forecast.notes if n.source_id == "event_x"]
        self.assertEqual(len(notes), 2)
        self.assertTrue(all("more than one record" in reason for reason in notes))

    def test_records_for_no_known_event_or_image_are_not_surfaced(self):
        forecast = self.forecast([record(event_id="event_404", image_id="image_404")])
        self.assertTrue(self.blockers_for(forecast, "event_x"))
        self.assertEqual(forecast.notes, ())


if __name__ == "__main__":
    unittest.main()

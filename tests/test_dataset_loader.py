"""Loading and indexing the real participant-facing dataset."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from helpers import REAL_DATASET, REPO_ROOT

from dataset_loader import (
    MAX_OPTIONS_PER_REQUEST,
    MIN_OPTIONS_PER_REQUEST,
    find_dataset_root,
    load_dataset,
)

EXPECTED_EVALUATION_REQUESTS = 250


class RealDatasetLoads(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(REAL_DATASET)

    def test_dataset_root_is_discoverable(self) -> None:
        self.assertEqual(find_dataset_root(REPO_ROOT), REAL_DATASET)

    def test_no_contract_violations(self) -> None:
        self.assertEqual(
            [str(issue) for issue in self.dataset.issues],
            [],
            "the shipped dataset should satisfy every contract rule",
        )

    def test_every_evaluation_request_is_loaded(self) -> None:
        self.assertEqual(len(self.dataset.requests), EXPECTED_EVALUATION_REQUESTS)
        self.assertEqual(len(self.dataset.request_by_id), EXPECTED_EVALUATION_REQUESTS)
        self.assertEqual(
            len(self.dataset.output_template_request_ids), EXPECTED_EVALUATION_REQUESTS
        )

    def test_sample_requests_carry_their_completed_output(self) -> None:
        self.assertTrue(self.dataset.sample_requests)
        for sample in self.dataset.sample_requests:
            self.assertIsNotNone(sample.affordability_status)
            self.assertIsNotNone(sample.recommended_payment_method)
            self.assertIsNotNone(sample.amount_safe_to_pay)
            self.assertLessEqual(sample.amount_safe_to_pay, sample.request.requested_amount)

    def test_blank_event_amounts_stay_none(self) -> None:
        missing = self.dataset.events_missing_amount
        self.assertTrue(missing, "the dataset ships events whose amount is in an image")
        for event in missing:
            self.assertIsNone(event.amount)
            self.assertTrue(
                self.dataset.images_for_event(event.event_id),
                f"{event.event_id} has no linked image to recover the amount from",
            )

    def test_blank_optional_fields_are_none(self) -> None:
        self.assertTrue(
            any(profile.max_installment_months is None for profile in self.dataset.profiles)
        )
        self.assertTrue(
            any(event.minimum_allowed_amount is None for event in self.dataset.events)
        )
        self.assertTrue(any(event.linked_event_id is None for event in self.dataset.events))
        self.assertTrue(any(message.request_id is None for message in self.dataset.messages))
        self.assertTrue(
            any(
                option.payment_frequency_days is None
                for option in self.dataset.payment_options
            )
        )

    def test_amounts_are_exact_decimals(self) -> None:
        request = self.dataset.requests[0]
        self.assertIsInstance(request.requested_amount, Decimal)
        profile = self.dataset.profile_for_request(request)
        self.assertIsNotNone(profile)
        self.assertIsInstance(profile.current_available_balance, Decimal)


class Indexes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(REAL_DATASET)

    def test_profile_index_covers_every_requesting_user(self) -> None:
        for request in self.dataset.requests:
            self.assertIn(request.user_id, self.dataset.profile_by_user)

    def test_requests_and_events_indexed_by_user(self) -> None:
        for request in self.dataset.requests:
            self.assertIn(request, self.dataset.requests_by_user[request.user_id])
            self.assertTrue(self.dataset.events_for(request.user_id))

    def test_options_indexed_by_request_within_contract_range(self) -> None:
        for request in self.dataset.requests:
            options = self.dataset.options_for(request.request_id)
            self.assertTrue(
                MIN_OPTIONS_PER_REQUEST <= len(options) <= MAX_OPTIONS_PER_REQUEST
            )
            for option in options:
                self.assertEqual(option.request_id, request.request_id)

    def test_messages_and_images_indexed_three_ways(self) -> None:
        for message in self.dataset.messages:
            self.assertIn(message, self.dataset.messages_by_user[message.user_id])
            if message.request_id:
                self.assertIn(message, self.dataset.messages_by_request[message.request_id])
            if message.related_event_id:
                self.assertIn(message, self.dataset.messages_by_event[message.related_event_id])
        for image in self.dataset.images:
            self.assertIn(image, self.dataset.images_by_user[image.user_id])
            if image.request_id:
                self.assertIn(image, self.dataset.images_by_request[image.request_id])
            if image.related_event_id:
                self.assertIn(image, self.dataset.images_by_event[image.related_event_id])

    def test_image_paths_resolve_to_png_files(self) -> None:
        for image in self.dataset.images:
            self.assertEqual(image.path.name, f"{image.image_id}.png")
            self.assertTrue(image.exists)

    def test_linked_events_are_indexed_and_resolvable(self) -> None:
        linked = [event for event in self.dataset.events if event.linked_event_id]
        self.assertTrue(linked)
        for event in linked:
            self.assertIn(event.linked_event_id, self.dataset.event_by_id)
            self.assertIn(
                event, self.dataset.events_by_linked_event[event.linked_event_id]
            )

    def test_exchange_rate_lookup(self) -> None:
        rate = self.dataset.exchange_rates[0]
        self.assertEqual(
            self.dataset.exchange_rate(rate.rate_date, rate.from_currency, rate.to_currency),
            rate.rate,
        )
        self.assertEqual(self.dataset.exchange_rate(rate.rate_date, "ZAR", "ZAR"), Decimal(1))
        self.assertIsNone(self.dataset.exchange_rate(date(1999, 1, 1), "USD", "INR"))

    def test_known_request_ids_include_samples(self) -> None:
        known = self.dataset.known_request_ids
        self.assertIn(self.dataset.requests[0].request_id, known)
        self.assertIn(self.dataset.sample_requests[0].request_id, known)


class OptionSchedules(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(REAL_DATASET)

    def test_schedule_uses_only_supplied_fields(self) -> None:
        for option in self.dataset.payment_options:
            schedule = option.schedule()
            self.assertEqual(len(schedule), max(option.number_of_payments, 1))
            self.assertEqual(schedule[0][0], option.first_payment_date)
            for when, amount in schedule:
                self.assertEqual(amount, option.payment_amount)
            if len(schedule) > 1:
                gap = (schedule[1][0] - schedule[0][0]).days
                self.assertEqual(gap, option.payment_frequency_days)


if __name__ == "__main__":
    unittest.main()

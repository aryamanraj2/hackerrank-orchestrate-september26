"""Deterministic loading, validation and indexing of the Buy or Wait? dataset.

This module is a pure data-access layer. It parses every participant-facing
file in ``dataset/``, keeps blank optional fields as ``None`` (never as zero),
validates the schemas and referential integrity the problem statement relies
on, and exposes explicit indexes by user, request and related event.

No affordability logic lives here.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from output_schema import (
    REQUIRED_OUTPUT_COLUMNS as OUTPUT_COLUMNS,
    validate_output_columns,
    validate_output_request_ids,
)
from validation import DatasetError, ValidationIssue, data_line

# --------------------------------------------------------------------------
# Contract constants
# --------------------------------------------------------------------------

DATASET_DIRNAME = "dataset"
IMAGE_SUBDIR = Path("media") / "images"
IMAGE_SUFFIX = ".png"

REQUEST_TYPES = frozenset(
    {
        "purchase",
        "travel",
        "education",
        "family_transfer",
        "debt_repayment",
        "investment",
        "housing",
        "emergency_expense",
        "other",
    }
)

OPTION_PAYMENT_METHODS = frozenset({"full_payment", "partial_payment", "installments"})

MIN_OPTIONS_PER_REQUEST = 2
MAX_OPTIONS_PER_REQUEST = 4

# Statuses whose rows legitimately carry no settlement date (non-cash value
# snapshots). Every other row must state when it settles or is due to settle.
STATUSES_WITHOUT_SETTLEMENT = frozenset({"unrealized"})

REQUESTS_COLUMNS = (
    "request_id",
    "user_id",
    "request_date",
    "request_type",
    "requested_amount",
    "desired_completion_date",
    "allows_partial_payment",
    "request_text",
)

SAMPLE_REQUESTS_COLUMNS = REQUESTS_COLUMNS + OUTPUT_COLUMNS[1:]

PROFILE_COLUMNS = (
    "user_id",
    "home_currency",
    "current_available_balance",
    "minimum_balance_to_keep",
    "financial_priorities",
    "expense_categories_to_protect",
    "expense_categories_user_is_willing_to_reduce",
    "expense_categories_user_is_willing_to_stop",
    "payment_methods_user_will_consider",
    "max_installment_months",
)

EVENT_COLUMNS = (
    "event_id",
    "user_id",
    "event_type",
    "description",
    "category",
    "direction",
    "amount",
    "currency",
    "event_date",
    "settlement_date",
    "status",
    "linked_event_id",
    "flexibility",
    "minimum_allowed_amount",
)

RATE_COLUMNS = ("rate_date", "from_currency", "to_currency", "rate")

OPTION_COLUMNS = (
    "payment_option_id",
    "request_id",
    "payment_method",
    "payment_amount",
    "number_of_payments",
    "first_payment_date",
    "payment_frequency_days",
    "financing_fee",
    "total_payable_amount",
)

MESSAGE_COLUMNS = (
    "message_id",
    "user_id",
    "request_id",
    "related_event_id",
    "sent_at",
    "source_type",
    "message_text",
)

IMAGE_COLUMNS = ("image_id", "user_id", "request_id", "related_event_id")


# --------------------------------------------------------------------------
# Field parsing (blank optional values stay None)
# --------------------------------------------------------------------------


def parse_text(value: str | None) -> str:
    return (value or "").strip()


def parse_optional_text(value: str | None) -> str | None:
    text = parse_text(value)
    return text or None


def parse_required_text(value: str | None, column: str) -> str:
    text = parse_text(value)
    if not text:
        raise ValueError(f"'{column}' is required but blank")
    return text


def parse_optional_date(value: str | None, column: str) -> date | None:
    text = parse_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"'{column}' is not an ISO YYYY-MM-DD date: {text!r}") from exc


def parse_date(value: str | None, column: str) -> date:
    parsed = parse_optional_date(value, column)
    if parsed is None:
        raise ValueError(f"'{column}' is required but blank")
    return parsed


def parse_datetime(value: str | None, column: str) -> datetime:
    text = parse_text(value)
    if not text:
        raise ValueError(f"'{column}' is required but blank")
    normalised = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ValueError(f"'{column}' is not an ISO-8601 timestamp: {text!r}") from exc


def parse_optional_decimal(value: str | None, column: str) -> Decimal | None:
    """Parse a money field. Blank stays ``None``; NaN and infinities are errors.

    Non-finite values would silently poison every comparison in the forecast,
    so they are rejected at the boundary rather than carried forward.
    """
    text = parse_text(value).replace(",", "")
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"'{column}' is not a number: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"'{column}' is not a finite number: {value!r}")
    return parsed


def parse_decimal(value: str | None, column: str) -> Decimal:
    parsed = parse_optional_decimal(value, column)
    if parsed is None:
        raise ValueError(f"'{column}' is required but blank")
    return parsed


def parse_optional_int(value: str | None, column: str) -> int | None:
    """Parse a count field. Fractional input is an error, never truncated."""
    text = parse_text(value)
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"'{column}' is not an integer: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"'{column}' is not a finite number: {value!r}")
    if parsed != parsed.to_integral_value():
        raise ValueError(f"'{column}' is not a whole number: {value!r}")
    return int(parsed)


def parse_int(value: str | None, column: str) -> int:
    parsed = parse_optional_int(value, column)
    if parsed is None:
        raise ValueError(f"'{column}' is required but blank")
    return parsed


def parse_bool(value: str | None, column: str) -> bool:
    text = parse_text(value).lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    raise ValueError(f"'{column}' is not a boolean: {value!r}")


def parse_list(value: str | None) -> tuple[str, ...]:
    """Split a pipe-delimited profile field, dropping blank segments."""
    text = parse_text(value)
    if not text:
        return ()
    return tuple(part.strip() for part in text.split("|") if part.strip())


# --------------------------------------------------------------------------
# Record types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FinancialProfile:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: tuple[str, ...]
    expense_categories_to_protect: tuple[str, ...]
    expense_categories_user_is_willing_to_reduce: tuple[str, ...]
    expense_categories_user_is_willing_to_stop: tuple[str, ...]
    payment_methods_user_will_consider: tuple[str, ...]
    max_installment_months: int | None
    line: int

    @property
    def considers_installments(self) -> bool:
        return "installments" in self.payment_methods_user_will_consider


@dataclass(frozen=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str
    line: int


@dataclass(frozen=True)
class SampleRequest:
    """A public example: the request plus its already-completed output row."""

    request: Request
    amount_safe_to_pay: Decimal | None
    affordability_status: str | None
    recommended_payment_method: str | None
    payment_plan: str | None
    earliest_date_for_full_payment: date | None
    spending_changes_needed: str | None
    decision_explanation: str | None

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def user_id(self) -> str:
        return self.request.user_id


@dataclass(frozen=True)
class FinancialEvent:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Decimal | None
    currency: str
    event_date: date
    settlement_date: date | None
    status: str
    linked_event_id: str | None
    flexibility: str
    minimum_allowed_amount: Decimal | None
    line: int

    @property
    def amount_is_missing(self) -> bool:
        """True when the amount must be recovered from a linked image."""
        return self.amount is None

    @property
    def is_stoppable(self) -> bool:
        return self.flexibility in {"stoppable", "reducible_or_stoppable"}

    @property
    def is_reducible(self) -> bool:
        return self.flexibility in {"reducible", "reducible_or_stoppable"}


@dataclass(frozen=True)
class ExchangeRate:
    rate_date: date
    from_currency: str
    to_currency: str
    rate: Decimal
    line: int


@dataclass(frozen=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: int | None
    financing_fee: Decimal
    total_payable_amount: Decimal
    line: int

    def schedule(self) -> tuple[tuple[date, Decimal], ...]:
        """The option's dated payments, derived only from supplied fields."""
        if self.number_of_payments <= 1 or self.payment_frequency_days is None:
            return ((self.first_payment_date, self.payment_amount),)
        from datetime import timedelta

        return tuple(
            (
                self.first_payment_date + timedelta(days=self.payment_frequency_days * i),
                self.payment_amount,
            )
            for i in range(self.number_of_payments)
        )


@dataclass(frozen=True)
class Message:
    message_id: str
    user_id: str
    request_id: str | None
    related_event_id: str | None
    sent_at: datetime
    source_type: str
    message_text: str
    line: int


@dataclass(frozen=True)
class ImageRef:
    image_id: str
    user_id: str
    request_id: str | None
    related_event_id: str | None
    path: Path
    line: int

    @property
    def exists(self) -> bool:
        return self.path.is_file()


# --------------------------------------------------------------------------
# Table reading
# --------------------------------------------------------------------------


def _read_table(
    path: Path,
    expected_columns: Sequence[str],
    build: Callable[[Mapping[str, str], int], object],
    source: str,
) -> tuple[list[object], tuple[str, ...]]:
    """Read one CSV, enforcing its header and per-row field parsing."""
    if not path.is_file():
        raise DatasetError([ValidationIssue(source, "file is missing")])

    issues: list[ValidationIssue] = []
    records: list[object] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = tuple(reader.fieldnames or ())
        missing = [column for column in expected_columns if column not in columns]
        if missing:
            raise DatasetError(
                [
                    ValidationIssue(
                        source,
                        f"missing required column(s): {', '.join(missing)}; found {list(columns)}",
                        line=1,
                    )
                ]
            )
        for index, row in enumerate(reader):
            line = data_line(index)
            identifier = parse_text(row.get(expected_columns[0])) or None
            try:
                records.append(build(row, line))
            except ValueError as exc:
                issues.append(ValidationIssue(source, str(exc), line=line, identifier=identifier))
    if issues:
        raise DatasetError(issues, f"{path.name}: {len(issues)} unparsable row(s)")
    return records, columns


def _build_profile(row: Mapping[str, str], line: int) -> FinancialProfile:
    return FinancialProfile(
        user_id=parse_required_text(row.get("user_id"), "user_id"),
        home_currency=parse_required_text(row.get("home_currency"), "home_currency"),
        current_available_balance=parse_decimal(
            row.get("current_available_balance"), "current_available_balance"
        ),
        minimum_balance_to_keep=parse_decimal(
            row.get("minimum_balance_to_keep"), "minimum_balance_to_keep"
        ),
        financial_priorities=parse_list(row.get("financial_priorities")),
        expense_categories_to_protect=parse_list(row.get("expense_categories_to_protect")),
        expense_categories_user_is_willing_to_reduce=parse_list(
            row.get("expense_categories_user_is_willing_to_reduce")
        ),
        expense_categories_user_is_willing_to_stop=parse_list(
            row.get("expense_categories_user_is_willing_to_stop")
        ),
        payment_methods_user_will_consider=parse_list(
            row.get("payment_methods_user_will_consider")
        ),
        max_installment_months=parse_optional_int(
            row.get("max_installment_months"), "max_installment_months"
        ),
        line=line,
    )


def _build_request(row: Mapping[str, str], line: int) -> Request:
    request_type = parse_required_text(row.get("request_type"), "request_type")
    if request_type not in REQUEST_TYPES:
        raise ValueError(
            f"'request_type' is not an allowed value: {request_type!r}"
        )
    return Request(
        request_id=parse_required_text(row.get("request_id"), "request_id"),
        user_id=parse_required_text(row.get("user_id"), "user_id"),
        request_date=parse_date(row.get("request_date"), "request_date"),
        request_type=request_type,
        requested_amount=parse_decimal(row.get("requested_amount"), "requested_amount"),
        desired_completion_date=parse_date(
            row.get("desired_completion_date"), "desired_completion_date"
        ),
        allows_partial_payment=parse_bool(
            row.get("allows_partial_payment"), "allows_partial_payment"
        ),
        request_text=parse_required_text(row.get("request_text"), "request_text"),
        line=line,
    )


def _build_sample_request(row: Mapping[str, str], line: int) -> SampleRequest:
    return SampleRequest(
        request=_build_request(row, line),
        amount_safe_to_pay=parse_optional_decimal(
            row.get("amount_safe_to_pay"), "amount_safe_to_pay"
        ),
        affordability_status=parse_optional_text(row.get("affordability_status")),
        recommended_payment_method=parse_optional_text(row.get("recommended_payment_method")),
        payment_plan=parse_optional_text(row.get("payment_plan")),
        earliest_date_for_full_payment=parse_optional_date(
            row.get("earliest_date_for_full_payment"), "earliest_date_for_full_payment"
        ),
        spending_changes_needed=parse_optional_text(row.get("spending_changes_needed")),
        decision_explanation=parse_optional_text(row.get("decision_explanation")),
    )


def _build_event(row: Mapping[str, str], line: int) -> FinancialEvent:
    return FinancialEvent(
        event_id=parse_required_text(row.get("event_id"), "event_id"),
        user_id=parse_required_text(row.get("user_id"), "user_id"),
        event_type=parse_required_text(row.get("event_type"), "event_type"),
        description=parse_text(row.get("description")),
        category=parse_required_text(row.get("category"), "category"),
        direction=parse_required_text(row.get("direction"), "direction"),
        amount=parse_optional_decimal(row.get("amount"), "amount"),
        currency=parse_required_text(row.get("currency"), "currency"),
        event_date=parse_date(row.get("event_date"), "event_date"),
        settlement_date=parse_optional_date(row.get("settlement_date"), "settlement_date"),
        status=parse_required_text(row.get("status"), "status"),
        linked_event_id=parse_optional_text(row.get("linked_event_id")),
        flexibility=parse_required_text(row.get("flexibility"), "flexibility"),
        minimum_allowed_amount=parse_optional_decimal(
            row.get("minimum_allowed_amount"), "minimum_allowed_amount"
        ),
        line=line,
    )


def _build_rate(row: Mapping[str, str], line: int) -> ExchangeRate:
    return ExchangeRate(
        rate_date=parse_date(row.get("rate_date"), "rate_date"),
        from_currency=parse_required_text(row.get("from_currency"), "from_currency"),
        to_currency=parse_required_text(row.get("to_currency"), "to_currency"),
        rate=parse_decimal(row.get("rate"), "rate"),
        line=line,
    )


def _build_option(row: Mapping[str, str], line: int) -> PaymentOption:
    method = parse_required_text(row.get("payment_method"), "payment_method")
    if method not in OPTION_PAYMENT_METHODS:
        raise ValueError(f"'payment_method' is not an allowed value: {method!r}")
    return PaymentOption(
        payment_option_id=parse_required_text(row.get("payment_option_id"), "payment_option_id"),
        request_id=parse_required_text(row.get("request_id"), "request_id"),
        payment_method=method,
        payment_amount=parse_decimal(row.get("payment_amount"), "payment_amount"),
        number_of_payments=parse_int(row.get("number_of_payments"), "number_of_payments"),
        first_payment_date=parse_date(row.get("first_payment_date"), "first_payment_date"),
        payment_frequency_days=parse_optional_int(
            row.get("payment_frequency_days"), "payment_frequency_days"
        ),
        financing_fee=parse_decimal(row.get("financing_fee"), "financing_fee"),
        total_payable_amount=parse_decimal(
            row.get("total_payable_amount"), "total_payable_amount"
        ),
        line=line,
    )


def _build_message(row: Mapping[str, str], line: int) -> Message:
    return Message(
        message_id=parse_required_text(row.get("message_id"), "message_id"),
        user_id=parse_required_text(row.get("user_id"), "user_id"),
        request_id=parse_optional_text(row.get("request_id")),
        related_event_id=parse_optional_text(row.get("related_event_id")),
        sent_at=parse_datetime(row.get("sent_at"), "sent_at"),
        source_type=parse_required_text(row.get("source_type"), "source_type"),
        message_text=parse_text(row.get("message_text")),
        line=line,
    )


def _image_builder(images_dir: Path) -> Callable[[Mapping[str, str], int], ImageRef]:
    def build(row: Mapping[str, str], line: int) -> ImageRef:
        image_id = parse_required_text(row.get("image_id"), "image_id")
        return ImageRef(
            image_id=image_id,
            user_id=parse_required_text(row.get("user_id"), "user_id"),
            request_id=parse_optional_text(row.get("request_id")),
            related_event_id=parse_optional_text(row.get("related_event_id")),
            path=images_dir / f"{image_id}{IMAGE_SUFFIX}",
            line=line,
        )

    return build


# --------------------------------------------------------------------------
# Dataset container
# --------------------------------------------------------------------------


def _group(records: Iterable[object], key: Callable[[object], str | None]) -> dict:
    grouped: dict[str, list] = {}
    for record in records:
        value = key(record)
        if value is None:
            continue
        grouped.setdefault(value, []).append(record)
    return {name: tuple(items) for name, items in grouped.items()}


@dataclass(frozen=True)
class Dataset:
    """Every participant-facing record, parsed once and indexed for lookup."""

    root: Path
    requests: tuple[Request, ...]
    sample_requests: tuple[SampleRequest, ...]
    profiles: tuple[FinancialProfile, ...]
    events: tuple[FinancialEvent, ...]
    exchange_rates: tuple[ExchangeRate, ...]
    payment_options: tuple[PaymentOption, ...]
    messages: tuple[Message, ...]
    images: tuple[ImageRef, ...]
    output_template_columns: tuple[str, ...]
    output_template_request_ids: tuple[str, ...]
    issues: tuple[ValidationIssue, ...] = ()

    # -- indexes ---------------------------------------------------------
    profile_by_user: Mapping[str, FinancialProfile] = field(default_factory=dict)
    request_by_id: Mapping[str, Request] = field(default_factory=dict)
    requests_by_user: Mapping[str, tuple[Request, ...]] = field(default_factory=dict)
    sample_request_by_id: Mapping[str, SampleRequest] = field(default_factory=dict)
    event_by_id: Mapping[str, FinancialEvent] = field(default_factory=dict)
    events_by_user: Mapping[str, tuple[FinancialEvent, ...]] = field(default_factory=dict)
    events_by_linked_event: Mapping[str, tuple[FinancialEvent, ...]] = field(default_factory=dict)
    options_by_request: Mapping[str, tuple[PaymentOption, ...]] = field(default_factory=dict)
    messages_by_user: Mapping[str, tuple[Message, ...]] = field(default_factory=dict)
    messages_by_request: Mapping[str, tuple[Message, ...]] = field(default_factory=dict)
    messages_by_event: Mapping[str, tuple[Message, ...]] = field(default_factory=dict)
    images_by_user: Mapping[str, tuple[ImageRef, ...]] = field(default_factory=dict)
    images_by_request: Mapping[str, tuple[ImageRef, ...]] = field(default_factory=dict)
    images_by_event: Mapping[str, tuple[ImageRef, ...]] = field(default_factory=dict)
    rate_by_date_pair: Mapping[tuple[date, str, str], Decimal] = field(default_factory=dict)

    # -- convenience lookups --------------------------------------------
    def profile_for_request(self, request: Request) -> FinancialProfile | None:
        return self.profile_by_user.get(request.user_id)

    def options_for(self, request_id: str) -> tuple[PaymentOption, ...]:
        return self.options_by_request.get(request_id, ())

    def events_for(self, user_id: str) -> tuple[FinancialEvent, ...]:
        return self.events_by_user.get(user_id, ())

    def messages_for_request(self, request_id: str) -> tuple[Message, ...]:
        return self.messages_by_request.get(request_id, ())

    def images_for_event(self, event_id: str) -> tuple[ImageRef, ...]:
        return self.images_by_event.get(event_id, ())

    def exchange_rate(
        self, on_date: date, from_currency: str, to_currency: str
    ) -> Decimal | None:
        """The supplied rate for a dated currency pair, or ``None``.

        Same-currency pairs return ``1``. No rate is derived, inverted or
        interpolated here: the caller decides how to handle a missing rate.
        """
        if from_currency == to_currency:
            return Decimal(1)
        return self.rate_by_date_pair.get((on_date, from_currency, to_currency))

    @property
    def known_request_ids(self) -> frozenset[str]:
        """Evaluation and sample request ids, which supporting files may cite."""
        return frozenset(self.request_by_id) | frozenset(self.sample_request_by_id)

    @property
    def events_missing_amount(self) -> tuple[FinancialEvent, ...]:
        return tuple(event for event in self.events if event.amount_is_missing)


# --------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------


def find_dataset_root(start: Path | str | None = None) -> Path:
    """Locate ``dataset/`` by walking up from ``start`` (default: this file)."""
    origin = Path(start).resolve() if start is not None else Path(__file__).resolve()
    candidates = [origin] + list(origin.parents) if origin.is_dir() else list(origin.parents)
    for candidate in candidates:
        dataset_dir = candidate / DATASET_DIRNAME
        if (dataset_dir / "requests.csv").is_file():
            return dataset_dir
    raise DatasetError(
        [
            ValidationIssue(
                DATASET_DIRNAME,
                f"could not locate a '{DATASET_DIRNAME}/requests.csv' at or above {origin}",
            )
        ]
    )


def _duplicate_issues(
    records: Sequence[object], key: Callable[[object], str], source: str
) -> list[ValidationIssue]:
    seen: dict[str, int] = {}
    issues: list[ValidationIssue] = []
    for record in records:
        identifier = key(record)
        line = getattr(record, "line", None)
        if identifier in seen:
            issues.append(
                ValidationIssue(
                    source,
                    f"duplicate identifier, first seen on line {seen[identifier]}",
                    line=line,
                    identifier=identifier,
                )
            )
        else:
            seen[identifier] = line
    return issues


def _reference_issue(
    source: str,
    record: object,
    identifier: str,
    column: str,
    value: str,
    target_file: str,
) -> ValidationIssue:
    return ValidationIssue(
        source,
        f"references {value!r} which is not present in {target_file}",
        line=getattr(record, "line", None),
        identifier=identifier,
        column=column,
    )


def validate_dataset(dataset: Dataset) -> tuple[ValidationIssue, ...]:
    """Check every referential rule the problem statement depends on."""
    issues: list[ValidationIssue] = []

    issues += _duplicate_issues(dataset.profiles, lambda p: p.user_id, "financial_profiles.csv")
    issues += _duplicate_issues(dataset.requests, lambda r: r.request_id, "requests.csv")
    issues += _duplicate_issues(
        dataset.sample_requests, lambda s: s.request_id, "sample_requests.csv"
    )
    issues += _duplicate_issues(dataset.events, lambda e: e.event_id, "financial_events.csv")
    issues += _duplicate_issues(
        dataset.payment_options, lambda o: o.payment_option_id, "request_payment_options.csv"
    )
    issues += _duplicate_issues(dataset.messages, lambda m: m.message_id, "messages.csv")
    issues += _duplicate_issues(dataset.images, lambda i: i.image_id, "images.csv")

    known_users = frozenset(dataset.profile_by_user)
    known_events = frozenset(dataset.event_by_id)
    known_requests = dataset.known_request_ids

    # Every request must have the profile it will be decided against.
    for request in dataset.requests:
        if request.user_id not in known_users:
            issues.append(
                _reference_issue(
                    "requests.csv",
                    request,
                    request.request_id,
                    "user_id",
                    request.user_id,
                    "financial_profiles.csv",
                )
            )
        if request.desired_completion_date < request.request_date:
            issues.append(
                ValidationIssue(
                    "requests.csv",
                    "desired_completion_date is before request_date",
                    line=request.line,
                    identifier=request.request_id,
                )
            )

    for sample in dataset.sample_requests:
        if sample.user_id not in known_users:
            issues.append(
                _reference_issue(
                    "sample_requests.csv",
                    sample.request,
                    sample.request_id,
                    "user_id",
                    sample.user_id,
                    "financial_profiles.csv",
                )
            )

    # Events: owning profile, lifecycle link, settlement date, missing amounts.
    for event in dataset.events:
        if event.user_id not in known_users:
            issues.append(
                _reference_issue(
                    "financial_events.csv",
                    event,
                    event.event_id,
                    "user_id",
                    event.user_id,
                    "financial_profiles.csv",
                )
            )
        if event.linked_event_id and event.linked_event_id not in known_events:
            issues.append(
                _reference_issue(
                    "financial_events.csv",
                    event,
                    event.event_id,
                    "linked_event_id",
                    event.linked_event_id,
                    "financial_events.csv",
                )
            )
        if event.settlement_date is None and event.status not in STATUSES_WITHOUT_SETTLEMENT:
            issues.append(
                ValidationIssue(
                    "financial_events.csv",
                    f"settlement_date is blank for status {event.status!r}",
                    line=event.line,
                    identifier=event.event_id,
                    column="settlement_date",
                )
            )
        if event.amount_is_missing:
            # The spec recovers a blank amount from a linked image, so a
            # readable image file must exist for the event.
            linked = dataset.images_for_event(event.event_id)
            if not linked:
                issues.append(
                    ValidationIssue(
                        "financial_events.csv",
                        "amount is blank and no images.csv row links to this event",
                        line=event.line,
                        identifier=event.event_id,
                        column="amount",
                    )
                )
            else:
                for image in linked:
                    if not image.exists:
                        issues.append(
                            ValidationIssue(
                                "images.csv",
                                f"image file is missing for blank amount on event "
                                f"{event.event_id}: {image.path}",
                                line=image.line,
                                identifier=image.image_id,
                            )
                        )

    # Payment options must belong to a supplied request, and each evaluation
    # request must carry the two-to-four options the spec promises.
    for option in dataset.payment_options:
        if option.request_id not in known_requests:
            issues.append(
                _reference_issue(
                    "request_payment_options.csv",
                    option,
                    option.payment_option_id,
                    "request_id",
                    option.request_id,
                    "requests.csv / sample_requests.csv",
                )
            )
        if option.payment_method == "installments" and option.number_of_payments > 1:
            if option.payment_frequency_days is None:
                issues.append(
                    ValidationIssue(
                        "request_payment_options.csv",
                        "installment option with multiple payments has no payment_frequency_days",
                        line=option.line,
                        identifier=option.payment_option_id,
                        column="payment_frequency_days",
                    )
                )

    for request in dataset.requests:
        count = len(dataset.options_for(request.request_id))
        if not MIN_OPTIONS_PER_REQUEST <= count <= MAX_OPTIONS_PER_REQUEST:
            issues.append(
                ValidationIssue(
                    "request_payment_options.csv",
                    f"request has {count} payment option(s); the contract expects "
                    f"{MIN_OPTIONS_PER_REQUEST}-{MAX_OPTIONS_PER_REQUEST}",
                    line=request.line,
                    identifier=request.request_id,
                )
            )

    # Supporting evidence must point at records that exist.
    for message in dataset.messages:
        if message.user_id not in known_users:
            issues.append(
                _reference_issue(
                    "messages.csv",
                    message,
                    message.message_id,
                    "user_id",
                    message.user_id,
                    "financial_profiles.csv",
                )
            )
        if message.request_id and message.request_id not in known_requests:
            issues.append(
                _reference_issue(
                    "messages.csv",
                    message,
                    message.message_id,
                    "request_id",
                    message.request_id,
                    "requests.csv / sample_requests.csv",
                )
            )
        if message.related_event_id and message.related_event_id not in known_events:
            issues.append(
                _reference_issue(
                    "messages.csv",
                    message,
                    message.message_id,
                    "related_event_id",
                    message.related_event_id,
                    "financial_events.csv",
                )
            )

    for image in dataset.images:
        if image.user_id not in known_users:
            issues.append(
                _reference_issue(
                    "images.csv",
                    image,
                    image.image_id,
                    "user_id",
                    image.user_id,
                    "financial_profiles.csv",
                )
            )
        if image.request_id and image.request_id not in known_requests:
            issues.append(
                _reference_issue(
                    "images.csv",
                    image,
                    image.image_id,
                    "request_id",
                    image.request_id,
                    "requests.csv / sample_requests.csv",
                )
            )
        if image.related_event_id and image.related_event_id not in known_events:
            issues.append(
                _reference_issue(
                    "images.csv",
                    image,
                    image.image_id,
                    "related_event_id",
                    image.related_event_id,
                    "financial_events.csv",
                )
            )
        if not image.exists:
            issues.append(
                ValidationIssue(
                    "images.csv",
                    f"image file does not exist: {image.path}",
                    line=image.line,
                    identifier=image.image_id,
                )
            )

    # The blank template states the output contract; keep it in sync.
    issues += validate_output_columns(
        dataset.output_template_columns, source="dataset/output.csv"
    )
    issues += validate_output_request_ids(
        dataset.output_template_request_ids,
        tuple(request.request_id for request in dataset.requests),
        source="dataset/output.csv",
    )
    return tuple(issues)


def _read_output_template(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not path.is_file():
        raise DatasetError([ValidationIssue("dataset/output.csv", "file is missing")])
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    if not rows:
        raise DatasetError([ValidationIssue("dataset/output.csv", "file is empty")])
    header = tuple(cell.strip() for cell in rows[0])
    request_ids = tuple(row[0].strip() for row in rows[1:] if row and row[0].strip())
    return header, request_ids


def load_dataset(root: Path | str | None = None, *, strict: bool = False) -> Dataset:
    """Load, index and validate every participant-facing file.

    Parsing failures (bad header, unparsable field) raise :class:`DatasetError`
    immediately. Referential findings are collected on ``dataset.issues``; pass
    ``strict=True`` to raise on those as well.
    """
    dataset_root = Path(root) if root is not None else find_dataset_root()
    images_dir = dataset_root / IMAGE_SUBDIR

    requests, _ = _read_table(
        dataset_root / "requests.csv", REQUESTS_COLUMNS, _build_request, "dataset/requests.csv"
    )
    samples, _ = _read_table(
        dataset_root / "sample_requests.csv",
        SAMPLE_REQUESTS_COLUMNS,
        _build_sample_request,
        "dataset/sample_requests.csv",
    )
    profiles, _ = _read_table(
        dataset_root / "financial_profiles.csv",
        PROFILE_COLUMNS,
        _build_profile,
        "dataset/financial_profiles.csv",
    )
    events, _ = _read_table(
        dataset_root / "financial_events.csv",
        EVENT_COLUMNS,
        _build_event,
        "dataset/financial_events.csv",
    )
    rates, _ = _read_table(
        dataset_root / "exchange_rates.csv",
        RATE_COLUMNS,
        _build_rate,
        "dataset/exchange_rates.csv",
    )
    options, _ = _read_table(
        dataset_root / "request_payment_options.csv",
        OPTION_COLUMNS,
        _build_option,
        "dataset/request_payment_options.csv",
    )
    messages, _ = _read_table(
        dataset_root / "messages.csv", MESSAGE_COLUMNS, _build_message, "dataset/messages.csv"
    )
    images, _ = _read_table(
        dataset_root / "images.csv",
        IMAGE_COLUMNS,
        _image_builder(images_dir),
        "dataset/images.csv",
    )
    template_columns, template_request_ids = _read_output_template(dataset_root / "output.csv")

    dataset = Dataset(
        root=dataset_root,
        requests=tuple(requests),
        sample_requests=tuple(samples),
        profiles=tuple(profiles),
        events=tuple(events),
        exchange_rates=tuple(rates),
        payment_options=tuple(options),
        messages=tuple(messages),
        images=tuple(images),
        output_template_columns=template_columns,
        output_template_request_ids=template_request_ids,
        profile_by_user={profile.user_id: profile for profile in profiles},
        request_by_id={request.request_id: request for request in requests},
        requests_by_user=_group(requests, lambda r: r.user_id),
        sample_request_by_id={sample.request_id: sample for sample in samples},
        event_by_id={event.event_id: event for event in events},
        events_by_user=_group(events, lambda e: e.user_id),
        events_by_linked_event=_group(events, lambda e: e.linked_event_id),
        options_by_request=_group(options, lambda o: o.request_id),
        messages_by_user=_group(messages, lambda m: m.user_id),
        messages_by_request=_group(messages, lambda m: m.request_id),
        messages_by_event=_group(messages, lambda m: m.related_event_id),
        images_by_user=_group(images, lambda i: i.user_id),
        images_by_request=_group(images, lambda i: i.request_id),
        images_by_event=_group(images, lambda i: i.related_event_id),
        rate_by_date_pair={
            (rate.rate_date, rate.from_currency, rate.to_currency): rate.rate for rate in rates
        },
    )

    issues = validate_dataset(dataset)
    if strict and issues:
        raise DatasetError(issues, "dataset validation failed")
    return Dataset(**{**dataset.__dict__, "issues": issues})

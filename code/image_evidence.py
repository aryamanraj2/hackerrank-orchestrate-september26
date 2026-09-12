"""Offline overlay of image-extracted amounts onto blank financial events.

A blank amount means the figure lives in a linked image. A separate resolver
reads those images once and writes a provenance-carrying JSON artifact; this
module only consumes it. Every record is checked against the dataset before
its amount is trusted: the event must still be blank, the image must be the one
``images.csv`` links to that event, the PNG must be byte-identical to the one
that was read, and the amount must be a plain positive decimal in the event's
own currency. Anything else is rejected and the amount stays blank, so the
forecast keeps its blocker instead of booking a guess.

The dataset itself is never modified. Resolved amounts are applied to copies
of the events one forecast is about to use.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

#: Where the resolver writes its artifact.
IMAGE_AMOUNTS_PATH = Path(__file__).resolve().parent / "evidence" / "image_amounts.json"

#: Digits with an optional fractional part. No sign, exponent or grouping.
PLAIN_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class ImageEvidenceError(ValueError):
    """The artifact exists but cannot be read as an image-amount artifact."""


def load_records(path: Path | None) -> list[dict] | None:
    """The artifact's records, or ``None`` when there is no artifact."""
    if path is None or not Path(path).is_file():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ImageEvidenceError(f"{path}: not valid JSON ({error})") from error
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ImageEvidenceError(f"{path}: expected an object with a 'records' list of objects")
    return records


def _rejection(dataset, record: dict, duplicates: set) -> str | None:
    """Why this resolved record cannot be applied, or ``None`` if it can."""
    event_id = record.get("event_id")
    image_id = record.get("image_id")
    if event_id in duplicates:
        return "more than one record for this event"
    event = dataset.event_by_id.get(event_id)
    if event is None:
        return "event is not in the dataset"
    if event.amount is not None:
        return "event already has a supplied amount"
    image = next(
        (ref for ref in dataset.images_for_event(event_id) if ref.image_id == image_id),
        None,
    )
    if image is None:
        return f"images.csv does not link {image_id} to this event"
    if not image.exists:
        return f"image file {image.path.name} is missing"
    expected = record.get("image_sha256")
    digest = hashlib.sha256(image.path.read_bytes()).hexdigest()
    if not isinstance(expected, str) or expected.lower() != digest:
        return "image_sha256 does not match the image file"
    if record.get("currency") != event.currency:
        return f"currency {record.get('currency')!r} is not the event's {event.currency}"
    amount = record.get("amount")
    if not isinstance(amount, str) or not PLAIN_DECIMAL.fullmatch(amount):
        return f"amount {amount!r} is not a plain decimal string"
    try:
        value = Decimal(amount)
    except InvalidOperation:
        return f"amount {amount!r} does not parse"
    if not value.is_finite() or value <= 0:
        return f"amount {amount!r} is not positive"
    return None


def _owner(dataset, record: dict) -> str | None:
    """The user a record is about, from its event or failing that its image."""
    event = dataset.event_by_id.get(record.get("event_id"))
    if event is not None:
        return event.user_id
    image = next((i for i in dataset.images if i.image_id == record.get("image_id")), None)
    return image.user_id if image is not None else None


def apply_image_evidence(
    dataset, events: Sequence, user_id: str, path: Path | None = IMAGE_AMOUNTS_PATH
) -> tuple[tuple, list[tuple[str, str]]]:
    """``events`` with validated image amounts filled in, plus audit notes.

    Notes are ``(source_id, reason)`` pairs for this user's records only: one
    per applied amount, rejected record and unresolved record.
    """
    records = load_records(path)
    if records is None:
        return tuple(events), []

    counts: dict = {}
    for record in records:
        counts[record.get("event_id")] = counts.get(record.get("event_id"), 0) + 1
    duplicates = {event_id for event_id, count in counts.items() if count > 1}

    amounts: dict[str, Decimal] = {}
    notes: list[tuple[str, str]] = []
    for record in records:
        if _owner(dataset, record) != user_id:
            continue
        event_id = str(record.get("event_id"))
        image_id = record.get("image_id")
        status = record.get("status")
        if status == "unresolved" and event_id not in duplicates:
            reason = record.get("reason") or "no reason given"
            notes.append((event_id, f"{image_id} left unresolved: {reason}; amount stays blank"))
            continue
        rejection = (
            f"status is {status!r}"
            if status not in {"resolved", "unresolved"}
            else _rejection(dataset, record, duplicates)
        )
        if rejection is not None:
            notes.append(
                (event_id, f"{image_id} evidence rejected: {rejection}; amount stays blank")
            )
            continue
        amounts[event_id] = Decimal(record["amount"])
        notes.append(
            (
                event_id,
                f"amount {record['amount']} {record['currency']} read from {image_id} "
                f"field {record.get('field_label')!r}",
            )
        )

    patched = tuple(
        replace(event, amount=amounts[event.event_id]) if event.event_id in amounts else event
        for event in events
    )
    return patched, notes

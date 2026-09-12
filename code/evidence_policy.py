"""Deterministic reading of message evidence that withholds future income.

A message is untrusted prose. It never carries an instruction this system
obeys, and it never invents a financial fact. The one thing it can do here is
state, in explicit terms, that a future payout from an income stream is not
confirmed money: the payout is still pending, the balance is not yet
withdrawable, the run has been delayed or cancelled. When a message says that
plainly, projecting the next occurrence of that stream would count money the
user cannot spend.

The policy is deliberately narrow. It fires only on explicit financial-state
wording about the payout itself, it needs two independent markers so that a
single word lifted out of an unrelated sentence cannot trigger it, and a
statement confirming the income overrides it for the stream that statement
actually names. Prose that merely sounds uncertain does nothing.

What a hold does is equally narrow: it withholds *future projected* credits
for that stream. Cash that already settled stays in the opening balance
exactly as it was — a message cannot unspend history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Sequence

#: Explicit statements that a future payout is not yet money. Each entry is one
#: independent marker; the wording is matched in the languages the dataset uses.
HOLD_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"payout is still pending",
        r"payout (?:has been|was) (?:delayed|cancelled|canceled)",
        r"(?:is ?n.t|is not|not yet) withdrawable",
        r"balance is ?n.t[^.]{0,40}withdraw",
        r"pembayaran berikutnya[^.]{0,60}masih tertunda",
        r"pembayaran[^.]{0,40}(?:ditunda|dibatalkan)",
        r"(?:belum|tidak) dapat ditarik",
        r"(?:still )?pending approval",
        r"belum disetujui",
        r"(?:stay|stays) out of the (?:payout|payment)",
        r"tidak masuk pembayaran",
    )
)

#: A statement that the income *is* confirmed. Payroll notices routinely confirm
#: a base salary while flagging a separate variable component as pending; that
#: is not a reason to withhold the confirmed stream.
CONFIRMED_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"confirmed (?:base|monthly|regular) salary",
        r"salary is confirmed",
        r"gaji (?:pokok|rutin)[^.]{0,40}dikonfirmasi",
        r"dikonfirmasi adalah",
        r"(?:has been|was) (?:released|completed|paid|settled)",
        r"is now withdrawable",
        r"sudah (?:cair|dibayarkan|selesai)",
    )
)

#: One marker is a word; two are a statement. Requiring two is what keeps a
#: negated sentence ("there are no pending proceeds") from reading as a hold.
MIN_MARKERS = 2

#: Words that place a sentence on one income stream rather than another. A
#: payroll notice routinely confirms the fixed part and withholds the variable
#: part in the same breath, so the two have to be told apart. ``variable`` is
#: matched first: "monthly sales commission" is a commission before it is
#: anything else.
STREAM_ROLE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "variable",
        ("commission", "bonus", "payout", "earnings", "komisi", "insentif"),
    ),
    (
        "base",
        ("salary", "payroll", "wages", "retainer", "gaji", "penggajian", "upah"),
    ),
)

#: Only earned income streams can be held. Everything else is either already
#: excluded from the forecast or is not a stream at all.
INCOME_EVENT_TYPES = frozenset({"income"})


@dataclass(frozen=True)
class IncomeHold:
    """One income stream whose future projections a message withholds."""

    #: The stream label — the supplied event description it was detected from.
    stream: str
    message_id: str
    reason: str


def stream_role(text: str) -> str | None:
    """Which kind of income this text speaks about, or ``None`` if unstated."""
    lowered = text.lower()
    for role, words in STREAM_ROLE_WORDS:
        if any(word in lowered for word in words):
            return role
    return None


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[.!?\n]+", text) if part.strip()]


def _markers(text: str) -> int:
    return sum(1 for marker in HOLD_MARKERS if marker.search(text))


def _is_confirmed(text: str) -> bool:
    return any(marker.search(text) for marker in CONFIRMED_MARKERS)


def states_income_is_unconfirmed(text: str) -> bool:
    """Whether this prose explicitly says a future payout is not yet money."""
    return _markers(text) >= MIN_MARKERS and not _is_confirmed(text)


def _roles_in(text: str, predicate) -> set[str]:
    """The stream roles named in the sentences ``predicate`` accepts."""
    return {
        role
        for sentence in _sentences(text)
        if predicate(sentence)
        for role in (stream_role(sentence),)
        if role is not None
    }


def _confirmation_reaches(
    text: str,
    stream: str,
    streams: Sequence[str],
    confirmed_roles: set[str],
    *,
    targeted: bool,
) -> bool:
    """Whether a confirmation in ``text`` speaks for ``stream`` in particular.

    Releasing a hold is the one move here that makes a forecast richer, so it
    needs the stream named, not merely implied. "The payout has been released"
    is a statement about a stream only when there is one payout stream it
    could mean; beside two of them it identifies neither, and both stay held.
    Ambiguity leaves the money out of the forecast.
    """
    if targeted:
        return True
    if stream and stream.lower() in text.lower():
        return True
    role = stream_role(stream)
    if role is None or role not in confirmed_roles:
        return False
    # A role identifies a stream only when one stream answers to it. Two
    # payouts beside each other make "the payout has cleared" mean neither.
    return [stream_role(other) for other in streams].count(role) == 1


def _verdicts(
    text: str, streams: Sequence[str], *, targeted: bool = False
) -> dict[str, bool]:
    """Each stream this message speaks about, mapped to hold (True) or clear.

    A message can do both at once — "your confirmed base salary is X, the
    commission on open deals is still pending" confirms one stream while
    withholding another. Each clause is read against the stream it names, and
    a clause that names no stream applies to all of them. A stream nobody
    mentioned is left exactly as it was.

    ``targeted`` says the message already names its stream by linking to one
    of its events, so the wording does not have to name it a second time.
    """
    held = _markers(text) >= MIN_MARKERS
    confirmed = _is_confirmed(text)
    if not held and not confirmed:
        return {}
    confirmed_roles = _roles_in(text, _is_confirmed)
    hold_roles = _roles_in(text, lambda sentence: _markers(sentence) > 0)

    verdicts: dict[str, bool] = {}
    for stream in streams:
        role = stream_role(stream)
        if confirmed and _confirmation_reaches(
            text, stream, streams, confirmed_roles, targeted=targeted
        ):
            verdicts[stream] = False
            continue
        if not held:
            continue
        if not targeted:
            if hold_roles and (role is None or role not in hold_roles):
                continue  # the hold is about a different stream
            if confirmed_roles and role is None:
                continue  # unlabelled stream beside a targeted confirmation
        verdicts[stream] = True
    return verdicts


def income_streams(dataset, user_id: str) -> tuple[str, ...]:
    """Every income stream this user has history for, by supplied description."""
    return tuple(
        sorted(
            {
                event.description
                for event in dataset.events_for(user_id)
                if event.direction == "credit"
                and event.event_type in INCOME_EVENT_TYPES
            }
        )
    )


def _linked_streams(dataset, message, user_id: str, request) -> tuple[str, ...]:
    """The income streams this message could speak for, or ``()`` if none.

    A message must be about this user, and about either one of their income
    events, the request under decision, or their account in general. One
    addressed to a different request belongs to that decision, not this one.
    A message tied to a single income event speaks only for that event's
    stream; a general notice is narrowed later by the wording itself.
    """
    if message.related_event_id:
        event = dataset.event_by_id.get(message.related_event_id)
        if event is None or event.user_id != user_id:
            return ()
        if event.direction != "credit" or event.event_type not in INCOME_EVENT_TYPES:
            return ()
        return (event.description,)
    if message.request_id:
        if request is None or message.request_id != request.request_id:
            return ()
    return income_streams(dataset, user_id)


def income_holds(
    dataset,
    user_id: str,
    *,
    as_of: date | None = None,
    request=None,
) -> dict[str, IncomeHold]:
    """Income streams whose future projections are withheld, by stream.

    Only messages this user had received by ``as_of`` are considered, so a
    decision never rests on evidence that had not arrived yet. Messages are
    then read in the order they arrived and each one overrides the last for
    the streams it speaks about: a later confirmation clears an earlier hold,
    a later hold replaces an earlier confirmation. That is the conflict order
    the challenge rules give — an explicit amendment first, then the newer
    record from the same source.
    """
    holds: dict[str, IncomeHold] = {}
    messages = sorted(
        dataset.messages_by_user.get(user_id, ()),
        key=lambda message: (message.sent_at, message.message_id),
    )
    streams = frozenset(income_streams(dataset, user_id))
    for message in messages:
        if as_of is not None and message.sent_at.date() > as_of:
            continue
        linked = _linked_streams(dataset, message, user_id, request)
        if not linked:
            continue
        verdicts = _verdicts(
            message.message_text, linked, targeted=bool(message.related_event_id)
        )
        for stream, held in verdicts.items():
            if not held:
                holds.pop(stream, None)
                continue
            holds[stream] = IncomeHold(
                stream=stream,
                message_id=message.message_id,
                reason=(
                    f"{message.message_id} states that {stream} is pending and "
                    "not withdrawable; future occurrences withheld"
                ),
            )
    return {stream: hold for stream, hold in holds.items() if stream in streams}

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
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping, Sequence

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


# ---------------------------------------------------------------------------
# Income facts: amounts, paydays and ended income stated by payroll notices.
# ---------------------------------------------------------------------------
#
# A notice is matched on its fact sentences only. The opener, the closing line
# and any advice in between ("use the revised date for anything you pay around
# payday", "income that has ended should be removed from future estimates")
# are never read, so they cannot widen what the stated fact does.

_AMOUNT = r"(?P<currency>[A-Z]{3}) (?P<amount>\d[\d,]*(?:\.\d+)?)"
_DATE = r"(?P<date>\d{4}-\d{2}-\d{2})"
_WORDY_DATE = r"(?P<wordy_date>\d{1,2} [A-Z][a-z]+ \d{4})"

#: Stream words, matched against income stream descriptions (English in the
#: dataset regardless of the notice language).
SALARY_WORDS = ("salary", "payroll", "wage")
SEASONAL_WORDS = ("season", "contract", "temporary")
HOUSEHOLD_WORDS = ("household",)

#: next_only: only the next projected occurrence. from_date: every projected
#: occurrence on or after ``effective_date`` (the request date when unstated).
NEXT_ONLY = "next_only"
FROM_DATE = "from_date"

#: What each kind does when more than one stream matches: ``True`` applies it
#: to every candidate, ``False`` to none. Whichever keeps less money in the
#: forecast is the safer reading.
APPLY_TO_ALL_WHEN_AMBIGUOUS = {
    "salary_increase": False,
    "base_salary_confirmed": False,
    "regular_salary_confirmed": False,
    "remaining_salary_confirmed": False,
    "fx_salary_confirmed": False,
    "next_salary_reduced": True,
    "temporary_pay": True,
    "payday_moved": True,
    "income_ended": True,
    "one_off_extra": False,
}

#: (kind, scope, stream words, spared word, pattern). ``spared`` names the one
#: stream a notice says continues; it is excluded from the targets only when
#: exactly one candidate carries it.
INCOME_TEMPLATES: tuple[tuple[str, str, tuple[str, ...], str, re.Pattern[str]], ...] = tuple(
    (kind, scope, words, spared, re.compile(pattern))
    for kind, scope, words, spared, pattern in (
        # T14 salary increase.
        ("salary_increase", FROM_DATE, SALARY_WORDS, "",
         rf"monthly salary has increased to {_AMOUNT}\. The change applies from {_DATE}"),
        ("salary_increase", FROM_DATE, SALARY_WORDS, "",
         rf"Gaji bulanan Anda naik menjadi {_AMOUNT}\. Perubahan ini berlaku mulai {_DATE}"),
        # T08 base salary confirmed (the commission hold is income_holds' job).
        ("base_salary_confirmed", FROM_DATE, ("base",), "",
         rf"confirmed base salary is {_AMOUNT}"),
        ("base_salary_confirmed", FROM_DATE, ("base",), "",
         rf"Gaji pokok yang dikonfirmasi adalah {_AMOUNT}"),
        # T05 next salary reduced (no Indonesian wording exists in the dataset).
        ("next_salary_reduced", NEXT_ONLY, SALARY_WORDS, "",
         rf"next salary is reduced to {_AMOUNT}"),
        # T07 temporary pay for the next payroll.
        ("temporary_pay", NEXT_ONLY, SALARY_WORDS, "",
         rf"temporary monthly pay is {_AMOUNT}\. The reduced amount continues for the next payroll"),
        ("temporary_pay", NEXT_ONLY, SALARY_WORDS, "",
         rf"Gaji bulanan sementara Anda adalah {_AMOUNT}\. Jumlah yang lebih rendah masih berlaku untuk penggajian berikutnya"),
        # T13 regular salary, plus a one-off extra that states no date.
        ("regular_salary_confirmed", FROM_DATE, SALARY_WORDS, "",
         rf"regular salary for the next payroll is {_AMOUNT}"),
        ("regular_salary_confirmed", FROM_DATE, SALARY_WORDS, "",
         rf"Gaji rutin Anda untuk penggajian berikutnya adalah {_AMOUNT}"),
        ("one_off_extra", NEXT_ONLY, SALARY_WORDS, "",
         rf"one-time arrears adjustment of {_AMOUNT}"),
        ("one_off_extra", NEXT_ONLY, SALARY_WORDS, "",
         rf"penyesuaian tunggakan satu kali sebesar {_AMOUNT}"),
        # T22 salary confirmed for a date, converted on that date.
        ("fx_salary_confirmed", NEXT_ONLY, SALARY_WORDS, "",
         rf"Your salary of {_AMOUNT} is confirmed for {_DATE}"),
        ("fx_salary_confirmed", NEXT_ONLY, SALARY_WORDS, "",
         rf"Gaji sebesar {_AMOUNT} dikonfirmasi untuk {_DATE}"),
        ("fx_salary_confirmed", NEXT_ONLY, SALARY_WORDS, "",
         rf"employer has confirmed a {_AMOUNT} salary credit for {_WORDY_DATE}"),
        # T09 payday moved.
        ("payday_moved", NEXT_ONLY, SALARY_WORDS, "",
         rf"confirmed salary is now expected on {_DATE}"),
        ("payday_moved", NEXT_ONLY, SALARY_WORDS, "",
         rf"Gaji yang sudah dikonfirmasi kini diperkirakan masuk pada {_DATE}"),
        # T17 seasonal contract ended.
        ("income_ended", FROM_DATE, SEASONAL_WORDS, "",
         r"current seasonal contract has ended"),
        ("income_ended", FROM_DATE, SEASONAL_WORDS, "",
         r"Kontrak musiman saat ini telah berakhir"),
        # T24 employment ended.
        ("income_ended", FROM_DATE, SALARY_WORDS, "",
         r"Your employment has ended"),
        ("income_ended", FROM_DATE, SALARY_WORDS, "",
         r"Hubungan kerja Anda telah berakhir"),
        # T20 one household income ended; the salary that remains is stated.
        ("income_ended", FROM_DATE, HOUSEHOLD_WORDS, "salary",
         r"One household employment record has ended"),
        ("income_ended", FROM_DATE, HOUSEHOLD_WORDS, "salary",
         r"Salah satu sumber pendapatan kerja rumah tangga telah berakhir"),
        ("remaining_salary_confirmed", FROM_DATE, ("salary",), "",
         rf"remaining confirmed monthly salary is {_AMOUNT}"),
        ("remaining_salary_confirmed", FROM_DATE, ("salary",), "",
         rf"Sisa gaji bulanan yang dikonfirmasi adalah {_AMOUNT}"),
        # T01 first salary with a confirmed credit date.
        ("first_income", NEXT_ONLY, SALARY_WORDS, "",
         rf"Your first salary will be {_AMOUNT}\. The confirmed credit date is {_DATE}"),
        ("first_income", NEXT_ONLY, SALARY_WORDS, "",
         rf"Gaji pertama Anda sebesar {_AMOUNT}\. Tanggal kredit yang dikonfirmasi adalah {_DATE}"),
        # T02 first salary from a new employer.
        ("first_income", NEXT_ONLY, SALARY_WORDS, "",
         rf"Your first salary from the new employer is {_AMOUNT}\. It is confirmed for {_DATE}"),
        ("first_income", NEXT_ONLY, SALARY_WORDS, "",
         rf"Gaji pertama dari perusahaan baru adalah {_AMOUNT}\. Pembayaran sudah dikonfirmasi untuk {_DATE}"),
        # T03 first salary scheduled.
        ("first_income", NEXT_ONLY, SALARY_WORDS, "",
         rf"Your first salary of {_AMOUNT} is scheduled for {_DATE}"),
        ("first_income", NEXT_ONLY, SALARY_WORDS, "",
         rf"Gaji pertama Anda sebesar {_AMOUNT} dijadwalkan pada {_DATE}"),
        # T06 salary resumes (no Indonesian wording exists in the dataset). The
        # childcare sentence in the same notice states no amount.
        ("income_resumes", NEXT_ONLY, SALARY_WORDS, "",
         rf"Regular salary of {_AMOUNT} resumes on {_DATE}"),
        ("unpriced_commitment", NEXT_ONLY, (), "",
         r"A new recurring childcare payment begins in the same month"),
    )
)

#: Facts that book one dated salary credit for the user's salary income as a
#: whole, rather than adjusting a projected stream.
DATED_CREDIT_KINDS = frozenset({"first_income", "income_resumes"})


@dataclass(frozen=True)
class IncomeFact:
    """One income fact a notice states, not yet tied to a projected stream."""

    kind: str
    message_id: str
    sent_at: date
    scope: str
    stream_words: tuple[str, ...]
    #: The stream description of the income event the message links to, if any.
    related_stream: str = ""
    spared_word: str = ""
    amount: Decimal | None = None
    currency: str | None = None
    effective_date: date | None = None


def _fact_date(match: re.Match[str]) -> date | None:
    groups = match.groupdict()
    if groups.get("date"):
        return date.fromisoformat(groups["date"])
    if groups.get("wordy_date"):
        return datetime.strptime(groups["wordy_date"], "%d %B %Y").date()
    return None


def parse_income_facts(text: str, *, message_id: str, sent_at: date, related_stream: str = "") -> list[IncomeFact]:
    """Every template fact sentence in ``text``, in template order."""
    text = text.replace("’", "'")
    facts = []
    for kind, scope, words, spared, pattern in INCOME_TEMPLATES:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            facts.append(
                IncomeFact(
                    kind=kind,
                    message_id=message_id,
                    sent_at=sent_at,
                    scope=scope,
                    stream_words=words,
                    related_stream=related_stream,
                    spared_word=spared,
                    amount=Decimal(groups["amount"].replace(",", "")) if groups.get("amount") else None,
                    currency=groups.get("currency"),
                    effective_date=_fact_date(match),
                )
            )
    return facts


def income_facts(dataset, user_id: str, *, as_of: date, request=None) -> list[IncomeFact]:
    """Income facts from messages that apply to this decision, oldest first.

    A message applies when it is this user's, it had arrived by ``as_of``, and
    it is addressed to no request or to this one.
    """
    facts: list[IncomeFact] = []
    messages = sorted(
        dataset.messages_by_user.get(user_id, ()),
        key=lambda message: (message.sent_at, message.message_id),
    )
    for message in messages:
        if message.sent_at.date() > as_of:
            continue
        if message.request_id and (request is None or message.request_id != request.request_id):
            continue
        event = dataset.event_by_id.get(message.related_event_id or "")
        related = (
            event.description
            if event is not None
            and event.user_id == user_id
            and event.direction == "credit"
            and event.event_type in INCOME_EVENT_TYPES
            else ""
        )
        facts += parse_income_facts(
            message.message_text,
            message_id=message.message_id,
            sent_at=message.sent_at.date(),
            related_stream=related,
        )
    return facts


def fact_targets(fact: IncomeFact, streams: Mapping[str, str]) -> tuple[tuple[str, ...], str | None]:
    """The projected streams a fact applies to, and a note when it is not clear-cut.

    ``streams`` maps each projected series label to the text that names it
    (its description, or every description inside a merged series). A linked
    income event names its stream outright; otherwise the fact's stream words
    pick candidates. One candidate is the target. Several take the safer
    reading for the fact's kind; none changes nothing.
    """
    if fact.related_stream:
        matches = [label for label, text in streams.items() if fact.related_stream in text]
    else:
        matches = [
            label
            for label, text in streams.items()
            if any(word in text.lower() for word in fact.stream_words)
        ]
        spared = [label for label in matches if fact.spared_word and fact.spared_word in streams[label].lower()]
        if len(spared) == 1 and len(matches) > 1:
            matches.remove(spared[0])
    if not matches:
        return (), f"{fact.message_id}: {fact.kind} matches no projected income stream; nothing changed"
    if len(matches) == 1:
        return (matches[0],), None
    names = ", ".join(sorted(matches))
    if APPLY_TO_ALL_WHEN_AMBIGUOUS[fact.kind]:
        return tuple(sorted(matches)), f"{fact.message_id}: {fact.kind} could mean {names}; applied to all (safer)"
    return (), f"{fact.message_id}: {fact.kind} could mean {names}; applied to none (safer)"

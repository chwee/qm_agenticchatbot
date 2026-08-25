"""WhatsApp Intent Router — free-text classification.

Every inbound message passes through here before being dispatched to a module.
There is no command parsing (Requirement 7): a message is classified as free
text regardless of whether it begins with `/` — nothing special-cases it.

classify_free_text() uses the Router Agent (LLM) to map natural language to
a module (A / B / C).
"""
from __future__ import annotations

import logging
import re

from .. import context
from .. import repositories as repo
from ..config import settings

log = logging.getLogger(__name__)

# Short replies that plausibly confirm/select a previously offered option
# ("intake 2", "the second one", "go with that", a bare number, ...).
_CONFIRMATION_RE = re.compile(
    r"\b(intake|option|number|choice|go (with|for)|the (first|second|third|\d+)(st|nd|rd|th)?"
    r"\s*(one|option|intake)?|yes|yeah|sure|ok(ay)?|sounds good|let'?s do (it|that))\b",
    re.IGNORECASE,
)

# A message that names enrollment explicitly, or names a specific course/
# schedule code, carries its own enrollment signal regardless of what the
# assistant said before it — distinct from a bare confirmation ("yes",
# "intake 2") that only means ENROLLMENT in light of a prior specific offer.
_ENROLLMENT_KEYWORD_RE = re.compile(
    r"\b(enrol\w*|enroll\w*|sign\s*up|register|join\s+the\s+course|attend\w*)\b", re.IGNORECASE
)
_CODE_RE = re.compile(r"\b([Cc]\d{4}|[Ss][Hh]\d{4})\b")

# Shared with _heuristic()'s inline payment-keyword check below — factored
# out so _explicit_module_signal() (Tier 0's override carve-out) doesn't
# duplicate the keyword list. "pay" alone (not just "paid"/"payment") added
# after a live report: "how to pay the course" matched none of the original
# words and fell through to ENQUIRY (Module A) instead of Module C, on both
# this deterministic path and the LLM Router Agent's own PAYMENT definition
# below, which had the identical narrow-definition gap.
_PAYMENT_KEYWORD_RE = re.compile(
    r"\b(pay|paid|payment|transferred|receipt|paynow)\b", re.IGNORECASE
)

# A message that's ENTIRELY just an email address — the shape of a reply
# continuing Module A's reminder email-ask (Requirement 4 AC10-AC11), never
# a payment claim. Guards _looks_like_payment_intent() below: an address
# like "pay@mycompany.com" contains "pay" as a whole word (surrounded by
# non-word chars "@"/"."), so _PAYMENT_KEYWORD_RE alone would misread a
# plain email-continuation reply as payment intent and wrongly pull it out
# of Module A's reminder flow — the exact risk ROUTER_REDESIGN.md flags
# ("email reply continuing a reminder flow must stay in Module A, not fall
# through to Module C on keyword match"), confirmed live before this guard
# was added. A payment claim WITH other text ("I paid, my email is
# pay@x.com") is unaffected — only a bare, email-only message is exempted.
_BARE_EMAIL_RE = re.compile(r"^\s*[\w.+-]+@[\w-]+\.[\w.-]+\s*$")


def _has_explicit_enrollment_signal(text: str) -> bool:
    """True if the message itself — independent of conversation history —
    states enrollment intent (a keyword, or a course/schedule code), as
    opposed to being a bare confirmation that only makes sense in light of
    what the assistant just offered."""
    return bool(_ENROLLMENT_KEYWORD_RE.search(text or "") or _CODE_RE.search(text or ""))


def _looks_like_payment_intent(body: str) -> bool:
    """True if the message itself states payment intent — the single
    canonical check for this, used everywhere a raw _PAYMENT_KEYWORD_RE
    match against a PARTICIPANT's own message would otherwise be read as
    payment intent (Tier 0's override carve-out, the deterministic
    ENQUIRY/PAYMENT split, and the reminder-in-progress exemptions below).
    See _BARE_EMAIL_RE's docstring for why a bare email reply is excluded."""
    body = body or ""
    if _BARE_EMAIL_RE.match(body):
        return False
    return bool(_PAYMENT_KEYWORD_RE.search(body))


def _explicit_module_signal(body: str) -> str | None:
    """The module this message unambiguously states intent for, independent
    of any pending flow — None if it's ambiguous/context-dependent. Used
    only as Tier 0's override carve-out (_sticky_dispatch()): an explicit
    signal for a DIFFERENT module than the one currently pending still wins,
    mirroring the precedent already established for AC5/AC7/AC8 below."""
    if _has_explicit_enrollment_signal(body):
        return "B"
    if _looks_like_payment_intent(body):
        return "C"
    return None


__all__ = ["classify_free_text", "route", "DISAMBIGUATION_MENU"]

# Requirement 3 AC13 — shown when classify_free_text() returns "DISAMBIGUATE"
# (a genuinely low-confidence classification with no deterministic signal to
# resolve it). Kept short/numbered for an easy WhatsApp reply; orchestrator.py
# sends this verbatim and records the pending flow (see _sticky_dispatch()'s
# "ROUTER" branch for how the reply is matched back).
DISAMBIGUATION_MENU = (
    "I want to make sure I help with the right thing — could you clarify what you need?\n\n"
    "1) Course info\n2) Enrol / change an enrolment\n3) Payment"
)


def _parse_disambiguation_reply(body: str) -> str | None:
    """Resolve a reply to DISAMBIGUATION_MENU to a module — a bare 1/2/3, or
    a keyword naming the option, in either order. None if it doesn't match
    any option (the caller falls through to normal Tier 1 classification in
    that case, rather than re-showing the menu)."""
    t = (body or "").strip().lower()
    if t == "1" or re.search(r"\b(course|enquir|info)\w*\b", t):
        return "A"
    if t == "2" or re.search(r"\b(enrol|enroll|regist|change)\w*\b", t):
        return "B"
    if t == "3" or re.search(r"\bpay\w*\b", t):
        return "C"
    return None


def _recent_history() -> list[dict]:
    """Best-effort fetch of the last few chat turns for the current sender."""
    try:
        wid = context.whatsapp_id()
    except Exception:  # noqa: BLE001
        return []
    if not wid:
        return []
    try:
        return repo.recent_memory(wid, limit=6)
    except Exception:  # noqa: BLE001
        return []


def _last_assistant_offered_enrollment(history: list[dict]) -> bool:
    """True if the most recent assistant turn presented enroll/intake options."""
    for turn in reversed(history):
        if turn.get("role") == "assistant":
            t = (turn.get("content") or "").lower()
            return any(k in t for k in ("intake", "sh20", "schedule"))
    return False


def _reminder_in_progress(body: str, history: list[dict]) -> bool:
    """True if a reminder exchange is genuinely in play this turn — bot-
    offered (the offer, an email-ask, a which-course-ask, a which-intake-ask)
    OR participant-initiated ("remind me about..." out of the blue), and
    either just now or a turn or two back. Delegates entirely to module_a's
    own _recent_reminder_intent() rather than re-implementing the same
    detection here — the two used to be two separate, narrower checks (one
    scanning only the assistant's recent turns for a reply-to-an-offer, one
    scanning only the participant's brand-new message for fresh intent) and
    each still missed a real case the other didn't anticipate, most
    recently: a bare email-only reply continuing a PARTICIPANT-initiated
    request from a turn or two back contains no "remind" wording anywhere
    in the assistant's own recent turns OR in this message itself — only in
    the participant's OWN earlier message, which neither of the two old
    checks ever looked at (observed live: classified ENROLLMENT and tripped
    the registration gate on a participant who'd never expressed any
    enrollment intent at all). module_a._recent_reminder_intent() already
    covers exactly this by construction; one shared source of truth avoids
    the two files drifting out of sync about what counts.

    Also doubles as the distinction between Module A's reminder flow and
    Module B's own, superficially similar enrolment intake question ("which
    intake would you like to register for?") — that question never says
    "remind", so it correctly does NOT match here (confirmed live: an
    earlier version of this check keyed on "intake" instead, and briefly
    hijacked a genuine reply to Module B's own intake question away from
    it).

    Lazy-imported: module_a.py doesn't import this module, so this is safe,
    but importing at call time rather than module load time avoids any
    ordering assumption between the two."""
    from .module_a import _recent_reminder_intent

    return _recent_reminder_intent(body, history)


def _heuristic(body: str, history: list[dict] | None = None) -> str:
    t = (body or "").lower()
    # A reminder exchange in progress — bot-offered (a reply to the offer,
    # an email-ask, a which-course-ask, a which-intake-ask) OR participant-
    # initiated ("remind me about..." out of the blue, even a turn or two
    # back) — stays with Module A regardless of this reply's own shape. See
    # _reminder_in_progress() for the full detection logic and the history
    # of narrower checks that each missed a real case this one covers.
    # Also yields to a genuine payment claim ("I already paid...") arriving
    # mid-reminder — found live: this override had no exemption for payment
    # at all, so "I already paid for my other course, can you check" was
    # forced back to Module A instead of Module C. _looks_like_payment_intent()
    # (not the raw keyword match) is what still lets a bare email reply
    # continuing the reminder's own email-ask stay in Module A.
    if (
        _reminder_in_progress(body or "", history or [])
        and not _has_explicit_enrollment_signal(t)
        and not _looks_like_payment_intent(body or "")
    ):
        return "A"
    if _has_explicit_enrollment_signal(t):
        return "B"
    if _looks_like_payment_intent(body or ""):
        return "C"
    # Short confirmation/selection replies continuing a just-offered enrollment
    # choice ("intake 2", "the second one", "yes") belong to Module B too.
    if history and _CONFIRMATION_RE.search(t) and _last_assistant_offered_enrollment(history):
        return "B"
    return "A"


def classify_free_text(body: str, has_media: bool) -> str:
    """Classify free text into module A / B / C."""
    if has_media:
        return "C"
    history = _recent_history()
    if not settings.openai_api_key:
        return _heuristic(body, history)
    try:
        from ._base import kickoff_agent

        convo = "\n".join(f"{h['role']}: {h['content']}" for h in history) or "(no prior messages)"
        out = kickoff_agent(
            role="WhatsApp Intent Router",
            goal="Classify each inbound WhatsApp message into exactly one intent module.",
            backstory=(
                "You are the entry-point router for Q&M Dental Group's training WhatsApp. "
                "Route to:\n"
                "  ENQUIRY    = questions about courses, fees, funding, eligibility, schedules.\n"
                "  ENROLLMENT = wanting to enroll, checking status, getting invoice — including "
                "short replies that CONFIRM or SELECT an intake/course the assistant just offered "
                "(e.g. 'intake 2', 'the second one', 'yes let's do that', a bare number), even if "
                "the word 'enroll' is not used. This also covers any question about the "
                "participant's OWN enrollment/registration record, even when it starts with the "
                "words 'what courses...' — e.g. 'what courses am I enrolled in', 'what did I "
                "register for', 'which courses have I signed up for' are ENROLLMENT (personal "
                "record), NOT ENQUIRY, even though the superficially similar 'what courses do you "
                "offer' (the catalogue) IS ENQUIRY. The distinguishing signal is a first-person "
                "possessive/reflexive about a past action — 'I enrolled', 'I registered', 'I "
                "signed up', 'my enrollments', 'my status' — vs. asking about the catalogue in "
                "general.\n"
                "  PAYMENT    = submitting payment proof, asking about receipts, asking how or "
                "where to pay, payment methods, or an outstanding balance — any question about "
                "the act of paying, not just proof already submitted.\n\n"
                "There is no command syntax to recognise — classify every message, including "
                "one that happens to start with '/', purely on its natural-language content."
            ),
            tools=[],
            task_description=(
                "Recent conversation with this participant (oldest first):\n"
                f"{convo}\n\n"
                f"Classify the participant's NEW message into exactly one label: "
                f"ENQUIRY, ENROLLMENT, or PAYMENT.\n\n"
                f"New message: \"{body}\"\n\n"
                "If the assistant's last message offered course intakes or an enroll option and "
                "this new message is selecting/confirming one, classify as ENROLLMENT.\n\n"
                "Also report your confidence: HIGH if the message clearly and unambiguously fits "
                "one category, MEDIUM if it's a reasonable guess but could plausibly be read another "
                "way, LOW if it's genuinely ambiguous or you're largely guessing.\n\n"
                "Respond with EXACTLY two lines, nothing else:\n"
                "LABEL: <ENQUIRY|ENROLLMENT|PAYMENT>\n"
                "CONFIDENCE: <HIGH|MEDIUM|LOW>"
            ),
            expected_output="Two lines: 'LABEL: <word>' and 'CONFIDENCE: <word>'",
            max_iter=2,
        ).strip()
        # Requirement 3 AC13: parse both the label and a confidence level.
        # Falls back to the pre-v1.28 substring-match style (and defaults
        # confidence to HIGH, i.e. no disambiguation) if the model doesn't
        # follow the two-line format — this can never make classification
        # LESS reliable than before this change, only add a new path on top.
        label_m = re.search(r"LABEL:\s*(ENQUIRY|ENROLLMENT|PAYMENT)", out, re.IGNORECASE)
        conf_m = re.search(r"CONFIDENCE:\s*(HIGH|MEDIUM|LOW)", out, re.IGNORECASE)
        label = label_m.group(1).upper() if label_m else out.upper()
        confidence = conf_m.group(1).upper() if conf_m else "HIGH"
        if "ENROLL" in label:
            module = "B"
        elif "PAY" in label:
            module = "C"
        else:
            module = "A"
        override_fired = False

        # Deterministic cross-check (Requirement 3 AC5): the LLM classifier
        # is instructed to only treat a bare confirmation-style reply as
        # ENROLLMENT when the assistant's own immediately preceding message
        # actually offered a specific course/intake — but prompt wording
        # alone doesn't reliably hold it to that (observed: "yes" in reply
        # to a generic "are you interested in our courses?" opener still got
        # classified ENROLLMENT, opening the registration gate on vague
        # interest rather than genuine commitment). Downgrade back to
        # ENQUIRY whenever the message is a bare confirmation with no
        # enrollment signal of its own AND there was no actual prior offer
        # to confirm — the same deterministic check _heuristic() already
        # applies in the no-API-key fallback, now applied here too.
        if (
            module == "B"
            and _CONFIRMATION_RE.search(body or "")
            and not _has_explicit_enrollment_signal(body or "")
            and not _last_assistant_offered_enrollment(history)
        ):
            log.info(
                "Router: downgrading bare confirmation from ENROLLMENT to ENQUIRY "
                "(no prior specific course/intake offer): %r", body,
            )
            module = "A"
            override_fired = True

        # Deterministic cross-check: any reply following a reminder-related
        # assistant turn, OR the participant's own message (now or a turn or
        # two back), must stay with Module A regardless of what the LLM
        # classifier decided — observed live under four different failure
        # shapes: a bare email address (classified PAYMENT and sent to
        # Module C, which had nothing to do with it); a bare date/ordinal
        # answering "which intake?" (classified ENROLLMENT, producing a full
        # enrolment confirmation for a course never asked for); a course
        # name/position answering "which course?" (also classified
        # ENROLLMENT); and a bare email-only reply CONTINUING a
        # participant-initiated request from a turn or two back, where
        # "remind" appears nowhere in the assistant's recent turns or in
        # this message itself, only in the participant's own earlier one
        # (classified ENROLLMENT, tripping the registration gate on someone
        # who'd never expressed enrollment intent at all). Originally two
        # separate overrides here (one scanning only the assistant's recent
        # turns, one scanning only this message) — merged into the single
        # _reminder_in_progress() check once the email-only case showed
        # neither alone was sufficient; see its own docstring. Without this
        # override the whole point of module_a.py's directive mechanism is
        # moot, since that logic never runs if the turn is routed elsewhere
        # first.
        #
        # Also yields to a genuine payment claim, same as _heuristic() —
        # found live: "I already paid for my other course, can you check"
        # arriving mid-reminder was forced back to Module A, which had no
        # way to act on it. _looks_like_payment_intent() (not a raw keyword
        # match) still keeps a bare email reply continuing the reminder's
        # own email-ask in Module A, per ROUTER_REDESIGN.md's own flagged
        # risk for this override.
        if (
            module != "A"
            and _reminder_in_progress(body or "", history)
            and not _has_explicit_enrollment_signal(body or "")
            and not _looks_like_payment_intent(body or "")
        ):
            log.info(
                "Router: overriding %s to ENQUIRY (reminder exchange in progress): %r",
                module, body,
            )
            module = "A"
            override_fired = True

        # Requirement 3 AC13: a genuinely low-confidence classification that
        # no deterministic signal resolved gets a disambiguation menu instead
        # of a guess — see _sticky_dispatch()'s "ROUTER" branch for how the
        # participant's next reply is matched back against it.
        if (
            confidence == "LOW"
            and not override_fired
            and not _has_explicit_enrollment_signal(body or "")
            and not _looks_like_payment_intent(body or "")
        ):
            log.info(
                "Router: low-confidence classification (label=%s) — disambiguating: %r",
                label, body,
            )
            return "DISAMBIGUATE"

        return module
    except Exception as e:  # noqa: BLE001
        log.warning("router agent failed, using heuristic: %s", e)
        return _heuristic(body, history)


def _sticky_dispatch(body: str) -> str | None:
    """Tier 0 (Requirement 3 AC10-AC12): if this participant has an
    unexpired pending flow recorded by whichever module last left a
    question open (Module A's reminder stage, Module B's intake-selection/
    confirmation stage), dispatch straight back to it — no LLM call, no
    keyword matching. None if there's nothing pending, or the new message
    carries its own explicit, unambiguous signal for a DIFFERENT module
    (_explicit_module_signal()), in which case Tier 1 (classify_free_text())
    decides normally instead — the same "explicit signal always wins"
    precedent already established for AC5/AC7/AC8 below.

    Replaces the CLASS of bug AC5/AC7/AC8's ~8 rounds of point-patches were
    each separately chasing: none of them had any persisted signal for
    "a specific module is already mid-flow with this participant" — they
    all re-derived it from raw recent chat text every turn, which is why a
    stale, unrelated keyword mention a turn or two back could misfire. See
    design_qm.md Component 3 (v1.26) for the full incident this was built
    from (a numbered intake-list reply diverted to Module A because an
    older, already-resolved reminder confirmation still said "remind")."""
    try:
        wid = context.whatsapp_id()
    except Exception:  # noqa: BLE001
        return None
    if not wid:
        return None
    try:
        state = repo.get_conversation_state(wid)
    except Exception as e:  # noqa: BLE001
        log.warning("conversation_state lookup failed, skipping Tier 0: %s", e)
        return None
    if not state:
        return None
    owner = state["active_module"]

    # Requirement 3 AC13: "ROUTER" isn't a real module — it means the last
    # turn was a disambiguation menu (classify_free_text() returned
    # DISAMBIGUATE). Resolve the participant's reply against it deterministically
    # rather than re-running Tier 1 on a message the LLM just told us it
    # couldn't classify. Either way the flow is cleared: a resolved answer
    # dispatches once, and an unparseable one falls through to normal
    # classification rather than repeating the menu forever.
    if owner == "ROUTER" and state.get("active_flow") == "awaiting_disambiguation":
        repo.clear_conversation_state_if_owner(wid, "ROUTER")
        resolved = _parse_disambiguation_reply(body or "")
        if resolved:
            log.info("Router: disambiguation resolved to %s: %r", resolved, body)
            return resolved
        return None

    override = _explicit_module_signal(body or "")
    if override and override != owner:
        return None
    log.info(
        "Router: Tier 0 sticky dispatch to %s (flow=%s): %r",
        owner, state.get("active_flow"), body,
    )
    return owner


def route(payload: dict) -> dict:
    """Return the routing decision for an inbound message payload.

    No command parsing (Requirement 7) — every message, `/`-prefixed or not,
    is classified as free text. An image attachment still routes straight to
    Module C without a model call (Requirement 3 AC2); everything else tries
    Tier 0 sticky dispatch first, then falls back to classify_free_text().
    """
    body     = (payload.get("message") or {}).get("body", "")
    has_media = bool(payload.get("media"))

    if has_media:
        module = "C"
    else:
        module = _sticky_dispatch(body) or classify_free_text(body, has_media)
    return {
        "module":    module,
        "arg":       body,
        "has_media": has_media,
    }

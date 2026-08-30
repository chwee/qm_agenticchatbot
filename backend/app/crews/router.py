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
from dataclasses import dataclass, field

from .. import context
from .. import repositories as repo
from ..config import settings

log = logging.getLogger(__name__)


# ── Routing trace (DIALOGUE_STATE_REDESIGN.md phase 0) ──────────────────────
# Records HOW a routing decision was reached, not just its outcome. Purely
# observational — nothing here influences control flow.
#
# Why this exists: Tier 0's sticky dispatch has always logged its decision via
# log.info(), but the query trace file (backend/logs/) is written by
# query_log.emit(). So the trace every debugging session actually reads showed
# an identical "free text → Module B" whether the decision came from the
# sticky dispatcher, the LLM classifier, or the no-API-key heuristic — three
# very different paths, indistinguishable in the artefact used to tell them
# apart. MEMORY_CONTEXT_REDESIGN.md grepped those logs for "Tier 0", found
# nothing, and concluded the path might not run at all; it runs, it was just
# never written there.
#
# Every later phase of the redesign is gated on measuring misroute rates from
# these logs, so this has to land before any of them.
@dataclass
class RouteTrace:
    """One turn's routing provenance. Rendered into the query log by
    orchestrator._print_routing()."""

    tier: str = ""                      # media | sticky | disambiguation | llm | heuristic
    pending_module: str | None = None    # conversation_state as it was AT ENTRY,
    pending_flow: str | None = None      # i.e. before this turn touched it
    llm_label: str | None = None         # what the classifier actually returned,
    llm_confidence: str | None = None    # before any deterministic override
    overrides: list[str] = field(default_factory=list)
    final: str = ""

    def note_override(self, what: str, before: str, after: str) -> None:
        self.overrides.append(f"{what} ({before} → {after})")

    _TIER_LABELS = {
        "media":          "media attachment → Module C, no model call",
        "sticky":         "Tier 0 sticky dispatch (pending flow)",
        "disambiguation": "Tier 0 disambiguation-menu reply",
        "llm":            "Tier 1 LLM classifier",
        "heuristic":      "Tier 1 heuristic fallback (no API key, or agent failed)",
    }

    def render(self) -> list[str]:
        """Trace lines (unprefixed) describing this decision, most important
        first. Only non-empty facts are emitted, so a simple turn stays short."""
        lines = [f"tier       : {self._TIER_LABELS.get(self.tier, self.tier or '?')}"]
        pending = (
            f"{self.pending_module}/{self.pending_flow}"
            if self.pending_module else "none (idle)"
        )
        lines.append(f"pending    : {pending}")
        if self.llm_label:
            conf = self.llm_confidence or "?"
            lines.append(f"classifier : {self.llm_label} (confidence {conf})")
        for o in self.overrides:
            lines.append(f"override   : {o}")
        if self.final:
            lines.append(f"decision   : {self.final}")
        return lines

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


def _restates_pending_course(body: str, state: dict | None) -> bool:
    """True if EVERY course/schedule code named in `body` is already part of
    the pending question — its course_code, schedule_code, or one of its
    candidates (DIALOGUE_STATE_REDESIGN.md phase 1's flow_context, phase 3's
    first real read of it for a routing decision, not just observational
    logging) — i.e. the participant is restating or confirming what's
    already open, not naming something new. False whenever there's no
    flow_context to compare against (no pending flow, or a module that
    hasn't started dual-writing it), which keeps the old blunt "any code at
    all is a signal" behaviour as the fallback exactly where nothing better
    is available yet.

    Why this exists: _CODE_RE alone can't tell "C2601" meant as an answer
    (repeating back the very course already under discussion) from "C2601"
    meant as a genuine override — a bare course-code mention pending a
    Module A reminder ask about that SAME course was, before this, being
    read as an unconditional enrollment signal and forced out to Tier 1's
    topic classifier every time, even though nothing about the turn had
    actually changed topic."""
    if not state:
        return False
    flow_context = state.get("flow_context") or {}
    known = {c.upper() for c in (flow_context.get("candidates") or ()) if c}
    for key in ("course_code", "schedule_code"):
        val = flow_context.get(key)
        if val:
            known.add(val.upper())
    if not known:
        return False
    mentioned = {c.upper() for c in _CODE_RE.findall(body or "")}
    return bool(mentioned) and mentioned <= known


def _explicit_module_signal(body: str, state: dict | None = None) -> str | None:
    """The module this message unambiguously states intent for, independent
    of any pending flow — None if it's ambiguous/context-dependent. Used
    only as Tier 0's override carve-out (_sticky_dispatch()): an explicit
    signal for a DIFFERENT module than the one currently pending still wins,
    mirroring the precedent already established for AC5/AC7/AC8 below.

    `state` (phase 3): the pending conversation_state row, if any — passed
    through to _restates_pending_course() so a course/schedule code that
    merely repeats what's already pending doesn't count as a signal on its
    own. An enrollment KEYWORD ('enrol', 'sign up'...) is untouched by this
    and always signals "B" regardless — restating a code and stating fresh
    intent are different things."""
    if _ENROLLMENT_KEYWORD_RE.search(body or ""):
        return "B"
    if _CODE_RE.search(body or "") and not _restates_pending_course(body, state):
        return "B"
    if _looks_like_payment_intent(body):
        return "C"
    return None


__all__ = ["classify_free_text", "route", "DISAMBIGUATION_MENU", "RouteTrace"]

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


# DIALOGUE_STATE_REDESIGN.md phase 2.1 — a fixed, five-item vocabulary
# summarising what the assistant's last turn DID, never its actual wording.
# Deliberately narrow: this is not the same move as phase 2 reversed. Phase
# 2's contamination problem was dense, unpredictable, dozens-of-tokens-long
# free prose bleeding "enrol"/"payment"/"invoice"/"intake" into the
# classifier at high, unpredictable token mass. This is one label from a
# closed set, with no course names, fees, codes, or dates in it — measured
# to matter: replaying 9 historical misroutes through the phase-2-only
# classifier only fixed 5 (56%) — the 4 failures were all short, low-context
# messages ("Ya, how about course 2") where the participant-only window
# genuinely has nothing to ground "course 2" against once every assistant
# signal is gone, so the model reasonably reads it as fresh enrollment
# intent instead of a reference to a list it can no longer see happened.
_ASSISTANT_ACTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"available courses|\[C\d{4}\]", re.IGNORECASE), "listed_courses"),
    (re.compile(r"upcoming intakes|\[SH\d{4}\]", re.IGNORECASE), "showed_intake_options"),
    (re.compile(r"shall i go ahead and (enrol|cancel)", re.IGNORECASE), "asked_to_confirm_enrolment_or_cancellation"),
    (re.compile(r"which intake date should i remind", re.IGNORECASE), "asked_which_intake_for_reminder"),
    (re.compile(r"invoice|paynow|skillsfuture claim", re.IGNORECASE), "discussed_payment_or_invoice"),
]


def _assistant_last_action(history: list[dict]) -> str:
    """The fixed-vocabulary label for the most recent assistant turn, or
    "none" if there wasn't one. Falls back to the generic "replied" when a
    turn exists but matches none of the patterns above — never invents a
    label outside the closed set, and never returns any of the turn's own
    text."""
    for turn in reversed(history):
        if turn.get("role") == "assistant":
            content = turn.get("content") or ""
            for pattern, label in _ASSISTANT_ACTION_PATTERNS:
                if pattern.search(content):
                    return label
            return "replied"
    return "none"


def _recent_history() -> list[dict]:
    """Best-effort fetch of the last few chat turns for the current sender,
    scoped to their current episode (DIALOGUE_STATE_REDESIGN.md phase 2) —
    a 60-minute idle gap starts a new one, so this can no longer silently
    span across days the way a bare created_at-DESC LIMIT did.

    Deliberately still BOTH roles, not participant-only: this return value
    also feeds _last_assistant_offered_enrollment() and
    _reminder_in_progress() below, which need real assistant turns to
    check. Only classify_free_text()'s own LLM prompt strips assistant text
    out of it — see that function's `participant_turns` for why (root cause
    #1: the classifier's own window used to be overwhelmingly the bot
    talking to itself)."""
    try:
        wid = context.whatsapp_id()
    except Exception:  # noqa: BLE001
        return []
    if not wid:
        return []
    try:
        session_id = repo.current_session_id(wid)
        return repo.recent_memory(wid, limit=6, session_id=session_id)
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


def classify_free_text(body: str, has_media: bool, trace: RouteTrace | None = None) -> str:
    """Classify free text into module A / B / C.

    `trace`, if given, is populated with how the decision was reached (phase 0
    instrumentation) — observational only, never consulted for control flow.
    Optional so the public signature stays backward-compatible."""
    trace = trace if trace is not None else RouteTrace()
    if has_media:
        trace.tier = "media"
        return "C"
    history = _recent_history()
    if not settings.openai_api_key:
        trace.tier = "heuristic"
        return _heuristic(body, history)
    try:
        from ._base import kickoff_agent

        # DIALOGUE_STATE_REDESIGN.md phase 2 (root cause #1): participant
        # turns only, not the assistant's own prior replies. By token mass
        # the old mixed-role window was overwhelmingly the bot talking to
        # itself, and that text is dense with "enrol"/"payment"/"invoice"/
        # "intake" — exactly the tokens this 3-label classifier keys on
        # (the distractor effect: topically-related-but-irrelevant content
        # degrades classification far more than unrelated filler). Every
        # deterministic override below that DOES need assistant text
        # (_last_assistant_offered_enrollment, _reminder_in_progress) reads
        # from `history` directly, untouched by this filter — only the LLM
        # classifier's own visible context is narrowed.
        participant_turns = [h["content"] for h in history if h.get("role") == "user"]
        # phase 2.1: one fixed-label line ahead of the participant turns —
        # see _assistant_last_action()'s own comment for why this exists and
        # why it's a different, much lower-risk move than restoring the
        # assistant's actual reply text.
        convo_lines = [f"assistant_last_action: {_assistant_last_action(history)}"]
        if participant_turns:
            convo_lines += [f"participant: {c}" for c in participant_turns[-3:]]
        else:
            convo_lines.append("(no prior participant messages)")
        convo = "\n".join(convo_lines)
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
                "Context for this participant's conversation (their conversation partner's "
                "actual replies are deliberately not shown to you in full — a separate "
                "deterministic check elsewhere already verifies whether a specific course/intake "
                "offer is genuinely pending). The first line, 'assistant_last_action', is the "
                "ONLY thing you're told about what their conversation partner just did, "
                "summarised as one fixed label — 'listed_courses' means a catalogue of courses "
                "was just shown, so a bare reference like 'course 2' most likely names an item "
                "from that list rather than stating fresh enrollment intent on its own; "
                "'showed_intake_options' is the same idea for a list of intake dates; 'none' "
                "means this is the opening message of the conversation. The remaining lines are "
                "the participant's own recent messages, oldest first:\n"
                f"{convo}\n\n"
                f"Classify the participant's NEW message into exactly one label: "
                f"ENQUIRY, ENROLLMENT, or PAYMENT.\n\n"
                f"New message: \"{body}\"\n\n"
                "A short reply with no topic of its own (e.g. 'yes', 'intake 2', 'the second "
                "one', a bare number, 'how about course 2') only means ENROLLMENT if it's "
                "plausibly confirming or selecting something a specific offer was made for — if "
                "assistant_last_action is 'listed_courses' or 'none', or you otherwise cannot "
                "tell what it would be confirming, this is almost always still just asking about "
                "the catalogue (ENQUIRY), not committing to enrol; report LOW confidence instead "
                "of guessing ENROLLMENT when genuinely unsure — a deterministic check elsewhere "
                "resolves the true edge cases correctly using the full conversation, not just "
                "what's shown here.\n\n"
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
        trace.tier = "llm"
        # Truncated: an unparseable `out` falls back to the whole raw reply as
        # `label`, which can be a paragraph — the trace wants the signal, not
        # the essay.
        trace.llm_label = label if label_m else f"UNPARSED:{label[:40]}"
        trace.llm_confidence = confidence
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
            trace.note_override("bare confirmation, no prior offer", module, "A")
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
            trace.note_override("reminder exchange in progress", module, "A")
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
            trace.note_override("low confidence, unresolved", module, "DISAMBIGUATE")
            return "DISAMBIGUATE"

        return module
    except Exception as e:  # noqa: BLE001
        log.warning("router agent failed, using heuristic: %s", e)
        trace.tier = "heuristic"
        trace.note_override("classifier raised", type(e).__name__, "heuristic")
        return _heuristic(body, history)


def _sticky_dispatch(body: str, trace: RouteTrace | None = None) -> str | None:
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
    trace = trace if trace is not None else RouteTrace()
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
        trace.note_override("conversation_state lookup failed", type(e).__name__, "Tier 1")
        return None
    if not state:
        return None
    owner = state["active_module"]
    # Recorded even when Tier 0 goes on to decline the turn below — "there WAS
    # a pending flow and it was overridden" is exactly the distinction the old
    # trace could not make.
    trace.pending_module = owner
    trace.pending_flow = state.get("active_flow")

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
            trace.tier = "disambiguation"
            return resolved
        trace.note_override("disambiguation reply unparseable", "ROUTER", "Tier 1")
        return None

    override = _explicit_module_signal(body or "", state)
    if override and override != owner:
        trace.note_override("explicit signal beats pending flow", owner, override)
        return None
    log.info(
        "Router: Tier 0 sticky dispatch to %s (flow=%s): %r",
        owner, state.get("active_flow"), body,
    )
    trace.tier = "sticky"
    return owner


def _record_pending(trace: RouteTrace) -> None:
    """Fill trace.pending_* on a path that never reaches _sticky_dispatch()
    (currently only the media short-circuit), so the trace can still show that
    a flow was open when the money path preempted it. Best-effort: a trace
    detail must never be able to break routing."""
    try:
        wid = context.whatsapp_id()
        state = repo.get_conversation_state(wid) if wid else None
    except Exception:  # noqa: BLE001
        return
    if state:
        trace.pending_module = state.get("active_module")
        trace.pending_flow = state.get("active_flow")


def route(payload: dict) -> dict:
    """Return the routing decision for an inbound message payload.

    No command parsing (Requirement 7) — every message, `/`-prefixed or not,
    is classified as free text. An image attachment still routes straight to
    Module C without a model call (Requirement 3 AC2); everything else tries
    Tier 0 sticky dispatch first, then falls back to classify_free_text().

    The returned dict carries a "trace" (RouteTrace) recording HOW the decision
    was reached — see RouteTrace's own comment. Purely additive: no module
    reads the route dict, and orchestrator.py only ever read "module".
    """
    body     = (payload.get("message") or {}).get("body", "")
    has_media = bool(payload.get("media"))
    trace = RouteTrace()

    if has_media:
        module = "C"
        trace.tier = "media"
        _record_pending(trace)
    else:
        # One trace object through BOTH tiers — a Tier 0 miss must still leave
        # its pending-flow reading visible on the Tier 1 decision that follows.
        module = _sticky_dispatch(body, trace) or classify_free_text(body, has_media, trace)
    trace.final = module
    return {
        "module":    module,
        "arg":       body,
        "has_media": has_media,
        "trace":     trace,
    }

"""Module B — Enrollment, Invoice & Payment Pipeline.

Free-text path (the only path — Requirement 7, slash commands removed)
────────────────────────────────────────────────────────────────────────
_run_agent() passes the registered profile and the numbered course list so
the LLM can:
  • Classify sub-intent: enroll / status / invoice
  • Extract course name or number and optional schedule reference
  • Resolve numbered list references statelessly (re-queries DB each time)
  • Summarise the resolved course + intake and wait for explicit agreement
    before enrolling (Requirement 5 AC2-AC4)

Quick-enrol shortcut (unaffected by command removal — Requirement 5 AC8)
──────────────────────────────────────────────────────────────────────────
_quick_enroll_from_text() detects a C-code and an SH-code appearing together
anywhere in free text and enrols immediately, bypassing the LLM entirely.
It reuses _smart_enroll(), which applies pattern-based field detection:

  Pattern matching (applied regardless of position):
    • [STFG]\\d{7}[A-Za-z]        → NRIC
    • *@*.*                        → email
    • SH{YY}{ID}                   → schedule SH-code (direct DB id)
    • trailing pure-integer        → schedule item number (1-based)
    • leading field (text/C-code)  → course
    • remaining text field         → full name

  After detection, any blank field is filled from the participant's registered
  profile (guaranteed present — the progressive-registration gate in
  orchestrator.py runs before Module B is ever invoked).
"""
from __future__ import annotations

import logging

import re

from .. import context
from .. import repositories as repo
from ..config import settings
from ..services import courses as courses_svc
from ..services import enrollment
from ..tools import MODULE_B_TOOLS
from ..utils import (
    course_short_id,
    schedule_short_id,
    valid_email,
    valid_nric,
)
from . import module_a
from ._base import build_agent as _base_build_agent
from ._base import run_task as _base_run_task

_SH_CODE_RE      = re.compile(r"^[Ss][Hh]\d{4}$")
_C_CODE_IN_TEXT  = re.compile(r"\b([Cc]\d{4})\b")
_SH_CODE_IN_TEXT = re.compile(r"\b([Ss][Hh]\d{4})\b")
# A numbered line naming an SH-code — Step 2's intake-options list shape
# (Requirement 3 AC10). Deliberately narrower than "any SH-code present":
# a completed enrolment confirmation, or a status/invoice answer, can each
# legitimately mention a single SH-code inline without being an open
# "which intake?" list (observed live — see _run_agent()'s conversation_state
# write for the incident this narrowed check exists to avoid).
_INTAKE_LIST_RE = re.compile(r"(?m)^\**\s*\d+\.\**\s+.*[Ss][Hh]\d{4}")
# "cour\w{0,3}" tolerates common misspellings of "course" (coure, cours,
# coures) — the exact reported trigger message had this typo, and the LLM
# resolved it fine, but a strict "course" literal would silently miss it
# here, defeating the whole detector for the very case it was built for.
_COURSE_POSITION_RE = re.compile(r"\bcour\w{0,3}\s*(?:number\s*|no\.?\s*|#\s*)?(\d+)\b", re.IGNORECASE)


def _resolve_course_ref(text: str) -> str | None:
    """Best-effort resolve an explicit course reference (a C-code, or a
    bare 'course N' catalogue-position reference) in `text` to a canonical
    course name — None if no explicit reference is present. Deliberately
    narrow (same philosophy as orchestrator.py's _infer_course_interest()):
    only unambiguous, high-precision signals, not fuzzy name matching."""
    text = text or ""
    m = _C_CODE_IN_TEXT.search(text)
    if m:
        course = repo.get_course_by_course_id(m.group(1).upper())
        if course:
            return course["name"]
    m = _COURSE_POSITION_RE.search(text)
    if m:
        all_courses = repo.list_courses()
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(all_courses):
            return all_courses[idx]["name"]
    return None


def _recent_topic_course(history: list[dict], current_body: str) -> str | None:
    """The most recent explicit course reference from the participant's own
    prior messages (skipping the current message itself) — None if none
    found in recent history."""
    current_body = (current_body or "").strip()
    for turn in reversed(history):
        if turn.get("role") != "user":
            continue
        content = (turn.get("content") or "").strip()
        if content == current_body:
            continue
        ref = _resolve_course_ref(content)
        if ref:
            return ref
    return None


def _course_switch_note(body: str, history: list[dict]) -> str:
    """Reported bug: a participant discussing 'course 4' typed 'course 1' by
    what looks like a typo two turns later, and got enrolled in course 1
    with no acknowledgment of the topic jump — course 1 was itself a fully
    valid reference, so nothing in the existing validation had any basis to
    reject it. This doesn't try to guess which one is 'right' (a genuine
    change of mind is equally possible) — it deterministically detects the
    switch and requires the Step 3 confirmation summary to name both
    courses explicitly, giving the participant one clear chance to catch a
    possible mistake before confirming. Returns "" when the current message
    has no explicit course reference of its own, or it matches the most
    recently discussed course (nothing to flag)."""
    current_ref = _resolve_course_ref(body)
    if not current_ref:
        return ""
    topic_ref = _recent_topic_course(history, body)
    if not topic_ref or topic_ref == current_ref:
        return ""
    return (
        f"\nSYSTEM NOTE (context, not written by the participant — this is informational, it "
        f"does NOT skip any step the way a SYSTEM DIRECTIVE does; use it within your normal "
        f"STEP 1-3 flow below): they were recently discussing '{topic_ref}', but this message "
        f"resolves to a DIFFERENT course, '{current_ref}' — this may be a typo (e.g. the wrong "
        f"course-list number) rather than a deliberate change of mind. If you reach STEP 3 (the "
        f"confirmation summary) for '{current_ref}' this turn, you MUST explicitly mention this "
        f"shift in that summary — e.g. \"You were just asking about {topic_ref} — just to "
        f"confirm, you're enrolling in {current_ref}, intake [...]. Shall I go ahead and enrol "
        f"you?\" — so they get one clear chance to catch a possible mistake before confirming.\n"
    )

# Requirement 5 AC2-AC4: the confirmation summary the agent sends before
# enrolling must end with this exact sentence and carry both codes, so the
# *next* turn can deterministically recognise "this reply answers a pending
# confirmation" instead of relying on the LLM re-reading its own prior
# message from free text (unreliable in testing — see task_qm.md Task 10.4).
_CONFIRM_MARKER = "Shall I go ahead and enrol you?"

_AGREE_RE = re.compile(
    r"\b(yes|yeah|yep|sure|ok(ay)?|confirm|go ahead|proceed|do it|sounds good|let'?s do (it|that))\b",
    re.IGNORECASE,
)
_DECLINE_RE = re.compile(
    r"\b(no|nope|nah|not (yet|now|really|that one)|cancel|different (date|intake|one)|wait|hold on)\b",
    re.IGNORECASE,
)

# ── Stale-context guard ──────────────────────────────────────────────────
# A bare "ok sure" / "yes" with no pending enrolment confirmation and no
# course/intake reference of its own is almost never re-starting enrolment
# — it's answering whatever the assistant's own immediately preceding
# message actually asked (e.g. an offer to save an email for a reminder).
# Without this guard, STEP 1/2 will happily resolve "the" course+intake from
# older history and re-issue a fresh confirmation the participant never
# asked for — harmless in isolation (Enroll Participant still isn't callable
# without a genuine agreed_now), but it sets up a real risk: a follow-up
# "yes" (meant to answer the reminder offer) would then match this newly
# re-issued marker and trigger an enrolment they never re-consented to.
_POSITION_RE = re.compile(
    r"\b(item|list|course|number|no\.?|option|intake)\s*#?\s*\d+\b"
    r"|\b\d+(st|nd|rd|th)?\b"
    r"|\bthe\s+(first|second|third|fourth|fifth)\b",
    re.IGNORECASE,
)
_BARE_REPLY_WORDS = {
    "ok", "okay", "sure", "yes", "yeah", "yep", "no", "nope", "nah",
    "thanks", "thank", "you", "got", "it", "alright", "great", "cool",
    "perfect", "noted", "please", "fine",
}


def _has_own_course_reference(body: str) -> bool:
    """True if `body` names a course/schedule code or a list-position/
    ordinal of its own — something concrete for STEP 1/2 to resolve from
    THIS message, as opposed to a bare reply that only makes sense in light
    of whatever the assistant just asked."""
    body = body or ""
    return bool(_C_CODE_IN_TEXT.search(body) or _SH_CODE_IN_TEXT.search(body) or _POSITION_RE.search(body))


def _is_bare_reply(body: str) -> bool:
    """True if `body` is made up entirely of short acknowledgement words
    ('Ok sure', 'yes please') with nothing else — no course reference, no
    other content of its own."""
    words = re.findall(r"[A-Za-z']+", (body or "").lower())
    return bool(words) and len(words) <= 4 and all(w in _BARE_REPLY_WORDS for w in words)


def _stale_context_directive(
    body: str, history: list[dict], pending: tuple[str, str] | None, pending_cancel: bool = False,
) -> str:
    """See the module-level comment above. Empty when this doesn't apply,
    leaving the agent's own judgement in charge exactly as before this
    existed. `pending_cancel` (Requirement 11 AC5) is the cancellation
    analogue of `pending` — a bare reply answering either kind of open
    confirmation is NOT stale context."""
    if pending or pending_cancel or not _is_bare_reply(body) or _has_own_course_reference(body):
        return ""
    last_assistant = next(
        (t.get("content") or "" for t in reversed(history) if t.get("role") == "assistant"), ""
    )
    return (
        f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): there is no "
        f"pending enrolment confirmation for them to be answering right now. Your own "
        f"immediately preceding message was:\n\"{last_assistant}\"\n"
        f"This reply is answering THAT message, not restarting enrolment. Do NOT resolve a "
        f"course or intake from older conversation history, and do NOT re-summarise or "
        f"re-offer to enrol them — there is nothing here that reopens the ENROLL sequence. If "
        f"your last message offered to save their email/interest for a reminder and this looks "
        f"like agreement, delegate that to your coworker 'Q&M Training Enquiry Assistant' (it "
        f"has the tool to actually save it) rather than claiming you saved something yourself. "
        f"Otherwise, just acknowledge naturally and briefly.\n"
    )


log = logging.getLogger(__name__)


def _pending_confirmation(history: list[dict]) -> tuple[str, str] | None:
    """If the assistant's last message was a confirmation summary awaiting
    agreement, return (course_code, sh_code) extracted from it.

    Returns None if there's no history, the last turn wasn't from the
    assistant, or it doesn't end with the exact confirmation marker — which
    a completed-enrolment message never does, so this can't be confused
    with "already enrolled, participant said something else afterward".
    """
    if not history:
        return None
    last = history[-1]
    if last.get("role") != "assistant":
        return None
    content = (last.get("content") or "").strip()
    # Case-insensitive: if the marker ever lands as a sentence continuation
    # rather than a fresh sentence, the model may lowercase "Shall" — the
    # same failure mode found and fixed in module_a.py's reminder-offer
    # marker (_pending_reminder_offer), applied here defensively too.
    if not content.lower().endswith(_CONFIRM_MARKER.lower()):
        return None
    c_m = _C_CODE_IN_TEXT.search(content)
    sh_m = _SH_CODE_IN_TEXT.search(content)
    if not (c_m and sh_m):
        return None
    return c_m.group(1).upper(), sh_m.group(1).upper()


def _confirmation_directive(body: str, pending: tuple[str, str] | None) -> str:
    """Deterministically classify this turn's reply against a pending
    confirmation (agree / decline / ambiguous) and return a directive string
    to inject into the agent's task_description — empty if there's nothing
    pending, or the reply doesn't clearly go either way (left to the agent's
    own judgement in that case, same as before this existed)."""
    if not pending:
        return ""
    c_code, sh_code = pending
    agree = bool(_AGREE_RE.search(body))
    decline = bool(_DECLINE_RE.search(body))
    if agree and not decline:
        return (
            f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they just "
            f"AGREED to your pending proposal to enrol them in {c_code}, intake {sh_code}. "
            f"Call 'Validate Enrollment' then 'Enroll Participant' now with course='{c_code}' "
            f"and sh_code='{sh_code}' exactly — do not ask again, do not re-summarise, do not "
            f"re-derive the course/intake from this new message.\n"
        )
    if decline and not agree:
        return (
            f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they just "
            f"DECLINED your pending proposal to enrol them in {c_code}, intake {sh_code}. Do "
            f"NOT call 'Validate Enrollment' or 'Enroll Participant'. Acknowledge briefly and "
            f"continue the conversation normally — do not repeat the same summary. If it would "
            f"help, you may offer to save their interest/email for a reminder — but only if you "
            f"actually delegate that to your coworker 'Q&M Training Enquiry Assistant' the "
            f"moment they agree (it has the tool to save it, you don't); never promise this and "
            f"then not follow through, and never claim to have saved anything yourself.\n"
        )
    return ""


# ── Cancellation confirmation (Requirement 11 AC5) — mirrors _CONFIRM_MARKER/
# _pending_confirmation/_confirmation_directive exactly, the same "never act
# without a fresh, deterministic confirmation" standard already required for
# enrolment (Requirement 5 AC2-AC4). A separate marker so a pending
# enrolment confirmation and a pending cancellation confirmation are never
# ambiguous with each other — only one can be true at a time, since both are
# gated on the LAST assistant turn ending with the ONE specific marker.
_CANCEL_CONFIRM_MARKER = "Shall I go ahead and cancel this enrollment?"


def _pending_cancellation(history: list[dict]) -> bool:
    """True if the assistant's last message was a cancellation summary
    awaiting agreement — same exact-suffix check as _pending_confirmation(),
    case-insensitive for the same reason (a marker landing mid-sentence can
    get lowercased by the model)."""
    if not history:
        return False
    last = history[-1]
    if last.get("role") != "assistant":
        return False
    content = (last.get("content") or "").strip()
    return content.lower().endswith(_CANCEL_CONFIRM_MARKER.lower())


def _cancellation_directive(body: str, pending_cancel: bool) -> str:
    """Deterministically classify this turn's reply against a pending
    cancellation confirmation — empty if nothing's pending or the reply is
    ambiguous, same fallback-to-judgement pattern as _confirmation_directive()."""
    if not pending_cancel:
        return ""
    agree = bool(_AGREE_RE.search(body))
    decline = bool(_DECLINE_RE.search(body))
    if agree and not decline:
        return (
            f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they just "
            f"AGREED to cancel their enrollment. Call 'Request Cancellation' now with the reason "
            f"and invoice number already established in this conversation — do not ask again, do "
            f"not re-summarise.\n"
        )
    if decline and not agree:
        return (
            f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they just "
            f"DECLINED cancelling their enrollment. Do NOT call 'Request Cancellation'. "
            f"Acknowledge briefly and continue the conversation normally — do not repeat the "
            f"same summary.\n"
        )
    return ""


def _smart_enroll(params: dict, customer: dict) -> dict:
    """Resolve enrollment params (from command or agent) to an enrollment result.

    params keys (all optional except 'course'):
        course, name, nric, email, schedule

    Detection order for each pipe-separated field:
      NRIC pattern   → nric
      Email pattern  → email
      SH-code        → schedule_id (direct DB id)
      Trailing digit → schedule_no (1-based index)
      First text     → course
      Second text    → name

    Blank fields fall back to the customer's registered profile.
    """
    raw_course   = (params.get("course")   or "").strip()
    raw_name     = (params.get("name")     or "").strip()
    raw_nric     = (params.get("nric")     or "").strip()
    raw_email    = (params.get("email")    or "").strip()
    raw_schedule = (params.get("schedule") or "").strip().rstrip(".,;:")

    # ── Pattern classification ────────────────────────────────────────────────
    # Build the list of all non-empty raw fields (in order) and re-classify each.
    all_fields = [f for f in [raw_course, raw_name, raw_nric, raw_email, raw_schedule] if f]

    nric_val     = ""
    email_val    = ""
    schedule_no  : int | None = None
    schedule_id  : int | None = None
    remaining    : list[str] = []

    for field in all_fields:
        if valid_nric(field):
            nric_val = field.upper()
        elif valid_email(field):
            email_val = field
        elif _SH_CODE_RE.match(field):
            sched = repo.get_schedule_by_schedule_id(field)
            if sched:
                schedule_id = sched["id"]  # store the actual DB PK
        else:
            remaining.append(field)

    # Trailing pure-integer in remaining → schedule item number (only if no SH-code)
    if schedule_id is None and remaining and remaining[-1].isdigit():
        schedule_no = int(remaining.pop())

    # First remaining text → course (resolves C-code or item number)
    course = courses_svc.resolve_course_arg(remaining[0]) if remaining else ""
    # Second remaining text → name override
    name = remaining[1] if len(remaining) > 1 else ""

    # ── Profile fallback (registration gate guarantees these exist) ───────────
    if not name:
        name = customer.get("full_name") or ""
    if not nric_val:
        nric_val = customer.get("nric") or ""
    if not email_val:
        email_val = customer.get("email") or ""

    return enrollment.enroll(
        context.whatsapp_id(), context.phone(),
        course, name, nric_val, email_val,
        schedule_no=schedule_no,
        schedule_id=schedule_id,
    )


def _fallback_reply() -> str:
    all_courses = repo.list_courses()
    if all_courses:
        c = all_courses[0]
        eg_c  = c.get("course_id") or course_short_id(c["id"])
        scheds = repo.get_schedules(c["id"])
        eg_sh = (scheds[0].get("schedule_id") or schedule_short_id(scheds[0]["id"])) if scheds else "SCHEDULE_CODE"
    else:
        eg_c, eg_sh = "COURSE_CODE", "SCHEDULE_CODE"
    return (
        "Let's get you enrolled! I'm having trouble understanding right now, but if you "
        "already know the course code and intake code, mention them together and I can "
        f"enrol you directly — for example \"{eg_c} {eg_sh}\". Otherwise, just ask me to "
        "list the courses or show the intake dates and we'll take it from there."
    )


def build_specialist_agent(body: str, history: list[dict]):
    """The Module B agent, fully configured for this turn — same tool-gating
    Task 10.8 relies on (Enroll Participant only present when agreed_now),
    computed here so the guarantee holds identically whether this agent ends
    up being the turn's own entry point (_run_agent() below) or a coworker
    delegated to from Module A (module_a.py: _run_agent()). Both call sites
    use this exact same function — never re-implemented at the other site,
    so the guarantee can't drift between them.

    The operational protocol below (STEP 0-3, the ABSOLUTE RULE, the
    confirmation-marker requirement) lives in backstory, not task_description,
    deliberately: backstory is bound to the Agent object and applies no
    matter which Task it's asked to run, whereas a delegated ad-hoc Task only
    carries whatever short description the delegating agent's own LLM
    composed — it would never see task_description built here."""
    pending = _pending_confirmation(history)

    # Structural guard, not just a prompt instruction (prompt-only versions of
    # this rule proved unreliable — see task_qm.md Task 10.4's "known gap" and
    # its Task 10.8 follow-up): 'Enroll Participant' is only ever offered to
    # the agent on the one turn that's a clean AGREE reply to a confirmation
    # summary it just sent. Every other turn — including a fresh course+intake
    # resolving for the first time, or a re-selection after the participant
    # declined and changed their mind — the tool is absent entirely, so the
    # agent cannot skip straight to enrolling no matter how confident it is;
    # it has no choice but to fall through to the summarise-and-ask step.
    agreed_now = bool(pending) and bool(_AGREE_RE.search(body)) and not _DECLINE_RE.search(body)

    # Requirement 11 AC5 — same structural gate, same reasoning, for
    # cancellation: 'Request Cancellation' is only ever offered on the one
    # turn that's a clean AGREE reply to a cancellation summary this agent
    # just sent (_CANCEL_CONFIRM_MARKER). Independent of agreed_now above —
    # only one of the two pending-confirmation types can be true on any
    # given turn, since both require the LAST assistant turn to end with
    # their own distinct marker.
    pending_cancel = _pending_cancellation(history)
    cancel_confirmed_now = (
        pending_cancel and bool(_AGREE_RE.search(body)) and not _DECLINE_RE.search(body)
    )

    turn_tools = list(MODULE_B_TOOLS)
    if not agreed_now:
        turn_tools = [t for t in turn_tools if getattr(t, "name", "") != "Enroll Participant"]
    if not cancel_confirmed_now:
        turn_tools = [t for t in turn_tools if getattr(t, "name", "") != "Request Cancellation"]

    return _base_build_agent(
        role="Q&M Enrollment Pipeline Agent",
        goal="Enroll participants accurately or retrieve their enrollment status / invoice.",
        allow_delegation=True,
        tools=turn_tools,
        backstory=(
            "You handle course enrollment and status enquiries for Q&M Dental Group.\n"
            "The participant is already registered — their name, NRIC and email are known "
            "and are auto-filled by your tools when you omit them.\n"
            "Participants may reference courses and intakes by the numbered list they saw "
            "in a previous WhatsApp reply (stateless — re-query the DB each time), or by a "
            "short confirmation like 'intake 2' or 'yes' that only makes sense against the "
            "conversation history you're given — resolve it and act, don't hand the decision "
            "back to the participant as a command to type themselves.\n"
            "A specific intake must always be named or chosen by the participant before you "
            "enroll them — never enroll on a course alone and let the next available date "
            "default silently; show the intake options and ask instead.\n"
            "Once a specific course AND a specific intake are both established, you must still "
            "NOT enroll immediately — first summarise exactly what you're about to book (course "
            "name and intake date) and ask the participant to confirm. Only once THEY explicitly "
            "agree, in their next message, do you actually create the enrolment. If they decline "
            "instead, acknowledge that and continue helping — never enrol without that separate "
            "confirmation turn, and never repeat the identical confirmation summary twice.\n"
            "Never fabricate invoice numbers or confirmations — always use your tools.\n\n"
            "This is a free-text, conversational reply — write it the way a helpful human "
            "staff member would text back, not a rigid printout.\n\n"
            "IMPORTANT — how to determine intent and act, on every task you're given:\n"
            "Your task will give you the participant's registered profile, the current course "
            "catalogue, recent conversation history, and their new message — and, on some turns, "
            "an authoritative 'SYSTEM DIRECTIVE' line. Determine the intent and act:\n"
            "  ENROLL  — follow this exact sequence, in order. Do not skip ahead to a later step "
            "just because you already have enough information to finish sooner.\n"
            "    STEP 0 — if a 'SYSTEM DIRECTIVE' line appears in your task, it is authoritative: "
            "follow it exactly (call the tools it says to call, or don't, and reply accordingly) "
            "and skip the rest of these steps entirely. It already resolved the confirmation "
            "decision deterministically — do not re-derive or second-guess it. A 'SYSTEM NOTE' "
            "line is different — it's informational context, not a resolved decision, and does "
            "NOT skip anything: work through STEP 1-3 normally, incorporating it where it says to "
            "(usually inside your Step 3 confirmation summary, if you reach that step).\n"
            "    ABSOLUTE RULE (applies only when there is no SYSTEM DIRECTIVE in your task): you "
            "are FORBIDDEN from calling 'Enroll Participant' unless you are certain, from "
            "re-reading the conversation history yourself, that the participant's brand-new "
            "message is a clear agreement to a course+intake summary YOU sent as your own "
            "immediately preceding message. If in doubt, do NOT enrol — fall back to STEP 3's "
            "summary-and-ask instead. There are no exceptions to this for the conversational flow "
            "(the C-code+SH-code quick-codes shortcut is handled elsewhere, not by you). Note: "
            "'Enroll Participant' is only ever included in your tool list on the turn this rule "
            "allows it — if you don't see it available to call, that on its own means this is not "
            "that turn; go to STEP 3 and summarise instead of searching for another way to enrol.\n"
            "    STEP 1 — identify the course (name, C-code, or number from the course catalogue "
            "in your task) from the participant's new message or the conversation history.\n"
            "    STEP 2 — identify a SPECIFIC intake (SH-code, or item number from an intake "
            "list already shown), named or chosen by the participant, either in this message or "
            "by confirming/selecting one you already offered earlier. A bare item number, "
            "ordinal, or SH-code with no other content (e.g. '2', 'intake 2', 'the second one', "
            "'SH2604') ALWAYS refers to the numbered intake list YOU most recently sent — map it "
            "yourself from that history, do NOT call 'Course Schedule' again to re-fetch it.\n"
            "      - If no specific intake is known yet: call 'Course Schedule' for the resolved "
            "course, name the course in your reply, list the intake options, and ask them to "
            "pick one. STOP HERE this turn — do not proceed to Step 3.\n"
            "      - If the participant DID name a specific date or intake (anywhere in this "
            "message or the conversation history) but it does not match any of the dates "
            "'Course Schedule' actually returns for this course, do NOT silently drop it and "
            "ask again as if nothing was said — that reads as if you ignored them. Call "
            "'Course Schedule', then explicitly say their requested date/intake isn't available "
            "for this course before listing the real options — e.g. \"I don't see a 1-2 Jan 2026 "
            "intake for the 2-Day Basic Certificate in Dental Assisting — here are the actual "
            "dates: 21-22 Jul 2026, 18-19 Aug 2026. Which would you like?\" STOP HERE this turn "
            "— do not proceed to Step 3.\n"
            "    STEP 3 — course and intake are now both known and there was no SYSTEM DIRECTIVE "
            "(this is the first turn they became fully known). Reply with a confirmation summary "
            "and STOP — do NOT call 'Validate Enrollment' or 'Enroll Participant' this turn. The "
            "summary MUST include the exact course C-code and SH-code in brackets, and MUST end "
            "with EXACTLY this sentence, verbatim, as the very last thing in your reply: "
            f"\"{_CONFIRM_MARKER}\" — for example: 'You're enrolling in [C2601] 2-Day Basic "
            f"Certificate in Dental Assisting, intake [SH2601] 21-22 Jul 2026. {_CONFIRM_MARKER}' "
            "This exact phrasing is required so your next reply can be matched back to this one — "
            "do not paraphrase, shorten, or reword that closing sentence.\n"
            "      Worked example: history shows your own previous reply contained "
            "'1. [SH2603] 14-15 Jul 2026' and '2. [SH2604] 11-12 Aug 2026', and the participant's "
            "brand-new message is 'intake 2'. Per Step 2, the chosen intake is SH2604. There is no "
            "SYSTEM DIRECTIVE, so per Step 3 you reply with a confirmation summary naming [C2601] "
            "and [SH2604], ending with the exact marker sentence, and STOP.\n"
            "  STATUS  — for ANY personal enrollment/payment status question — whether they ask "
            "about all their courses, a specific named course, or just 'my status' generally — "
            "call 'My Enrollments' (pass the course name/code if one was named, blank for all). "
            "It always returns the complete, correct picture (course, date, status, invoice, "
            "receipt for every enrollment), so it is always the right tool — there is no "
            "separate 'just the latest one' tool, because guessing 'latest' silently answers "
            "wrong whenever the participant has more than one enrollment. This is the tool for "
            "'status of all courses I enrolled', not 'List Courses' or 'Course Schedule', which "
            "only describe the catalog, not what THIS participant is enrolled in.\n"
            "    - If the participant has more than one enrollment in the SAME course (different "
            "intake dates) and refers to a specific one by list position (e.g. 'item 2', 'the "
            "second one') or by date, resolve that reference yourself against a numbered "
            "enrollment list already shown earlier in the conversation history — by either you "
            "or the participant's payment conversation — before or instead of calling the tool "
            "again; match the exact invoice number from that list text. Never guess which one "
            "they mean.\n"
            "  INVOICE — call 'Resend Invoice'.\n"
            "  CANCEL  — Requirement 11 AC5, same confirm-before-acting standard as ENROLL:\n"
            "    STEP 0 (same as ENROLL's STEP 0) — a SYSTEM DIRECTIVE about cancellation is "
            "authoritative; follow it and skip the rest of this CANCEL sequence.\n"
            "    STEP 1 — identify which enrollment: an invoice number they stated, or (if they "
            "have only one active enrollment) that one. If they have more than one active "
            "enrollment and named none, call 'My Enrollments' and ask which one (by course/date/"
            "invoice) — do not guess.\n"
            "    STEP 2 — you are FORBIDDEN from calling 'Request Cancellation' unless you are "
            "certain the participant's brand-new message is a clear agreement to a cancellation "
            "summary YOU sent as your own immediately preceding message — there are no "
            "exceptions. 'Request Cancellation' is only ever included in your tool list on the "
            "turn this rule allows it; if you don't see it available, that alone means this is "
            "not that turn — summarise and ask instead of searching for another way to cancel.\n"
            "    STEP 3 — the enrollment is identified and there was no SYSTEM DIRECTIVE (first "
            "turn this became clear). Reply with a short summary of what will be cancelled "
            "(course, invoice number) and STOP — do NOT call 'Request Cancellation' this turn. "
            "End your reply with EXACTLY this sentence, verbatim, as the very last thing you "
            f"write: \"{_CANCEL_CONFIRM_MARKER}\" — do not paraphrase, shorten, or reword it.\n\n"
            "IMPORTANT — tool output rules:\n"
            "A tool's output is your source of FACTS, not your reply template. Compose a "
            "short, natural WhatsApp reply around them. Any identifiers that matter "
            "downstream — course/schedule codes ([C2601]/[SH2603]), invoice numbers, "
            "amounts, confirmation details — must be carried over exactly as the tool "
            "returned them; never round, reword, paraphrase, or guess a number or code.\n"
            "This applies double to SkillsFuture/fee claim status: never upgrade wording like "
            "'SkillsFuture claimable' or 'SkillsFuture credit up to $X' into 'fully claimable' "
            "or 'no cost to you' — those only hold when the tool output itself says the net "
            "payable is S$0. If the tool states a net payable amount, always state that exact "
            "amount; do not paraphrase it away.\n"
            "If the participant is just browsing broadly (e.g. 'show me all courses with "
            "schedules') rather than actively choosing one to enroll in, do not paste the "
            "tool's full nested numbered/bulleted layout verbatim — summarise naturally "
            "(course names, fees, and that intakes are available) and only surface specific "
            "SH-codes once they've settled on one particular course, so you can resolve their "
            "choice. Never show a slash command to the participant — there is no command "
            "syntax in this system any more; enrolling is something you do for them with your "
            "tools once you have a course and intake, not something they type themselves.\n\n"
            "IMPORTANT — if you need general course/fee/SkillsFuture info you have no tool for:\n"
            "If a coworker named 'Q&M Training Enquiry Assistant' is available to you, delegate a "
            "general enquiry question (e.g. detailed SkillsFuture eligibility rules, career "
            "pathways, course content details beyond what your own tools return) to them via "
            "'Delegate work to coworker' rather than guessing or declining to answer, then weave "
            "their answer into your own reply. Never delegate anything about the participant's "
            "own enrollment/invoice/payment record — resolving that is your job, not theirs."
        ),
    )


def _run_agent(body: str) -> str:
    customer = context.customer()
    profile_name  = customer.get("full_name") or ""
    profile_nric  = customer.get("nric")      or ""
    profile_email = customer.get("email")     or ""

    # Re-query course list each time — fully stateless.
    all_courses = repo.list_courses()
    course_list = "\n".join(
        f"  {i}. [{c.get('course_id') or course_short_id(c['id'])}] {c['name']}"
        for i, c in enumerate(all_courses, 1)
    ) or "  (none available)"

    wid = context.whatsapp_id()
    history = repo.recent_memory(wid, limit=8)
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history) or "(no prior messages)"
    pending = _pending_confirmation(history)
    pending_cancel = _pending_cancellation(history)
    directive = (
        _confirmation_directive(body, pending)
        or _cancellation_directive(body, pending_cancel)
        or _stale_context_directive(body, history, pending, pending_cancel)
    )
    switch_note = _course_switch_note(body, history)
    # DB-truth snapshot for the conversation_state write below (Requirement
    # 3 AC10/AC12) — a completed enrolment's own confirmation reply
    # naturally repeats the SH-code it just booked, which would otherwise
    # false-positive against the SH-code check as if Step 2's intake-options
    # list were still open (observed live). A real new enrolment row is the
    # one signal that can't be confused with that, mirroring run()'s own
    # enrollment_before/after fabrication guard elsewhere in this codebase.
    enrollment_before = (repo.latest_enrollment(wid) or {}).get("id")

    agent = build_specialist_agent(body, history)

    reply = _base_run_task(
        agent=agent,
        coworkers=[module_a.build_agent()],
        task_description=(
            f"Registered participant profile:\n"
            f"  Name:  {profile_name or '(missing)'}\n"
            f"  NRIC:  {profile_nric or '(missing)'}\n"
            f"  Email: {profile_email or '(missing)'}\n\n"
            f"Available courses:\n{course_list}\n\n"
            "Recent conversation with this participant (oldest first) — use it to resolve "
            "references like 'intake 2', 'the second one', or 'yes let's do that' back to the "
            "specific course/SH-code the assistant previously offered:\n"
            f"{convo}\n\n"
            f"New message from the participant: \"{body}\"\n"
            f"{directive}\n"
            f"{switch_note}\n"
            "Apply the ENROLL / STATUS / INVOICE protocol from your role instructions to the "
            "profile, course list, conversation history, and new message above.\n"
            "Reply in your own natural words, but keep every code, invoice number, amount, "
            "and date exactly as the tool returned it — never round, reword, or guess them. "
            "Return ONLY the message text to send back on WhatsApp."
        ),
        expected_output=(
            "A short, conversational WhatsApp reply covering the enrollment confirmation "
            "summary awaiting agreement, the completed enrollment, schedule options, status, "
            "or invoice details — with all codes/numbers/dates carried over exactly from the "
            "tool output."
        ),
    )

    # Requirement 3 AC10/AC12: record which stage (if any) this reply leaves
    # open, so the Router can dispatch the participant's next reply straight
    # back here (crews/router.py: _sticky_dispatch()) instead of re-deriving
    # "is this still about enrolment" from text. No change to the enrolment
    # logic above — this only observes the outcome it already produced.
    enrollment_after = (repo.latest_enrollment(wid) or {}).get("id")
    just_enrolled = enrollment_after is not None and enrollment_after != enrollment_before
    if just_enrolled:
        # A genuine new enrolment was created this turn (DB-verified, not
        # inferred from the reply text) — nothing left open, regardless of
        # the confirmation naming the SH-code it just booked.
        repo.clear_conversation_state_if_owner(wid, "B")
    elif reply.strip().lower().endswith(_CONFIRM_MARKER.lower()):
        # Step 3's summary-and-wait — awaiting the participant's explicit
        # agree/decline (_pending_confirmation() will recognise it next turn).
        repo.set_conversation_state(wid, "B", "awaiting_enroll_confirm")
    elif reply.strip().lower().endswith(_CANCEL_CONFIRM_MARKER.lower()):
        # Requirement 11 AC5's cancellation analogue — same reasoning.
        repo.set_conversation_state(wid, "B", "awaiting_cancel_confirm")
    elif _INTAKE_LIST_RE.search(reply):
        # Step 2's "here are the intake options, which would you like?" — a
        # numbered list of SH-codes, not just any single SH-code mention (a
        # status/invoice answer can legitimately mention one too). This is
        # the exact stage that had no recorded owner in the reported bug (a
        # numbered-list reply diverted to Module A instead of completing
        # the enrolment already in progress here).
        repo.set_conversation_state(wid, "B", "awaiting_intake_selection")
    else:
        # A status/invoice answer or a decline — nothing left open.
        repo.clear_conversation_state_if_owner(wid, "B")

    return reply


def _quick_enroll_from_text(body: str, customer: dict) -> str | None:
    """Detect C-code + SH-code in free text and enroll immediately.

    Bypasses the LLM to avoid it misinterpreting SH2604 as 'intake number 4'.
    Returns the enrollment reply, or None if both codes are not found.
    """
    c_m  = _C_CODE_IN_TEXT.search(body)
    sh_m = _SH_CODE_IN_TEXT.search(body)
    if c_m and sh_m:
        result = _smart_enroll(
            {"course": c_m.group(1), "schedule": sh_m.group(1)},
            customer,
        )
        return result["reply"]
    return None


def run(payload: dict, route: dict) -> str:
    # No registration-completeness check here by design: orchestrator.py
    # never dispatches to Module B until the participant's profile is
    # complete (Requirement 5 AC1, Requirement 2 AC4-AC8) — it owns the
    # whole progressive-registration mechanic, including resuming a
    # multi-turn registration flow, since only it can reliably tell a
    # continuation reply apart from a message the Router would otherwise
    # misclassify away from Module B.
    body     = (payload.get("message") or {}).get("body", "")
    customer = context.customer()
    wid      = context.whatsapp_id()

    # Short-circuit: C-code + SH-code in free text → enroll directly, no LLM.
    quick = _quick_enroll_from_text(body, customer)
    if quick is not None:
        # Completes in one turn — nothing left pending (Requirement 3 AC12).
        repo.clear_conversation_state_if_owner(wid, "B")
        return quick

    if not settings.openai_api_key:
        # Canned fallback, not a real stage — clear defensively rather than
        # leave a stale flow behind that a resumed conversation might not
        # actually still be in.
        repo.clear_conversation_state_if_owner(wid, "B")
        return _fallback_reply()
    try:
        return _run_agent(body)
    except Exception as e:  # noqa: BLE001
        log.error("Module B agent failed: %s", e)
        repo.clear_conversation_state_if_owner(wid, "B")
        return _fallback_reply()

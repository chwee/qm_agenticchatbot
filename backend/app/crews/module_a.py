"""Module A — AI Chatbot & Lead Nurturing (Reactive Conversational Agent).

Proposal Section 5.3. Handles enquiry-intent messages: answers FAQs from the
knowledge base, captures leads, and escalates to staff when unsure. Every
enquiry — regardless of whether it begins with '/' — goes to the conversational
CrewAI agent (with Postgres-backed chat memory); there is no deterministic
command path (Requirement 7).
"""
from __future__ import annotations

import logging
import re

from .. import context
from .. import repositories as repo
from ..config import settings
from ..faq import FAQ_KNOWLEDGE_BASE
from ..services import courses as course_svc
from ..services import leads
from ..tools import MODULE_A_TOOLS
from ..utils import is_registered
from ._base import build_agent as _base_build_agent
from ._base import run_task as _base_run_task

log = logging.getLogger(__name__)

# ── Reminder-offer confirmation (mirrors module_b.py's _CONFIRM_MARKER /
# _pending_confirmation pattern) ────────────────────────────────────────────
# Without a fixed marker + deterministic reply classification, the agent
# sometimes treated a bare "yes" (agreeing to the reminder offer, without
# actually giving an email) as if an email had been provided — calling
# 'Save Lead Data' with none and then falsely claiming "I've saved your
# interest..." (observed live: leads.email stayed null). Saving a lead for a
# reminder needs the participant's actual email, the same way progressive
# registration needs a real name/NRIC/email and enrollment needs an explicit
# confirmation — not just an agreement to the idea of providing one.
_REMINDER_OFFER_MARKER = "Would you like me to save your email so I can send you a reminder?"
# Second marker for the follow-up when they agreed but hadn't actually given
# an email yet — without its own fixed ending, the *next* turn (the email
# itself) has nothing deterministic to detect "this is still answering the
# reminder exchange" against, and can get misrouted away from Module A
# entirely by the Router (observed live: an email reply here was classified
# PAYMENT and sent to Module C, which has no way to act on it).
_REMINDER_EMAIL_ASK_MARKER = "Could you share your email address so I can save it?"
# The "which intake?" ask (Requirement 4 AC13) is composed entirely by
# run()'s post-agent backstop, not by a directive the agent is asked to
# reproduce — see _reminder_directive()'s docstring for why a pre-agent
# guess at which course/dates to ask about proved unreliable. Its wording
# deliberately includes "remind" so the existing 2-turn reminder-context
# window (_pending_reminder_window()) recognises a reply to it.
_REMINDER_DATE_ASK_TEXT = "Which intake date should I remind you about?"
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_AGREE_RE = re.compile(r"\b(yes|yeah|yep|sure|ok(ay)?|please|go ahead|sounds good)\b", re.IGNORECASE)
_DECLINE_RE = re.compile(r"\b(no|nope|nah|not (now|yet|really)|don'?t|skip)\b", re.IGNORECASE)

# v1.15: fuzzy keyword-proximity check, not an exact-marker match. Requiring
# the agent to reproduce _REMINDER_OFFER_MARKER/_REMINDER_EMAIL_ASK_MARKER
# verbatim already failed twice — once on casing (a marker landing as a
# sentence continuation gets lowercased, e.g. "By the way, would you
# like..."), and once on paraphrasing (observed live: "Could you please
# share your email address so I can save it for the reminder?" is clearly
# the same request, but has extra words the exact-suffix check couldn't
# tolerate, so it silently failed to match at all). The two markers are
# kept as instructions in the backstory below — still useful, consistent
# phrasing — but detection no longer depends on them being followed exactly.
#
# v1.16: the original version of this check also required "email" and
# "remind" to appear in the SAME sentence within an 80-character window —
# still too strict, and failed live on a reply that legitimately splits the
# two across sentences ("Sure! I can remind you about the Dental Reception
# ... 25 Aug 2026. Could you share your email address..."). Simplified to
# "both keywords appear anywhere in the message" — no proximity or sentence-
# boundary requirement at all.
#
# v1.18 (Requirement 4 AC13 follow-up): requiring "email" at all turned out
# to be one requirement too many — a REPEAT reminder request from a
# participant whose email is already on file has no reason for the agent to
# mention "email" again at any stage (offer, date-ask, or confirmation), so
# this never matched for a second, third, etc. reminder in the same
# conversation. Observed live: a participant who'd already completed one
# reminder asked for a reminder on a different course; the agent correctly
# asked which intake ("...I'll make sure to remind you!" — no "email"
# anywhere in that exchange, since it didn't need to ask for one again), the
# participant answered with a date, and — because this check required
# "email" too — pending stayed False, no directive fired, and the agent
# composed a plausible "Got it, I've noted that..." reply without ever
# calling 'Save Lead Data'. Simplified to just "remind" appearing anywhere —
# the one keyword genuinely common to every stage of this flow, offer
# through confirmation, regardless of whether email needs to be asked for
# again. `_reminder_directive()`'s own stage-branching (email present in the
# reply vs. an intake choice vs. decline/agree) still disambiguates what to
# actually do; a false-positive "pending" with a reply matching none of
# those branches simply returns no directive, same as before this session's
# established pattern for every other reminder-detection simplification.
_REMINDER_KEYWORD_RE = re.compile(r"\bremind\w*\b", re.IGNORECASE)


def _has_reminder_context(content: str) -> bool:
    return bool(_REMINDER_KEYWORD_RE.search(content))


def _pending_reminder_window(history: list[dict]) -> str | None:
    """Text to run _has_reminder_context() against, or None if there's
    nothing pending. Spans up to the last TWO assistant turns, not just the
    latest one — when the participant agrees to the offer without an email
    ("Yes."), Module A's follow-up asks specifically for the email
    (_REMINDER_EMAIL_ASK_MARKER: "Could you share your email address so I
    can save it?") and has no reason to repeat the word "remind" itself;
    checking only that single turn made the whole exchange invisible the
    moment offer and ask landed on separate turns (observed live: an email
    reply to the ask-only turn was misclassified ENROLLMENT and triggered
    the registration prompt instead of being saved as a lead). Still
    requires the participant's message to be the very next reply to an
    assistant turn — this only widens WHAT's scanned, not WHEN it applies."""
    if not history or history[-1].get("role") != "assistant":
        return None
    assistant_turns = [t.get("content") or "" for t in history if t.get("role") == "assistant"]
    return " ".join(assistant_turns[-2:])


def _pending_reminder_offer(history: list[dict]) -> bool:
    """True if the assistant's recent turn(s) appear to be asking about or
    offering to save an email for a reminder — see _pending_reminder_window()
    and _has_reminder_context()."""
    window = _pending_reminder_window(history)
    return bool(window) and _has_reminder_context(window)


def _recent_reminder_intent(body: str, history: list[dict]) -> bool:
    """True if a reminder is genuinely in play THIS turn — covers both the
    bot-initiated path (_pending_reminder_window(), unchanged) AND a
    participant-initiated request, which _pending_reminder_window() alone
    cannot detect: it only ever joins ASSISTANT turns, but a proactive
    "remind me..." request starts life in the PARTICIPANT's own message, and
    the assistant's immediate follow-up (asking for their email) has no
    reason to repeat the word "remind" itself (_REMINDER_EMAIL_ASK_MARKER
    doesn't contain it) — so by the very next turn (them supplying the
    email), the assistant-only window has already lost all trace of it
    (observed live: the ambiguity guard below never even ran, and the reply
    silently guessed the most-recently-discussed course instead of asking).
    Checking the participant's brand-new message directly, and the last
    couple of REAL turns either side of it, closes that gap without
    changing what _pending_reminder_window() itself means or is used for
    elsewhere (stage detection: which/intake ask)."""
    if _has_reminder_context(body):
        return True
    window = _pending_reminder_window(history)
    if window and _has_reminder_context(window):
        return True
    recent = history[-4:] if history else []
    combined = " ".join((t.get("content") or "") for t in recent)
    return _has_reminder_context(combined)


# ── Intake resolution for a reminder (Requirement 4 AC13) ──────────────────
# A reminder against a course with more than one upcoming intake is
# ambiguous unless a specific date is resolved — mirrors Requirement 5 AC5's
# enrollment-side rule (never enrol without a specific intake), applied here
# to the reminder-save path instead.
_ORDINAL_WORDS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
_SH_CODE_IN_TEXT_RE = re.compile(r"\b[Ss][Hh]\d{4}\b")
_INTAKE_LABELED_POSITION_RE = re.compile(r"\b(?:option|intake|number|no\.?)\s*([1-9])\b", re.IGNORECASE)
# Distinguishes "we're answering the which-intake ask" from "we're
# answering the original offer/email-ask" — checked against the recent
# assistant turn(s), NOT against whether the customer happens to already
# have an email on file (a registered participant has one regardless of
# where they are in THIS reminder exchange, so that would misfire on the
# very first offer-response).
#
# Deliberately matched against the distinctive core of
# _REMINDER_DATE_ASK_TEXT ("...intake date should I remind...") rather than
# a bare `\bintake\b` — a plain "intake" match is FAR too broad: every
# ordinary Course Schedule reply already ends with its own unrelated
# call-to-action ("Just let me know which intake you'd like to go for, and
# I can help you with the enrollment!"), which sits in the very same
# 2-turn assistant window whenever a course/schedule question was one of
# the last couple of things discussed. Observed live: that boilerplate
# false-matched this check, making the course-ambiguity backstop below
# believe an intake had already been asked about (so it skipped itself) on
# a turn that had never actually reached the intake stage at all — the
# reminder silently saved a guessed, unconfirmed course instead of asking
# which one was meant.
_INTAKE_ASK_CONTEXT_RE = re.compile(r"intake date should i remind", re.IGNORECASE)


# ── Explicit course-reference anchor ────────────────────────────────────────
# A message naming a course explicitly (a C-code, or a catalogue position
# like "course 1") was observed, twice live, resolving to the WRONG course —
# the agent's own reply used whatever course was most recently discussed in
# conversation instead of the one actually named, even when it had just
# called 'List Courses' and the correct mapping was right there in the tool
# result. This is the same "topic drift vs. explicit reference" ambiguity
# Task 28 already solved for Module B's confirmation summary (a SYSTEM NOTE
# naming the resolved course authoritatively) — applied here to Module A.
_COURSE_POSITION_RE = re.compile(r"\bcour\w{0,3}\s*(?:number\s*|no\.?\s*|#\s*)?(\d+)\b", re.IGNORECASE)
_COURSE_CODE_IN_TEXT_RE = re.compile(r"\b[Cc]\d{4}\b")


def _explicit_course_note(body: str) -> str:
    """If the participant's new message names a course explicitly (a C-code
    or a catalogue position), return a SYSTEM NOTE anchoring the agent to
    that exact course for this turn — informational, not an authoritative
    directive that skips other reasoning, since it should enhance normal
    resolution, not replace it."""
    text = body or ""
    token = None
    m = _COURSE_CODE_IN_TEXT_RE.search(text)
    if m:
        token = m.group(0)
    else:
        m = _COURSE_POSITION_RE.search(text)
        if m:
            token = m.group(1)
    if not token:
        return ""
    try:
        resolved_name = course_svc.resolve_course_arg(token)
        course = repo.get_course_by_name(resolved_name)
    except Exception:  # noqa: BLE001
        return ""
    if not course:
        return ""
    return (
        f"\nSYSTEM NOTE (authoritative — not written by the participant): their message names "
        f"a specific course — \"{course['name']}\" [{course.get('course_id') or ''}] — use "
        f"exactly this course, not any other course discussed earlier in the conversation, when "
        f"resolving what they mean this turn.\n"
    )


# ── Reminder course-ambiguity guard (Requirement 4 AC15) ───────────────────
# A reminder-offer/recap that names more than one course leaves
# customers.preferred_course — a single, possibly-stale value — with
# nothing reliable to anchor to. Observed live: an offer recapping three
# separate courses discussed earlier in the conversation was followed by a
# reminder silently saved against whichever course happened to be
# preferred_course at that moment, not necessarily the one (or any of the
# ones) the participant actually meant. Rather than try to recall or infer
# which course is intended, the participant is asked to state it
# explicitly — mirroring the existing "ask which intake, don't guess" rule
# (AC13) one level up, for the course itself.
_WHICH_COURSE_ASK_TEXT = "Which course would you like this reminder for?"
_COURSE_ASK_CONTEXT_RE = re.compile(r"\bwhich course\b", re.IGNORECASE)


def _courses_mentioned(text: str) -> list[dict]:
    """Every catalogue course explicitly named in `text` — by its full name
    appearing verbatim, or a C-code. Used to detect genuine ambiguity (more
    than one distinct course mentioned), not to resolve a single reference
    (see _explicit_course_note() for that, and _resolve_course_choice()
    below for matching a reply against a specific candidate set)."""
    text = text or ""
    if not text:
        return []
    try:
        all_courses = repo.list_courses()
    except Exception:  # noqa: BLE001
        return []
    low = text.lower()
    found: dict[int, dict] = {}
    for c in all_courses:
        name = c.get("name") or ""
        if name and name.lower() in low:
            found[c["id"]] = c
    for m in _COURSE_CODE_IN_TEXT_RE.finditer(text):
        try:
            resolved_name = course_svc.resolve_course_arg(m.group(0))
            course = repo.get_course_by_name(resolved_name)
        except Exception:  # noqa: BLE001
            continue
        if course:
            found[course["id"]] = course
    return list(found.values())


def _recent_courses_text(body: str, history: list[dict]) -> str:
    """Wider-than-stage-detection source text for course-AMBIGUITY scanning
    (Requirement 4 AC15) — the full recent conversation (both roles, up to
    the last 8 messages already fetched by the caller) plus the
    participant's brand-new message, not just the narrow last-2-ASSISTANT-
    turn window (_pending_reminder_window()) used elsewhere in this file for
    STAGE detection ("did the assistant just ask which intake/course").
    Those are different jobs: stage detection must stay narrow and precise
    (it's reading what the assistant JUST asked), but "which courses has
    this participant actually been discussing" can legitimately span
    several turns back — e.g. a reminder requested (or an email supplied)
    turns after the courses in question were originally discussed, well
    outside that narrower window (observed live: two courses discussed two
    exchanges apart were compressed by the narrow window down to a false
    "only one course" reading, letting a reminder silently save against the
    wrong one instead of asking). Deliberately generous: surfacing a course
    that's no longer actually relevant only costs one extra confirmation
    question, never a wrong silent save — the actual failure this guards
    against."""
    recent = history[-8:] if history else []
    parts = [t.get("content") or "" for t in recent]
    parts.append(body or "")
    return " ".join(parts)


_STOPWORDS = {
    "a", "an", "the", "for", "and", "or", "of", "in", "on", "to", "with",
    "please", "course", "the", "i", "want", "like", "would", "me",
}


def _significant_words(text: str) -> set[str]:
    """Lowercased, punctuation-stripped, stopword-filtered word set — used
    by _resolve_course_choice()'s word-overlap fallback below."""
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return {w for w in text.split() if w and w not in _STOPWORDS}


def _resolve_course_choice(body: str, candidates: list[dict]) -> dict | None:
    """Best-effort match of a free-text reply to ONE specific course from
    `candidates` — by C-code (against the full catalogue), the course's
    name appearing in the reply, an ordinal word, or a labelled/bare list
    position (both positional forms against `candidates`, in the order
    they were offered — mirrors _resolve_intake_choice()'s approach one
    level up). Returns None if ambiguous or unmatched, leaving the agent's
    own judgement in charge exactly as any other unmatched reply in this
    flow."""
    text = (body or "").strip()
    if not text or not candidates:
        return None
    m = _COURSE_CODE_IN_TEXT_RE.search(text)
    if m:
        try:
            resolved_name = course_svc.resolve_course_arg(m.group(0))
            course = repo.get_course_by_name(resolved_name)
        except Exception:  # noqa: BLE001
            course = None
        if course and course["id"] in {c["id"] for c in candidates}:
            return course
    low = text.lower()
    # Checked both directions: the full candidate name appearing in a long
    # reply (as in _courses_mentioned()), OR a short reply appearing as a
    # meaningful prefix/substring of the candidate's (longer) name —
    # observed live: "Infection Control" (the participant's own shorthand)
    # didn't match "Infection Control for Dental Clinics" when only the
    # first direction was checked. The length-4 floor keeps a trivial short
    # reply ("the") from spuriously matching by coincidence.
    matches = [
        c for c in candidates
        if (c.get("name") or "").lower() in low
        or (len(low) >= 4 and low in (c.get("name") or "").lower())
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        # Word-overlap fallback — exact substring (either direction) still
        # misses a very ordinary paraphrase: punctuation spoken out
        # ("&" said as "and"), or trailing filler words ("... please").
        # Observed live: "CPR and Basic Life Support please" didn't match
        # "CPR & Basic Life Support (BLS) for Dental Clinics" in EITHER
        # substring direction — the "&"/"and" mismatch and the trailing
        # "please" broke both. Score each candidate by what fraction of its
        # own distinctive (non-stopword) words appear in the reply; only
        # act on a single, clear winner — a tie, or too weak an overlap,
        # is left unresolved exactly like any other unmatched reply here.
        reply_words = _significant_words(text)
        if reply_words:
            scored = []
            for c in candidates:
                cand_words = _significant_words(c.get("name") or "")
                if not cand_words:
                    continue
                overlap = len(cand_words & reply_words)
                ratio = overlap / len(cand_words)
                if overlap >= 2 and ratio >= 0.5:
                    scored.append((ratio, c))
            if scored:
                best_ratio = max(r for r, _ in scored)
                best = [c for r, c in scored if r == best_ratio]
                if len(best) == 1:
                    return best[0]
    for word, idx in _ORDINAL_WORDS.items():
        if word in low and 1 <= idx <= len(candidates):
            return candidates[idx - 1]
    m = _INTAKE_LABELED_POSITION_RE.search(text)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]
    return None


def _course_schedules_for(customer: dict) -> tuple[dict | None, list[dict]]:
    """Best-effort: the customer's preferred_course resolved to its course
    row and upcoming schedules, or (None, []) if not resolvable — never
    raises, since this only ever gates an optional follow-up question."""
    name = (customer or {}).get("preferred_course")
    if not name:
        return None, []
    try:
        course = repo.get_course_by_name(name)
        if not course:
            return None, []
        return course, repo.get_schedules(course["id"])
    except Exception:  # noqa: BLE001
        return None, []


def _resolve_intake_choice(body: str, schedules: list[dict]) -> dict | None:
    """Best-effort match of a free-text reply to one of a course's upcoming
    intakes — by SH-code, shared date digits with the schedule's label, an
    ordinal word, or a labelled/bare list position. Returns None if
    ambiguous or unmatched, leaving the agent's own judgement in charge
    exactly as any other unmatched reply in this flow."""
    text = (body or "").strip()
    if not text or not schedules:
        return None
    m = _SH_CODE_IN_TEXT_RE.search(text)
    if m:
        for s in schedules:
            if (s.get("schedule_id") or "").upper() == m.group(0).upper():
                return s
    reply_digits = set(re.findall(r"\d{2,4}", text))
    if reply_digits:
        # Score by OVERLAP COUNT, not just non-empty overlap — every intake
        # in the same year shares that year's digit group (e.g. "2026"), so
        # a reply naming a full date ("22 sep 2026") would otherwise match
        # every schedule in that year and look ambiguous, even though the
        # day ("22") uniquely identifies one of them (observed live: a
        # genuine, unambiguous date reply was rejected as ambiguous this
        # way, and the participant's chosen intake was never saved). Prefer
        # whichever schedule shares the MOST digit groups with the reply;
        # only a genuine tie for the top score is still treated as
        # ambiguous.
        scored = [
            (len(reply_digits & set(re.findall(r"\d{2,4}", s.get("label") or ""))), s)
            for s in schedules
        ]
        scored = [(score, s) for score, s in scored if score > 0]
        if scored:
            best_score = max(score for score, _ in scored)
            best = [s for score, s in scored if score == best_score]
            if len(best) == 1:
                return best[0]
    low = text.lower()
    for word, idx in _ORDINAL_WORDS.items():
        if word in low and 1 <= idx <= len(schedules):
            return schedules[idx - 1]
    m = _INTAKE_LABELED_POSITION_RE.search(text)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(schedules):
            return schedules[idx - 1]
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(schedules):
            return schedules[idx - 1]
    return None


def _reminder_directive(
    body: str,
    pending: bool,
    customer: dict | None = None,
    context_window: str = "",
    course_context: str = "",
) -> str:
    """Deterministically resolve a reply to a pending reminder exchange.
    Empty when nothing is pending or the reply is ambiguous/unrelated,
    leaving the agent's own judgement in charge exactly as before this
    existed.

    Deliberately does NOT try to pre-compute which course/intakes this
    reminder needs, or embed a specific date list, in the "known_email"
    branch below — `customer` is a snapshot taken at the START of this
    turn, before the agent has decided (via its own Save Lead Data call)
    which course is actually being discussed. Observed live: the
    conversation had drifted from a course briefly mentioned several turns
    earlier back to a different one, and while the agent correctly saved
    the RIGHT course via Save Lead Data, a directive built here from the
    STALE `customer.preferred_course` had already told it — and it
    dutifully repeated — the WRONG course's intake dates in its final
    reply, producing an internally inconsistent answer (the tool call and
    the reply text disagreed about which course the reminder was even
    for). Course/date correctness for this branch is now entirely the job
    of run()'s post-agent check (see its own comment), which only ever
    trusts a fresh database read taken AFTER the agent has made its own
    determination — a single source of truth instead of two that can
    disagree."""
    if not pending:
        return ""
    body = body or ""
    customer = customer or {}
    course_context = course_context or context_window

    # Answering the "which intake?" ask specifically — checked FIRST, ahead
    # of "which course" below, even though a course must logically be
    # resolved before an intake question makes sense: the 2-turn window
    # (_pending_reminder_window()) can still contain an OLDER "which
    # course?" ask alongside a newer "which intake?" one once the course
    # has already been resolved and the conversation has moved on — checking
    # "which course" first would then re-trigger course resolution using a
    # reply that was actually answering the (newer, more relevant) intake
    # question instead (observed live: "25 Aug" — a date, answering "which
    # intake?" — matched the stale "which course?" text still in the window,
    # failed to resolve as a course, and produced no directive at all for
    # what should have been a straightforward intake confirmation). Reaching
    # the intake-ask stage at all implies a course was already resolved, so
    # it takes priority — keyed off the actual recent conversation
    # (context_window), not off whether the customer happens to have an
    # email on file: an already-registered participant has one regardless
    # of where they are in THIS reminder exchange, which would otherwise
    # misfire on the very first offer-response (observed live: "the 08-09
    # September one please" was caught by the AGREE check below instead,
    # because "please" happens to match it, and the agent re-asked for an
    # email it already had).
    if _INTAKE_ASK_CONTEXT_RE.search(context_window):
        course, schedules = _course_schedules_for(customer)
        chosen = _resolve_intake_choice(body, schedules)
        if chosen and course:
            # Explicitly names the course, not just the date — leaving it
            # out on the assumption the agent wouldn't touch an
            # already-correct preferred_course proved wrong: it was
            # observed live substituting a DIFFERENT, earlier-mentioned
            # course into this same Save Lead Data call, pairing that wrong
            # course with the right course's date (a combination that
            # doesn't correspond to any real intake at all).
            return (
                f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they just "
                f"chose an intake date for their reminder: {chosen['label']}, for the course "
                f"\"{course['name']}\" — this is the ONLY course this concerns; do not substitute "
                f"a different course mentioned earlier in the conversation. Call 'Save Lead Data' "
                f"now with preferred_course='{course['name']}' and course_date='{chosen['label']}' "
                f"and confirm the reminder is set for that date. Do not ask for anything else — "
                f"you already have their email.\n"
            )
        return ""

    # Answering the "which course?" ask specifically (Requirement 4 AC15) —
    # only reached when the window does NOT also contain "which intake?"
    # (checked above); once it does, that takes priority (see its own
    # comment for why).
    if _COURSE_ASK_CONTEXT_RE.search(context_window):
        candidates = _courses_mentioned(context_window)
        chosen_course = _resolve_course_choice(body, candidates)
        if not chosen_course:
            return ""
        try:
            schedules = repo.get_schedules(chosen_course["id"])
        except Exception:  # noqa: BLE001
            schedules = []
        if len(schedules) > 1:
            options = "; ".join(s.get("label") or "" for s in schedules)
            return (
                f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they just "
                f"confirmed the course for their reminder: \"{chosen_course['name']}\" — this is "
                f"the ONLY course this concerns. Call 'Save Lead Data' now with "
                f"preferred_course='{chosen_course['name']}'. Do NOT include course_date yet — "
                f"this course has more than one upcoming intake and none has been chosen. Ask "
                f"which intake they'd like the reminder for ({options}), ending your reply with "
                f"EXACTLY this sentence, verbatim, as the very last thing you write: "
                f"\"{_REMINDER_DATE_ASK_TEXT}\" — do not paraphrase it.\n"
            )
        date_kw = f" and course_date='{schedules[0]['label']}'" if schedules else ""
        return (
            f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they just "
            f"confirmed the course for their reminder: \"{chosen_course['name']}\". Call 'Save "
            f"Lead Data' now with preferred_course='{chosen_course['name']}'{date_kw} and confirm "
            f"the reminder is set. Do not ask for anything else — you already have their email.\n"
        )

    email_m = _EMAIL_RE.search(body)
    decline = _DECLINE_RE.search(body)
    # A participant-initiated reminder request ("remind me about...") is
    # itself the agreement — there's no bot offer to say "yes" to, so
    # requiring one of _AGREE_RE's keywords here missed every such request
    # that didn't happen to also contain one (observed live: "remind me
    # nearer the date" alone, from an already-registered participant,
    # matched neither agree nor decline and fell through with no directive
    # at all, skipping the ambiguity check entirely).
    agree = bool(_AGREE_RE.search(body)) or _has_reminder_context(body)

    # An email in THIS message always wins (a stronger signal than any
    # decline/agree wording that happens to coexist with it). Otherwise, if
    # they agreed (and didn't also decline) and the customer already has an
    # email on file — from registration, or an earlier turn in this same
    # exchange — that counts as "email known" too: demanding they retype an
    # email the system already has would be pointless friction, and forcing
    # this case through the "ask for email" branch below meant the
    # date-completeness check further down was silently skipped whenever
    # the agent (reasonably) used the on-file email on its own initiative
    # instead of literally complying with a directive telling it "you do
    # NOT have their email yet" — which was simply false for a registered
    # participant (observed live: an already-registered participant's bare
    # "Yes." saved successfully but never asked which of the two intakes
    # the reminder was for).
    known_email = email_m.group(0) if email_m else None
    if not known_email and agree and not decline:
        known_email = customer.get("email") or None

    # Requirement 4 AC15: computed once, shared by both branches below —
    # whether the recent conversation recapped MORE THAN ONE course, and the
    # participant's OWN message this turn doesn't already resolve which one.
    # Uses the WIDER course_context (not the narrow stage-detection
    # context_window): a participant-initiated request can supply the email
    # turns after the courses were actually discussed, well past the narrow
    # window (see _recent_courses_text()'s docstring).
    mentioned = _courses_mentioned(course_context)
    ambiguous = len(mentioned) > 1 and not _resolve_course_choice(body, mentioned)

    if known_email:
        if ambiguous:
            names = "; ".join(c["name"] for c in mentioned)
            return (
                f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): more than "
                f"one course was discussed in this conversation ({names}) — do NOT guess or "
                f"assume which one this reminder is for, and do NOT include preferred_course in "
                f"this call. Call 'Save Lead Data' now with ONLY email='{known_email}' so it's "
                f"never lost. Then ask them explicitly which course they'd like the reminder for, "
                f"listing the course names above, ending your reply with EXACTLY this sentence, "
                f"verbatim, as the very last thing you write: \"{_WHICH_COURSE_ASK_TEXT}\" — "
                f"required so their next reply is recognised as answering this; do not paraphrase "
                f"it.\n"
            )
        source_note = (
            f"WITH their email: {known_email}" if email_m
            else f"agreed, and they already have an email on file: {known_email} — use that, do "
                 f"NOT ask them to retype it"
        )
        # No pre-built date list here — see this function's docstring for
        # why: `customer` can be stale about which course is even being
        # discussed, so a directive built from it here can disagree with
        # what the agent itself correctly determines via Save Lead Data.
        # The agent resolves the course from its own understanding of the
        # conversation (using 'Course Schedule' if it needs to check the
        # real intake options); a follow-up backstop in run() independently
        # verifies completeness afterward against a fresh database read.
        return (
            f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they just "
            f"replied to your reminder offer — they {source_note}. Call 'Save Lead Data' now "
            f"with email='{known_email}' (and preferred_course too, if it hasn't already been "
            f"saved this conversation) so their email is never lost even if they don't reply "
            f"again. If their message clearly names a specific intake/date, include it as "
            f"course_date exactly as given. Otherwise — and especially if the course has more "
            f"than one upcoming intake — do NOT guess or pick one yourself; leave course_date "
            f"out and ask them which intake they'd like the reminder for instead (use 'Course "
            f"Schedule' if you need to check the real options for whichever course this "
            f"actually is).\n"
        )

    if decline and not agree:
        return (
            f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they "
            f"DECLINED the reminder offer. Do NOT call 'Save Lead Data' for an email — you "
            f"don't have one. Acknowledge briefly and continue the conversation normally.\n"
        )
    if agree and not decline:
        ambiguity_note = ""
        if ambiguous:
            names = "; ".join(c["name"] for c in mentioned)
            # Without this, the agent's own free reasoning defaults to
            # whichever course was most recently discussed and states it —
            # and a specific intake date alongside it — in this very reply,
            # even though this directive never asked it to (observed live:
            # asking only for email still produced "I can remind you about
            # the CPR course... next intake is 28 Jul", flatly contradicting
            # the "don't guess, ask" requirement one email-exchange early).
            ambiguity_note = (
                f" More than one course was discussed in this conversation ({names}) — do NOT "
                f"name, confirm, or commit to any specific one of them in this reply (which "
                f"course this reminder is for will be asked separately, right after the email); "
                f"just ask for their email."
            )
        return (
            f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they agreed "
            f"to your reminder offer but did NOT include an actual email address in this "
            f"message, and none is on file yet either — a bare 'yes'/'sure' is not an email. "
            f"Do NOT call 'Save Lead Data' for an email and do NOT say anything has been saved "
            f"or that a reminder will be sent.{ambiguity_note} Ask them to share their email "
            f"address now, ending your reply with EXACTLY this sentence, verbatim, as the very "
            f"last thing you write: \"{_REMINDER_EMAIL_ASK_MARKER}\" — required so their next "
            f"reply is still recognised as answering this, instead of being routed elsewhere; do "
            f"not paraphrase it.\n"
        )
    return ""

_PERSONA = (
    "\n\n# Your Role\n"
    "You are the friendly WhatsApp assistant for Q&M Dental Group's training programmes. "
    "Answer ONLY from the knowledge base above and the tools provided. This is a free-text, "
    "conversational reply — write it the way a helpful human staff member would text back, "
    "not a rigid printout. Never invent fees, dates, funding amounts, or policies. If a "
    "participant shares their details (name, NRIC, email, preferred course or intake date), "
    "call 'Save Lead Data'. If you cannot answer confidently, or the query is sensitive or "
    "out of scope, call 'Flag For Staff Review' and tell the participant a colleague will "
    "follow up.\n\n"
    "IMPORTANT — tool output rules:\n"
    "When you call 'List Courses', 'Course Fees', 'Course Schedule', or 'SkillsFuture Info', "
    "the tool's text is your source of FACTS, not your reply template. Weave the facts you "
    "need into a short, natural, conversational WhatsApp reply that answers what the "
    "participant actually asked — you don't have to relay every course/line the tool "
    "returned. Carry over exact figures, dates, and codes (e.g. [C2601], SH-codes, fee "
    "amounts) precisely as given — never round, reword, or guess them — but the sentences "
    "around them are yours to write naturally.\n"
    "This holds even when the participant asks broadly for 'all courses' or 'everything with "
    "schedules' — do not paste the tool's nested numbered/bulleted layout verbatim; that reads "
    "like a printed brochure, not a text from a person. For a broad request, summarise: course "
    "names and fees are usually enough, in a sentence or two per course.\n"
    "Never show a slash command to the participant — there is no command syntax in this system "
    "any more. Enrolling is never something the participant types themselves — once they've told "
    "you (in their own words) which course and intake they want, that is enrollment intent — but "
    "you have NO tool that can actually create that enrolment yourself (see the delegate-or-"
    "redirect section below). Acknowledge what you understood and route it properly; never say "
    "or imply an enrolment is done, confirmed, or that an invoice is on its way unless that came "
    "from your coworker's own genuine confirmation this turn — 'Save Lead Data' records interest "
    "only, it does not enrol anyone, and claiming otherwise would be fabricating a financial "
    "confirmation the participant will reasonably rely on.\n\n"
    "IMPORTANT — multi-turn context:\n"
    "The participant's new message may lean on something said earlier (e.g. 'what about the "
    "fees?' after a course was named a few messages ago, or 'and the next intake?'). Check the "
    "conversation history first and resolve the missing piece (which course, which detail) "
    "from it before calling a tool. Only if the history genuinely doesn't establish what's "
    "needed should you ask the participant a short clarifying question — never guess a course, "
    "code, or figure, and never call a tool with a guessed input.\n\n"
    "IMPORTANT — personal enrollment/payment status, and enrolling: delegate if you can, "
    "otherwise redirect:\n"
    "None of YOUR OWN tools can check the participant's personal enrollment/registration/payment "
    "record, or enrol them in a course — never guess, infer from conversation history, or reuse a "
    "course/fee mentioned earlier as if it confirmed an enrollment; that would be fabricating "
    "their personal record. If asked something like 'what am I enrolled in', 'did I register for "
    "X', 'what's my status', 'have I paid', or to actually enrol them in a course:\n"
    "  - IF a coworker named 'Q&M Enrollment Pipeline Agent' is available to you this turn (only "
    "offered once the participant is fully registered), delegate that specific part to them via "
    "'Delegate work to coworker' — in the context you hand off, include the participant's "
    "registered profile if known and any course/intake already discussed in this conversation, so "
    "they don't have to guess it from scratch. Weave their answer into your own reply naturally, "
    "in your own words — don't just paste it verbatim — but never round, reword, or drop a code, "
    "invoice number, date, or amount they gave you.\n"
    "  - IF that coworker is NOT available this turn, tell them to just ask about it directly (in "
    "their own words) and it'll be handled next message, exactly as before — never claim you "
    "checked or enrolled them when you did not.\n"
    "This applies even if a course name and fee were discussed earlier in the same conversation — "
    "discussing a course is not the same as being enrolled in it.\n\n"
    "IMPORTANT — capture interest for follow-up, and offer a reminder before they leave:\n"
    "As soon as a specific course is clearly the one the participant is interested in — they "
    "named it, or it's obviously what the conversation has been about — call 'Save Lead Data' "
    "with preferred_course set to that course's name, even if they haven't shared any contact "
    "detail yet and even if they've said they're not ready to register. This is what lets us "
    "follow up with them later if they never come back to enrol.\n"
    "Separately, watch for the conversation winding down without them committing to enrol — "
    "they say they'll think about it, want to come back later, seem hesitant, or are giving "
    "closing-type replies ('ok thanks', 'I'll consider', 'maybe later') rather than asking "
    "more. The FIRST time this happens in a conversation (never repeat this ask again in the "
    "same conversation, and never ask while they're still actively asking questions), briefly "
    "summarise the course(s) discussed and ask if they'd like a reminder, ending your reply "
    f"with EXACTLY this sentence, verbatim, as the very last thing you write: "
    f"\"{_REMINDER_OFFER_MARKER}\" — this exact phrasing is required so your next reply can be "
    "matched back to this one; do not paraphrase, shorten, or reword it.\n"
    "IMPORTANT — responding to a reply to that offer: saving a lead for a reminder needs their "
    "ACTUAL email address, the same way registration needs a real name/NRIC/email and enrolling "
    "needs an explicit confirmation — agreeing to the IDEA of a reminder is not the same as "
    "giving you the email itself. If a 'SYSTEM DIRECTIVE' line appears in your task, it already "
    "deterministically resolved whether they gave an email, declined, or agreed without one — "
    "follow it exactly. In particular: NEVER call 'Save Lead Data' for an email, and NEVER say "
    "you've saved anything or will send a reminder, unless an actual email address is present "
    "(in the directive or the participant's own message) — a bare 'yes'/'sure' means you must "
    "ask for the email address itself, not claim success."
)


def _fallback_reply() -> str:
    return (
        "Thanks for your message! I can help with our dental assisting courses — just ask me "
        "in your own words, for example \"what courses do you offer?\", \"how much is it?\", or "
        "\"when's the next intake?\". Let me know when you're ready to enrol and I'll take care "
        "of it for you."
    )


# ── Fabricated-enrollment guard ─────────────────────────────────────────────
# Structural safeguard, not just a prompt instruction: the backstory already
# explicitly forbade claiming an enrolment happened without delegating and
# relaying a genuine confirmation — and the model did it anyway (observed
# live: after only calling 'Save Lead Data', it replied "I've got you
# enrolled... invoice... shortly", with no Enroll Participant call and no
# delegation anywhere in the trace). Module A has no tool that can actually
# create an enrolment; run() below verifies genuineness with a fresh
# repo.latest_enrollment() read taken before and after the agent runs — not
# a ContextVar flag (tried and removed; see context.py's note on why).
_FABRICATED_ENROLLMENT_RE = re.compile(
    r"\b(you'?re enrolled|you'?ve been enrolled|you are now enrolled|got you enrolled|"
    r"successfully enrolled|enrolment confirmed|enrollment confirmed|"
    r"invoice\b[^.!?\n]{0,60}\b(sent|shortly|has been sent|is on (its|it'?s) way))\b",
    re.IGNORECASE,
)


def _enrollment_claim_fallback() -> str:
    return (
        "Sorry — let me make sure I get this right before confirming anything. I don't have "
        "you enrolled yet. If you'd like to enrol, just tell me the course and intake date "
        "you want, and I'll take care of the rest properly."
    )


def build_agent(*, allow_delegation: bool = False):
    """Construct Module A's Agent — exposed (not just used internally) so
    module_b.py can include it as a delegation coworker: Module A never
    creates state, so it's always safe to offer as a coworker regardless of
    the participant's registration status."""
    return _base_build_agent(
        role="Q&M Training Enquiry Assistant",
        goal="Answer dental training enquiries accurately, capture leads, and escalate when unsure.",
        backstory=FAQ_KNOWLEDGE_BASE + _PERSONA,
        tools=MODULE_A_TOOLS,
        allow_delegation=allow_delegation,
    )


def _run_agent(body: str) -> str:
    wid = context.whatsapp_id()
    history = repo.recent_memory(wid, limit=8)
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history) or "(no prior messages)"

    # Delegation coworker: the enrollment specialist is only ever offered
    # once the participant is fully registered — this conditional inclusion
    # IS the registration gate for this direction of delegation (Requirement
    # 2/5 AC1). An unregistered participant's crew simply never has Module B
    # as an available coworker, so an enrollment action is structurally
    # unreachable here, not merely discouraged by prompt wording (same
    # "tool/agent availability, not prompt wording" pattern as Task 10.8's
    # Enroll Participant tool-gating).
    coworkers = []
    customer = context.customer()
    if is_registered(customer):
        from .module_b import build_specialist_agent
        coworkers = [build_specialist_agent(body, history)]

    agent = build_agent(allow_delegation=bool(coworkers))
    reminder_window = _pending_reminder_window(history) or ""
    # _recent_reminder_intent() (not just the bot-offer-only window above)
    # so a PARTICIPANT-initiated "remind me about..." request is recognised
    # too — see its docstring for why the narrow window alone misses it.
    pending = _recent_reminder_intent(body, history)
    course_context = _recent_courses_text(body, history)

    # Requirement 4 AC15, deterministic short-circuit: when a reminder is
    # pending, no email is known yet (in this message or on file), the
    # participant hasn't declined, and more than one course is genuinely in
    # play with nothing in this message resolving which one — the correct
    # reply is entirely deterministic (ask for the email, name no course) —
    # skip the agent for it rather than trust it to follow a SYSTEM
    # DIRECTIVE telling it the same thing, which this session has already
    # found unreliable for comparable asks (see the intake-date ask further
    # below, composed the same deterministic way for the same reason).
    # Observed live: even with an explicit "do NOT name a course" directive
    # present, the agent still opened with "...remind you about the CPR
    # course... next intakes are 28 Jul and 25 Aug" before asking for email.
    if pending and not _DECLINE_RE.search(body):
        known_email_now = bool(_EMAIL_RE.search(body)) or bool((customer or {}).get("email"))
        if not known_email_now:
            mentioned = _courses_mentioned(course_context)
            if len(mentioned) > 1 and not _resolve_course_choice(body, mentioned):
                repo.set_conversation_state(wid, "A", "awaiting_reminder")
                return f"Sure! {_REMINDER_EMAIL_ASK_MARKER}"

    reminder_directive = _reminder_directive(body, pending, customer, reminder_window, course_context)
    course_note = _explicit_course_note(body)

    reply = _base_run_task(
        agent=agent,
        coworkers=coworkers,
        task_description=(
            "Recent conversation with this participant (oldest first):\n"
            f"{convo}\n\n"
            f"New message from the participant:\n\"{body}\"\n"
            f"{course_note}"
            f"{reminder_directive}\n"
            "Reply helpfully, in your own natural words. If the new message depends on "
            "something from the conversation above (a course, an intake, a figure already "
            "discussed), resolve it from the history and act — don't make the participant "
            "repeat themselves. Use your tools for course/fee/schedule/SkillsFuture facts, "
            "save any details they share, and escalate if you are unsure.\n\n"
            "Before replying, check the conversation history above: if you (the assistant) "
            "already offered to save a reminder email earlier in this conversation, do not "
            "offer it again — only ask once, and only if it hasn't been asked yet.\n\n"
            "Any course information tool (List Courses, Course Fees, Course Schedule, "
            "SkillsFuture Info) gives you facts to answer with, not a script to repeat — "
            "compose a short, human, conversational reply, keeping exact codes/fees/dates "
            "unchanged from what the tool returned. If, after checking the history, you still "
            "can't tell which course or detail the participant means, ask them directly instead "
            "of guessing. "
            "Return ONLY the message text to send back on WhatsApp."
        ),
        expected_output=(
            "A short, friendly, conversational WhatsApp reply that accurately reflects any "
            "facts a tool returned (codes, fees, dates carried over exactly), or a brief "
            "clarifying question if the request is genuinely ambiguous."
        ),
    )

    # Structural backstop (Requirement 4 AC13) — the SOLE authority on
    # reminder date-completeness, deliberately independent of anything
    # computed before the agent ran. Prompt-only compliance with "ask which
    # intake, don't invent one" was found unreliable in two distinct,
    # reproducible ways (the agent silently drops the question, or invents
    # the earliest intake and confirms success anyway) — but a THIRD,
    # separate problem showed those failure modes weren't the only risk:
    # the directive's own course/date GUESS could itself be wrong, because
    # `customer` (the pre-agent snapshot) can be stale about which course
    # is even being discussed when the topic has drifted since the last
    # explicit course mention. Observed live: the agent correctly saved the
    # RIGHT course via its own Save Lead Data call, but a directive built
    # from the stale snapshot had already told it the WRONG course's
    # intake dates, and it repeated them — an internally inconsistent
    # reply where the save and the text disagreed about the course.
    #
    # Fixed by never trusting a pre-agent guess for correctness at all:
    # only a FRESH database read, taken after the agent has made its own
    # determination, is ever treated as ground truth for "which course"
    # and "is a date still needed" — one source of truth instead of two
    # that can silently disagree.
    if pending:
        fresh_customer = repo.get_or_create_customer(wid)

        # Course-ambiguity backstop (Requirement 4 AC15), checked before the
        # date-completeness one below: if the recent reminder window
        # recapped MORE THAN ONE course, don't trust whatever ended up as
        # preferred_course, regardless of what the directive or the agent's
        # own reasoning decided — verify the participant's OWN reply this
        # turn actually resolves to one of the mentioned courses. If not,
        # the course is still genuinely unresolved and must be asked for,
        # even if the agent went ahead and saved something anyway. Gated on
        # email already being on file, same as the date-completeness check
        # below — otherwise this could fire (and falsely claim "I have your
        # email on file") on the bare "yes" turn that precedes the email
        # even being asked for. ALSO gated on the window not already
        # containing "which intake?" — the 2-turn window can still hold an
        # OLDER "which course?" ask alongside a newer "which intake?" one
        # once the course has already been resolved and the exchange has
        # moved on (mirrors _reminder_directive()'s identical ordering fix;
        # observed live: a bare date reply correctly answering "which
        # intake?" was rejected here as failing to resolve as a course,
        # re-triggering the already-answered "which course?" question).
        # Uses the wider course_context, not reminder_window, for the same
        # reason as the pre-agent directive above (see _recent_courses_text()).
        mentioned = _courses_mentioned(course_context)
        stale_course = fresh_customer.get("preferred_course")
        stale_date = fresh_customer.get("course_date")
        # A reminder that was ALREADY fully saved before this turn started
        # (a real `reminders` row exists for exactly this course+date) is
        # not ambiguous right now — it's done. A stale "remind" mention
        # still lingering in the 2-turn window (_pending_reminder_window())
        # can make `pending` look True long after the reminder actually
        # completed and the conversation moved on to something unrelated —
        # observed live: an enrollment attempt with no course named in it
        # ("Chen Wei, S1785000A") was read as "still resolving an ambiguous
        # reminder," which reverted AND DELETED the already-saved reminder
        # as a side effect, then replied with a stale "which course?" ask
        # that ignored the actual enrollment attempt entirely.
        already_saved = bool(stale_course) and bool(stale_date) and any(
            r.get("course_name") == stale_course and r.get("course_date") == stale_date
            for r in repo.reminders_for_customer(wid)
        )
        if (
            fresh_customer.get("email")
            and len(mentioned) > 1
            and not already_saved
            and not _INTAKE_ASK_CONTEXT_RE.search(reminder_window)
            and not _resolve_course_choice(body, mentioned)
        ):
            if stale_course or stale_date:
                log.warning(
                    "Module A resolved an ambiguous reminder (%d courses recapped) without an "
                    "explicit participant choice — reverting: wid=%s course=%r date=%r",
                    len(mentioned), wid, stale_course, stale_date,
                )
                repo.update_customer(wid, preferred_course="", course_date="")
                if stale_course and stale_date:
                    repo.delete_reminder(wid, stale_course, stale_date)
            names = "; ".join(c["name"] for c in mentioned)
            repo.set_conversation_state(wid, "A", "awaiting_reminder")
            return (
                f"Got it — I have your email on file. We discussed a few courses ({names}) — "
                f"{_WHICH_COURSE_ASK_TEXT}"
            )

        _course, schedules = _course_schedules_for(fresh_customer)
        email_known = bool(fresh_customer.get("email"))
        if email_known and len(schedules) > 1:
            course_date_after = fresh_customer.get("course_date")
            # Direct correctness check: does the saved date match one of
            # THIS course's actual intake labels at all? Catches not just an
            # invented date, but a genuinely corrupted combination — observed
            # live: the agent saved a real date that belongs to a DIFFERENT
            # course than the one it paired it with (a course briefly
            # mentioned earlier in the conversation), producing a
            # course+date pairing that doesn't correspond to any real
            # intake. A weaker earlier version of this check only verified
            # "does the participant's message plausibly resolve to SOME
            # date in the (possibly wrong) course's schedule" — insufficient
            # here, since a coincidental digit overlap (e.g. both courses
            # having an intake ending in the same day-of-month) let the
            # wrong pairing pass anyway.
            valid_labels = {s.get("label") for s in schedules}
            if course_date_after and course_date_after not in valid_labels:
                bad_course = fresh_customer.get("preferred_course")
                log.warning(
                    "Module A saved course_date=%r that isn't a real intake for %r — "
                    "reverting: wid=%s", course_date_after, bad_course, wid,
                )
                repo.update_customer(wid, course_date="")
                # save_lead_tool() already persisted a `reminders` row with
                # this exact (course, date) pairing by the time this check
                # runs (the tool call and this validation happen at
                # different points in the same turn) — remove it too, or
                # an invalid reminder survives even after the customer
                # record itself is corrected.
                if bad_course:
                    repo.delete_reminder(wid, bad_course, course_date_after)
                course_date_after = ""
            if not course_date_after:
                options = "; ".join(s.get("label") or "" for s in schedules)
                reply = (
                    f"Got it — I've saved your interest, and I have your email on file. "
                    f"{_REMINDER_DATE_ASK_TEXT} We have {options}."
                )
                repo.set_conversation_state(wid, "A", "awaiting_reminder")
            else:
                # A specific intake resolved this turn (or was already on
                # file) — the reminder is complete, nothing left to ask.
                repo.clear_conversation_state_if_owner(wid, "A")
        elif email_known:
            # Email known, course known, single intake (or none needing
            # disambiguation) — the reminder resolves without a date question.
            repo.clear_conversation_state_if_owner(wid, "A")
        else:
            # Email still not known (this course was never ambiguous, so
            # the pre-agent short-circuit above never fired) — the agent's
            # own reply is presumably asking for it; still open regardless
            # of how many intakes this course has.
            repo.set_conversation_state(wid, "A", "awaiting_reminder")
    else:
        # No reminder was in play this turn at all (pending is False) —
        # release any stale flow from an earlier, abandoned reminder rather
        # than leaving it to trap an unrelated future reply.
        repo.clear_conversation_state_if_owner(wid, "A")

    return reply


def run(payload: dict, route: dict) -> str:
    # Every enquiry updates the lead/customer record, regardless of
    # registration stage or whether contact details were volunteered
    # (Requirement 4 AC7) — best-effort, never blocks the reply.
    try:
        leads.record_enquiry_activity(context.whatsapp_id())
    except Exception as e:  # noqa: BLE001
        log.warning("lead activity update failed: %s", e)

    body = (payload.get("message") or {}).get("body", "")
    if not settings.openai_api_key:
        return _fallback_reply()

    wid = context.whatsapp_id()
    enrollment_before = (repo.latest_enrollment(wid) or {}).get("id")

    try:
        reply = _run_agent(body)
    except Exception as e:  # noqa: BLE001
        log.error("Module A agent failed: %s", e)
        return _fallback_reply()

    if _FABRICATED_ENROLLMENT_RE.search(reply):
        # Verified against a fresh DB read (was a NEW enrollment id created
        # since the top of this turn?), not context.enrollment_was_created().
        # That flag is set via the same synchronous "Delegate work to
        # coworker" hop this guard exists to police (Module A relaying
        # Module B's genuine confirmation) — CrewAI's own docs and source
        # describe that hop as synchronous/same-thread, but it was observed
        # live NOT reliably propagating a ContextVar write back to this
        # check: a real, invoiced enrollment (confirmed via query_log and
        # the enrollments table) still got its genuine confirmation replaced
        # by the fallback below, falsely telling an actually-enrolled
        # participant they weren't enrolled — arguably worse than the
        # original fabrication bug, since this time a real success was
        # denied. A before/after comparison against enrollments.id doesn't
        # depend on any in-process propagation at all.
        enrollment_after = (repo.latest_enrollment(wid) or {}).get("id")
        genuinely_created = enrollment_after is not None and enrollment_after != enrollment_before
        if not genuinely_created:
            log.warning(
                "Module A reply claimed an enrolment that didn't happen — replacing: %r", reply,
            )
            reply = _enrollment_claim_fallback()

    return reply

"""Inbound message orchestrator — stateless, per-user-isolated routing.

Architecture overview
─────────────────────
The WhatsApp gateway is a shared, stateless channel.  Every inbound message
arrives as an independent HTTP request.  There is no session concept.

Multi-user isolation is enforced entirely through the customers table:
  · whatsapp_id (the raw gateway JID) is the ONLY stable, always-present
    identifier.  It is used as the primary partition key for every DB lookup.
  · No global mutable state is shared between users.
  · get_or_create_customer() is an atomic upsert — two concurrent first
    messages from different users never collide.

WhatsApp account types
──────────────────────
@c.us   Legacy accounts.  chat_id = "6591234567@c.us".
        The gateway extracts the real phone from the JID directly.
        Phone is auto-confirmed; only full_name/nric/email still required.

@lid    Linked Identity accounts.  chat_id = "153811586920512@lid".
        The 15-digit LID is NOT the phone number.  gateway sends phone=null.
        All four registration fields must be supplied by the user.

Registration gate
─────────────────
Every inbound message passes through a registration check before routing.
A customer is considered "registered" when ALL of the following are set:
    phone_confirmed = TRUE, full_name, nric, email

If not registered, the reply is free text by default — there is no command
syntax shown to the customer:
  1. Whatever the customer typed is scanned for the still-missing fields
     (phone, full name, NRIC, email), in any order, in their own words.
     A legacy "field | field | field[ | field]" reply is still recognised as
     a shortcut, but it is never required or advertised.
  2. Any field found is saved immediately, so a customer who gives partial
     details across a couple of messages never has to repeat themselves.
  3. Once all four fields are known → save, reply with confirmation + query
     invitation, stop.
  4. Otherwise → ask (in free text) only for whatever is still missing, stop.

If registered → route normally to Module A / B / C.

There is no command syntax (Requirement 7) — every message is free text,
handled in one self-contained turn with no session memory. When an agent
needs customer context, it reads from the customers table using the
whatsapp_id and/or phone stored in the context module.
"""
from __future__ import annotations

import logging
import re

from . import context
from . import query_log
from . import repositories as repo
from .config import settings
from .crews import module_a, module_b, module_c, router
from .utils import is_registered
from .whatsapp_client import send_whatsapp

log = logging.getLogger(__name__)

_MODULES = {"A": module_a, "B": module_b, "C": module_c}

# Real E.164 numbers are 7–14 digits.
# WhatsApp LIDs are exactly 15 digits — reject those.
_PHONE_RE = re.compile(r"^\d{7,14}$")

# ── Deterministic course-interest capture ───────────────────────────────────
# Deliberately narrow (a C-code, or an explicit "course N" position
# reference) rather than fuzzy full-text matching against course names —
# raw participant text is much noisier than an LLM-resolved course argument,
# so this only fires on unambiguous, high-precision signals.
_COURSE_CODE_IN_TEXT_RE = re.compile(r"\b[Cc]\d{4}\b")
# "cour\w{0,3}" tolerates common misspellings of "course" (coure, cours,
# coures) — found via crews/module_b.py's identical pattern silently
# missing a live "coure 1" typo; applied here too for the same reason.
_COURSE_POSITION_RE = re.compile(r"\bcour\w{0,3}\s*(?:number\s*|no\.?\s*|#\s*)?(\d+)\b", re.IGNORECASE)

# ── Free-text registration extraction ───────────────────────────────────────
# Structured tokens (email, NRIC, phone) are pulled out with regex — never
# invented by an LLM. Whatever's left over after stripping those and common
# filler words is taken as the full name; an LLM is only ever asked to pull
# the name (never the other fields) when the leftover text isn't usable.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_NRIC_RE = re.compile(r"\b[STFGMstfgm]\d{7}[A-Za-z]\b")
# NRIC-shaped (letter + 7 alphanumeric + letter = 9 chars) but not matching
# the strict digits-only _NRIC_RE — almost always a typo (e.g. capital 'I'
# for '1') rather than something unrelated, given the shape is otherwise
# exact. See _nric_near_miss() / _registration_prompt(). This shape alone is
# NOT enough on its own, though — an ordinary 9-letter English word starting
# with S/T/F/G/M (e.g. "MENTIONED") matches it purely by coincidence, with
# zero digits anywhere; _nric_near_miss() additionally requires at least one
# digit in the middle span, which a genuine NRIC typo (mostly digits, one
# character wrong) always has and a real word never does.
_NRIC_NEAR_MISS_RE = re.compile(r"\b[STFGMstfgm]([A-Za-z0-9]{7})[A-Za-z]\b")
_PHONE_TOKEN_RE = re.compile(r"(?<!\w)\+?\d[\d \-]{5,15}\d(?!\w)")
_NAME_FILLER_RE = re.compile(
    r"\b(my\s+name\s+is|full\s+name\s+is|full\s+name|name\s+is|i\s*am|i'?m|this\s+is|"
    r"you\s+can\s+call\s+me|call\s+me|you\s+can\s+reach\s+me\s+at|reach\s+me\s+at|"
    r"reach\s+me|hey\s+there|hi\s+there|good\s+morning|good\s+afternoon|good\s+evening|"
    r"name|phone\s+number|phone|nric|email\s+address|email|is|are|am|here|there|hi|hiya|"
    r"hello|hey|greetings|so|well|just|okay|ok|yeah|yes|sure|call|reach|contact|you|can|"
    r"me|at|thanks|thank\s+you|please|the|my|and|this|"
    # Ordinary conversational words — without these, a plain sentence with no
    # name in it at all (e.g. "I just want to explore the course") can reduce
    # to a short letters-only remainder that structurally passes as a name.
    # Not an attempt at a complete English stopword list — just wide enough
    # that common non-registration replies collapse to empty, so they're
    # correctly rejected instead of accidentally looking like a name.
    r"i|we|they|it|to|for|of|in|on|with|about|not|no|do|does|did|would|could|should|"
    r"will|shall|want|wants|wanted|need|needs|needed|explore|exploring|explored|"
    r"interested|interest|consider|considering|think|thinking|thought|know|knowing|"
    r"see|seeing|tell|telling|ask|asking|help|helping|look|looking|try|trying|get|"
    r"getting|come|coming|go|going|check|checking|find|finding|what|why|how|when|"
    r"where|who|which|more|info|information|details|detail|later|yet|still|maybe|"
    r"probably|actually|really|course|courses|class|classes|programme|program|enrol|"
    r"enroll|enrolment|enrollment|register|registration|registering|schedule|"
    r"schedules|fee|fees|price|prices|cost|costs)\b",
    re.IGNORECASE,
)

# Explicit self-introduction cues — the ONLY basis on which the regex path
# below extracts a name (v1.10). The previous approach (strip everything in
# _NAME_FILLER_RE, accept whatever's left as a name) kept resurfacing the
# same class of bug on new phrasings it hadn't anticipated — "I just want to
# explore the course" and "sorry, change mind" both reduced to a short,
# letters-only remainder that structurally passed as a name, even after two
# rounds of adding more words to the filler list (v1.5, v1.7). No filler
# list can ever be complete. A message with no explicit cue — including a
# bare name with nothing else, e.g. "Tan Wei Ling" — now deliberately
# returns None here and falls through to the LLM path instead, which has
# its own provenance check (_extracted_from_source) so it can't accept a
# name that isn't actually present in the message.
#
# v1.15: "I am"/"I'm"/"this is" were deliberately REMOVED from this list.
# Unlike "my name is X" or "call me X" (structurally always followed by a
# name — nothing else makes grammatical sense there), "I am X" is genuinely
# ambiguous: it introduces a name ("I am John") just as often as it
# continues into an ordinary statement ("I am just trying to explore my
# option."). Once that weaker cue matched, the same filler-stripping
# weakness resurfaced *within* the captured span — "option" isn't on the
# filler list, so "I am just trying to explore my option." reduced to
# "Option" and was wrongly accepted as a name. A message using "I'm"/"I am"
# to state a real name (e.g. "I'm Tan Wei Ling") now falls through to the
# LLM path instead, which can actually tell the two apart semantically —
# the regex path only keeps the cues where a name is the *only* thing that
# can grammatically follow.
_NAME_INTRO_RE = re.compile(
    r"(?:my\s+name\s+is|full\s+name\s+is|name\s+is|"
    r"you\s+can\s+call\s+me|call\s+me)\s*[:\-]?\s*"
    r"([A-Za-z][A-Za-z .'\-]*)",
    re.IGNORECASE,
)

_FIELD_LABELS = {
    "phone": "phone number",
    "full_name": "full name",
    "nric": "NRIC",
    "email": "email address",
}

_REG_CONFIRM = "Thank you, *{name}*! Your details are registered. ✅"

# Display name of the CrewAI agent each module runs, for the terminal trace.
# Kept here (not derived from crews/*.py) since it's purely a label, and each
# module's own `role=` string is the single source of truth for the agent itself.
_AGENT_NAMES = {
    "A": "Q&M Training Enquiry Assistant",
    "B": "Q&M Enrollment Pipeline Agent",
    "C": "Q&M Payment & Accounts Agent",
}

_BOX_W = 78


def _print_header(whatsapp_id: str, body: str, media: object) -> None:
    """Trace the start of a turn — query in, before any routing/agent work."""
    query_log.emit("\n┌─ TURN " + "─" * (_BOX_W - 8))
    query_log.emit(f"│ From    : {whatsapp_id or '(unknown)'}")
    if body:
        for i, line in enumerate(body.splitlines() or [""]):
            query_log.emit(f"│ Message : \"{line}\"" if i == 0 else f"│            {line}")
    else:
        query_log.emit("│ Message : (empty)")
    if media:
        query_log.emit("│ Media   : [attached]")


def _print_routing(label: str) -> None:
    """Trace the routing/agent-assignment line — which module (and agent, if
    any) is about to handle this turn. Any tool calls the agent makes trace
    live, right after this line, via crews/_base.py's tool wrapper."""
    query_log.emit("├─ ROUTING " + "─" * (_BOX_W - 11))
    query_log.emit(f"│ {label}")


def _print_reply(reply: str) -> None:
    """Trace the end of a turn — the exact reply text being sent back."""
    query_log.emit("├─ REPLY " + "─" * (_BOX_W - 9))
    for line in (reply or "(no reply)").splitlines() or ["(no reply)"]:
        query_log.emit(f"│ {line}")
    query_log.emit("└" + "─" * _BOX_W)


def _is_valid_phone(digits: str) -> bool:
    """True if the digit string looks like a real E.164 phone (not a LID)."""
    return bool(_PHONE_RE.match(digits))


def _is_registered(customer: dict) -> bool:
    """True when all four registration fields are present and phone is
    confirmed. Thin wrapper — the check itself lives in utils.is_registered()
    so module_a.py can reuse it (for the delegation-coworker gate) without a
    circular import against this module."""
    return is_registered(customer)


def _missing_fields(customer: dict) -> list[str]:
    """Registration fields still outstanding for this customer, in ask order."""
    missing = []
    if not customer.get("phone_confirmed"):
        missing.append("phone")
    if not customer.get("full_name"):
        missing.append("full_name")
    if not customer.get("nric"):
        missing.append("nric")
    if not customer.get("email"):
        missing.append("email")
    return missing


def _has_any_field(customer: dict) -> bool:
    """True once at least one registration field has been captured."""
    return bool(
        customer.get("phone_confirmed")
        or customer.get("full_name")
        or customer.get("nric")
        or customer.get("email")
    )


def _parse_piped_reply(body: str, customer: dict) -> dict | None:
    """Legacy structured shortcut, still recognised but never advertised.

    Accepted formats:
      4 fields  → phone | full name | NRIC | email   (all users)
      3 fields  → full name | NRIC | email            (@c.us only, phone already confirmed)

    Returns a dict with keys phone/full_name/nric/email, or None if not a match
    (in which case the caller falls back to free-text extraction).
    """
    parts = [p.strip() for p in body.split("|")]

    if len(parts) == 4:
        phone_raw = re.sub(r"\D", "", parts[0])
        if not _is_valid_phone(phone_raw):
            return None
        full_name, nric, email = parts[1], parts[2], parts[3]
    elif len(parts) == 3 and customer.get("phone_confirmed") and customer.get("phone"):
        # @c.us shortcut: phone is already confirmed, skip it in the form
        phone_raw = customer["phone"]
        full_name, nric, email = parts[0], parts[1], parts[2]
    else:
        return None

    full_name = full_name.strip()
    nric = nric.strip()
    email = email.strip()

    if not full_name or not nric or not email or "@" not in email:
        return None

    return {"phone": phone_raw, "full_name": full_name, "nric": nric, "email": email}


# Short decline/filler-only replies that are never a name, even though they
# pass every structural check below (single alphabetic word, right length).
# Checked against the whole remaining message, not word-by-word, so a real
# name that happens to contain one of these as a substring is unaffected.
_DECLINE_WORDS = {
    "no", "nope", "nah", "not", "never", "none", "later", "maybe",
    "skip", "cancel", "stop", "wait", "nothing", "no thanks", "no thank you",
    "not now", "not yet", "not really", "no not yet",
}


def _looks_like_decline(text: str) -> bool:
    """True if the whole message is just a short decline/filler utterance
    ('no', 'not now', 'nope'...) — never a name, and not worth an LLM
    name-extraction call either."""
    normalized = re.sub(r"[^\w\s]", "", (text or "")).strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized in _DECLINE_WORDS


def _looks_like_name(cleaned: str) -> bool:
    """Structural plausibility check — letters/spaces/apostrophes/hyphens
    only, 1-5 words, sane length. Shared by both the regex and LLM
    extraction paths so an LLM guess is held to the same bar."""
    if not cleaned or len(cleaned) > 60:
        return False
    if not re.match(r"^[A-Za-z][A-Za-z .'\-]*$", cleaned):
        return False
    words = cleaned.split()
    return 1 <= len(words) <= 5


def _extracted_from_source(candidate: str, source: str) -> bool:
    """True if every word of `candidate` actually appears (case-insensitive,
    whole-word) somewhere in `source`. The regex extraction path can only
    ever slice out a substring of the participant's own message, so it can
    never hallucinate a name that isn't there — the LLM fallback path has
    no such guarantee, and needs this check to close that gap (this is what
    catches an LLM inventing a plausible-looking name, e.g. "Not Moment",
    out of a message that names no one)."""
    source_words = {w.lower() for w in re.findall(r"[A-Za-z']+", source)}
    return all(w.lower() in source_words for w in candidate.split())


def _clean_name_remainder(text: str) -> str | None:
    """Extract a name only when the message has an explicit self-introduction
    cue (_NAME_INTRO_RE) — see that regex's comment for why. Returns None
    otherwise, deferring to the LLM path."""
    if _looks_like_decline(text):
        return None
    text = text.replace("’", "'")
    m = _NAME_INTRO_RE.search(text)
    if not m:
        return None
    # Stop at "and" so "my name is Tan Wei Ling and my email is ..." doesn't
    # swallow the next clause — the character class in _NAME_INTRO_RE allows
    # spaces/letters, so it wouldn't otherwise stop there on its own.
    candidate = re.split(r"\band\b", m.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
    # Cleanup pass on just the captured span (safe here — unlike running this
    # over the whole message, this text is already anchored to an actual
    # introduction) to trim trailing pleasantries, e.g. "I'm Tan Wei Ling
    # please" -> "Tan Wei Ling".
    cleaned = _NAME_FILLER_RE.sub(" ", candidate)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".!?")
    if not _looks_like_name(cleaned):
        return None
    if cleaned.islower() or cleaned.isupper():
        cleaned = cleaned.title()
    return cleaned


def _has_leftover_text(remainder: str) -> bool:
    """True if remainder still has non-filler alphabetic content worth an LLM
    look — avoids an LLM call on every turn of a chat that's just "hi"."""
    stripped = _NAME_FILLER_RE.sub(" ", remainder)
    stripped = re.sub(r"[^A-Za-z]", " ", stripped)
    return bool(stripped.strip())


def _llm_extract_name(body: str) -> str | None:
    """Ask the LLM to pull just a full name out of free text, if present.

    Scoped to name only — email/NRIC/phone always come from regex above so
    nothing sensitive is ever hallucinated.
    """
    try:
        from .crews._base import kickoff_agent

        out = kickoff_agent(
            role="Registration Assistant",
            goal="Extract a person's full name from a short WhatsApp message, if present.",
            backstory=(
                "You read a short registration reply from a new WhatsApp participant. "
                "The message may also mention a phone number, NRIC, or email — ignore those "
                "entirely, they are handled elsewhere. Only extract the person's full name."
            ),
            tools=[],
            task_description=(
                f'Message: "{body}"\n\n'
                "If the message states the participant's full name, reply with ONLY that name "
                "(no labels, no punctuation, no extra words). If no name is present, reply with "
                "exactly: NONE"
            ),
            expected_output="A person's full name, or the word NONE",
            max_iter=2,
        ).strip()
        if not out or out.upper() == "NONE":
            return None
        if not _looks_like_name(out) or not _extracted_from_source(out, body):
            log.warning("registration name-extraction returned an implausible name, discarding: %r", out)
            return None
        if out.islower() or out.isupper():
            out = out.title()
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("registration name-extraction agent failed: %s", e)
        return None


def _infer_course_interest(body: str) -> str | None:
    """Pull an explicit course reference straight out of the participant's
    own message — a C-code, or "course N" naming its position in the
    catalogue (e.g. "I interest in course 4") — independent of which module
    ends up handling the turn or whether any tool gets called this turn.
    Returns the resolved course name, or None if nothing recognisable is
    present."""
    text = body or ""

    m = _COURSE_CODE_IN_TEXT_RE.search(text)
    if m:
        course = repo.get_course_by_course_id(m.group(0).upper())
        if course:
            return course["name"]

    m = _COURSE_POSITION_RE.search(text)
    if m:
        all_courses = repo.list_courses()
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(all_courses):
            return all_courses[idx]["name"]

    return None


def _parse_free_text_reply(body: str) -> dict:
    """Pull whatever registration fields it can out of a natural free-text
    message, in any order, without requiring any special syntax."""
    remainder = body

    email_m = _EMAIL_RE.search(remainder)
    email = email_m.group(0) if email_m else None
    if email_m:
        remainder = remainder.replace(email_m.group(0), " ")

    nric_m = _NRIC_RE.search(remainder)
    nric = nric_m.group(0).upper() if nric_m else None
    if nric_m:
        remainder = remainder.replace(nric_m.group(0), " ")

    phone = None
    phone_m = _PHONE_TOKEN_RE.search(remainder)
    if phone_m:
        digits = re.sub(r"\D", "", phone_m.group(0))
        if _is_valid_phone(digits):
            phone = digits
            remainder = remainder.replace(phone_m.group(0), " ")

    full_name = _clean_name_remainder(remainder)
    if not full_name and not _looks_like_decline(remainder) and settings.openai_api_key and _has_leftover_text(remainder):
        full_name = _llm_extract_name(body)

    return {"phone": phone, "full_name": full_name, "nric": nric, "email": email}


def _extract_registration_fields(body: str, customer: dict) -> dict:
    """Best-effort extraction of phone/full_name/nric/email from a reply.

    Tries the legacy piped shortcut first (exact match only), then falls back
    to free-text extraction. Either way the result is a dict with all four
    keys, each either a found value or None.
    """
    piped = _parse_piped_reply(body, customer)
    if piped:
        return piped
    return _parse_free_text_reply(body)


def _format_missing(missing: list[str]) -> str:
    labels = [_FIELD_LABELS[f] for f in missing]
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _nric_near_miss(text: str) -> str | None:
    """First NRIC-shaped-but-invalid token in `text`, or None. Skips a token
    that also satisfies the strict pattern (a real NRIC, not a near-miss),
    and skips a token with no digits at all in its middle span (an ordinary
    word coincidentally matching the letter-count shape, e.g. "MENTIONED" —
    observed live triggering a false NRIC hint before this check existed;
    a genuine typo'd NRIC always has mostly digits, a real word never has
    any)."""
    for m in _NRIC_NEAR_MISS_RE.finditer(text or ""):
        token, middle = m.group(0), m.group(1)
        if _NRIC_RE.fullmatch(token):
            continue
        if not any(c.isdigit() for c in middle):
            continue
        return token.upper()
    return None


def _registration_prompt(missing: list[str], has_progress: bool, body: str = "") -> str:
    """Free-text ask for whatever registration fields are still missing —
    never shows a command/pipe format, just plain conversational English.

    If NRIC is still missing and `body` contained something NRIC-shaped but
    invalid (a likely typo — e.g. a capital 'I' where a '1' was meant), name
    it specifically instead of silently re-asking as if nothing had been
    said (same "acknowledge the specific invalid input" pattern as Task
    10.7's invalid-intake fix — observed live: a mistyped NRIC got silently
    dropped, with no indication anything was wrong with it). The hint is
    appended after the base template rather than replacing it, so the
    message still starts with one of the two prefixes
    _is_registration_prompt() recognises — the continuation flow depends on
    that prefix to know a registration reply is still expected next turn."""
    ask = _format_missing(missing)
    if not has_progress:
        base = (
            "Welcome to Q&M Dental Training! 👋\n\n"
            f"Before we continue, could you share your {ask}? "
            "Just reply in your own words, however's easiest for you."
        )
    else:
        base = (
            f"Thanks! I still need your {ask} to complete your registration — "
            "could you share that?"
        )
    if "nric" in missing:
        near_miss = _nric_near_miss(body)
        if near_miss:
            base += (
                f" (By the way, '{near_miss}' doesn't look like a valid NRIC — it should be a "
                f"letter, followed by exactly 7 digits, then a letter, e.g. S1234567A — could "
                f"you double-check that one specifically?)"
            )
    return base


def _advance_registration(whatsapp_id: str, body: str, customer: dict) -> dict:
    """Extract whatever registration fields this message contains, persist
    them, and report whether registration is now complete.

    Fields are saved incrementally (via repo.update_customer /
    set_customer_phone) so a customer who gives details across a couple of
    messages is never asked to repeat what they've already provided.

    Returns one of:
      {"status": "complete", "customer": {...}}
      {"status": "phone_collision"}
      {"status": "incomplete", "missing": [...], "customer": {...}}
    """
    extracted = _extract_registration_fields(body, customer)

    if extracted["phone"] and not customer.get("phone_confirmed"):
        try:
            customer = repo.set_customer_phone(whatsapp_id, extracted["phone"])
        except ValueError:
            return {"status": "phone_collision"}

    updates = {}
    if extracted["full_name"] and not customer.get("full_name"):
        updates["full_name"] = extracted["full_name"]
    if extracted["nric"] and not customer.get("nric"):
        updates["nric"] = extracted["nric"]
    if extracted["email"] and not customer.get("email"):
        updates["email"] = extracted["email"]
    if updates:
        customer = repo.update_customer(whatsapp_id, **updates)

    missing = _missing_fields(customer)
    if missing:
        return {"status": "incomplete", "missing": missing, "customer": customer}

    try:
        customer = repo.register_customer(
            whatsapp_id, customer["phone"], customer["full_name"],
            customer["nric"], customer["email"],
        )
    except ValueError:
        return {"status": "phone_collision"}

    return {"status": "complete", "customer": customer}


def _parse_sender(sender: object) -> tuple[str, str | None, str | None]:
    """Extract (whatsapp_id, phone_or_none, display_name_or_none) from the
    gateway's from-field.

    The gateway always sends:
        from: { chat_id: "<jid>", phone: "<e164>"|null, name: "<str>"|null }

    For direct API testing the legacy flat form is accepted too:
        from: "6591234567"   →   synthetic chat_id "6591234567@c.us"
    """
    if isinstance(sender, dict):
        whatsapp_id  = str(sender.get("chat_id") or "").strip()
        raw_phone    = sender.get("phone")
        display_name = sender.get("name") or None
    else:
        raw = str(sender or "").strip()
        digits = re.sub(r"\D", "", raw)
        whatsapp_id  = f"{digits}@c.us" if digits else raw
        raw_phone    = digits if digits else None
        display_name = None

    phone: str | None = None
    if raw_phone:
        digits = re.sub(r"\D", "", str(raw_phone))
        if _is_valid_phone(digits):
            phone = digits

    return whatsapp_id, phone, display_name


def _get_routing_id(payload: dict) -> str:
    """Return the whatsapp_id to use as the reply target (best-effort)."""
    sender = payload.get("from") or {}
    wid, phone, _ = _parse_sender(sender)
    return wid or re.sub(r"\D", "", str(phone or ""))


# ── Progressive registration (v1.1) ─────────────────────────────────────────
# Registration is no longer a blanket pre-routing gate: a customer needs only
# whatsapp_id (minimum registration) to reach Module A. Full-profile
# registration is only ever triggered when routing resolves to Module B
# (enrollment intent) — see handle_inbound() below.

def _is_registration_prompt(content: str) -> bool:
    """True if `content` is one of _registration_prompt()'s two static
    templates. Reliable as an "is a prompt still open" marker because that
    function is pure string formatting, never LLM output."""
    text = content or ""
    return text.startswith("Welcome to Q&M Dental Training!") or text.startswith("Thanks! I still need your")


def _awaiting_registration_reply(whatsapp_id: str) -> bool:
    """True if the last thing we sent this participant was a registration
    prompt — i.e. this inbound message is presumably their reply to it,
    regardless of how it would otherwise classify."""
    try:
        last = repo.recent_memory(whatsapp_id, limit=1)
    except Exception:  # noqa: BLE001
        return False
    if not last:
        return False
    turn = last[-1]
    return turn.get("role") == "assistant" and _is_registration_prompt(turn.get("content"))


def _resume_body(history: list[dict], fallback: str) -> str:
    """Find the participant's original enrollment-intent message that opened
    the still-open registration flow, by walking chat history backward past
    every (registration-prompt, reply) pair. Falls back to `fallback` (the
    current turn's body) if no earlier trigger message can be found —
    e.g. registration completed in the very first reply."""
    for i in range(len(history) - 1, -1, -1):
        turn = history[i]
        if turn.get("role") != "user":
            continue
        preceding = history[i - 1] if i > 0 else None
        if preceding and preceding.get("role") == "assistant" and _is_registration_prompt(preceding.get("content")):
            continue  # this user turn was itself a reply to a prompt — keep walking back
        return turn.get("content") or fallback
    return fallback


def _dispatch(payload: dict, decision: dict, module: str, whatsapp_id: str, phone: str | None) -> tuple[str, str, str]:
    """Run the routed module and return (reply, module, agent_name) — the one
    place every call site funnels through so the routing trace and the
    actual dispatch never drift apart. No command parsing exists
    (Requirement 7), so every message is free text and every module is
    Module A/B/C — never SYSTEM or ERROR, which only ever came from the
    now-removed command parser. The `module`/`agent_name` returned here are
    what the WhatsApp mock's "who answered" display (a dev-tool convenience,
    not a requirement) ultimately shows — returned as plain values, not a
    ContextVar, since the /webhook/whatsapp/sync route runs this via
    `run_in_threadpool()`: a ContextVar `.set()` inside that worker thread
    would NOT be visible back on the caller's context once the thread
    finishes (context.py already documents this exact class of failure from
    two unrelated past attempts) — a return value crosses that boundary
    correctly, a ContextVar write does not.

    Requirement 3 AC13: "DISAMBIGUATE" is a fourth possible decision from the
    router (not a real module) — a genuinely low-confidence classification
    with no deterministic signal to resolve it. No agent runs this turn; the
    menu is returned directly, and the pending flow is recorded the same way
    Module A/B record their own (Requirement 3 AC10), so router.py's Tier 0
    `_sticky_dispatch()` can match the participant's next reply against it."""
    if module == "DISAMBIGUATE":
        log.info("Routed: wid=%s phone=%s module=DISAMBIGUATE", whatsapp_id, phone)
        _print_routing("free text → disambiguation menu (low-confidence classification)")
        repo.set_conversation_state(whatsapp_id, "ROUTER", "awaiting_disambiguation")
        return router.DISAMBIGUATION_MENU, "ROUTER", "WhatsApp Intent Router"

    log.info("Routed: wid=%s phone=%s module=%s", whatsapp_id, phone, module)
    agent_name = _AGENT_NAMES.get(module, "?")
    _print_routing(f"free text → Module {module} (agent: {agent_name})")
    try:
        return _MODULES.get(module, module_a).run(payload, decision), module, agent_name
    except module_c.RerouteRequested as e:
        # Requirement 3 AC14 — Module C determined this isn't a payment
        # question and handed it back. Redirect once (a plain second call,
        # not a loop): if the redirected module also somehow raises, that
        # propagates up to process_and_reply()'s existing top-level
        # catch-all, which is the natural one-hop cap — no extra bookkeeping
        # needed for it.
        log.info(
            "Routed: wid=%s phone=%s module=C rerouted to %s (%s)",
            whatsapp_id, phone, e.candidate, e.reason,
        )
        candidate_agent = _AGENT_NAMES.get(e.candidate, "?")
        _print_routing(f"Module C rerouted → Module {e.candidate} (agent: {candidate_agent})")
        return _MODULES.get(e.candidate, module_a).run(payload, decision), e.candidate, candidate_agent


def _persist_memory(whatsapp_id: str, user_text: str, reply: str) -> None:
    """Append this turn's user/assistant messages to chat_memory
    (best-effort — a write failure here must never block the reply)."""
    try:
        if user_text:
            repo.add_memory(whatsapp_id, "user", user_text)
        if reply:
            repo.add_memory(whatsapp_id, "assistant", reply)
    except Exception as exc:  # noqa: BLE001
        log.warning("Memory write failed for wid=%s: %s", whatsapp_id, exc)


def _result(reply: str, module: str = "", agent: str = "") -> dict:
    """Build handle_inbound()'s return shape — a plain dict, not a
    ContextVar (see _dispatch()'s docstring for why: a ContextVar write
    doesn't survive the /webhook/whatsapp/sync route's run_in_threadpool()
    boundary). `module`/`agent` are only used by the WhatsApp mock's
    optional "who answered" display — whatsapp_client.send_whatsapp() and
    every other real consumer only ever reads `reply`."""
    return {"reply": reply, "module": module, "agent": agent}


# The four registration-gate deterministic replies below (phone collision,
# incomplete-profile prompt, declined-not-reprompting) are never composed by
# an agent — they're the Orchestrator's own text, gating Module B's
# enrollment path specifically (Requirement 2 AC4-AC8) — labelled "B" here
# to match how the query trace log already frames them ("free text → Module
# B (ENROLLMENT) — profile incomplete, prompting").
_REGISTRATION_AGENT = "Registration"


def handle_inbound(payload: dict) -> dict:
    """Route and dispatch one inbound message, returning
    {"reply": str, "module": str, "agent": str} — `module`/`agent` identify
    which module/agent actually composed `reply` (used only by the WhatsApp
    mock's optional "who answered" display; every other caller reads just
    `reply`).

    Steps
    ─────
    1.  Parse sender: extract whatsapp_id (stable), phone (maybe null), name.
    2.  Atomic upsert in customers: one row per whatsapp_id — this alone is
        minimum registration; nothing else is required to proceed.
    2a. Auto-confirm phone for @c.us accounts (gateway always resolves it).
    3.  Progressive registration (free text, no command syntax shown):
          - If a registration prompt is still open (our last message to this
            participant), treat this reply as a continuation: advance
            registration and, once complete, resume the original
            enrollment-intent message automatically (Requirement 2 AC8).
          - Otherwise, route normally. Only when routing resolves to Module B
            (enrollment intent) AND the profile is incomplete does this turn
            trigger the *first* registration prompt — Module A (enquiries)
            and Module C never gate on registration (Requirement 2 AC3,
            Requirement 3 AC7).
    4.  Route to Module A / B / C via the intent router.
    5.  Persist chat memory keyed by whatsapp_id (best-effort).
    6.  Return the reply (caller delivers `reply` via the gateway).
    """
    # ── 1. Parse sender ───────────────────────────────────────────────────────
    sender = payload.get("from") or {}
    whatsapp_id, gateway_phone, display_name = _parse_sender(sender)

    msg  = payload.get("message") or {}
    body = (msg.get("body", "") if isinstance(msg, dict) else "") or payload.get("text", "") or ""
    media = payload.get("media")

    _print_header(whatsapp_id, body, media)

    if not whatsapp_id:
        log.warning("Inbound message with no whatsapp_id — dropping")
        _print_routing("dropped — no whatsapp_id on inbound payload")
        _print_reply("")
        return _result("")

    # ── 2. Isolate customer (atomic upsert) — minimum registration ───────────
    customer = repo.get_or_create_customer(whatsapp_id, display_name=display_name)
    log.debug(
        "Customer resolved: wid=%s phone=%s registered=%s",
        whatsapp_id, customer.get("phone"), _is_registered(customer),
    )

    # ── 2a. Auto-confirm phone for @c.us accounts ─────────────────────────────
    if gateway_phone and not customer.get("phone_confirmed"):
        try:
            customer = repo.set_customer_phone(whatsapp_id, gateway_phone)
            log.info("Phone auto-confirmed from gateway: wid=%s phone=%s", whatsapp_id, gateway_phone)
        except ValueError as e:
            log.warning("Phone collision during auto-confirm: %s", e)

    # ── 2b. Deterministic course-interest capture (best-effort) ──────────────
    # Runs on every inbound message, independent of routing outcome — this is
    # what catches a message like "I interest in course 4" even when it gets
    # intercepted by the registration gate below and never reaches an agent
    # that could otherwise notice and save it (relying on the conversational
    # agent to proactively call 'Save Lead Data' for this proved unreliable
    # in testing: it answered fee/schedule questions correctly but essentially
    # never called the tool for it).
    try:
        course_interest = _infer_course_interest(body)
        if course_interest:
            # Reassign, not discard: `customer` is what _set_ctx() below
            # hands to every tool this turn via context.customer() — without
            # this, a message that both names a new course AND needs that
            # course resolved this same turn (e.g. a reminder request) would
            # only see the update starting next turn, since update_customer()
            # returns the fresh row but the caller previously never kept it.
            # update_customer_course_interest(), not a plain update_customer()
            # call — a message naming only a new course, with no date of its
            # own, must clear any course_date already on file from a
            # DIFFERENT, earlier course, or it survives the switch and gets
            # misread as belonging to this new one (Requirement 4 AC14).
            customer = repo.update_customer_course_interest(whatsapp_id, course_interest)
    except Exception as exc:  # noqa: BLE001
        log.warning("Course-interest capture failed for wid=%s: %s", whatsapp_id, exc)

    def _set_ctx(cust: dict) -> None:
        context.set_message({
            "whatsapp_id":  whatsapp_id,
            "phone":        cust.get("phone"),
            "display_name": cust.get("full_name") or display_name or cust.get("display_name"),
            "customer":     cust,
            "body":         body,
            "media":        media,
        })

    # ── 3. Registration continuation — is a prompt still open? ────────────────
    # No message is special-cased on its text (Requirement 7 AC2, no command
    # syntax) — a reply that supplies no new field just falls through to
    # normal routing below (same as any other no-progress reply), which is
    # what actually lets a participant escape the flow, not its raw text.
    # `prompt_was_open` is computed once and reused in step 4 too, so a
    # declined/no-progress reply can't get the identical prompt re-shown
    # there just because the Router re-classifies it as ENROLLMENT again.
    prompt_was_open = not _is_registered(customer) and _awaiting_registration_reply(whatsapp_id)
    if prompt_was_open:
        result = _advance_registration(whatsapp_id, body, customer)

        if result["status"] == "phone_collision":
            reply = (
                "That phone number is already linked to another account. "
                "Could you double-check it and share the correct one, along with "
                "the rest of your details?"
            )
            _print_routing("registration — phone collision (continuing flow)")
            _print_reply(reply)
            _persist_memory(whatsapp_id, body, reply)
            return _result(reply, "B", _REGISTRATION_AGENT)

        if result["status"] == "incomplete" and (
            result["customer"] != customer
            or ("nric" in result["missing"] and _nric_near_miss(body))
        ):
            # Genuine progress, OR a clearly-attempted-but-invalid NRIC — the
            # latter must NOT fall through to the "no progress, abandon"
            # branch below: it isn't unrelated content, it's a real attempt
            # that just doesn't validate, and deserves specific feedback
            # (via _registration_prompt's near-miss hint) rather than being
            # silently treated as if nothing had been said (observed live:
            # exactly this — a mistyped NRIC produced "No worries, no rush!"
            # with no indication anything was wrong with the attempt).
            customer = result["customer"]
            reply = _registration_prompt(result["missing"], has_progress=_has_any_field(customer), body=body)
            log.info("Unregistered user wid=%s — still missing %s", whatsapp_id, result["missing"])
            _print_routing("registration — awaiting more details (continuing flow)")
            _print_reply(reply)
            _persist_memory(whatsapp_id, body, reply)
            return _result(reply, "B", _REGISTRATION_AGENT)

        if result["status"] == "complete":
            # complete — resume the original enrollment-intent message
            customer = result["customer"]
            log.info(
                "Customer registered: wid=%s name=%s phone=%s",
                whatsapp_id, customer["full_name"], customer["phone"],
            )
            _set_ctx(customer)
            history = repo.recent_memory(whatsapp_id, limit=20)
            original_body = _resume_body(history, fallback=body)
            resumed_payload = {**payload, "message": {**msg, "body": original_body}}
            decision = router.route(resumed_payload)
            module = decision["module"]
            dispatched_reply, resolved_module, agent_name = _dispatch(
                resumed_payload, decision, module, whatsapp_id, customer.get("phone")
            )
            reply = _REG_CONFIRM.format(name=customer["full_name"]) + "\n\n" + dispatched_reply
            _print_reply(reply)
            _persist_memory(whatsapp_id, body, reply)
            return _result(reply, resolved_module, agent_name)

        # status == "incomplete" with NO new field captured — the participant
        # said something else (declining, asking a question, changing the
        # subject) rather than supplying registration details. Don't nag with
        # the same prompt again: abandon the flow and fall through to normal
        # routing below, exactly as if no prompt had been open. Module B still
        # can't be reached without a complete profile (step 4 re-checks that),
        # so this can't leak an unregistered participant into an enrolment.
        log.info("Registration flow abandoned (no progress) — routing normally: wid=%s", whatsapp_id)

    # ── 4. Normal routing ──────────────────────────────────────────────────────
    _set_ctx(customer)
    decision = router.route(payload)
    module   = decision["module"]

    # First time enrollment intent is seen against an incomplete profile —
    # trigger the registration prompt. Module A (ENQUIRY) and Module C
    # (PAYMENT) never reach this check, so they're never gated.
    if module == "B" and not _is_registered(customer) and prompt_was_open:
        # A prompt was already open and step 3 just found no progress on this
        # exact message — the Router re-classifying it as ENROLLMENT again
        # (e.g. recent history still mentions a course) must not re-trigger
        # the identical prompt. Answer once, softly, without touching Module B.
        reply = (
            "No worries — no rush! Feel free to keep asking about our courses "
            "and fees, and just let me know whenever you're ready to enrol."
        )
        _print_routing("free text → Module B (ENROLLMENT) — registration declined, not re-prompting")
        _print_reply(reply)
        _persist_memory(whatsapp_id, body, reply)
        return _result(reply, "B", _REGISTRATION_AGENT)

    if module == "B" and not _is_registered(customer):
        result = _advance_registration(whatsapp_id, body, customer)

        if result["status"] == "phone_collision":
            reply = (
                "That phone number is already linked to another account. "
                "Could you double-check it and share the correct one, along with "
                "the rest of your details?"
            )
            _print_routing("free text → Module B (ENROLLMENT) — phone collision")
            _print_reply(reply)
            _persist_memory(whatsapp_id, body, reply)
            return _result(reply, "B", _REGISTRATION_AGENT)

        if result["status"] == "incomplete":
            customer = result["customer"]
            reply = _registration_prompt(result["missing"], has_progress=_has_any_field(customer), body=body)
            log.info("Enrollment intent from unregistered wid=%s — missing %s", whatsapp_id, result["missing"])
            _print_routing("free text → Module B (ENROLLMENT) — profile incomplete, prompting")
            _print_reply(reply)
            _persist_memory(whatsapp_id, body, reply)
            return _result(reply, "B", _REGISTRATION_AGENT)

        # completed in this same message (e.g. the legacy piped shortcut) —
        # fall through and dispatch this same message normally, no resume needed
        customer = result["customer"]
        _set_ctx(customer)

    reply, resolved_module, agent_name = _dispatch(payload, decision, module, whatsapp_id, customer.get("phone"))
    _print_reply(reply)

    # ── 5. Persist chat memory (best-effort, never blocks the reply) ──────────
    _persist_memory(whatsapp_id, body, reply)

    return _result(reply, resolved_module, agent_name)


def process_and_reply(payload: dict) -> dict:
    """Handle an inbound message and deliver the reply via the WhatsApp gateway.

    This is the top-level entry point called by the webhook.  Errors are caught
    here so a bad payload never causes a 500 to the gateway. Returns
    handle_inbound()'s {"reply", "module", "agent"} dict unchanged — the
    gateway send below only ever needs `reply`.

    Routing: always use whatsapp_id (the raw JID from the gateway).
    ──────────────────────────────────────────────────────────────
    The gateway caches msg.from → msg.from so /send-reply resolves the correct
    JID.  @lid accounts must be sent to via their @lid JID — using phone@c.us
    silently fails because @lid accounts do not live on the @c.us server.
    Phone is used only for database lookups and customer verification.
    """
    routing_id = _get_routing_id(payload)
    try:
        result = handle_inbound(payload)
    except Exception as exc:  # noqa: BLE001
        log.exception("Unhandled error in handle_inbound: %s", exc)
        result = _result(
            "Sorry, something went wrong on our side. Please try again in a moment."
        )

    if routing_id and result.get("reply"):
        send_whatsapp(routing_id, result["reply"])

    return result

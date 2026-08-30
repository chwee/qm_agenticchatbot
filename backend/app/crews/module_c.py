"""Module C — Payment Verification & Accounts (Multimodal Verification Agent).

Proposal Section 5.5. The most autonomous module. Any message carrying a
screenshot is verified by the vision model and settled deterministically
(auto-confirm + receipt, flag staff, or ask for a resend) — the money path
never depends on the conversational LLM, and never depended on any command
syntax either (it's triggered by the image attachment itself). An invoice
may need more than one screenshot — a
SkillsFuture claim confirmation, a PayNow transfer confirmation, or both,
depending on the course's fee/subsidy split — since WhatsApp always delivers
one image per message, so proofs are verified and recorded one at a time as
they arrive; the invoice is complete once all confirmed proofs sum to the full
course fee. Free-text payment intent without an image is handled by the
Module C agent, which explains how to submit proof, reports the outstanding
balance, or resends receipts.
"""
from __future__ import annotations

import logging
import re

from .. import context
from .. import repositories as repo
from ..config import settings
from ..dialogue import PendingQuestion, Slots, TurnResult, log_dialogue_state
from ..services import payments
from ..tools import MODULE_C_TOOLS
from ._base import kickoff_agent
from .router import _looks_like_payment_intent

log = logging.getLogger(__name__)


class RerouteRequested(Exception):
    """Requirement 3 AC14: raised when this module determines the message
    routed to it isn't actually about payment. Module C is the one module
    with zero delegation coworkers (Requirement 12 explicitly scopes
    delegation to Module A <-> Module B only), so unlike A/B it has no way
    to hand off part of a task and compose a combined reply — it can only
    say "wrong module entirely" and step aside. orchestrator._dispatch()
    catches this and redirects to `candidate` once (capped — if the second
    module also somehow fails, the exception propagates to
    process_and_reply()'s existing top-level catch-all)."""
    def __init__(self, candidate: str, reason: str = ""):
        self.candidate = candidate
        self.reason = reason
        super().__init__(f"reroute to Module {candidate}: {reason}")


# Requirement 3 AC14 — the agent is instructed to reply with EXACTLY this
# (nothing else) when the message isn't actually about payment; matched
# against the FULL reply, so it can't accidentally fire on a normal payment
# answer that merely happens to mention a course or an enrollment in passing.
_REROUTE_RE = re.compile(r"^REROUTE:([AB])$")

# Pulls an invoice number out of a free-text caption, e.g. "pay for INV-2026-0861".
_INVOICE_RE = re.compile(r"\bINV-\d{4}-\d{4}\b", re.IGNORECASE)

# ── General "how/what to pay" question — answered deterministically ─────────
# A bare question about payment METHODS (not a specific invoice/balance) is
# answered without ever invoking the LLM agent at all. Prompt-only compliance
# was tried first (an explicit backstory rule: "answer generically, do not
# name any course mentioned earlier for an unrelated reason") and proved
# unreliable — observed live: "what is the course payment methods?", asked
# right after an unrelated reminder-flow exchange about a DIFFERENT course,
# still opened with "For the [that unrelated course]..." even with the rule
# in place. The conversation history's most recent course mention is simply
# too salient for the model to reliably ignore on instruction alone — the
# same "prompt-only compliance for a consequential decision proved
# unreliable" lesson already applied everywhere else in this codebase
# (module_a.py/module_b.py's markers, router.py's overrides). A deterministic
# short-circuit removes the risk entirely rather than asking the model not
# to take it.
_PAYMENT_METHODS_QUESTION_RE = re.compile(
    r"\bpayment\s+methods?\b|\bhow\s+(?:do|can|to)\s+(?:i|we|you)\s+pay\b|"
    r"\bhow\s+to\s+pay\b|\bways?\s+to\s+pay\b|\bhow\s+(?:do\s+you|does\s+this)\s+accept\s+payment\b",
    re.IGNORECASE,
)
# Any of these means the question is actually about a SPECIFIC invoice/
# balance/course, not payment methods in general — the LLM agent handles it
# instead, with the full conversation history it needs for that.
_SPECIFIC_PAYMENT_SIGNAL_RE = re.compile(
    r"\bINV-\d{4}-\d{4}\b|\b[Cc]\d{4}\b|\bowe\b|\bbalance\b|\bstill\s+(?:owe|need|pay)\b|"
    r"\bmy\s+(?:invoice|balance|enrollment|enrolment|course)\b",
    re.IGNORECASE,
)


def _general_payment_methods_reply(body: str) -> str | None:
    """Deterministic answer for a bare 'what payment methods do you accept' /
    'how do I pay' question — None if `body` doesn't have that shape, or
    names a specific invoice/course/balance (in which case the LLM agent
    below handles it, since that needs the full conversation context this
    function deliberately never looks at)."""
    body = body or ""
    if not _PAYMENT_METHODS_QUESTION_RE.search(body):
        return None
    if _SPECIFIC_PAYMENT_SIGNAL_RE.search(body):
        return None
    return (
        "You can pay by attaching either a PayNow transfer confirmation screenshot, a "
        "SkillsFuture claim confirmation screenshot, or both — whichever your invoice needs "
        "— right here in this chat, mentioning your invoice number in the same message.\n\n"
        "If you're not sure which one your invoice needs, or don't have your invoice number "
        "handy, just ask and I'll look it up for you."
    )

# Deterministic list-position disambiguation. A participant with more than
# one enrollment (especially two in the SAME course on different intake
# dates) needs "item 2" / "list 2" / "the second one" resolved to the exact
# invoice they mean — never to whichever enrollment a tool defaults to
# ("latest") if that isn't the one they meant. See _list_reference_directive().
_LIST_MARKER_RE = re.compile(r"(?m)^\**\s*(\d+)\.\**\s+")
_LIST_REF_RE = re.compile(
    r"\b(?:item|list|course|number|no\.?|option)\s*#?\s*(\d+)\b"
    r"|\b(\d+)(?:st|nd|rd|th)\b"
    r"|\bthe\s+(first|second|third|fourth|fifth|sixth)\b",
    re.IGNORECASE,
)
_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6}


def _parse_enrollment_list(content: str) -> dict[int, str]:
    """Parse a numbered enrollment list — the fixed format
    services/enrollment.py: all_status_text() always produces for more than
    one enrollment — out of a chat message, returning {position: invoice_no}.
    Tolerant of light reformatting (bold markers etc.) since it's parsing an
    LLM's relay of the tool output, not the tool's raw output directly.
    Empty if no recognisable numbered list with invoice numbers is present."""
    markers = list(_LIST_MARKER_RE.finditer(content))
    if not markers:
        return {}
    result: dict[int, str] = {}
    for i, m in enumerate(markers):
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(content)
        inv_m = _INVOICE_RE.search(content[start:end])
        if inv_m:
            try:
                result[int(m.group(1))] = inv_m.group(0).upper()
            except ValueError:
                continue
    return result


def _find_enrollment_list(history: list[dict]) -> dict[int, str]:
    """Most recent assistant turn in history containing a numbered
    enrollment list, parsed — empty if none does."""
    for turn in reversed(history):
        if turn.get("role") != "assistant":
            continue
        parsed = _parse_enrollment_list(turn.get("content") or "")
        if parsed:
            return parsed
    return {}


def _extract_list_position(body: str) -> int | None:
    m = _LIST_REF_RE.search(body)
    if not m:
        return None
    if m.group(1):
        return int(m.group(1))
    if m.group(2):
        return int(m.group(2))
    if m.group(3):
        return _ORDINAL_WORDS.get(m.group(3).lower())
    return None


def _list_reference_directive(body: str, history: list[dict]) -> str:
    """If the new message refers to a specific enrollment by list position
    ('item 2', 'the second one') and a numbered enrollment list already
    exists in chat history, deterministically resolve the exact invoice
    number and inject it as an authoritative directive. This is what
    prevents the agent from calling a tool that silently defaults to the
    most recent enrollment instead of the one actually referenced — the
    exact reported failure mode ('pay for course list 2' kept resolving to
    a different, wrong invoice). Returns "" if there's nothing to resolve —
    the agent falls back to its own judgement (e.g. a date-based reference
    like 'the one on 11-12 Aug', which this regex doesn't cover) in that case."""
    position = _extract_list_position(body)
    if position is None:
        return ""
    invoice_no = _find_enrollment_list(history).get(position)
    if not invoice_no:
        return ""
    return (
        f"\nSYSTEM DIRECTIVE (authoritative — not written by the participant): they are "
        f"referring to item {position} from the enrollment list shown earlier in this "
        f"conversation, which is invoice {invoice_no}. Use exactly this invoice number for "
        f"whatever they're asking — e.g. pass invoice_no='{invoice_no}' to 'Payment Balance', "
        f"or state this invoice number if they're asking how/where to pay. Do not call a tool "
        f"that would default to a different enrollment, and do not use any other invoice "
        f"number this turn.\n"
    )


def _fallback_reply() -> str:
    return (
        "To submit payment, just attach your PayNow transfer confirmation and/or your "
        "SkillsFuture claim confirmation (whichever your invoice needs), and mention your "
        "invoice number in the same message.\n\n"
        "If your invoice needs both, send them as two separate messages — each is "
        "verified and receipted on its own.\n"
        "Your invoice number is on the invoice email sent during enrollment — just ask if "
        "you'd like it resent, or ask about your enrollment status."
    )


def _run_agent(body: str) -> str:
    wid = context.whatsapp_id()
    history = repo.recent_memory(wid, limit=8)
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history) or "(no prior messages)"
    list_directive = _list_reference_directive(body, history)

    raw = kickoff_agent(
        role="Q&M Payment & Accounts Agent",
        goal="Help participants submit payment proof and resend receipts.",
        backstory=(
            "You handle payments for Q&M Dental Group training. An invoice can require ONE or "
            "TWO kinds of proof depending on the course's fee/subsidy split: a SkillsFuture "
            "claim confirmation (from the MySkillsFuture portal), a PayNow transfer "
            "confirmation, or both. Each is sent as its own screenshot — WhatsApp delivers one "
            "image per message, so if both are needed they arrive as two separate messages, "
            "verified and receipted independently; the invoice is only complete once "
            "everything needed has been confirmed. If a participant says they have paid but "
            "did not attach a screenshot, ask them to resend with the screenshot. If they ask "
            "what they still owe, use the 'Payment Balance' tool rather than guessing — it "
            "tells you exactly what's still needed. If they don't know or didn't give their "
            "invoice number, don't just ask them to go look it up — call 'My Enrollments' "
            "or 'Resend Invoice' yourself to find/resend it, then tell them the invoice number "
            "so they can attach their screenshot mentioning it. A participant can have more "
            "than one enrollment, including more than one in the SAME course on different "
            "intake dates — 'My Enrollments' returns all of them, and if they've referred to a "
            "specific one (by list position like 'item 2', or by date), you must match that "
            "exact one rather than assuming the most recent — see the task instructions for how "
            "to resolve that reference. If they want a receipt, use the Resend Receipt tool (it "
            "resends every receipt for their enrollment, since there may be more than one).\n\n"
            "This is a free-text, conversational reply — write it the way a helpful human "
            "staff member would text back, not a rigid printout. Any code, invoice number, "
            "or amount a tool returns must be carried over exactly — never round, reword, or "
            "guess one. Keep replies short for WhatsApp.\n\n"
            "IMPORTANT — a GENERAL 'how do I pay' / 'what payment methods do you accept' "
            "question (no specific invoice, course, or balance being asked about) is answered "
            "GENERICALLY, directly, without calling any tool: payment is submitted by attaching "
            "either a PayNow transfer confirmation screenshot, a SkillsFuture claim confirmation "
            "screenshot, or both — whichever their invoice needs — mentioning their invoice "
            "number in the same message. Do NOT call 'My Enrollments' or 'Resend Invoice' just "
            "to answer this general question — those are for resolving a SPECIFIC invoice, not "
            "for explaining how payment works in general, and calling them here produces a "
            "confusing answer about an enrollment the participant never asked about. Do NOT name "
            "any particular course in this generic answer either, even one mentioned earlier in "
            "the conversation for a completely unrelated reason (browsing the catalogue, asking "
            "about a reminder) — a bare mention like that does NOT make this new message about "
            "that course's payment specifically, and naming it anyway is just as misleading as "
            "calling the tools (observed live: 'what is the course payment methods?', asked "
            "right after an unrelated reminder request, first triggered wrongly-scoped tool "
            "calls claiming 'no enrollment' and resending a different invoice with no "
            "explanation; even after that was fixed, the answer still opened with 'For the "
            "[unrelated course]...' as if the question had been about that course all along, "
            "which is equally confusing). Only mention a specific course/invoice when the "
            "participant's OWN new message actually names one, or once they go on to ask about "
            "THEIR balance, THEIR invoice number, or say they're ready to pay a specific one.\n\n"
            "IMPORTANT — if this genuinely isn't about payment:\n"
            "You have no tools for general course/fee/schedule questions or for creating/"
            "changing an enrollment. If — and ONLY if — the message has NO payment framing "
            "whatsoever (e.g. 'what courses do you offer', 'I want to enrol in course 2', "
            "'tell me about SkillsFuture eligibility') do NOT attempt to answer it yourself or "
            "guess — reply with EXACTLY 'REROUTE:A' (a course/enquiry question) or EXACTLY "
            "'REROUTE:B' (an enrollment question), nothing else, no punctuation, no explanation.\n"
            "Do NOT reroute a message that asks HOW/WHERE to pay, about payment methods, "
            "outstanding balance, receipts, or proof of payment — those are always yours to "
            "handle, even when the wording also mentions 'the course' or 'my enrollment' in "
            "passing (e.g. 'how to pay the course', 'where do I pay for my enrollment' are BOTH "
            "payment questions — never reroute either of those). When genuinely unsure whether "
            "something is a payment question, answer it yourself (using your tools, or asking a "
            "short clarifying question) rather than rerouting — rerouting is only for a message "
            "that is unmistakably NOT about payment at all."
        ),
        tools=MODULE_C_TOOLS,
        task_description=(
            "Recent conversation with this participant (oldest first) — check it for an "
            "invoice number or enrollment already mentioned before assuming it's missing. This "
            "history may include a NUMBERED LIST of the participant's enrollments shown earlier "
            "in the conversation (it doesn't matter whether you or another part of the system "
            "showed it) — e.g. '1. Infection Control... Invoice: INV-2026-0921' and "
            "'2. Infection Control... Invoice: INV-2026-0922':\n"
            f"{convo}\n\n"
            f"New message from the participant, about payment:\n\"{body}\"\n"
            f"{list_directive}\n"
            "There is no screenshot attached to this message. Resolve what you can from the "
            "conversation above, then act:\n"
            "  - STEP 0: if a 'SYSTEM DIRECTIVE' line appears above, it already deterministically "
            "resolved which invoice the participant means — use exactly that invoice number, "
            "skip straight to acting on it, and do not call a tool that might return a "
            "different enrollment.\n"
            "  - Otherwise, if this is a GENERAL question about how to pay or what payment "
            "methods/proof types are accepted — no specific invoice, course, or balance being "
            "asked about — answer it directly per your role instructions' 'how do I pay' rule, "
            "with NO tool call at all. Check this BEFORE assuming any course mentioned earlier "
            "in the conversation is what this new message is about.\n"
            "  - Otherwise, if the participant refers to a specific enrollment by list position "
            "(e.g. 'item 2', 'course list 2', 'the second one') or by a course name plus intake "
            "date, and a numbered list matching that reference already exists in the history "
            "above (no SYSTEM DIRECTIVE resolved it, e.g. because they used a date instead of a "
            "position): read the EXACT invoice number for that specific item directly from that "
            "list text yourself. Do NOT call a tool and do NOT use whatever invoice number a "
            "tool most recently returned if it doesn't match the item they actually asked "
            "for — a participant with two enrollments in the same course on different dates "
            "must get the invoice for the SPECIFIC one they named, never just 'the latest'.\n"
            "  - If they want a receipt, resend it (there may be more than one).\n"
            "  - If they ask how much they still owe / what's left to pay, call 'Payment "
            "Balance' — pass invoice_no explicitly whenever you already know it (from a SYSTEM "
            "DIRECTIVE, from resolving a list reference yourself, or already stated in this "
            "conversation); only leave it blank when no specific invoice is known or implied. "
            "Report exactly what it says (it may be a SkillsFuture claim amount, a PayNow "
            "amount, both, or 'fully paid').\n"
            "  - If they're asking about paying but the invoice number isn't known from this "
            "message, the history, or the rule above, look it up yourself ('My Enrollments' / "
            "'Resend Invoice') rather than telling them to go find it — if 'My Enrollments' "
            "returns more than one matching enrollment, ask the participant to confirm which "
            "one (naming the dates) rather than guessing; otherwise tell them the invoice "
            "number and ask them to attach the relevant screenshot(s), mentioning it.\n"
            "  - Only ask a clarifying question if you truly cannot resolve their intent "
            "(e.g. no enrollment on record at all).\n"
            "Return ONLY the WhatsApp reply text."
        ),
        expected_output=(
            "A short, conversational WhatsApp reply about payment submission or receipt, with "
            "any invoice number/amount carried over exactly from a tool's output — OR, if the "
            "message isn't actually about payment, exactly 'REROUTE:A' or 'REROUTE:B'."
        ),
    )

    m = _REROUTE_RE.match(raw.strip())
    if m:
        # Structural backstop, not just prompt wording (this codebase's
        # established pattern — prompt-only compliance for a decision this
        # consequential has repeatedly proven unreliable elsewhere). Found
        # live: with no enrollment on file yet, the agent rerouted genuine
        # payment questions ("how to pay the course", "where can I pay",
        # "can I get a receipt", "is my payment confirmed") — its own
        # reasoning after seeing 'My Enrollments' return nothing apparently
        # outweighed the prompt's explicit counter-examples naming those
        # exact phrases. _looks_like_payment_intent() (the same check
        # router.py's Tier 1 already trusts for this) is authoritative here:
        # if the participant's OWN message shows a real payment signal, the
        # marker is discarded as a mistaken self-assessment and answered
        # with the canned payment-help reply instead of rerouting away from
        # the module that's supposed to own exactly this question.
        if _looks_like_payment_intent(body):
            log.warning(
                "Module C tried to reroute a message with its own payment signal — "
                "discarding REROUTE:%s: %r", m.group(1), body,
            )
            return _fallback_reply()
        raise RerouteRequested(m.group(1), reason=f"not a payment question: {body!r}")

    # DIALOGUE_STATE_REDESIGN.md phase 1 — observational only. Unlike
    # module_a.py/module_b.py, Module C never calls set_conversation_state()
    # at all: a documented gap (see docs/DIALOGUE_STATE_REDESIGN.md), not
    # something this phase changes — giving Module C real ownership here
    # would be an actual routing behaviour change (the Router's Tier 0
    # sticky dispatch would start handing subsequent turns back to Module C
    # when it doesn't today), which is out of scope for a dual-write. This
    # only logs what WOULD be asserted if this flow were ever given
    # ownership, so later phases have real data on how often that gap
    # matters — it deliberately never calls set_conversation_state.
    inv_m = _INVOICE_RE.search(raw) or _INVOICE_RE.search(body)
    log_dialogue_state(
        log, wid, PendingQuestion.PAYMENT_PROOF,
        Slots(invoice_no=inv_m.group(0).upper() if inv_m else None),
    )
    return raw


def run(payload: dict, route: dict) -> TurnResult:
    # phase 4: conforms to the same TurnResult contract as module_a/b for
    # orchestrator._dispatch() — but, unlike those two, Module C is
    # deliberately given NO real pending-question ownership here (every
    # return below defaults to pending=NONE). That's unchanged from before
    # this phase: Module C has never called set_conversation_state() at all
    # (see docs/DIALOGUE_STATE_REDESIGN.md's "still open" notes) — giving it
    # one now would be an actual routing behaviour change (Tier 0 sticky
    # dispatch would start handing subsequent turns back to Module C), not
    # a mechanical relocation of an existing write, so it's left for a
    # separate, explicit decision rather than folded into this refactor.
    data, mimetype = context.media()
    wid   = context.whatsapp_id()
    phone = context.phone()
    body  = (payload.get("message") or {}).get("body", "") or ""

    # Pull an invoice number out of the message text if it's mentioned
    # (e.g. "pay for INV-2026-0861") — never required, just used when present.
    invoice_no: str | None = None
    m = _INVOICE_RE.search(body)
    if m:
        invoice_no = m.group(0).upper()

    # Any screenshot present -> verify + settle (the multimodal money path).
    # Unaffected by command removal (Requirement 7) — this is triggered by
    # the image attachment itself, never by caption text. process_payment()
    # falls back to the participant's own latest outstanding invoice when
    # none is stated anywhere, so this still completes even with a bare
    # screenshot and no caption at all.
    if data:
        reply = payments.process_payment(wid, phone, data, mimetype, invoice_no)
        return TurnResult(reply=reply, module="C")

    # A bare "what payment methods / how do I pay" question — deterministic,
    # bypasses the LLM entirely (see _general_payment_methods_reply()'s own
    # comment for why prompt-only compliance proved unreliable here).
    general_reply = _general_payment_methods_reply(body)
    if general_reply is not None:
        return TurnResult(reply=general_reply, module="C")

    # Free-text payment intent, no image — receipt resends, "what do I still
    # owe", and "I paid but forgot to attach the screenshot" are all handled
    # conversationally by the agent below (or the canned fallback with no
    # API key); there is no deterministic /receipt or /pay-no-image shortcut
    # any more (Requirement 7).
    if not settings.openai_api_key:
        return TurnResult(reply=_fallback_reply(), module="C")
    try:
        return TurnResult(reply=_run_agent(body), module="C")
    except RerouteRequested:
        # Requirement 3 AC14 — never swallow this into the generic fallback;
        # orchestrator._dispatch() is what actually redirects the turn.
        raise
    except Exception as e:  # noqa: BLE001
        log.error("Module C agent failed: %s", e)
        return TurnResult(reply=_fallback_reply(), module="C")

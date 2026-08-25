"""Validates a candidate prompt fix for the module_b_enroll_happy_path defect
BEFORE touching app/crews/module_b.py.

Reimplements _run_agent() locally with one addition inserted into the ENROLL
branch of the task_description: an explicit, mechanical rule for resolving a
bare confirmation ("intake 2") against the SH-code list already shown in
conversation history, plus a worked example, plus an explicit instruction not
to re-call 'Course Schedule' when that mapping is already available. Nothing
else in the prompt is changed. Runs the same 5-trial methodology as
debug_module_b_repeat.py so the failure rate is directly comparable.

This script imports shared pieces (kickoff_agent, MODULE_B_TOOLS,
COMMAND_REFERENCE, course_short_id) from the real app modules - it does not
duplicate business logic, only the prompt text under test. No file in app/ is
modified.
"""
from __future__ import annotations

import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from client import SyncClient
from identity import new_identity

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import context, database, repositories as repo  # noqa: E402
from app.commands import COMMAND_REFERENCE  # noqa: E402
from app.crews._base import kickoff_agent  # noqa: E402
from app.tools import MODULE_B_TOOLS  # noqa: E402
from app.utils import course_short_id  # noqa: E402

N_TRIALS = 5
database.init_db()

# ── candidate ENROLL-branch addition (inserted verbatim into task_description) ──
_CANDIDATE_ENROLL_ADDITION = (
    "    - Resolving a bare confirmation: if the participant's new message is "
    "ONLY a bare item number, ordinal, or SH-code with no other content (e.g. "
    "'2', 'intake 2', 'the second one', 'SH2604'), this ALWAYS refers to the "
    "numbered intake list YOU most recently sent in the conversation history "
    "above. Do NOT call 'Course Schedule' again in this case - you already "
    "have the mapping written in your own previous message in the history. "
    "Find that list, map the number/ordinal to its SH-code, and treat that "
    "SH-code as the participant's chosen intake for the rest of this turn.\n"
    "      Worked example: history shows your own previous reply contained "
    "'1. [SH2603] 14-15 Jul 2026' and '2. [SH2604] 11-12 Aug 2026', and the "
    "participant's new message is 'intake 2'. The chosen intake is SH2604. "
    "Proceed immediately, in this same turn, to call 'Validate Enrollment' "
    "then 'Enroll Participant' with sh_code='SH2604' - do NOT reply with the "
    "intake list again, and do NOT call 'Course Schedule' again.\n"
)


def _run_agent_candidate(body: str) -> str:
    customer = context.customer()
    profile_name = customer.get("full_name") or ""
    profile_nric = customer.get("nric") or ""
    profile_email = customer.get("email") or ""

    all_courses = repo.list_courses()
    course_list = "\n".join(
        f"  {i}. [{c.get('course_id') or course_short_id(c['id'])}] {c['name']}"
        for i, c in enumerate(all_courses, 1)
    ) or "  (none available)"

    wid = context.whatsapp_id()
    history = repo.recent_memory(wid, limit=8)
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history) or "(no prior messages)"

    return kickoff_agent(
        role="Q&M Enrollment Pipeline Agent",
        goal="Enroll participants accurately or retrieve their enrollment status / invoice.",
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
            "Never fabricate invoice numbers or confirmations — always use your tools.\n\n"
            "This is a free-text, conversational reply — write it the way a helpful human "
            "staff member would text back, not a rigid printout.\n\n"
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
            "tool's full nested numbered/bulleted layout with an arrow (→) and a separate "
            "/enroll line for every single intake — summarise naturally (course names, fees, "
            "and that intakes are available) and only give SH-codes / an /enroll line once "
            "they've settled on one specific course and intake. Never invent a placeholder "
            "like 'Your Name|Your NRIC|Your Email' in an /enroll line — use the participant's "
            "real details exactly as the tool returned them, or leave the example out.\n\n"
            + COMMAND_REFERENCE
        ),
        tools=MODULE_B_TOOLS,
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
            f"New message from the participant: \"{body}\"\n\n"
            "Determine the intent and act:\n"
            "  ENROLL  — identify the course (name, C-code, or number from list above) from THIS "
            "message or the conversation history. Then check the intake: a SPECIFIC intake "
            "(SH-code, or item number from an intake list already shown) must be named or chosen "
            "by the participant — either in this message, or by confirming/selecting one you "
            "already offered earlier in the conversation. This is a hard requirement: you must "
            "NEVER call 'Enroll Participant' while the intake is still unspecified, even though "
            "the tool would silently default to the next available date if you omitted it — that "
            "default is for the /enroll command shortcut, not for this conversational flow.\n"
            + _CANDIDATE_ENROLL_ADDITION +
            "    - If the intake is not yet known: call 'Course Schedule' for the resolved "
            "course, name the course in your reply (so the participant can correct you if it's "
            "the wrong one), list the intake options, and ask them to pick one. Stop there for "
            "this turn — do not enroll yet.\n"
            "    - Once both the course and a specific intake are established (this message or "
            "conversation history), call 'Validate Enrollment' (pass only course — name/NRIC/"
            "email auto-fill). If VALID, call 'Enroll Participant' yourself passing sh_code "
            "(preferred) or schedule_no — do this directly, do NOT ask the participant to type "
            "the /enroll command themselves once you have both pieces.\n"
            "  STATUS  — if they're asking about ALL their courses, a specific named course, or "
            "just say 'my enrollments' generally, call 'My Enrollments' (pass the course name/"
            "code if one was named, blank for all) — it returns every enrollment on record. "
            "This is the tool for 'status of all courses I enrolled', not 'List Courses' or "
            "'Course Schedule', which only describe the catalog, not what THIS participant is "
            "enrolled in. Use plain 'Enrollment Status' only for a quick single/latest check.\n"
            "  INVOICE — call 'Resend Invoice'.\n\n"
            "Reply in your own natural words, but keep every code, invoice number, amount, "
            "and date exactly as the tool returned it — never round, reword, or guess them. "
            "Return ONLY the message text to send back on WhatsApp."
        ),
        expected_output=(
            "A short, conversational WhatsApp reply covering the enrollment confirmation, "
            "schedule options, status, or invoice details — with all codes/numbers/dates "
            "carried over exactly from the tool output."
        ),
    )


client = SyncClient()
results = []

for i in range(1, N_TRIALS + 1):
    chat_id, phone, name = new_identity(f"promptfix{i}")
    reg_body = f"{name} | S1234567A | eval-{phone}@example.com"
    client.send(chat_id, phone, name, reg_body)

    turn1 = "I want to enroll in the infection control course"
    reply1 = client.send(chat_id, phone, name, turn1)

    # turn 2 goes through the CANDIDATE prompt, in-process (not via HTTP,
    # since the live backend still runs the unmodified module_b.py).
    customer = repo.get_or_create_customer(chat_id, display_name=name)
    context.set_message({
        "whatsapp_id": chat_id,
        "phone": phone,
        "display_name": customer.get("full_name"),
        "customer": customer,
        "body": "intake 2",
        "media": None,
    })
    reply2 = _run_agent_candidate("intake 2")

    enrolled = len(repo.enrollments_by_whatsapp_id(chat_id)) > 0
    results.append(enrolled)

    print(f"\n{'='*70}\nTRIAL {i}  chat_id={chat_id}")
    print(f"  turn1 reply (first 100 chars): {reply1[:100]!r}")
    print(f"  turn2 reply (first 150 chars): {reply2[:150]!r}")
    print(f"  ENROLLED (ground truth from DB): {enrolled}")

client.close()

n_success = sum(results)
print(f"\n{'='*70}")
print(f"CANDIDATE PROMPT RESULT: {n_success}/{N_TRIALS} trials correctly resolved 'intake 2' into a real enrollment")
print(f"Observed failure rate: {(N_TRIALS - n_success) / N_TRIALS:.0%}  (baseline was 4/5 = 80%)")

"""Lead capture, escalation and nurture-follow-up services (Module A)."""
from __future__ import annotations

import logging

from .. import repositories as repo
from ..config import settings
from ..email_service import send_email
from . import courses as courses_svc

log = logging.getLogger(__name__)


def record_enquiry_activity(whatsapp_id: str) -> None:
    """Mark the lead/customer record as active on every enquiry — regardless
    of registration stage (minimum or full) or whether contact details were
    explicitly volunteered (that's still save_lead()'s job). Writes to
    `customers` (the authoritative identity table), never the deprecated
    `leads` table save_lead() targets.
    """
    repo.mark_customer_enquiry(whatsapp_id)


def save_lead(phone: str, whatsapp_id: str = "", **fields) -> dict:
    """Create/update a lead and mark them awaiting response for nurturing.

    Recognised fields: name, nric, dob, email, preferred_course, course_date.

    Writes to the legacy `leads` table (phone-keyed) as before — the
    `/admin/leads` dashboard still reads from it — and also mirrors
    `email`/`preferred_course`/`course_date` onto the authoritative
    `customers` table (whatsapp_id-keyed), since that's what
    customers_due_for_followup() actually queries; without this, a captured
    lead's course interest and contact email never reached the record the
    follow-up scheduler reads, so it silently never got followed up.
    `course_date` mirroring (Requirement 4 AC13) is what lets a reminder's
    intake-completeness be checked back reliably — without it, `customers`
    had no column at all to read a resolved intake back from, so a check
    against it could never see a save as complete no matter how many times
    the tool was called (found live: a reminder's date-completeness check
    kept re-asking after supposedly saving the chosen intake, traced back to
    this gap). `name`/`nric` are deliberately NOT mirrored:
    `customers.full_name`/`nric` are only ever meant to be set through the
    validated registration flow (orchestrator._advance_registration, which
    regex-validates NRIC format), and a lead-capture value here has no such
    validation — mirroring it could silently plant a bad value that the
    registration flow would then treat as already-set and never re-ask for.
    """
    # Resolve/validate preferred_course before it's persisted anywhere below
    # — found live: the agent sometimes passes a raw, unresolved reference
    # like "Course 1"/"course 4" (a catalogue-position phrasing
    # resolve_course_arg() didn't yet handle) straight through as the course
    # NAME, with nothing here to catch it. That string can never match a
    # real course again, silently corrupting `customers.preferred_course`
    # and breaking the reminders-table join
    # (repositories.reminders_due_for_dispatch()) for that reminder forever.
    # resolve_course_arg() now also handles "course N" phrasing (see its own
    # docstring); repo.get_course_by_name() (fuzzy, case-insensitive) is the
    # final check — if the resolved text still doesn't match a real course,
    # the field is dropped from this save entirely rather than persisting
    # nonsense, the same "don't trust free text, verify against the actual
    # catalogue" standard already applied everywhere else a course reference
    # gets resolved in this codebase.
    if fields.get("preferred_course"):
        resolved = courses_svc.resolve_course_arg(fields["preferred_course"])
        matched = repo.get_course_by_name(resolved)
        if matched:
            fields["preferred_course"] = matched["name"]
        else:
            log.warning(
                "save_lead(): preferred_course %r didn't resolve to a real course — dropping",
                fields["preferred_course"],
            )
            fields.pop("preferred_course")

    lead = repo.upsert_lead(phone, **fields)
    if lead and lead["status"] == "enquiry":
        repo.set_lead_status(phone, "awaiting_response")

    if whatsapp_id:
        customer_updates = {
            k: fields[k] for k in ("email", "preferred_course", "course_date") if fields.get(k)
        }
        customer = None
        if "preferred_course" in customer_updates:
            # update_customer_course_interest(), not a plain update_customer()
            # call — clears any stale course_date left over from a DIFFERENT,
            # earlier course when the course is changing and no new date
            # accompanies it in this same call (see its own docstring for
            # the bug this closes: a stale date otherwise survives a course
            # switch and gets misread as belonging to the new course).
            customer = repo.update_customer_course_interest(
                whatsapp_id,
                customer_updates["preferred_course"],
                customer_updates.get("course_date"),
            )
            remaining = {k: v for k, v in customer_updates.items() if k == "email"}
            if remaining:
                customer = repo.update_customer(whatsapp_id, **remaining)
        elif customer_updates:
            customer = repo.update_customer(whatsapp_id, **customer_updates)

        # Requirement 4 AC14: a reminder is "complete" as soon as a course
        # AND a specific intake are both known — whether both arrived in
        # THIS call or one was already on file from an earlier turn (a
        # fresh read is needed here since it often completes across two
        # separate turns: course named earlier, date given now, or vice
        # versa). Persist it as its own row so a second, later reminder for
        # a DIFFERENT course/date doesn't overwrite this one —
        # customers.preferred_course/course_date only ever track the single
        # most recent one. Safe to check on every save regardless of what
        # changed this call: create_reminder() upserts on (whatsapp_id,
        # course_name, course_date), so re-affirming an already-saved
        # reminder just refreshes it rather than duplicating.
        customer = customer or repo.get_customer(whatsapp_id)
        course = (customer or {}).get("preferred_course")
        date = (customer or {}).get("course_date")
        if course and date:
            repo.create_reminder(whatsapp_id, course, date, (customer or {}).get("email"))

    return lead


def flag_for_staff(phone: str, reason: str, message: str) -> str:
    """Escalate a complex/sensitive/unrecognised query to the staff inbox."""
    repo.escalate_lead(phone, reason)
    task = repo.add_staff_task(phone, reason, message, module="A")
    send_email(
        settings.staff_email,
        subject=f"[Q&M Bot] Escalation from {phone}",
        body=(
            f"A WhatsApp enquiry needs human follow-up.\n\n"
            f"From: {phone}\nReason: {reason}\n\nMessage:\n{message}\n\n"
            f"Queue task id: {task['id']}"
        ),
    )
    return (
        "Thanks for your message — I've passed this to our team and a staff member "
        "will follow up with you shortly."
    )


# ── Follow-up nurture sequence (24h / 72h / 7d) ──────────────────────────────
_FOLLOWUP_MESSAGES = [
    # follow_up_count 0 -> first nudge (24h)
    "Hi! Just following up on your enquiry about our dental assisting courses. "
    "Let me know if you'd like to see what's available, or ask about SkillsFuture funding. 😊",
    # 1 -> second nudge (72h)
    "Hello again from Q&M Training! Our 2-Day Basic Certificate in Dental Assisting is "
    "SkillsFuture-claimable. Want the fee breakdown? Just ask and I'll send it over.",
    # 2 -> final nudge (7d)
    "Last reminder from Q&M Training — places for the upcoming intake are filling up. "
    "Let me know if you'd like the intake dates, or if you're ready to secure your spot.",
]


def followup_message_for(lead: dict) -> str:
    idx = min(lead.get("follow_up_count", 0), len(_FOLLOWUP_MESSAGES) - 1)
    return _FOLLOWUP_MESSAGES[idx]


# ── Reminder dispatch (Requirement 10 AC5) ───────────────────────────────────
def reminder_email_subject_and_body(reminder: dict) -> tuple[str, str]:
    """Compose the subject/body for one due reminder row (as returned by
    repositories.reminders_due_for_dispatch() — carries course_name,
    course_date, email, and customer_name). Participants were told
    throughout the conversation they'd get an EMAIL reminder as the date
    approaches (Module A's own reminder-confirmation replies say exactly
    this) — this is what makes that promise real; scheduler.py's
    run_reminder_dispatch() is what actually calls this and sends it."""
    name = reminder.get("customer_name") or "there"
    course = reminder["course_name"]
    date = reminder["course_date"]
    subject = f"Reminder: {course} starts {date}"
    body = (
        f"Hi {name},\n\n"
        f"Just a reminder that your upcoming course, {course}, starts on {date}.\n\n"
        "If you haven't enrolled yet, just reply on WhatsApp and we'll help you get sorted. "
        "If you've already enrolled, we look forward to seeing you there!\n\n"
        f"{settings.company_name}"
    )
    return subject, body

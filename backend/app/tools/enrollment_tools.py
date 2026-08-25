"""Module B tools — enrollment pipeline (validate, enroll, status, invoice).

All tools read whatsapp_id and phone from the request-scoped context so the LLM
never needs to supply them.  full_name / nric / email default to the participant's
registered profile when the LLM omits them (since the user must be registered to
reach Module B, these values are always available in context.customer()).
"""
from __future__ import annotations

from crewai.tools import tool

from .. import context
from .. import repositories as repo
from ..services import enrollment
from ..services import payments as payment_service
from ..utils import money


@tool("Validate Enrollment")
def validate_enrollment_tool(
    course: str,
    full_name: str = "",
    nric: str = "",
    email: str = "",
) -> str:
    """Validate enrollment fields before enrolling. Returns 'VALID' or a specific
    error message describing what to fix.
    full_name, nric, and email are optional — blank values are auto-filled from
    the participant's registered profile."""
    customer  = context.customer()
    name_val  = full_name or customer.get("full_name") or ""
    nric_val  = nric      or customer.get("nric")      or ""
    email_val = email     or customer.get("email")     or ""
    ok, err, _ = enrollment.validate_enrollment(course, name_val, nric_val, email_val)
    return "VALID" if ok else err


@tool("Enroll Participant")
def enroll_participant_tool(
    course: str,
    full_name: str = "",
    nric: str = "",
    email: str = "",
    schedule_no: int = 0,
    sh_code: str = "",
) -> str:
    """Enroll the participant in a course and email the invoice.
    course is required — use the name, C-code (C2601), or item number from 'List Courses'.
    full_name, nric, email are optional — auto-filled from the registered profile if blank.
    sh_code is the SH-code from 'Course Schedule' output (e.g. SH2601) — preferred over schedule_no.
    schedule_no is the 1-based intake number (fallback when sh_code is not available).
    DO NOT call this without sh_code or schedule_no unless the participant has explicitly
    chosen an intake — omitting both silently books the next available date, which is only
    correct for the C-code+SH-code quick-enrol shortcut, never for a conversation. If you
    don't have an intake the participant chose, call 'Course Schedule' and ask them first."""
    customer  = context.customer()
    name_val  = full_name or customer.get("full_name") or ""
    nric_val  = nric      or customer.get("nric")      or ""
    email_val = email     or customer.get("email")     or ""

    sid: int | None = None
    sno: int | None = None
    if sh_code:
        sched = repo.get_schedule_by_schedule_id(sh_code.upper())
        if sched:
            sid = sched["id"]
    if sid is None and schedule_no and schedule_no > 0:
        sno = schedule_no

    result = enrollment.enroll(
        context.whatsapp_id(), context.phone(),
        course, name_val, nric_val, email_val,
        schedule_no=sno,
        schedule_id=sid,
    )
    return result["reply"]


@tool("My Enrollments")
def my_enrollments_tool(course: str = "") -> str:
    """List the status of every course the participant is enrolled in — use this
    for ANY personal enrollment/payment status question, including a quick
    single/latest check ('what's my status') as well as 'status of all my
    courses' or 'am I enrolled in <course>'. It always returns the complete,
    correct picture — course, date, status, invoice number, and receipt
    number for every enrollment on record — so it is always the right tool,
    never just a fallback for the plural case.
    course is optional — the name, C-code, or list number of one course to
    filter to; leave blank to list every enrollment. NOTE: if the participant
    has more than one enrollment in the SAME course (different intake dates),
    this filter returns all of them together — it does not disambiguate by
    date. If the participant refers to a specific one by list position (e.g.
    'item 2') or by date, resolve that yourself against a numbered list
    already shown in the conversation history before calling this tool, or
    match the date/invoice in what this tool returns — never guess."""
    return enrollment.all_status_text(context.whatsapp_id(), context.phone(), course)


@tool("Resend Invoice")
def resend_invoice_tool() -> str:
    """Resend the latest invoice to the participant's email on record. No input needed."""
    return enrollment.resend_invoice(context.whatsapp_id(), context.phone())


@tool("Request Cancellation")
def request_cancellation_tool(reason: str, invoice_no: str = "") -> str:
    """Cancel the participant's enrollment and raise a credit note (Requirement 11 AC5).
    reason is required — a short description of why they're cancelling.
    invoice_no is optional — pass it if the participant named a specific invoice/enrollment
    (they may have more than one); otherwise their sole/latest active enrollment is used.
    Only ever call this on the turn the participant has explicitly confirmed cancelling —
    this tool is not available to you on any other turn."""
    wid = context.whatsapp_id()
    phone = context.phone()

    if invoice_no:
        enr = repo.get_enrollment_by_invoice(invoice_no.strip().upper())
    else:
        enr = repo.latest_enrollment(wid) or repo.latest_enrollment_for_phone(phone)

    if not enr:
        return "No matching enrollment found to cancel."
    if enr.get("whatsapp_id") != wid and enr.get("phone") != phone:
        return "That invoice doesn't belong to your account."
    if enr.get("status") == "cancelled":
        return f"Enrollment (invoice {enr.get('invoice_no')}) is already cancelled."

    result = payment_service.request_credit_note(
        enr["id"], reason or "Participant requested cancellation"
    )
    if not result.get("ok"):
        return result.get("error") or "Could not process the cancellation."

    cn = result["credit_note"]
    return (
        f"Your enrollment in {enr.get('course_name')} (invoice {enr.get('invoice_no')}) has been "
        f"cancelled. Credit note {cn.get('credit_note_no')} for {money(cn.get('amount', 0))} has "
        f"been raised and is now pending accounts approval."
    )

"""Enrollment pipeline services (Module B): validate → invoice → email → status.

Implements the Sequential Agentic Pipeline from the proposal (Section 5.4) as a
directed, goal-driven chain with conditional branching on validation.
"""
from __future__ import annotations

from datetime import datetime

from .. import repositories as repo
from ..config import settings
from ..email_service import send_email
from ..pdf import generate_invoice_pdf
from ..utils import compute_fee, money, new_invoice_no, schedule_short_id, valid_email, valid_nric
from .courses import resolve_course_arg

_STATUS_LABELS = {
    "enquiry": "Enquiry",
    "enrolled": "Enrolled — invoice pending",
    "invoice_sent": "Awaiting payment",
    "awaiting_payment": "Awaiting payment",
    "paid": "Paid",
    "receipt_issued": "Paid — receipt issued",
    "cancelled": "Cancelled",
}


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status.replace("_", " ").title())


def _sh(s: dict) -> str:
    """Return the stored schedule_id code for a schedule row, or compute it as fallback."""
    return s.get("schedule_id") or schedule_short_id(s["id"])


def validate_enrollment(course: str, name: str, nric: str, email: str) -> tuple[bool, str, dict | None]:
    """Validate enrollment fields. Returns (ok, error_text, course_row)."""
    missing = []
    if not course or not course.strip():
        missing.append("course")
    if not name or not name.strip():
        missing.append("full name")
    if not nric or not nric.strip():
        missing.append("NRIC")
    if not email or not email.strip():
        missing.append("email address")
    if missing:
        return False, (
            f"Enrollment incomplete — missing: {', '.join(missing)}. Could you share that "
            "and I'll get you enrolled?"
        ), None

    course_row = repo.get_course_by_name(resolve_course_arg(course))
    if not course_row:
        return False, (
            f"Course '{course}' not found — could you tell me which course you mean, or "
            "ask me to list the courses?"
        ), None

    if not valid_nric(nric):
        return False, (
            "That NRIC doesn't look quite right — it should be a letter (S/T/F/G), 7 "
            "digits, then a letter (e.g. S8512345A). Could you resend it?"
        ), None

    if not valid_email(email):
        return False, (
            f"That email address doesn't look right: '{email}'. Could you double-check "
            "and resend it?"
        ), None

    return True, "", course_row


def enroll(
    whatsapp_id: str,
    phone: str,
    course: str,
    name: str,
    nric: str,
    email: str,
    schedule_no: int | None = None,
    schedule_id: int | None = None,
) -> dict:
    """Run the full enrollment pipeline.

    Returns {"ok": bool, "reply": str, "enrollment": dict|None}.
    schedule_no : 1-based intake index from the intake list shown to the participant
    schedule_id : direct DB primary-key (from an SH-code); takes priority over schedule_no
    Omit both to use the next available date.
    """
    ok, err, course_row = validate_enrollment(course, name, nric, email)
    if not ok:
        return {"ok": False, "reply": err, "enrollment": None}

    schedules = repo.get_schedules(course_row["id"])

    if schedule_id is not None:
        schedule = repo.get_schedule(schedule_id)
        if not schedule or schedule.get("course_id") != course_row["id"]:
            sh = _sh(schedule) if schedule else schedule_short_id(schedule_id)
            avail = "\n".join(f"  [{_sh(s)}] {s['label']}" for s in schedules)
            return {
                "ok": False,
                "reply": (
                    f"Schedule [{sh}] does not belong to {course_row['name']}.\n\n"
                    f"Available intakes:\n{avail}\n\n"
                    f"Resend with a valid SH-code."
                ),
                "enrollment": None,
            }
    elif schedule_no is not None:
        if 1 <= schedule_no <= len(schedules):
            schedule = schedules[schedule_no - 1]
        else:
            avail = "\n".join(
                f"  [{_sh(s)}] {s['label']} ({s['seats']} seats)" for s in schedules
            )
            return {
                "ok": False,
                "reply": (
                    f"Intake number {schedule_no} is not valid for {course_row['name']}.\n\n"
                    f"Available intakes:\n{avail}\n\n"
                    f"Resend with a valid intake number or SH-code."
                ),
                "enrollment": None,
            }
    else:
        schedule = schedules[0] if schedules else None
    course_date = schedule["label"] if schedule else "To be confirmed"

    existing = repo.active_enrollment_for_course(
        whatsapp_id, course_row["id"], phone, course_date
    )
    if existing:
        status_str = _status_label(existing["status"])
        lines = [
            f"You are already enrolled in {existing['course_name']}.",
            f"Date: {existing['course_date']}",
            f"Status: {status_str}",
        ]
        if existing.get("invoice_no"):
            lines.append(f"Invoice: {existing['invoice_no']} — {money(existing['net_payable'])}")
        if existing["status"] in ("invoice_sent", "awaiting_payment"):
            lines += ["", "To pay, just attach your PayNow screenshot. Ask any time if you'd like the invoice resent."]
        elif existing["status"] in ("paid", "receipt_issued"):
            if existing.get("receipt_no"):
                lines.append(f"Receipt: {existing['receipt_no']}")
            lines += ["", "Just ask if you'd like your receipt resent."]
        return {"ok": False, "reply": "\n".join(lines), "enrollment": existing}

    fee, subsidy, net = compute_fee(course_row["full_fee"], course_row["sf_subsidy_cap"])
    invoice_no = new_invoice_no()

    enrollment = repo.create_enrollment(
        whatsapp_id=whatsapp_id,
        phone=phone,
        course_id=course_row["id"],
        course_name=course_row["name"],
        course_date=course_date,
        schedule_id=schedule["id"] if schedule else None,
        full_name=name.strip(),
        nric=nric.strip().upper(),
        email=email.strip(),
        fee=fee,
        sf_subsidy=subsidy,
        net_payable=net,
        invoice_no=invoice_no,
        status="enrolled",
    )

    # Enrollment complete — take the seat for this intake (released again if
    # this enrollment is later cancelled; see request_credit_note()).
    if schedule:
        repo.adjust_schedule_seats(schedule["id"], +1)

    # Update customer status and legacy lead if present.
    repo.set_customer_status(whatsapp_id, "enrolled")
    if phone and repo.get_lead(phone):
        repo.set_lead_status(phone, "enrolled")

    # Generate invoice PDF + deliver by email, then advance status.
    pdf_path = generate_invoice_pdf(enrollment)
    _send_invoice_email(enrollment, pdf_path)
    enrollment = repo.update_enrollment(enrollment["id"], status="invoice_sent")

    # Placeholder payments-ledger row for this invoice — Module C updates this
    # same row in place once the first payment proof is verified (see
    # process_payment()). expected_amount holds the full course fee here since
    # no proof type is known yet; it may need a SkillsFuture claim, a PayNow
    # transfer, or both, submitted as separate screenshots.
    repo.record_payment(
        enrollment_id=enrollment["id"],
        invoice_no=invoice_no,
        whatsapp_id=whatsapp_id,
        phone=phone,
        expected_amount=fee,
        verdict="pending",
        notes="Invoice issued — awaiting payment.",
    )

    if subsidy <= 0:
        fee_line = money(fee)
    elif net <= 0:
        fee_line = f"{money(fee)} (fully claimable through SkillsFuture — S$0 payable)"
    else:
        fee_line = f"{money(fee)} (SkillsFuture credit up to {money(subsidy)} — net payable {money(net)})"
    reply = (
        "Enrollment confirmed.\n\n"
        f"Course: {enrollment['course_name']}\n"
        f"Date: {course_date}\n"
        f"Name: {enrollment['full_name']} | NRIC: {enrollment['nric']}\n"
        f"Fee: {fee_line}\n\n"
        f"Invoice {invoice_no} sent to {enrollment['email']}.\n"
        f"When you're ready to pay, just attach your PayNow screenshot here."
    )
    return {"ok": True, "reply": reply, "enrollment": enrollment}


def _send_invoice_email(enrollment: dict, pdf_path) -> None:
    if enrollment["sf_subsidy"] and enrollment["sf_subsidy"] > 0:
        fee_lines = (
            f"  Course Fee                           : {money(enrollment['fee'])}\n"
            f"  Less: SkillsFuture Credit (claimable): -{money(enrollment['sf_subsidy'])}\n"
            f"  Net Payable                          : {money(enrollment['net_payable'])}\n"
        )
    else:
        fee_lines = f"  Net Payable                          : {money(enrollment['net_payable'])}\n"

    body = (
        f"Dear {enrollment['full_name']},\n\n"
        f"Thank you for enrolling in {enrollment['course_name']} "
        f"({enrollment['course_date']}). Your invoice is attached as a PDF — a summary "
        "is below.\n\n"
        "INVOICE\n"
        f"  Invoice No  : {enrollment['invoice_no']}\n"
        f"  Date        : {datetime.now():%d %b %Y}\n"
        f"  Participant : {enrollment['full_name']}\n"
        f"  NRIC        : {enrollment['nric']}\n"
        f"  Email       : {enrollment['email']}\n"
        f"  Course      : {enrollment['course_name']}\n"
        f"  Course Date : {enrollment['course_date']}\n\n"
        f"{fee_lines}\n"
        "PAYMENT — PayNow\n"
        f"  Pay to UEN: {settings.paynow_uen} ({settings.paynow_payee})\n"
        f"  Reference: {enrollment['invoice_no']}\n"
        "  (Scan the QR code in the attached invoice.)\n\n"
        "SKILLSFUTURE CLAIM INSTRUCTIONS\n"
        "  1. Log in to https://www.myskillsfuture.gov.sg with Singpass\n"
        "  2. Submit a SkillsFuture Credit claim for this course before the start date\n"
        "  3. Keep the claim confirmation for your records\n\n"
        "After payment, just send your PayNow screenshot on WhatsApp and we will verify\n"
        "it and email your receipt automatically.\n\n"
        f"{settings.company_name}"
    )
    send_email(
        enrollment["email"],
        subject=(
            f"Q&M Training — {enrollment['course_name']} ({enrollment['course_date']}) "
            f"— Invoice {enrollment['invoice_no']}"
        ),
        body=body,
        attachments=[pdf_path],
    )


def resend_invoice(whatsapp_id: str, phone: str) -> str:
    """Resend the invoice for the latest enrollment to email on record."""
    enrollment = repo.latest_enrollment(whatsapp_id) or repo.latest_enrollment_for_phone(phone)
    if not enrollment:
        return (
            "No enrollment found for your number. Let me know which course you'd like to "
            "enrol in and I'll get you started."
        )
    if not enrollment.get("invoice_no"):
        return "Your enrollment has no invoice yet. Please contact us for assistance."
    pdf_path = generate_invoice_pdf(enrollment)
    _send_invoice_email(enrollment, pdf_path)
    return (
        f"Invoice {enrollment['invoice_no']} re-sent to {enrollment['email']}.\n"
        f"Net payable: {money(enrollment['net_payable'])}."
    )


def _enrollment_line(e: dict, index: int) -> str:
    """Fixed, numbered format — deliberately not left to the LLM to compose,
    since a participant referring back to "item 2" needs a stable, parseable
    numbering that survives being relayed through chat history (see
    crews/module_c.py: _list_reference_directive(), which parses this exact
    shape back out to disambiguate a specific enrollment)."""
    lines = [
        f"{index}. {e['course_name']} — {e['course_date']}",
        f"   Status: {_status_label(e['status'])}",
    ]
    if e.get("invoice_no"):
        lines.append(f"   Invoice: {e['invoice_no']} — {money(e['net_payable'])}")
    if e.get("receipt_no"):
        lines.append(f"   Receipt: {e['receipt_no']}")
    return "\n".join(lines)


def all_status_text(whatsapp_id: str, phone: str, course: str | None = None) -> str:
    """Status of every course this participant is enrolled in, or just one course
    (name, C-code, or list number) when `course` is given.

    The single source of truth for personal enrollment/payment status —
    deliberately the only such lookup in the system (there is no "latest
    enrollment only" variant): a participant can have more than one
    enrollment, including more than one in the same course on different
    intake dates, and a "just the latest one" lookup can silently return the
    wrong one whenever that's true. This always returns the complete,
    unambiguous picture.
    """
    enrollments = repo.enrollments_for_customer(whatsapp_id, phone)
    if not enrollments:
        return (
            "No enrollment found for your number. Let me know which course you'd like to "
            "enrol in and I'll get you started."
        )

    if course and course.strip():
        resolved = resolve_course_arg(course)
        course_row = repo.get_course_by_name(resolved)
        if not course_row:
            return (
                f"Course '{course}' not found — could you tell me which course you mean, "
                "or ask me to list the courses?"
            )
        enrollments = [e for e in enrollments if e.get("course_id") == course_row["id"]]
        if not enrollments:
            return f"You have no enrollment on record for {course_row['name']}."

    header = (
        "Your enrollment status:" if len(enrollments) == 1
        else f"Your enrollment status ({len(enrollments)} courses):"
    )
    return header + "\n\n" + "\n\n".join(
        _enrollment_line(e, i) for i, e in enumerate(enrollments, 1)
    )

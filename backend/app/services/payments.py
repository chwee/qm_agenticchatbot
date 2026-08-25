"""Payment verification & accounts services (Module C).

Multimodal verification: a vision LLM extracts the amount and proof type from
each payment screenshot; code then applies confirm/mismatch/unreadable per
screenshot and tallies all confirmed proofs for the invoice against the course
fee (proposal Section 5.5). An invoice may need more than one proof — e.g. a
SkillsFuture claim screenshot covering the subsidised portion, plus a PayNow
screenshot covering the net payable — since WhatsApp delivers one image per
message (never a multi-image batch), so proofs always arrive one at a time
across separate messages and are recorded incrementally. Also covers receipts
(one per confirmed proof), the credit-note workflow, and the nightly accounts
report.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime

from .. import repositories as repo
from ..config import settings
from ..email_service import send_email
from ..llm import get_openai_client
from ..pdf import generate_credit_note_pdf, generate_receipt_pdf
from ..utils import money, new_credit_note_no, new_receipt_no

log = logging.getLogger(__name__)

_TOLERANCE = 0.01  # cents-level rounding slack for amount comparisons

_METHOD_LABELS = {
    "paynow": "PayNow transfer",
    "skillsfuture_claim": "SkillsFuture claim",
}

_VISION_PROMPT = """You are a payment verification assistant for a Singapore training provider.
You are shown a screenshot a customer submitted as proof of payment toward a course invoice.
There are two valid kinds of proof:
  - "paynow": a PayNow / bank transfer confirmation (banking app screenshot showing a
    transfer to the provider, with an amount, recipient/UEN, reference, and date/time).
  - "skillsfuture_claim": a SkillsFuture Credit claim confirmation from the MySkillsFuture
    portal (shows the course, the claim amount, a claim/reference number, and a claim
    status such as "Submitted" / "Approved").

Extract the following and return STRICT JSON only:
{
  "is_payment_proof": true/false,        // true if this is EITHER kind of proof above
  "readable": true/false,
  "payment_type": "paynow" or "skillsfuture_claim" or null,  // null if you truly cannot tell
  "detected_amount": number or null,     // numeric SGD amount, no currency symbol
  "currency": string or null,
  "recipient": string or null,           // payee name or UEN if visible (paynow only)
  "reference": string or null,           // transaction/claim reference number if visible
  "paid_at": string or null,             // date/time text as shown
  "confidence": number                   // 0..1 your confidence in detected_amount + payment_type
}
Do not invent values; use null when unsure."""


def extract_payment_proof(image_b64: str, mimetype: str) -> dict:
    """Run the vision LLM on one screenshot and return the raw extracted facts.

    No verdict is decided here — completion is a business-logic decision the
    caller makes against the invoice's running balance (see _evaluate_proof),
    since the same screenshot facts mean different things depending on how
    much of the invoice is already settled.
    """
    if not settings.openai_api_key:
        return {
            "is_payment_proof": False, "readable": False, "payment_type": None,
            "detected_amount": None, "confidence": 0.0,
            "notes": "OpenAI API key not configured.", "raw": {},
        }

    data_url = f"data:{mimetype or 'image/jpeg'};base64,{image_b64}"
    try:
        client = get_openai_client()
        resp = client.chat.completions.create(
            model=settings.openai_vision_model,
            temperature=0,
            max_tokens=500,
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
        )
        raw = json.loads(resp.choices[0].message.content)
    except Exception as e:  # noqa: BLE001
        log.error("vision extraction failed: %s", e)
        return {
            "is_payment_proof": False, "readable": False, "payment_type": None,
            "detected_amount": None, "confidence": 0.0, "notes": str(e), "raw": {},
        }

    payment_type = raw.get("payment_type")
    if payment_type not in ("paynow", "skillsfuture_claim"):
        payment_type = None

    return {
        "is_payment_proof": bool(raw.get("is_payment_proof")),
        "readable": bool(raw.get("readable")),
        "payment_type": payment_type,
        "detected_amount": raw.get("detected_amount"),
        "reference": raw.get("reference"),
        "recipient": raw.get("recipient"),
        "paid_at": raw.get("paid_at"),
        "confidence": float(raw.get("confidence") or 0.0),
        "raw": raw,
    }


def _remaining_balances(enrollment: dict, confirmed: list[dict]) -> dict:
    """Remaining balance still owed per payment type for this enrollment."""
    subsidy = float(enrollment["sf_subsidy"])
    net = float(enrollment["net_payable"])
    paid_sf = sum(
        float(p["detected_amount"] or 0) for p in confirmed if p.get("payment_type") == "skillsfuture_claim"
    )
    paid_paynow = sum(
        float(p["detected_amount"] or 0) for p in confirmed if p.get("payment_type") == "paynow"
    )
    return {
        "skillsfuture_claim": max(round(subsidy - paid_sf, 2), 0.0),
        "paynow": max(round(net - paid_paynow, 2), 0.0),
    }


def _evaluate_proof(extracted: dict, remaining: dict) -> tuple[str, str]:
    """Decide confirmed/mismatch/unreadable for one screenshot given the
    invoice's remaining balance per type. Returns (verdict, notes)."""
    if not extracted["readable"] or not extracted["is_payment_proof"] or extracted["detected_amount"] is None:
        return "unreadable", "Screenshot not readable or not a recognisable payment/claim proof."

    try:
        detected = float(extracted["detected_amount"])
    except (TypeError, ValueError):
        return "unreadable", "Non-numeric amount."

    payment_type = extracted.get("payment_type")
    if payment_type not in remaining:
        return (
            "mismatch",
            "Could not tell whether this is a PayNow transfer or a SkillsFuture claim — "
            "please resend a clearer screenshot.",
        )

    balance = remaining[payment_type]
    if balance <= 0:
        if payment_type == "skillsfuture_claim":
            return (
                "mismatch",
                "This invoice has no SkillsFuture-claimable portion outstanding — a claim "
                "screenshot isn't needed here (or one was already submitted).",
            )
        return (
            "mismatch",
            "This invoice has no PayNow portion outstanding — a transfer isn't needed here "
            "(or one was already submitted).",
        )

    if detected > balance + _TOLERANCE:
        return "mismatch", f"Detected {detected} exceeds the outstanding {payment_type} balance of {balance}."

    if extracted["confidence"] < settings.auto_confirm_threshold:
        return "mismatch", "Amount looks plausible but confidence is low — staff review."

    return "confirmed", "Amount accepted toward the outstanding balance."


def process_payment(
    whatsapp_id: str,
    phone: str,
    image_b64: str | None,
    mimetype: str | None,
    invoice_no: str | None = None,
) -> str:
    """Verify one payment/claim screenshot against an invoice.

    An invoice may need more than one proof; each screenshot is verified and
    recorded independently, and completion is decided by summing all confirmed
    proofs against the course fee.
    """
    if not image_b64:
        return (
            "No screenshot received.\n\n"
            "To submit payment, just attach your PayNow transfer or SkillsFuture claim "
            "confirmation screenshot, and mention your invoice number if you have it.\n\n"
            "Your invoice number is shown on the invoice email sent during enrollment — "
            "just ask if you'd like it resent."
        )

    if not invoice_no:
        # No invoice number stated anywhere in this message — fall back to the
        # participant's own latest outstanding invoice (the common case of one
        # active enrollment) rather than making them look it up themselves.
        auto = repo.latest_enrollment(whatsapp_id) or repo.latest_enrollment_for_phone(phone)
        if auto and auto.get("invoice_no") and auto["status"] in ("invoice_sent", "awaiting_payment"):
            invoice_no = auto["invoice_no"]

    if not invoice_no:
        return (
            "I couldn't find an invoice to match this screenshot to. Could you let me "
            "know your invoice number, or ask me to look up your enrollment?"
        )

    enrollment = repo.get_enrollment_by_invoice(invoice_no)
    if not enrollment:
        return (
            f"Invoice {invoice_no} not found.\n\n"
            "Please check the invoice number and try again, or just ask me to resend it."
        )
    if enrollment["whatsapp_id"] != whatsapp_id and enrollment.get("phone") != phone:
        return (
            f"Invoice {invoice_no} does not match your account.\n\n"
            "Please check the invoice number and try again."
        )
    if enrollment["status"] in ("paid", "receipt_issued"):
        return (
            f"Invoice {invoice_no} is already fully paid.\n"
            "Just ask if you'd like your receipt(s) resent."
        )

    if enrollment["status"] == "invoice_sent":
        repo.update_enrollment(enrollment["id"], status="awaiting_payment")

    confirmed_so_far = repo.confirmed_payments_for_enrollment(enrollment["id"])
    remaining = _remaining_balances(enrollment, confirmed_so_far)

    extracted = extract_payment_proof(image_b64, mimetype or "image/jpeg")
    verdict, notes = _evaluate_proof(extracted, remaining)

    # The invoice's placeholder payments row (verdict='pending') is resolved in
    # place on the first proof submitted (of either type); every proof after
    # that (a second type, or a retry after a mismatch/unreadable attempt)
    # inserts its own new row, preserving the full attempt history.
    pending = repo.pending_payment_for_enrollment(enrollment["id"])
    payment_fields = dict(
        payment_type=extracted.get("payment_type"),
        detected_amount=extracted.get("detected_amount"),
        reference=extracted.get("reference"),
        paid_at_text=extracted.get("paid_at"),
        verdict=verdict,
        confidence=extracted.get("confidence"),
        raw_extract=extracted.get("raw"),
        notes=notes,
    )
    if pending:
        payment = repo.update_payment(pending["id"], **payment_fields)
    else:
        payment = repo.record_payment(
            enrollment_id=enrollment["id"],
            invoice_no=enrollment.get("invoice_no"),
            whatsapp_id=whatsapp_id,
            phone=phone,
            expected_amount=remaining.get(extracted.get("payment_type"), 0),
            **payment_fields,
        )

    if verdict == "confirmed":
        return _confirm_proof(enrollment, payment)
    if verdict == "mismatch":
        return _flag_mismatch(enrollment, payment)
    return (
        "We couldn't read that screenshot clearly, or it doesn't look like a PayNow "
        "transfer or SkillsFuture claim confirmation.\n\n"
        "Could you resend a clearer screenshot?"
    )


def _confirm_proof(enrollment: dict, payment: dict) -> str:
    """Issue a receipt for this ONE confirmed proof, then check whether the
    invoice's full fee has now been reconciled across all confirmed proofs."""
    receipt_no = new_receipt_no()
    payment = repo.update_payment(payment["id"], receipt_no=receipt_no)
    method = _METHOD_LABELS.get(payment.get("payment_type"), "Payment")

    pdf_path = generate_receipt_pdf(enrollment, payment)
    send_email(
        enrollment["email"],
        subject=f"Q&M Training — Receipt {receipt_no}",
        body=(
            f"Dear {enrollment['full_name']},\n\n"
            f"We have received your {method} for {enrollment['course_name']} "
            f"({enrollment['course_date']}).\n\n"
            f"Receipt {receipt_no} is attached.\n"
            f"Amount received: {money(payment['detected_amount'])}\n\n"
            f"{settings.company_name}"
        ),
        attachments=[pdf_path],
    )

    confirmed = repo.confirmed_payments_for_enrollment(enrollment["id"])
    total_paid = sum(float(p["detected_amount"] or 0) for p in confirmed)
    fee = float(enrollment["fee"])
    outstanding = round(fee - total_paid, 2)

    if outstanding <= _TOLERANCE:
        repo.update_enrollment(enrollment["id"], status="receipt_issued", receipt_no=receipt_no)
        return (
            f"Payment complete for {enrollment['course_name']}!\n\n"
            f"{method} of {money(payment['detected_amount'])} confirmed — receipt {receipt_no} "
            f"sent to {enrollment['email']}.\n\n"
            f"Total received: {money(total_paid)} of {money(fee)}.\n"
            f"See you on {enrollment['course_date']}!"
        )

    remaining = _remaining_balances(enrollment, confirmed)
    still_needed = []
    if remaining["skillsfuture_claim"] > _TOLERANCE:
        still_needed.append(f"a SkillsFuture claim of {money(remaining['skillsfuture_claim'])}")
    if remaining["paynow"] > _TOLERANCE:
        still_needed.append(f"a PayNow transfer of {money(remaining['paynow'])}")
    needed_str = " and ".join(still_needed) or f"the remaining {money(outstanding)}"

    return (
        f"{method} of {money(payment['detected_amount'])} confirmed — receipt {receipt_no} "
        f"sent to {enrollment['email']}.\n\n"
        f"Outstanding balance: {money(outstanding)}. Please also send {needed_str}."
    )


def _flag_mismatch(enrollment: dict, payment: dict) -> str:
    repo.add_staff_task(
        whatsapp_id=enrollment.get("whatsapp_id", ""),
        phone=enrollment.get("phone"),
        reason="payment_mismatch",
        message=(
            f"Invoice {enrollment.get('invoice_no')}: {payment.get('notes')} "
            f"(type={payment.get('payment_type')}, detected={payment.get('detected_amount')}, "
            f"confidence={payment.get('confidence')})"
        ),
        module="C",
        payload=payment.get("raw_extract"),
    )
    send_email(
        settings.staff_email,
        subject=f"[Q&M Bot] Payment mismatch — {enrollment.get('invoice_no')}",
        body=(
            f"A payment screenshot needs manual review.\n\n"
            f"From: {enrollment['phone']}\nInvoice: {enrollment.get('invoice_no')}\n"
            f"Type: {payment.get('payment_type') or 'unclear'}\n"
            f"Detected: {payment.get('detected_amount')}\n"
            f"Confidence: {payment.get('confidence')}\nNotes: {payment.get('notes')}"
        ),
    )
    detected = payment.get("detected_amount")
    detected_str = money(detected) if detected is not None else "an unclear amount"
    return (
        f"We detected {detected_str} on that screenshot, but it doesn't match what's still "
        f"outstanding on invoice {enrollment.get('invoice_no')}.\n\n"
        "Our team has been notified and will review it. If this is an error, please resend "
        "a clearer screenshot."
    )


def outstanding_balance_text(whatsapp_id: str, phone: str, invoice_no: str | None = None) -> str:
    """Itemised remaining balance for one specific invoice (when `invoice_no`
    is given — always prefer this when the participant has more than one
    enrollment) or the participant's latest enrollment otherwise. An invoice
    may need a SkillsFuture claim, a PayNow transfer, or both."""
    if invoice_no:
        enrollment = repo.get_enrollment_by_invoice(invoice_no)
        if not enrollment or (enrollment["whatsapp_id"] != whatsapp_id and enrollment.get("phone") != phone):
            enrollment = None
    else:
        enrollment = repo.latest_enrollment(whatsapp_id) or repo.latest_enrollment_for_phone(phone)
    if not enrollment:
        return "No enrollment found for your number."
    if enrollment["status"] in ("paid", "receipt_issued"):
        return (
            f"Invoice {enrollment.get('invoice_no')} is fully paid. "
            "Just ask if you'd like your receipt(s) resent."
        )

    confirmed = repo.confirmed_payments_for_enrollment(enrollment["id"])
    remaining = _remaining_balances(enrollment, confirmed)
    lines = [f"Invoice {enrollment.get('invoice_no')} for {enrollment['course_name']}:"]
    if remaining["skillsfuture_claim"] > _TOLERANCE:
        lines.append(f"  SkillsFuture claim still needed: {money(remaining['skillsfuture_claim'])}")
    if remaining["paynow"] > _TOLERANCE:
        lines.append(f"  PayNow transfer still needed: {money(remaining['paynow'])}")
    if len(lines) == 1:
        return (
            f"Invoice {enrollment.get('invoice_no')} looks fully covered — send your "
            "payment screenshot if you haven't already, or just ask for your receipt(s) "
            "to be resent."
        )
    lines.append("Send the corresponding screenshot(s) to settle.")
    return "\n".join(lines)


def resend_receipt(whatsapp_id: str, phone: str) -> str:
    """Resend every payment receipt for the latest enrollment."""
    enrollment = repo.latest_enrollment(whatsapp_id) or repo.latest_enrollment_for_phone(phone)
    if not enrollment:
        return "No enrollment found for your number."

    confirmed = [
        p for p in repo.confirmed_payments_for_enrollment(enrollment["id"]) if p.get("receipt_no")
    ]
    if not confirmed:
        return "No receipt found yet. A receipt is issued after your payment is verified."

    for payment in confirmed:
        pdf_path = generate_receipt_pdf(enrollment, payment)
        send_email(
            enrollment["email"],
            subject=f"Q&M Training — Receipt {payment['receipt_no']}",
            body=(
                f"Dear {enrollment['full_name']},\n\n"
                f"Your receipt {payment['receipt_no']} is attached again as requested.\n\n"
                f"{settings.company_name}"
            ),
            attachments=[pdf_path],
        )
    numbers = ", ".join(p["receipt_no"] for p in confirmed)
    return f"Receipt(s) {numbers} re-sent to {enrollment['email']}."


# ── Credit-note workflow ─────────────────────────────────────────────────────
def request_credit_note(enrollment_id: int, reason: str) -> dict:
    enrollment = repo.get_enrollment(enrollment_id)
    if not enrollment:
        return {"ok": False, "error": "Enrollment not found."}
    if enrollment["status"] == "cancelled":
        return {"ok": False, "error": "Enrollment is already cancelled."}
    confirmed = repo.confirmed_payments_for_enrollment(enrollment_id)
    total_paid = round(sum(float(p["detected_amount"] or 0) for p in confirmed), 2)
    cn_no = new_credit_note_no()
    cn = repo.create_credit_note(enrollment_id, reason, total_paid, cn_no)
    repo.update_enrollment(enrollment_id, status="cancelled")

    # Cancellation releases the seat this enrollment was holding.
    if enrollment.get("schedule_id"):
        repo.adjust_schedule_seats(enrollment["schedule_id"], -1)

    send_email(
        settings.accounts_email,
        subject=f"[Q&M] Credit note requested {cn_no}",
        body=(
            f"Credit note {cn_no} requested for invoice {enrollment.get('invoice_no')}.\n"
            f"Amount paid to date: {money(total_paid)}\nReason: {reason}\n\n"
            "Approve from the accountant portal."
        ),
    )
    return {"ok": True, "credit_note": cn}


def approve_credit_note(credit_note_id: int) -> dict:
    """Flip the credit note to 'approved' and return immediately (Requirement
    11 AC3) — PDF generation and the participant email are NOT done here any
    more. They're comparatively slow (a PDF render + an SMTP round-trip) and
    were previously coupled to this synchronous staff-approval action, so a
    slow mail server made the "approve" click itself feel slow/uncertain
    from the admin UI. dispatch_pending_credit_notes() (a short-interval
    scheduler job, see scheduler.py) picks up every row this leaves in
    status='approved', notified_at IS NULL and does that work instead."""
    cn = repo.approve_credit_note(credit_note_id)
    if not cn:
        return {"ok": False, "error": "Credit note not found."}
    return {"ok": True, "credit_note": cn}


def dispatch_pending_credit_notes() -> int:
    """Generate the PDF and email every approved-but-not-yet-notified credit
    note (Requirement 11 AC3). Called by scheduler.py on a short interval —
    not daily like the other scheduled jobs, since staff expect a credit
    note they just approved to reach the participant promptly, not wait for
    the next day's cron hour."""
    sent = 0
    for cn in repo.credit_notes_pending_dispatch():
        enrollment = repo.get_enrollment(cn["enrollment_id"]) if cn.get("enrollment_id") else None
        if not enrollment:
            # No enrollment to attach this to (e.g. it was deleted) — nothing
            # meaningful to email; mark notified so it isn't retried forever.
            repo.mark_credit_note_notified(cn["id"])
            continue
        try:
            pdf_path = generate_credit_note_pdf(enrollment, cn)
            send_email(
                enrollment["email"],
                subject=f"Q&M Training — Credit Note {cn['credit_note_no']}",
                body=(
                    f"Dear {enrollment['full_name']},\n\n"
                    f"Please find attached credit note {cn['credit_note_no']} for "
                    f"{enrollment['course_name']}.\n\n{settings.company_name}"
                ),
                attachments=[pdf_path],
            )
            repo.mark_credit_note_notified(cn["id"])
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.error("Credit note dispatch failed for id=%s: %s", cn["id"], e)
    return sent


# ── Nightly accounts report (Module C scheduler) ─────────────────────────────
def nightly_report() -> str:
    """Build a CSV summary of enrollments by payment status and email accounts."""
    rows = repo.enrollments_by_status()
    out = settings.generated_dir / f"accounts_report_{datetime.now():%Y%m%d}.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["invoice_no", "name", "course", "course_date", "net_payable",
                    "status", "receipt_no", "created_at"])
        for r in rows:
            w.writerow([r.get("invoice_no"), r["full_name"], r["course_name"],
                        r.get("course_date"), r["net_payable"], r["status"],
                        r.get("receipt_no"), r["created_at"]])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = "\n".join(f"  {k}: {v}" for k, v in sorted(counts.items())) or "  (no records)"
    send_email(
        settings.accounts_email,
        subject=f"[Q&M] Nightly accounts report — {datetime.now():%d %b %Y}",
        body=f"Enrollment status summary:\n{summary}\n\nFull CSV attached.",
        attachments=[out],
    )
    return str(out)

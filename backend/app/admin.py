"""Admin dashboard + Accountant portal (lightweight).

Proposal Section 3 / 7.1: an admin view of all enrollments and statuses, and a
password-protected accountant portal for payment status, pending receipts,
overdue accounts and credit-note approval. Implemented as JSON endpoints plus a
single HTML page so the whole system stays self-contained (swap for Next.js /
Retool later without changing the backend).
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import repositories as repo
from .config import settings
from .services import payments as payment_service

router = APIRouter(prefix="/admin", tags=["admin"])
_security = HTTPBasic(realm="Q&M Admin")

# Simple built-in credential for the accountant portal (POC). Override via env in
# a real deployment; here it keeps the portal "password-protected" per the spec.
_ADMIN_USER = "admin"
_ADMIN_PASS = "qm-admin"


def _auth(creds: HTTPBasicCredentials = Depends(_security)) -> str:
    ok = secrets.compare_digest(creds.username, _ADMIN_USER) and secrets.compare_digest(
        creds.password, _ADMIN_PASS
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Q&M Admin"'},
        )
    return creds.username


@router.get("/enrollments")
def list_enrollments(status_filter: str | None = None, _: str = Depends(_auth)):
    return repo.enrollments_by_status(status_filter)


@router.get("/leads")
def list_leads(_: str = Depends(_auth)):
    return repo.all_leads()


@router.get("/reminders")
def list_reminders(_: str = Depends(_auth)):
    return repo.all_reminders()


@router.get("/staff-queue")
def staff_queue(_: str = Depends(_auth)):
    return repo.open_staff_tasks()


@router.get("/credit-notes")
def credit_notes(status_filter: str | None = None, _: str = Depends(_auth)):
    return repo.credit_notes_by_status(status_filter)


@router.post("/credit-notes/request")
def request_credit_note(enrollment_id: int, reason: str = "Cancellation", _: str = Depends(_auth)):
    return payment_service.request_credit_note(enrollment_id, reason)


@router.post("/credit-notes/{credit_note_id}/approve")
def approve_credit_note(credit_note_id: int, _: str = Depends(_auth)):
    return payment_service.approve_credit_note(credit_note_id)


@router.post("/reports/nightly")
def trigger_nightly_report(_: str = Depends(_auth)):
    path = payment_service.nightly_report()
    return {"ok": True, "report": path}


@router.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(_auth)) -> str:
    rows = repo.enrollments_by_status()
    tbody = "".join(
        f"<tr><td>{r.get('invoice_no') or '-'}</td><td>{r['full_name']}</td>"
        f"<td>{r['course_name']}</td><td>{r.get('course_date') or '-'}</td>"
        f"<td>S${r['net_payable']}</td><td>{r['status']}</td>"
        f"<td>{r.get('receipt_no') or '-'}</td></tr>"
        for r in rows
    ) or "<tr><td colspan='7'>No enrollments yet.</td></tr>"
    # Requirement 4 AC14: unlike customers.preferred_course/course_date
    # (single-slot, always the most recently discussed course, by design),
    # every row here is an independent reminder — the same participant can
    # legitimately appear more than once, one row per course+date they
    # asked to be reminded about.
    reminder_rows = repo.all_reminders()
    reminders_tbody = "".join(
        f"<tr><td>{r['whatsapp_id']}</td><td>{r['course_name']}</td>"
        f"<td>{r['course_date']}</td><td>{r.get('email') or '-'}</td>"
        f"<td>{r['created_at']}</td></tr>"
        for r in reminder_rows
    ) or "<tr><td colspan='5'>No reminders yet.</td></tr>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Q&M Admin Dashboard</title>
<style>
 body{{font-family:system-ui,Arial,sans-serif;margin:24px;color:#1F3864}}
 h1{{font-size:20px}} h2{{font-size:16px;margin-top:32px}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{border:1px solid #ccc;padding:6px 8px;text-align:left}}
 th{{background:#1F3864;color:#fff}} tr:nth-child(even){{background:#f3f5fb}}
</style></head><body>
<h1>Q&M AI Enquiry &amp; Enrollment — Admin Dashboard</h1>
<p>{settings.company_name}</p>
<table><thead><tr><th>Invoice</th><th>Name</th><th>Course</th><th>Date</th>
<th>Net Payable</th><th>Status</th><th>Receipt</th></tr></thead>
<tbody>{tbody}</tbody></table>
<h2>Reminders — one row per participant per course/date requested</h2>
<table><thead><tr><th>WhatsApp ID</th><th>Course</th><th>Intake Date</th>
<th>Email</th><th>Saved At</th></tr></thead>
<tbody>{reminders_tbody}</tbody></table>
</body></html>"""

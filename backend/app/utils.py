"""Small shared helpers: validation, money formatting, document numbers, short IDs."""
from __future__ import annotations

import re
from datetime import datetime

from . import database as db

# NRIC format per the proposal command schema (Section 10.3):
#   S/T/F/G + 7 digits + letter.  Format-only (no checksum) so the documented
#   sample S8512345A validates as shown in the mockups.
NRIC_RE = re.compile(r"^[STFGstfg]\d{7}[A-Za-z]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Short-code patterns
# Course  : C{YY}{ID:02d}  e.g. C2601, C2602
# Schedule: SH{YY}{ID:02d} e.g. SH2601, SH2603
_COURSE_CODE_RE   = re.compile(r"^[Cc](\d{2})(\d{2})$")
_SCHEDULE_CODE_RE = re.compile(r"^[Ss][Hh](\d{2})(\d{2})$")


def is_registered(customer: dict) -> bool:
    """True when all four registration fields are present and phone is
    confirmed. Single source of truth shared by orchestrator.py (the
    registration gate itself) and module_a.py (to decide whether Module B's
    agent may be offered as a delegation coworker this turn) — kept here
    rather than in orchestrator.py to avoid a circular import between
    orchestrator.py and module_a.py."""
    return bool(
        customer.get("phone_confirmed")
        and customer.get("full_name")
        and customer.get("nric")
        and customer.get("email")
    )


def valid_nric(nric: str) -> bool:
    return bool(NRIC_RE.match((nric or "").strip()))


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip()))


def money(amount) -> str:
    try:
        return f"S${float(amount):,.2f}"
    except (TypeError, ValueError):
        return f"S${amount}"


def _doc_number(prefix: str, seq_name: str) -> str:
    year = datetime.now().year
    n = db.next_seq(seq_name)
    return f"{prefix}-{year}-{n:04d}"


def new_invoice_no() -> str:
    return _doc_number("INV", "invoice_seq")


def new_receipt_no() -> str:
    return _doc_number("RCP", "receipt_seq")


def course_short_id(course_id: int) -> str:
    """Generate C{YY}{ID:02d} for a course DB row.  E.g. id=1, year=2026 → C2601."""
    yy = datetime.now().year % 100
    return f"C{yy:02d}{course_id:02d}"


def schedule_short_id(schedule_id: int) -> str:
    """Generate SH{YY}{ID:02d} for a schedule DB row.  E.g. id=3 → SH2603."""
    yy = datetime.now().year % 100
    return f"SH{yy:02d}{schedule_id:02d}"


def parse_course_code(code: str) -> int | None:
    """Parse a C-code to its database id.  'C2601' → 1.  Returns None if not a C-code."""
    m = _COURSE_CODE_RE.match((code or "").strip())
    return int(m.group(2)) if m else None


def parse_schedule_code(code: str) -> int | None:
    """Parse an SH-code to its database id.  'SH2603' → 3.  Returns None if not an SH-code."""
    m = _SCHEDULE_CODE_RE.match((code or "").strip())
    return int(m.group(2)) if m else None


def new_credit_note_no() -> str:
    return _doc_number("CN", "credit_note_seq")


def compute_fee(full_fee: float, sf_subsidy_cap: float) -> tuple[float, float, float]:
    """Return (fee, sf_subsidy, net_payable).

    SkillsFuture credit offsets up to the lesser of the cap and the fee; the net
    payable is what remains. Mirrors the /fees mockup (full fee, SFC claimable,
    net payable).
    """
    fee = float(full_fee)
    subsidy = min(float(sf_subsidy_cap), fee)
    net = round(fee - subsidy, 2)
    return round(fee, 2), round(subsidy, 2), net

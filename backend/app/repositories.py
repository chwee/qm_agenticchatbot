"""Data access layer — thin CRUD over PostgreSQL.

Grouped by entity. Business logic lives in app/services; CrewAI tools call
services; services call these repositories. Keeping SQL in one place keeps the
agent tools clean.

Identity model
──────────────
Every WhatsApp sender is identified in the customers table by their
whatsapp_id (the raw JID from the gateway, e.g. "6591234567@c.us" or
"153811586920512@lid").  phone is populated when confirmed.

All other tables (enrollments, payments, staff_queue, chat_memory) store
whatsapp_id as the per-user partition key so multi-user isolation is enforced
at the database level.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import database as db

log = logging.getLogger(__name__)


# ── Courses ──────────────────────────────────────────────────────────────────
def list_courses(active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM courses"
    if active_only:
        sql += " WHERE active = TRUE"
    sql += " ORDER BY id"
    return db.query_all(sql)


def get_course_by_name(name: str) -> Optional[dict]:
    """Fuzzy lookup by name/code (case-insensitive substring)."""
    term = f"%{name.strip().lower()}%"
    return db.query_one(
        """
        SELECT * FROM courses
        WHERE active = TRUE
          AND (lower(name) LIKE %s OR lower(code) LIKE %s)
        ORDER BY length(name) ASC
        LIMIT 1
        """,
        (term, term),
    )


def get_course(course_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM courses WHERE id = %s", (course_id,))


def get_course_by_course_id(course_id: str) -> Optional[dict]:
    """Look up an active course by its short display code (e.g. 'C2601')."""
    return db.query_one(
        "SELECT * FROM courses WHERE course_id = %s AND active = TRUE",
        (course_id.upper(),),
    )


def get_schedule(schedule_id: int) -> Optional[dict]:
    """Look up a single active schedule by its primary key."""
    return db.query_one(
        "SELECT * FROM course_schedules WHERE id = %s AND active = TRUE", (schedule_id,)
    )


def get_schedule_by_schedule_id(schedule_id: str) -> Optional[dict]:
    """Look up an active schedule by its short display code (e.g. 'SH2601')."""
    return db.query_one(
        "SELECT * FROM course_schedules WHERE schedule_id = %s AND active = TRUE",
        (schedule_id.upper(),),
    )


def get_schedules(course_id: int) -> list[dict]:
    return db.query_all(
        "SELECT * FROM course_schedules WHERE course_id = %s AND active = TRUE ORDER BY start_date",
        (course_id,),
    )


def adjust_schedule_seats(schedule_id: int, delta: int) -> Optional[dict]:
    """Increment (enrollment created) or decrement (enrollment cancelled) the
    seats taken for one intake. Clamped at 0 so a stray double-cancel can't
    push the count negative."""
    return db.query_one(
        """UPDATE course_schedules SET seats_taken = GREATEST(seats_taken + %s, 0)
           WHERE id = %s RETURNING *""",
        (delta, schedule_id),
    )


def next_schedule(course_id: int) -> Optional[dict]:
    rows = get_schedules(course_id)
    return rows[0] if rows else None


# ── Customers (central identity & verification store) ────────────────────────
#
# ISOLATION GUARANTEE: get_or_create_customer uses INSERT ... ON CONFLICT so
# two concurrent messages from different users never share a row.  All lookups
# below are keyed strictly by whatsapp_id — there is no shared mutable state
# between users.

def get_customer(whatsapp_id: str) -> Optional[dict]:
    """Look up a customer by their WhatsApp JID (primary key)."""
    return db.query_one(
        "SELECT * FROM customers WHERE whatsapp_id = %s", (whatsapp_id,)
    )


def get_customer_by_phone(phone: str) -> Optional[dict]:
    """Look up a confirmed customer by their real E.164 phone number."""
    return db.query_one(
        "SELECT * FROM customers WHERE phone = %s AND phone_confirmed = TRUE",
        (phone,),
    )


def get_or_create_customer(whatsapp_id: str, display_name: str | None = None) -> dict:
    """Return the customer row for this WhatsApp JID, creating it if new.

    Always updates last_contact_at and display_name (if supplied) so the record
    stays fresh without a separate UPDATE call on every message.
    """
    # Atomic upsert — no race condition between two simultaneous first messages
    row = db.query_one(
        """
        INSERT INTO customers (whatsapp_id, display_name)
        VALUES (%s, %s)
        ON CONFLICT (whatsapp_id) DO UPDATE
            SET last_contact_at = now(),
                display_name    = COALESCE(EXCLUDED.display_name, customers.display_name),
                updated_at      = now()
        RETURNING *
        """,
        (whatsapp_id, display_name),
    )
    return row  # type: ignore[return-value]


def register_customer(
    whatsapp_id: str,
    phone: str,
    full_name: str,
    nric: str,
    email: str,
) -> Optional[dict]:
    """Save full registration details for a customer.

    Sets phone_confirmed=TRUE and mirrors full_name into display_name per the
    registration requirement.  Raises ValueError if the phone is already owned
    by a different whatsapp_id.
    """
    existing = db.query_one(
        "SELECT whatsapp_id FROM customers WHERE phone = %s", (phone,)
    )
    if existing and existing["whatsapp_id"] != whatsapp_id:
        raise ValueError(f"Phone {phone} is already linked to another account")
    db.execute(
        """UPDATE customers
              SET phone           = %s,
                  phone_confirmed = TRUE,
                  full_name       = %s,
                  display_name    = %s,
                  nric            = %s,
                  email           = %s,
                  updated_at      = now()
            WHERE whatsapp_id = %s""",
        (phone, full_name, full_name, nric, email, whatsapp_id),
    )

    # Sync to legacy leads table so the admin view stays complete.
    # UPDATE by phone OR whatsapp_id covers leads created before phone was known.
    # If no row exists yet, INSERT one.
    try:
        updated = db.query_one(
            """UPDATE leads
                  SET name            = %s,
                      nric            = %s,
                      email           = %s,
                      phone_confirmed = TRUE,
                      updated_at      = now()
                WHERE phone = %s OR whatsapp_id = %s
               RETURNING id""",
            (full_name, nric, email, phone, whatsapp_id),
        )
        if updated is None:
            db.execute(
                """INSERT INTO leads (phone, whatsapp_id, name, nric, email, phone_confirmed)
                   VALUES (%s, %s, %s, %s, %s, TRUE)""",
                (phone, whatsapp_id, full_name, nric, email),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("leads sync skipped for wid=%s: %s", whatsapp_id, exc)

    return get_customer(whatsapp_id)


def set_customer_phone(whatsapp_id: str, phone: str) -> Optional[dict]:
    """Record the confirmed real phone for a customer.

    If another customer already owns this phone number we raise a ValueError so
    the caller can ask the user to send the correct number.
    """
    # Collision check: two @lid accounts cannot share the same real phone
    existing = db.query_one(
        "SELECT whatsapp_id FROM customers WHERE phone = %s", (phone,)
    )
    if existing and existing["whatsapp_id"] != whatsapp_id:
        raise ValueError(f"Phone {phone} is already linked to another account")

    db.execute(
        """UPDATE customers
              SET phone = %s, phone_confirmed = TRUE, updated_at = now()
            WHERE whatsapp_id = %s""",
        (phone, whatsapp_id),
    )
    return get_customer(whatsapp_id)


def update_customer(whatsapp_id: str, **fields: Any) -> Optional[dict]:
    """Partial update of customer profile fields."""
    if not fields:
        return get_customer(whatsapp_id)
    sets = ", ".join(f"{k} = %s" for k in fields)
    params = list(fields.values()) + [whatsapp_id]
    db.execute(
        f"UPDATE customers SET {sets}, updated_at = now() WHERE whatsapp_id = %s",
        params,
    )
    return get_customer(whatsapp_id)


def update_customer_course_interest(
    whatsapp_id: str, course_name: str, course_date: str | None = None,
) -> Optional[dict]:
    """Update preferred_course (and course_date, if given) — but if the
    course is CHANGING and no new course_date accompanies it, clears any
    existing course_date rather than leaving it silently attached to the
    new course. Plain update_customer() has no such guard: preferred_course
    and course_date can be set independently by different calls at
    different points in a conversation, so without this a course_date left
    over from an earlier, different course can survive a later switch and
    get misread as belonging to the new one (observed live: a participant
    switching from one course's completed reminder to a second, different
    course produced a THIRD, spurious reminder combining the new course
    with the old course's date, before the new one's own date was ever
    given)."""
    current = get_customer(whatsapp_id) or {}
    fields: dict[str, Any] = {"preferred_course": course_name}
    if course_date:
        fields["course_date"] = course_date
    elif course_name != current.get("preferred_course"):
        fields["course_date"] = ""
    return update_customer(whatsapp_id, **fields)


def set_customer_status(whatsapp_id: str, status: str) -> None:
    db.execute(
        "UPDATE customers SET status = %s, updated_at = now() WHERE whatsapp_id = %s",
        (status, whatsapp_id),
    )


def mark_customer_enquiry(whatsapp_id: str) -> None:
    """Promote a brand-new customer to 'enquiry' status on their first real
    interaction — never downgrades an existing enrolled/escalated/closed
    status. Without this, a customer who only ever enquires (never enrols)
    stays at the default 'new' status forever and is never picked up by
    customers_due_for_followup(), which filters on status."""
    db.execute(
        "UPDATE customers SET status = 'enquiry', updated_at = now() "
        "WHERE whatsapp_id = %s AND status = 'new'",
        (whatsapp_id,),
    )


def escalate_customer(whatsapp_id: str, reason: str) -> None:
    db.execute(
        """UPDATE customers
              SET escalated = TRUE, escalation_reason = %s,
                  status = 'escalated', updated_at = now()
            WHERE whatsapp_id = %s""",
        (reason, whatsapp_id),
    )


def all_customers() -> list[dict]:
    return db.query_all(
        "SELECT * FROM customers ORDER BY last_contact_at DESC"
    )


def customers_due_for_followup() -> list[dict]:
    """Customers awaiting response whose last contact crossed a nurture threshold.

    follow_up_count 0 → due after 24h
                    1 → due after 72h
                    2 → due after 7d
    After 3 follow-ups with no reply, stop (the agents handle escalation).
    """
    return db.query_all(
        """
        SELECT * FROM customers
        WHERE status IN ('enquiry','awaiting_response')
          AND escalated = FALSE
          AND follow_up_count < 3
          AND (
                (follow_up_count = 0 AND last_contact_at < now() - INTERVAL '24 hours')
             OR (follow_up_count = 1 AND last_contact_at < now() - INTERVAL '72 hours')
             OR (follow_up_count = 2 AND last_contact_at < now() - INTERVAL '7 days')
          )
        ORDER BY last_contact_at ASC
        """
    )


def bump_customer_followup(whatsapp_id: str) -> None:
    db.execute(
        """UPDATE customers
              SET follow_up_count = follow_up_count + 1,
                  last_contact_at = now(), updated_at = now()
            WHERE whatsapp_id = %s""",
        (whatsapp_id,),
    )


# ── Reminders (Requirement 4 AC14 — multiple simultaneous reminders) ─────────
# customers.preferred_course/course_date only ever track the single most
# recent course of interest; a completed reminder (course + a specific
# intake both known) gets its own row here instead, so a second reminder
# for a different course doesn't overwrite the first.
def create_reminder(
    whatsapp_id: str, course_name: str, course_date: str, email: str | None = None,
) -> Optional[dict]:
    """Upsert one reminder. The unique index on (whatsapp_id, course_name,
    course_date) is what prevents the same reminder being saved twice —
    calling this again with the same three values just refreshes email/
    updated_at rather than creating a duplicate row."""
    return db.query_one(
        """INSERT INTO reminders (whatsapp_id, course_name, course_date, email)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (whatsapp_id, course_name, course_date) DO UPDATE
               SET email = COALESCE(EXCLUDED.email, reminders.email),
                   updated_at = now()
           RETURNING *""",
        (whatsapp_id, course_name, course_date, email),
    )


def reminders_for_customer(whatsapp_id: str) -> list[dict]:
    return db.query_all(
        "SELECT * FROM reminders WHERE whatsapp_id = %s ORDER BY created_at",
        (whatsapp_id,),
    )


def all_reminders() -> list[dict]:
    """All reminders, across every participant — mirrors all_leads()'s
    pattern, for the same admin-visibility reason: without this, the only
    way to see that multiple independent reminders are actually being
    saved (as opposed to the single-slot customers.preferred_course/
    course_date, which is correct-by-design to only ever show the most
    recent one) was a raw SQL query, not anything in /admin/."""
    return db.query_all("SELECT * FROM reminders ORDER BY created_at DESC")


def reminders_due_for_dispatch(lead_days: int = 3) -> list[dict]:
    """Reminders whose course intake starts within `lead_days` and haven't
    been emailed yet (Requirement 10 AC5). Joins back to courses/
    course_schedules on name+label — the only way to recover a real DATE
    from `reminders.course_date`, which is just the intake's display label
    text (e.g. '21-22 Jul 2026'), mirrored verbatim at save time from
    course_schedules.label. A reminder with no matching schedule row (the
    course/intake was since removed) is silently excluded rather than
    erroring — nothing to remind about any more."""
    return db.query_all(
        """SELECT r.*, c.full_name AS customer_name
           FROM reminders r
           JOIN courses cr ON cr.name = r.course_name
           JOIN course_schedules cs ON cs.course_id = cr.id AND cs.label = r.course_date
           JOIN customers c ON c.whatsapp_id = r.whatsapp_id
           WHERE r.sent_at IS NULL
             AND cs.start_date BETWEEN CURRENT_DATE AND CURRENT_DATE + (%s || ' days')::interval
           ORDER BY cs.start_date""",
        (lead_days,),
    )


def mark_reminder_sent(reminder_id: int) -> None:
    db.execute("UPDATE reminders SET sent_at = now() WHERE id = %s", (reminder_id,))


def delete_reminder(whatsapp_id: str, course_name: str, course_date: str) -> None:
    """Remove one reminder row — used to clean up a row create_reminder()
    already persisted with a course_date that a later check determined
    doesn't actually belong to that course (the tool call that creates a
    reminder and the check that validates the date happen at different
    points in the same turn, so an invalid row can briefly exist before
    being caught)."""
    db.execute(
        "DELETE FROM reminders WHERE whatsapp_id = %s AND course_name = %s AND course_date = %s",
        (whatsapp_id, course_name, course_date),
    )


# ── Conversation state (Requirement 3 AC10-AC12 — sticky routing) ────────────
# Single source of truth for "which module/flow is this participant already
# mid-conversation with," written by Module A/B at their own existing
# pending-decision points and read by the Router (Tier 0) before it falls
# back to LLM/keyword classification — see crews/router.py: _sticky_dispatch().
def get_conversation_state(whatsapp_id: str) -> Optional[dict]:
    """The participant's pending flow, or None if there isn't one or it has
    expired — expiry is enforced here so a caller never has to separately
    check `expires_at` itself."""
    return db.query_one(
        "SELECT * FROM conversation_state WHERE whatsapp_id = %s"
        " AND active_module IS NOT NULL AND (expires_at IS NULL OR expires_at > now())",
        (whatsapp_id,),
    )


def set_conversation_state(whatsapp_id: str, active_module: str, active_flow: str) -> None:
    """Record (or refresh) a pending flow for this participant, always
    resetting the 30-minute expiry — a module calls this every turn its own
    reply still leaves a question open, so a flow that's still genuinely
    active never goes stale mid-exchange."""
    db.execute(
        """INSERT INTO conversation_state (whatsapp_id, active_module, active_flow, updated_at, expires_at)
           VALUES (%s, %s, %s, now(), now() + INTERVAL '30 minutes')
           ON CONFLICT (whatsapp_id) DO UPDATE
               SET active_module = EXCLUDED.active_module,
                   active_flow   = EXCLUDED.active_flow,
                   updated_at    = now(),
                   expires_at    = now() + INTERVAL '30 minutes'""",
        (whatsapp_id, active_module, active_flow),
    )


def clear_conversation_state_if_owner(whatsapp_id: str, active_module: str) -> None:
    """Clear the pending flow — but ONLY if it's currently owned by
    `active_module` (Requirement 3 AC12). A module must never clear a
    pending flow recorded by the OTHER module — e.g. Module A answering an
    unrelated enquiry must not wipe out a still-open Module B enrolment
    confirmation the participant hasn't gotten back to yet."""
    db.execute(
        "UPDATE conversation_state SET active_module = NULL, active_flow = NULL, updated_at = now()"
        " WHERE whatsapp_id = %s AND active_module = %s",
        (whatsapp_id, active_module),
    )


# ── Leads (legacy — kept for backward compat, agents may still reference) ────
def get_lead(phone: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM leads WHERE phone = %s", (phone,))


def get_lead_by_whatsapp_id(whatsapp_id: str) -> Optional[dict]:
    return db.query_one(
        "SELECT * FROM leads WHERE whatsapp_id = %s", (whatsapp_id,)
    )


def upsert_lead(phone: str, **fields: Any) -> dict:
    existing = get_lead(phone)
    fields = {k: v for k, v in fields.items() if v is not None}
    if existing:
        if fields:
            sets = ", ".join(f"{k} = %s" for k in fields)
            params = list(fields.values()) + [phone]
            db.execute(
                f"UPDATE leads SET {sets}, last_contact_at = now(), updated_at = now() WHERE phone = %s",
                params,
            )
        else:
            db.execute(
                "UPDATE leads SET last_contact_at = now(), updated_at = now() WHERE phone = %s",
                (phone,),
            )
        return get_lead(phone)  # type: ignore[return-value]

    cols = ["phone"] + list(fields.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    params = [phone] + list(fields.values())
    db.execute(
        f"INSERT INTO leads ({', '.join(cols)}) VALUES ({placeholders})",
        params,
    )
    return get_lead(phone)  # type: ignore[return-value]


def set_lead_status(phone: str, status: str) -> None:
    db.execute(
        "UPDATE leads SET status = %s, updated_at = now() WHERE phone = %s",
        (status, phone),
    )


def escalate_lead(phone: str, reason: str) -> None:
    db.execute(
        """UPDATE leads SET escalated = TRUE, escalation_reason = %s,
           status = 'escalated', updated_at = now() WHERE phone = %s""",
        (reason, phone),
    )


def all_leads() -> list[dict]:
    return db.query_all("SELECT * FROM leads ORDER BY updated_at DESC")


def leads_due_for_followup() -> list[dict]:
    """Legacy — prefer customers_due_for_followup() for new code."""
    return db.query_all(
        """
        SELECT * FROM leads
        WHERE status IN ('enquiry','awaiting_response')
          AND escalated = FALSE
          AND (
                (follow_up_count = 0 AND last_contact_at < now() - INTERVAL '24 hours')
             OR (follow_up_count = 1 AND last_contact_at < now() - INTERVAL '72 hours')
             OR (follow_up_count = 2 AND last_contact_at < now() - INTERVAL '7 days')
          )
        ORDER BY last_contact_at ASC
        """
    )


def bump_followup(phone: str) -> None:
    db.execute(
        """UPDATE leads SET follow_up_count = follow_up_count + 1,
           last_contact_at = now(), updated_at = now() WHERE phone = %s""",
        (phone,),
    )


# ── Chat memory (keyed by whatsapp_id) ───────────────────────────────────────
def add_memory(whatsapp_id: str, role: str, content: str) -> None:
    """Append a conversation turn to this user's history.

    whatsapp_id is the stable partition key — one user's history is always
    isolated from another's regardless of whether they share a real phone or
    whether the phone is known yet.
    """
    db.execute(
        "INSERT INTO chat_memory (whatsapp_id, role, content) VALUES (%s, %s, %s)",
        (whatsapp_id, role, content),
    )


def recent_memory(whatsapp_id: str, limit: int = 10) -> list[dict]:
    """Return the last `limit` turns for this user, oldest first."""
    rows = db.query_all(
        """SELECT role, content FROM chat_memory
           WHERE whatsapp_id = %s
           ORDER BY created_at DESC LIMIT %s""",
        (whatsapp_id, limit),
    )
    return list(reversed(rows))


# ── Enrollments (Module B) ───────────────────────────────────────────────────
def create_enrollment(**fields: Any) -> dict:
    cols = list(fields.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    params = list(fields.values())
    row = db.query_one(
        f"INSERT INTO enrollments ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *",
        params,
    )
    return row  # type: ignore[return-value]


def get_enrollment(enrollment_id: int) -> Optional[dict]:
    return db.query_one(
        "SELECT * FROM enrollments WHERE id = %s", (enrollment_id,)
    )


def get_enrollment_by_invoice(invoice_no: str) -> Optional[dict]:
    return db.query_one(
        "SELECT * FROM enrollments WHERE invoice_no = %s", (invoice_no.strip(),)
    )


def latest_enrollment(whatsapp_id: str) -> Optional[dict]:
    """Most recent enrollment for this user (by whatsapp_id)."""
    return db.query_one(
        """SELECT * FROM enrollments WHERE whatsapp_id = %s
           ORDER BY created_at DESC LIMIT 1""",
        (whatsapp_id,),
    )


def latest_enrollment_for_phone(phone: str) -> Optional[dict]:
    """Legacy lookup by phone — prefer latest_enrollment(whatsapp_id)."""
    return db.query_one(
        "SELECT * FROM enrollments WHERE phone = %s ORDER BY created_at DESC LIMIT 1",
        (phone,),
    )


def enrollments_by_whatsapp_id(whatsapp_id: str) -> list[dict]:
    return db.query_all(
        "SELECT * FROM enrollments WHERE whatsapp_id = %s ORDER BY created_at DESC",
        (whatsapp_id,),
    )


def enrollments_for_customer(whatsapp_id: str, phone: str | None = None) -> list[dict]:
    """Every enrollment for this customer, most recent first.

    whatsapp_id is the primary, always-present key; phone is a legacy fallback
    for records created before whatsapp_id was tracked.
    """
    rows = enrollments_by_whatsapp_id(whatsapp_id)
    if rows or not phone:
        return rows
    return db.query_all(
        "SELECT * FROM enrollments WHERE phone = %s ORDER BY created_at DESC",
        (phone,),
    )


def active_enrollment_for_course(
    whatsapp_id: str,
    course_id: int,
    phone: str | None = None,
    course_date: str | None = None,
) -> Optional[dict]:
    """Return a non-cancelled enrollment for this user + course + date, or None.

    course_date must be supplied so that enrolling on a different intake of the
    same course is correctly allowed.
    """
    date_filter = "AND course_date = %s" if course_date else ""

    def _params(key: str) -> list:
        base = [key, course_id]
        if course_date:
            base.append(course_date)
        return base

    row = db.query_one(
        f"""SELECT * FROM enrollments
           WHERE whatsapp_id = %s AND course_id = %s AND status != 'cancelled'
           {date_filter}
           ORDER BY created_at DESC LIMIT 1""",
        _params(whatsapp_id),
    )
    if row is None and phone:
        row = db.query_one(
            f"""SELECT * FROM enrollments
               WHERE phone = %s AND course_id = %s AND status != 'cancelled'
               {date_filter}
               ORDER BY created_at DESC LIMIT 1""",
            _params(phone),
        )
    return row


def update_enrollment(enrollment_id: int, **fields: Any) -> Optional[dict]:
    if not fields:
        return get_enrollment(enrollment_id)
    sets = ", ".join(f"{k} = %s" for k in fields)
    params = list(fields.values()) + [enrollment_id]
    return db.query_one(
        f"UPDATE enrollments SET {sets}, updated_at = now() WHERE id = %s RETURNING *",
        params,
    )


def enrollments_by_status(status: str | None = None) -> list[dict]:
    if status:
        return db.query_all(
            "SELECT * FROM enrollments WHERE status = %s ORDER BY created_at DESC",
            (status,),
        )
    return db.query_all("SELECT * FROM enrollments ORDER BY created_at DESC")


# ── Payments (Module C) ──────────────────────────────────────────────────────
def record_payment(**fields: Any) -> dict:
    if isinstance(fields.get("raw_extract"), (dict, list)):
        fields["raw_extract"] = json.dumps(fields["raw_extract"])
    cols = list(fields.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    params = list(fields.values())
    row = db.query_one(
        f"INSERT INTO payments ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *",
        params,
    )
    return row  # type: ignore[return-value]


def update_payment(payment_id: int, **fields: Any) -> Optional[dict]:
    if not fields:
        return db.query_one("SELECT * FROM payments WHERE id = %s", (payment_id,))
    if isinstance(fields.get("raw_extract"), (dict, list)):
        fields["raw_extract"] = json.dumps(fields["raw_extract"])
    sets = ", ".join(f"{k} = %s" for k in fields)
    params = list(fields.values()) + [payment_id]
    return db.query_one(
        f"UPDATE payments SET {sets} WHERE id = %s RETURNING *",
        params,
    )


def pending_payment_for_enrollment(enrollment_id: int) -> Optional[dict]:
    """The still-unresolved placeholder payment row created at invoice time, if any."""
    return db.query_one(
        """SELECT * FROM payments WHERE enrollment_id = %s AND verdict = 'pending'
           ORDER BY created_at DESC LIMIT 1""",
        (enrollment_id,),
    )


def payments_for_enrollment(enrollment_id: int) -> list[dict]:
    """Every payment proof submitted for this enrollment, oldest first."""
    return db.query_all(
        "SELECT * FROM payments WHERE enrollment_id = %s ORDER BY created_at ASC",
        (enrollment_id,),
    )


def confirmed_payments_for_enrollment(enrollment_id: int) -> list[dict]:
    """Confirmed payment proofs only — the ones that count toward the fee tally."""
    return db.query_all(
        """SELECT * FROM payments WHERE enrollment_id = %s AND verdict = 'confirmed'
           ORDER BY created_at ASC""",
        (enrollment_id,),
    )


# ── Staff queue (escalations) ────────────────────────────────────────────────
def add_staff_task(
    whatsapp_id: str,
    reason: str,
    message: str,
    module: str,
    phone: str | None = None,
    payload: dict | None = None,
) -> dict:
    row = db.query_one(
        """INSERT INTO staff_queue (whatsapp_id, phone, reason, message, module, payload)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (whatsapp_id, phone, reason, message, module, json.dumps(payload or {})),
    )
    return row  # type: ignore[return-value]


def open_staff_tasks() -> list[dict]:
    return db.query_all(
        "SELECT * FROM staff_queue WHERE status = 'open' ORDER BY created_at DESC"
    )


# ── Credit notes (Module C) ──────────────────────────────────────────────────
def create_credit_note(
    enrollment_id: int, reason: str, amount: float, credit_note_no: str
) -> dict:
    row = db.query_one(
        """INSERT INTO credit_notes (enrollment_id, reason, amount, credit_note_no)
           VALUES (%s, %s, %s, %s) RETURNING *""",
        (enrollment_id, reason, amount, credit_note_no),
    )
    return row  # type: ignore[return-value]


def approve_credit_note(credit_note_id: int) -> Optional[dict]:
    return db.query_one(
        "UPDATE credit_notes SET status = 'approved', resolved_at = now() WHERE id = %s RETURNING *",
        (credit_note_id,),
    )


def credit_notes_pending_dispatch() -> list[dict]:
    """Approved credit notes whose PDF/email fan-out hasn't run yet
    (Requirement 11 AC3) — read by services/payments.py:
    dispatch_pending_credit_notes(), decoupled from the synchronous staff
    approval action so PDF generation/email latency never blocks it."""
    return db.query_all(
        "SELECT * FROM credit_notes WHERE status = 'approved' AND notified_at IS NULL"
        " ORDER BY resolved_at"
    )


def mark_credit_note_notified(credit_note_id: int) -> None:
    db.execute(
        "UPDATE credit_notes SET notified_at = now() WHERE id = %s", (credit_note_id,)
    )


def credit_notes_by_status(status: str | None = None) -> list[dict]:
    if status:
        return db.query_all(
            "SELECT * FROM credit_notes WHERE status = %s ORDER BY created_at DESC",
            (status,),
        )
    return db.query_all("SELECT * FROM credit_notes ORDER BY created_at DESC")

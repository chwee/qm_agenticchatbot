"""PostgreSQL access — a small connection pool plus query helpers.

Uses psycopg 3 with a dict row factory so every row is a plain dict. autocommit
is on, which suits this app's short, independent statements (the WhatsApp command
schema is stateless — one message, one transactional unit of work).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Sequence

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import BASE_DIR, settings

log = logging.getLogger(__name__)

_pool: Optional[ConnectionPool] = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )
    return _pool


def query_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    with get_pool().connection() as conn:
        return conn.execute(sql, params or ()).fetchall()


def query_one(sql: str, params: Sequence[Any] | None = None) -> Optional[dict]:
    with get_pool().connection() as conn:
        return conn.execute(sql, params or ()).fetchone()


def execute(sql: str, params: Sequence[Any] | None = None) -> None:
    with get_pool().connection() as conn:
        conn.execute(sql, params or ())


def next_seq(seq_name: str) -> int:
    """Return the next value of a Postgres sequence (for document numbers)."""
    row = query_one("SELECT nextval(%s) AS v", (seq_name,))
    return int(row["v"])


# ── Schema bootstrap (for native Postgres without docker initdb) ─────────────
def _split_statements(sql_text: str) -> list[str]:
    """Split a script into executable statements.

    The project's .sql files contain no PL/pgSQL bodies or semicolons inside
    string literals, so a simple split on ';' after stripping line comments is
    safe and avoids needing executescript support.
    """
    statements: list[str] = []
    for chunk in sql_text.split(";"):
        lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            statements.append(stmt)
    return statements


def run_sql_file(path: Path) -> None:
    sql_text = path.read_text(encoding="utf-8")
    with get_pool().connection() as conn:
        for stmt in _split_statements(sql_text):
            conn.execute(stmt)


# Schema-only DDL — all use IF NOT EXISTS / IF EXISTS guards so they are
# genuine no-ops on every restart once the columns exist.
_SCHEMA_MIGRATIONS = [
    "ALTER TABLE payments       ADD COLUMN IF NOT EXISTS invoice_no  TEXT",
    "ALTER TABLE leads          ADD COLUMN IF NOT EXISTS course_date TEXT",
    "ALTER TABLE courses        ADD COLUMN IF NOT EXISTS course_id   TEXT",
    "ALTER TABLE course_schedules ADD COLUMN IF NOT EXISTS schedule_id TEXT",
    # Allow the 'pending' verdict (placeholder payment row created at invoice
    # time) on databases created before it existed. Drop+recreate every
    # restart so it's idempotent even though ADD CONSTRAINT has no IF NOT
    # EXISTS form.
    "ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_verdict_check",
    "ALTER TABLE payments ADD CONSTRAINT payments_verdict_check"
    " CHECK (verdict IN ('pending','confirmed','mismatch','unreadable'))",
    # An invoice can now be settled by multiple typed payment proofs (a
    # SkillsFuture claim screenshot + a PayNow screenshot); each gets its own
    # receipt. Same drop+recreate idempotency approach as verdict above.
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_type TEXT",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS receipt_no   TEXT",
    "ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_payment_type_check",
    "ALTER TABLE payments ADD CONSTRAINT payments_payment_type_check"
    " CHECK (payment_type IN ('paynow','skillsfuture_claim'))",
    # Seat tracking: course_schedules.seats_taken is incremented on a
    # successful enrollment and decremented on cancellation; enrollments.
    # schedule_id links each enrollment to the specific intake it took a
    # seat against, so cancellation can release the right one.
    "ALTER TABLE course_schedules ADD COLUMN IF NOT EXISTS seats_taken INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS schedule_id INTEGER"
    " REFERENCES course_schedules(id) ON DELETE SET NULL",
    # Mirrors save_lead()'s course_date onto the authoritative customers
    # table (Requirement 4 AC13) — previously only the deprecated, phone-
    # keyed `leads` table had this column, so a reminder's resolved intake
    # had nowhere on `customers` to be read back from, no matter how many
    # times it was "saved".
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS course_date TEXT",
    # New table for an existing database — schema.sql's own CREATE TABLE
    # only runs for a brand-new install (see init_db() below), so this is
    # the statement that actually takes effect against one already running.
    # Requirement 4 AC14: a participant can have more than one simultaneous
    # reminder; customers.preferred_course/course_date only ever track the
    # single most recent one, so a completed reminder gets its own row here.
    """CREATE TABLE IF NOT EXISTS reminders (
        id              SERIAL PRIMARY KEY,
        whatsapp_id     TEXT NOT NULL REFERENCES customers(whatsapp_id) ON DELETE CASCADE,
        course_name     TEXT NOT NULL,
        course_date     TEXT NOT NULL,
        email           TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # Requirement 3 AC10-AC12: sticky routing state — which module/flow a
    # participant is already mid-conversation with, so the Router can
    # dispatch their next reply straight back without re-deriving intent
    # from raw chat text (see router.py: _sticky_dispatch()).
    """CREATE TABLE IF NOT EXISTS conversation_state (
        whatsapp_id     TEXT PRIMARY KEY REFERENCES customers(whatsapp_id) ON DELETE CASCADE,
        active_module   TEXT,
        active_flow     TEXT,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at      TIMESTAMPTZ
    )""",
    # Requirement 10 AC5 — reminders were being saved but never actually
    # dispatched (scheduler.py had no job reading the table at all); this is
    # what lets run_reminder_dispatch() tell an already-sent reminder from
    # one still due.
    "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ",
    # Requirement 11 AC3 — decouples credit-note PDF generation/email from
    # the synchronous staff-approval action; dispatch_pending_credit_notes()
    # queries status='approved' AND notified_at IS NULL.
    "ALTER TABLE credit_notes ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ",
]

# Unique indexes — created once, never dropped on restart.
_INDEX_MIGRATIONS = [
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_courses_course_id"
    " ON courses(course_id) WHERE course_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_course_schedules_schedule_id"
    " ON course_schedules(schedule_id) WHERE schedule_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_receipt_no"
    " ON payments(receipt_no) WHERE receipt_no IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_reminders_wid_course_date"
    " ON reminders(whatsapp_id, course_name, course_date)",
]

# Expected code → DB code mapping used by the data-fix check.
_COURSE_CODES   = [("DACERT", "C2601"), ("INFCTRL", "C2602")]
_SCHEDULE_CODES = [
    ("DACERT",  "2026-07-21", "SH2601"),
    ("DACERT",  "2026-08-18", "SH2602"),
    ("INFCTRL", "2026-07-14", "SH2603"),
    ("INFCTRL", "2026-08-11", "SH2604"),
]


def _run_migrations(stmts: list[str]) -> None:
    for sql in stmts:
        try:
            execute(sql)
        except Exception as e:  # noqa: BLE001
            log.warning("Migration skipped (%s...): %s", sql[:60], e)


def _codes_need_fixing() -> bool:
    """Return True only if any course_id or schedule_id is wrong or missing."""
    for code, expected in _COURSE_CODES:
        row = query_one("SELECT course_id FROM courses WHERE code = %s", (code,))
        if row and row.get("course_id") != expected:
            return True

    for course_code, start_date, expected in _SCHEDULE_CODES:
        row = query_one(
            """SELECT cs.schedule_id
               FROM course_schedules cs
               JOIN courses c ON c.id = cs.course_id
               WHERE c.code = %s AND cs.start_date = %s""",
            (course_code, start_date),
        )
        if row and row.get("schedule_id") != expected:
            return True

    return False


def _fix_short_codes() -> None:
    """Correct course_id / schedule_id values.

    Only called when _codes_need_fixing() returned True.  Drops the unique
    indexes first to avoid constraint violations during the update, then
    recreates them after.
    """
    log.info("Short codes are wrong or missing — fixing now...")
    _run_migrations([
        "DROP INDEX IF EXISTS uq_courses_course_id",
        "DROP INDEX IF EXISTS uq_course_schedules_schedule_id",
    ])
    for code, cid in _COURSE_CODES:
        execute("UPDATE courses SET course_id = %s WHERE code = %s", (cid, code))
    for course_code, start_date, sid in _SCHEDULE_CODES:
        execute(
            """UPDATE course_schedules SET schedule_id = %s
               WHERE start_date = %s
               AND course_id = (SELECT id FROM courses WHERE code = %s)""",
            (sid, start_date, course_code),
        )
    _run_migrations(_INDEX_MIGRATIONS)
    log.info("Short codes fixed.")


def init_db() -> None:
    """Bootstrap the database on startup.

    Fresh install : schema DDL → seed data → schema migrations → index creation
    Existing DB   : schema migrations (ADD COLUMN IF NOT EXISTS — no-ops) →
                    code check (read-only) → fix only if wrong → index creation

    A correctly seeded existing database performs ZERO data writes on restart.
    """
    db_dir = BASE_DIR / "db"
    is_new = not (query_one("SELECT to_regclass('public.courses') AS t") or {}).get("t")
    if is_new:
        log.info("Initialising database schema and seed data...")
        run_sql_file(db_dir / "schema.sql")
        run_sql_file(db_dir / "seed.sql")
        log.info("Schema + seed applied.")

    # ADD COLUMN IF NOT EXISTS — true no-ops once columns exist.
    _run_migrations(_SCHEMA_MIGRATIONS)

    # Data fix: read-only check first; only writes if values are actually wrong.
    if _codes_need_fixing():
        _fix_short_codes()
    else:
        log.debug("Course/schedule codes are correct — no data changes on startup.")

    # CREATE UNIQUE INDEX IF NOT EXISTS — no-op if already present.
    _run_migrations(_INDEX_MIGRATIONS)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None

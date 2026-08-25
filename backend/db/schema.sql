-- ============================================================================
-- Q&M AI Enquiry & Enrollment System — PostgreSQL schema
-- ============================================================================
-- State / Memory layer (proposal Section 5.6):
--   customers     → central identity store (whatsapp_id is the primary key)
--   chat_memory   → per-user conversation history (keyed by whatsapp_id)
--   leads         → DEPRECATED alias kept for backward compat; use customers
--   Module B      → enrollments (status state machine)
--   Module C      → payments + credit_notes; accountant portal reads same DB
-- Idempotent: safe to run more than once.
-- ============================================================================

-- ── Courses (Module A FAQ / fee / schedule source of truth) ─────────────────
CREATE TABLE IF NOT EXISTS courses (
    id                  SERIAL PRIMARY KEY,
    code                TEXT UNIQUE NOT NULL,   -- internal code e.g. DACERT, INFCTRL
    course_id           TEXT,                   -- short display code e.g. C2601, C2602
    name                TEXT NOT NULL,
    full_fee            NUMERIC(10,2) NOT NULL,
    sf_subsidy_cap      NUMERIC(10,2) NOT NULL DEFAULT 0,
    description         TEXT,
    learning_outcomes   TEXT,
    entry_requirements  TEXT,
    job_pathways        TEXT,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Course intake dates ──────────────────────────────────────────────────────
-- seats_taken is maintained by the enrollment pipeline, not edited directly:
-- +1 when an enrollment against this schedule is created, -1 when that
-- enrollment is later cancelled (see services/enrollment.py, services/payments.py).
CREATE TABLE IF NOT EXISTS course_schedules (
    id          SERIAL PRIMARY KEY,
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    schedule_id TEXT,                           -- short display code e.g. SH2601, SH2602
    label       TEXT NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    seats       INTEGER NOT NULL DEFAULT 20,
    seats_taken INTEGER NOT NULL DEFAULT 0,
    active      BOOLEAN NOT NULL DEFAULT TRUE
);
-- Migration: add seats_taken to existing course_schedules tables (safe to re-run)
ALTER TABLE course_schedules ADD COLUMN IF NOT EXISTS seats_taken INTEGER NOT NULL DEFAULT 0;

-- ── Customers — central identity & verification store ────────────────────────
-- Every WhatsApp sender gets exactly ONE row here, identified by whatsapp_id.
-- This table is the authoritative reference for all other tables.
--
-- whatsapp_id  The raw WhatsApp JID sent by the gateway, e.g.:
--                @c.us accounts  →  "6591234567@c.us"   (legacy, phone = id)
--                @lid  accounts  →  "153811586920512@lid" (Linked Identity)
--              This value is STABLE — it never changes for a given user.
--              It is the ONLY reliable identifier arriving with every message.
--
-- phone        Real E.164 number (digits only, no +).
--              • Set immediately for @c.us accounts (gateway extracts it).
--              • NULL for @lid accounts until the user confirms their number
--                via the one-time prompt.
--              • UNIQUE — two customers cannot share the same phone.
--
-- phone_confirmed  TRUE once phone is trusted (either @c.us auto-resolved
--              or user replied with their number and it passed validation).
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    id                  SERIAL PRIMARY KEY,
    whatsapp_id         TEXT UNIQUE NOT NULL,
    phone               TEXT,
    phone_confirmed     BOOLEAN NOT NULL DEFAULT FALSE,
    display_name        TEXT,           -- WhatsApp notifyName, refreshed each contact
    full_name           TEXT,           -- real name from enrollment form
    nric                TEXT,
    email               TEXT,
    preferred_course    TEXT,
    course_date         TEXT,           -- preferred intake / schedule label (for a reminder or in-progress enquiry, not an enrolment record — see `enrollments.course_date` for that)
    status              TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new','enquiry','awaiting_response',
                                          'enrolled','escalated','closed')),
    follow_up_count     INTEGER NOT NULL DEFAULT 0,
    last_contact_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    escalated           BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_reason   TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Migration: add course_date to existing customers tables (safe to re-run)
ALTER TABLE customers ADD COLUMN IF NOT EXISTS course_date TEXT;
-- Partial unique index: phone uniqueness only when phone is set
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_phone
    ON customers(phone) WHERE phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customers_status
    ON customers(status);

-- ── Reminders (Module A — multiple simultaneous reminders per customer) ──────
-- customers.preferred_course/course_date only ever track the single most
-- recent course of interest — a participant asking for a reminder about a
-- second, different course would otherwise silently overwrite the first.
-- Each completed reminder (a course AND a specific intake both known) gets
-- its own row here instead; the unique index below is what prevents the
-- same course+date being saved twice for one participant.
CREATE TABLE IF NOT EXISTS reminders (
    id              SERIAL PRIMARY KEY,
    whatsapp_id     TEXT NOT NULL REFERENCES customers(whatsapp_id) ON DELETE CASCADE,
    course_name     TEXT NOT NULL,
    course_date     TEXT NOT NULL,          -- intake label, e.g. '21-22 Jul 2026'
    email           TEXT,
    sent_at         TIMESTAMPTZ,            -- Requirement 10 AC5 — set once scheduler.py has emailed it
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reminders_wid_course_date
    ON reminders(whatsapp_id, course_name, course_date);
-- Migration: add sent_at to existing reminders tables (safe to re-run)
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;

-- ── Conversation state (Router — Requirement 3 AC10-AC12, sticky dispatch) ───
-- Single source of truth for "which module/flow is this participant already
-- mid-conversation with." Written by Module A/B at the exact points each
-- already knows its own reply left a question open; read by the Router
-- (Tier 0, ahead of LLM/keyword classification) so the participant's very
-- next reply is dispatched straight back without re-deriving intent from
-- raw chat text. A module only ever clears state it recorded itself.
CREATE TABLE IF NOT EXISTS conversation_state (
    whatsapp_id     TEXT PRIMARY KEY REFERENCES customers(whatsapp_id) ON DELETE CASCADE,
    active_module   TEXT,               -- 'A' | 'B' | NULL (idle)
    active_flow     TEXT,               -- e.g. 'awaiting_reminder', 'awaiting_enroll_confirm'
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ         -- stale flows auto-release rather than trapping later turns
);

-- ── Leads (legacy alias — kept for backward compat) ──────────────────────────
-- New code should use the customers table directly.
-- Retained so existing tools/admin that reference leads still compile.
CREATE TABLE IF NOT EXISTS leads (
    id                  SERIAL PRIMARY KEY,
    phone               TEXT UNIQUE NOT NULL,
    whatsapp_id         TEXT UNIQUE,
    phone_confirmed     BOOLEAN NOT NULL DEFAULT FALSE,
    name                TEXT,
    nric                TEXT,
    dob                 DATE,
    email               TEXT,
    preferred_course    TEXT,
    course_date         TEXT,           -- preferred intake / schedule label
    status              TEXT NOT NULL DEFAULT 'enquiry'
                        CHECK (status IN ('enquiry','awaiting_response',
                                          'enrolled','escalated','closed')),
    follow_up_count     INTEGER NOT NULL DEFAULT 0,
    last_contact_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    escalated           BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_reason   TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Enrollments (Module B pipeline) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS enrollments (
    id              SERIAL PRIMARY KEY,
    whatsapp_id     TEXT NOT NULL,          -- FK to customers.whatsapp_id
    phone           TEXT NOT NULL,          -- denormalised for human reference
    course_id       INTEGER REFERENCES courses(id),
    course_name     TEXT NOT NULL,
    course_date     TEXT,
    schedule_id     INTEGER REFERENCES course_schedules(id) ON DELETE SET NULL,
    full_name       TEXT NOT NULL,
    nric            TEXT NOT NULL,
    email           TEXT NOT NULL,
    fee             NUMERIC(10,2) NOT NULL DEFAULT 0,
    sf_subsidy      NUMERIC(10,2) NOT NULL DEFAULT 0,
    net_payable     NUMERIC(10,2) NOT NULL DEFAULT 0,
    invoice_no      TEXT UNIQUE,
    receipt_no      TEXT UNIQUE,
    status          TEXT NOT NULL DEFAULT 'enrolled'
                    CHECK (status IN ('enquiry','enrolled','invoice_sent',
                                      'awaiting_payment','paid',
                                      'receipt_issued','cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_enrollments_whatsapp ON enrollments(whatsapp_id);
CREATE INDEX IF NOT EXISTS idx_enrollments_phone    ON enrollments(phone);
CREATE INDEX IF NOT EXISTS idx_enrollments_status   ON enrollments(status);
-- Migration: add schedule_id to existing enrollments tables (safe to re-run)
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS schedule_id INTEGER REFERENCES course_schedules(id) ON DELETE SET NULL;

-- ── Payments (Module C verification ledger) ──────────────────────────────────
-- One row is created per enrollment as soon as the invoice is issued
-- (verdict='pending', expected_amount=full course fee, no detected_amount yet).
-- An invoice may be settled by MULTIPLE payment proofs of different types —
-- e.g. a SkillsFuture claim screenshot covering the subsidised portion, plus a
-- PayNow screenshot covering the net payable — since the two are submitted as
-- separate images/messages (WhatsApp delivers one image per message; there is
-- no multi-image batch). The first verification attempt (of either type)
-- resolves the pending placeholder row in place; every payment after that
-- (a second type, or a retry after a mismatch/unreadable attempt) inserts an
-- additional row, so the table keeps one row per payment proof submitted.
-- Completion is decided by summing all 'confirmed' rows for the enrollment
-- and comparing the total to the enrollment's full fee (see services/payments.py).
CREATE TABLE IF NOT EXISTS payments (
    id               SERIAL PRIMARY KEY,
    enrollment_id    INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
    invoice_no       TEXT,                  -- denormalised from enrollments for quick lookup
    whatsapp_id      TEXT NOT NULL,
    phone            TEXT NOT NULL,
    payment_type     TEXT CHECK (payment_type IN ('paynow','skillsfuture_claim')),
    expected_amount  NUMERIC(10,2),         -- remaining balance for this payment's type at submission time
    detected_amount  NUMERIC(10,2),
    reference        TEXT,
    paid_at_text     TEXT,
    verdict          TEXT NOT NULL
                     CHECK (verdict IN ('pending','confirmed','mismatch','unreadable')),
    confidence       NUMERIC(4,3),
    raw_extract      JSONB,
    notes            TEXT,
    receipt_no       TEXT,                  -- one receipt per confirmed payment proof
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Migration: add invoice_no to existing payments tables (safe to re-run)
ALTER TABLE payments ADD COLUMN IF NOT EXISTS invoice_no TEXT;
-- Migration: add payment_type + per-payment receipt_no (safe to re-run)
ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_type TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS receipt_no   TEXT;
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_payment_type_check;
ALTER TABLE payments ADD CONSTRAINT payments_payment_type_check
    CHECK (payment_type IN ('paynow','skillsfuture_claim'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_receipt_no
    ON payments(receipt_no) WHERE receipt_no IS NOT NULL;
-- Migration: allow the 'pending' verdict on tables created before it existed
-- (column-level CHECK constraints are auto-named '<table>_<column>_check').
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_verdict_check;
ALTER TABLE payments ADD CONSTRAINT payments_verdict_check
    CHECK (verdict IN ('pending','confirmed','mismatch','unreadable'));
CREATE INDEX IF NOT EXISTS idx_payments_enrollment  ON payments(enrollment_id);
CREATE INDEX IF NOT EXISTS idx_payments_whatsapp    ON payments(whatsapp_id);

-- ── Conversational memory (keyed by whatsapp_id) ─────────────────────────────
CREATE TABLE IF NOT EXISTS chat_memory (
    id          SERIAL PRIMARY KEY,
    whatsapp_id TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_memory_wid ON chat_memory(whatsapp_id, created_at);

-- ── Staff escalation queue ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staff_queue (
    id          SERIAL PRIMARY KEY,
    whatsapp_id TEXT NOT NULL,
    phone       TEXT,
    reason      TEXT,
    message     TEXT,
    module      TEXT,
    payload     JSONB,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','resolved')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_staff_queue_status  ON staff_queue(status);
CREATE INDEX IF NOT EXISTS idx_staff_queue_wid     ON staff_queue(whatsapp_id);

-- ── Credit notes (Module C accountant workflow) ──────────────────────────────
CREATE TABLE IF NOT EXISTS credit_notes (
    id               SERIAL PRIMARY KEY,
    enrollment_id    INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
    credit_note_no   TEXT UNIQUE,
    reason           TEXT,
    amount           NUMERIC(10,2),
    status           TEXT NOT NULL DEFAULT 'requested'
                     CHECK (status IN ('requested','approved','rejected')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    notified_at      TIMESTAMPTZ  -- Requirement 11 AC3 — set once the PDF/email fan-out completes
);
-- Migration: add notified_at to existing credit_notes tables (safe to re-run)
ALTER TABLE credit_notes ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;

-- ── Document number sequences ────────────────────────────────────────────────
CREATE SEQUENCE IF NOT EXISTS invoice_seq     START 847;
CREATE SEQUENCE IF NOT EXISTS receipt_seq     START 847;
CREATE SEQUENCE IF NOT EXISTS credit_note_seq START 1;

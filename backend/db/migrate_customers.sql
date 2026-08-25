-- ============================================================================
-- Migration: introduce customers table as the central identity store.
-- Safe to run more than once (all changes are idempotent).
-- Run this against an existing database BEFORE starting the new backend.
-- New installs: just run schema.sql — this file is not needed.
-- ============================================================================

-- ── 1. Add whatsapp_id / phone_confirmed to leads first ──────────────────────
-- Must happen before step 2 which SELECTs from leads.whatsapp_id.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='leads' AND column_name='whatsapp_id') THEN
        ALTER TABLE leads ADD COLUMN whatsapp_id TEXT UNIQUE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='leads' AND column_name='phone_confirmed') THEN
        ALTER TABLE leads ADD COLUMN phone_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
END$$;
-- Existing leads came from @c.us accounts — their phone IS the real number.
UPDATE leads SET phone_confirmed = TRUE WHERE phone_confirmed = FALSE;

-- ── 2. Create customers table ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    id                  SERIAL PRIMARY KEY,
    whatsapp_id         TEXT UNIQUE NOT NULL,
    phone               TEXT,
    phone_confirmed     BOOLEAN NOT NULL DEFAULT FALSE,
    display_name        TEXT,
    full_name           TEXT,
    nric                TEXT,
    email               TEXT,
    preferred_course    TEXT,
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_phone
    ON customers(phone) WHERE phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customers_status
    ON customers(status);

-- ── 3. Populate customers from existing leads ─────────────────────────────────
-- Construct a @c.us JID for any lead that doesn't have a stored whatsapp_id.
INSERT INTO customers (
    whatsapp_id, phone, phone_confirmed,
    display_name, full_name, nric, email, preferred_course,
    status, follow_up_count, last_contact_at,
    escalated, escalation_reason, created_at, updated_at
)
SELECT
    COALESCE(l.whatsapp_id, l.phone || '@c.us'),
    l.phone,
    COALESCE(l.phone_confirmed, TRUE),
    l.name,
    l.name,
    l.nric,
    l.email,
    l.preferred_course,
    CASE l.status
        WHEN 'enquiry'           THEN 'enquiry'
        WHEN 'awaiting_response' THEN 'awaiting_response'
        WHEN 'enrolled'          THEN 'enrolled'
        WHEN 'escalated'         THEN 'escalated'
        WHEN 'closed'            THEN 'closed'
        ELSE 'new'
    END,
    l.follow_up_count,
    l.last_contact_at,
    l.escalated,
    l.escalation_reason,
    l.created_at,
    l.updated_at
FROM leads l
ON CONFLICT (whatsapp_id) DO NOTHING;

-- ── 4. chat_memory: rename phone → whatsapp_id ───────────────────────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='chat_memory' AND column_name='phone')
    AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name='chat_memory' AND column_name='whatsapp_id') THEN
        ALTER TABLE chat_memory ADD COLUMN whatsapp_id TEXT;
        UPDATE chat_memory SET whatsapp_id = phone || '@c.us' WHERE phone IS NOT NULL;
        ALTER TABLE chat_memory ALTER COLUMN whatsapp_id SET NOT NULL;
        ALTER TABLE chat_memory DROP COLUMN phone;
    END IF;
END$$;
CREATE INDEX IF NOT EXISTS idx_chat_memory_wid ON chat_memory(whatsapp_id, created_at);

-- ── 5. enrollments: add whatsapp_id ──────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='enrollments' AND column_name='whatsapp_id') THEN
        ALTER TABLE enrollments ADD COLUMN whatsapp_id TEXT;
        UPDATE enrollments SET whatsapp_id = phone || '@c.us' WHERE phone IS NOT NULL;
    END IF;
END$$;
CREATE INDEX IF NOT EXISTS idx_enrollments_whatsapp ON enrollments(whatsapp_id);

-- ── 6. payments: add whatsapp_id ─────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='payments' AND column_name='whatsapp_id') THEN
        ALTER TABLE payments ADD COLUMN whatsapp_id TEXT;
        UPDATE payments SET whatsapp_id = phone || '@c.us' WHERE phone IS NOT NULL;
    END IF;
END$$;
CREATE INDEX IF NOT EXISTS idx_payments_whatsapp ON payments(whatsapp_id);

-- ── 7. staff_queue: add whatsapp_id ──────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='staff_queue' AND column_name='whatsapp_id') THEN
        ALTER TABLE staff_queue ADD COLUMN whatsapp_id TEXT;
        UPDATE staff_queue SET whatsapp_id = phone || '@c.us' WHERE phone IS NOT NULL;
    END IF;
END$$;
CREATE INDEX IF NOT EXISTS idx_staff_queue_wid ON staff_queue(whatsapp_id);

-- ── 8. Document sequences (idempotent) ───────────────────────────────────────
CREATE SEQUENCE IF NOT EXISTS invoice_seq     START 847;
CREATE SEQUENCE IF NOT EXISTS receipt_seq     START 847;
CREATE SEQUENCE IF NOT EXISTS credit_note_seq START 1;

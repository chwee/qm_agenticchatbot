-- ============================================================================
-- Q&M AI Enquiry & Enrollment System — Supabase export
-- Generated 2026-08-29T15:51:24Z from local dev Postgres.
-- Run this ONCE against a fresh Supabase database (SQL Editor or psql -f).
-- Uses INSERT statements (not COPY FROM stdin) so it runs directly in the
-- Supabase SQL Editor, which does not support the psql COPY protocol.
-- No ALTER TABLE ... DISABLE TRIGGER: Supabase's SQL Editor role cannot
-- touch system FK-constraint triggers. Data rows are ordered so foreign
-- keys are always satisfied without needing to disable triggers.
-- Contains: full schema (idempotent) + all current data + sequence positions.
-- ============================================================================

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
-- DIALOGUE_STATE_REDESIGN.md phase 1 — dual-write only, nothing reads this
-- back to decide anything yet. Holds app.dialogue.state.Slots.to_json(): the
-- resolved course/intake/invoice and the candidate list actually offered,
-- keyed implicitly by whichever PendingQuestion active_flow represents.
-- Distinguishes sub-stages active_flow alone can't (e.g. module_a.py's
-- reminder flow writes 'awaiting_reminder' for three different questions —
-- see app/dialogue/state.py's PendingQuestion docstring).
ALTER TABLE conversation_state ADD COLUMN IF NOT EXISTS flow_context JSONB NOT NULL DEFAULT '{}'::jsonb;

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
-- DIALOGUE_STATE_REDESIGN.md phase 2 (episode segmentation) — every row gets
-- the whatsapp_id's current episode's id (repositories.current_session_id():
-- reused across a burst of activity, a fresh one minted after a 60-minute
-- gap). What actually stops the Router's window spanning across days — a
-- plain created_at-DESC LIMIT alone has no idea "6 turns ago" might have been
-- Saturday.
ALTER TABLE chat_memory ADD COLUMN IF NOT EXISTS session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_chat_memory_session ON chat_memory(whatsapp_id, session_id, created_at);

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

-- ============================================================================
-- Data
-- ============================================================================

--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Data for Name: chat_memory; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.chat_memory (id, whatsapp_id, role, content, created_at, session_id) VALUES (1, '6591234567@lid', 'user', 'ho', '2026-08-29 12:09:09.591832+00', 'c42f49b701d4');
INSERT INTO public.chat_memory (id, whatsapp_id, role, content, created_at, session_id) VALUES (2, '6591234567@lid', 'assistant', 'Hi there! How can I assist you today? If you have any questions about our training courses or anything else, feel free to ask!', '2026-08-29 12:09:09.594199+00', 'c42f49b701d4');
INSERT INTO public.chat_memory (id, whatsapp_id, role, content, created_at, session_id) VALUES (3, '6591234567@lid', 'user', 'what course you can provide', '2026-08-29 12:09:37.490676+00', 'c42f49b701d4');
INSERT INTO public.chat_memory (id, whatsapp_id, role, content, created_at, session_id) VALUES (4, '6591234567@lid', 'assistant', 'We offer several courses at Q&M Dental Group! Here’s a quick list for you:

1. **2-Day Basic Certificate in Dental Assisting** — S$600.00
2. **Infection Control for Dental Clinics** — S$420.00
3. **Advanced Dental Radiography & X-Ray Techniques** — S$950.00
4. **Dental Reception & Patient Communication Workshop** — S$350.00
5. **CPR & Basic Life Support (BLS) for Dental Clinics** — S$250.00
6. **Orthodontic Chairside Assisting Certificate** — S$1,200.00

If any of these catch your interest, just let me know which course and intake you''d like to know more about!', '2026-08-29 12:09:37.492591+00', 'c42f49b701d4');


--
-- Data for Name: customers; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.customers (id, whatsapp_id, phone, phone_confirmed, display_name, full_name, nric, email, preferred_course, course_date, status, follow_up_count, last_contact_at, escalated, escalation_reason, created_at, updated_at) VALUES (1, '6591234567@lid', '6591234567', true, 'Test User', NULL, NULL, NULL, NULL, NULL, 'enquiry', 0, '2026-08-29 12:09:33.099287+00', false, NULL, '2026-08-29 12:09:05.226665+00', '2026-08-29 12:09:33.099287+00');


--
-- Data for Name: conversation_state; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: courses; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.courses (id, code, course_id, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at) VALUES (1, 'DACERT', 'C2601', '2-Day Basic Certificate in Dental Assisting', 600.00, 500.00, 'A 2-day foundational certificate preparing participants for entry-level dental assisting roles in Singapore clinics.', 'Chairside assisting, infection control basics, dental instruments handling, patient management, and clinic workflow.', 'Open to all. No prior experience or certificate required. Suitable for career switchers and mid-career individuals.', 'Dental Assistant, Clinic Assistant, and Front-desk Dental Coordinator roles at private and group dental practices.', true, '2026-08-29 12:03:03.885047+00');
INSERT INTO public.courses (id, code, course_id, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at) VALUES (2, 'INFCTRL', 'C2602', 'Infection Control for Dental Clinics', 420.00, 500.00, 'Focused training on infection prevention and control standards for dental clinic environments.', 'Sterilisation protocols, cross-contamination prevention, PPE use, and clinic hygiene compliance.', 'Open to all. No prior experience required.', 'Strengthens employability for dental assisting and clinic support roles.', true, '2026-08-29 12:03:03.885047+00');
INSERT INTO public.courses (id, code, course_id, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at) VALUES (3, 'DENTRAD', 'C2603', 'Advanced Dental Radiography & X-Ray Techniques', 950.00, 0.00, 'A specialised course covering intraoral and panoramic radiography for dental support staff, including positioning, exposure settings, and radiation safety.', 'X-ray positioning and exposure technique, radiation safety and shielding, image quality troubleshooting, and digital sensor handling.', 'Prior completion of a basic dental assisting certificate (e.g. 2-Day Basic Certificate in Dental Assisting) or equivalent clinic experience is recommended.', 'Dental Radiographer, Senior Dental Assistant, and Imaging Support roles in clinics and specialist practices.', true, '2026-08-29 12:03:03.885047+00');
INSERT INTO public.courses (id, code, course_id, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at) VALUES (4, 'RECEPT', 'C2604', 'Dental Reception & Patient Communication Workshop', 350.00, 500.00, 'A half-day workshop for front-desk and reception staff covering patient communication, appointment scheduling, and clinic administration for dental practices.', 'Patient communication and service recovery, appointment and billing systems, handling enquiries and complaints, and basic dental terminology for front-desk staff.', 'Open to all. No prior experience required. Suitable for administrative and customer service staff.', 'Dental Receptionist, Clinic Coordinator, and Patient Service Associate roles.', true, '2026-08-29 12:03:03.885047+00');
INSERT INTO public.courses (id, code, course_id, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at) VALUES (5, 'CPRBLS', 'C2605', 'CPR & Basic Life Support (BLS) for Dental Clinics', 250.00, 200.00, 'A 1-day certification course in cardiopulmonary resuscitation and basic life support, tailored to emergency scenarios in a dental clinic setting.', 'Adult and child CPR technique, AED use, choking response, and recognising a dental-chair medical emergency.', 'Open to all clinic staff. No prior first-aid certification required.', 'Required or preferred certification for Dental Assistant, Dental Nurse, and Clinic Manager roles.', true, '2026-08-29 12:03:03.885047+00');
INSERT INTO public.courses (id, code, course_id, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at) VALUES (6, 'ORTHOASST', 'C2606', 'Orthodontic Chairside Assisting Certificate', 1200.00, 500.00, 'An in-depth certificate for dental assistants specialising in orthodontic chairside support, covering braces, aligners, and orthodontic instrument handling.', 'Orthodontic instrument identification, bracket and wire handling support, aligner fitting assistance, and orthodontic patient care protocols.', 'Completion of a basic dental assisting certificate (e.g. 2-Day Basic Certificate in Dental Assisting) or at least 6 months of clinic experience is required.', 'Orthodontic Assistant and Senior Dental Assistant roles at orthodontic and general dental practices.', true, '2026-08-29 12:03:03.885047+00');


--
-- Data for Name: course_schedules; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (1, 3, 'SH2606', '13-14 Oct 2026', '2026-10-13', '2026-10-14', 15, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (2, 2, 'SH2603', '14-15 Jul 2026', '2026-07-14', '2026-07-15', 20, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (3, 6, 'SH2612', '02-04 Nov 2026', '2026-11-02', '2026-11-04', 12, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (4, 4, 'SH2607', '25 Aug 2026', '2026-08-25', '2026-08-25', 25, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (5, 1, 'SH2601', '21-22 Jul 2026', '2026-07-21', '2026-07-22', 20, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (6, 5, 'SH2610', '25 Aug 2026', '2026-08-25', '2026-08-25', 20, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (7, 4, 'SH2608', '22 Sep 2026', '2026-09-22', '2026-09-22', 25, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (8, 5, 'SH2609', '28 Jul 2026', '2026-07-28', '2026-07-28', 20, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (9, 2, 'SH2604', '11-12 Aug 2026', '2026-08-11', '2026-08-12', 20, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (10, 3, 'SH2605', '08-09 Sep 2026', '2026-09-08', '2026-09-09', 15, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (11, 1, 'SH2602', '18-19 Aug 2026', '2026-08-18', '2026-08-19', 20, 0, true);
INSERT INTO public.course_schedules (id, course_id, schedule_id, label, start_date, end_date, seats, seats_taken, active) VALUES (12, 6, 'SH2611', '05-07 Oct 2026', '2026-10-05', '2026-10-07', 12, 0, true);


--
-- Data for Name: enrollments; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: credit_notes; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: leads; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: payments; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: reminders; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: staff_queue; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Name: chat_memory_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.chat_memory_id_seq', 4, true);


--
-- Name: course_schedules_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.course_schedules_id_seq', 12, true);


--
-- Name: courses_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.courses_id_seq', 6, true);


--
-- Name: credit_note_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.credit_note_seq', 1, false);


--
-- Name: credit_notes_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.credit_notes_id_seq', 1, false);


--
-- Name: customers_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.customers_id_seq', 2, true);


--
-- Name: enrollments_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.enrollments_id_seq', 1, false);


--
-- Name: invoice_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.invoice_seq', 847, false);


--
-- Name: leads_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.leads_id_seq', 1, false);


--
-- Name: payments_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.payments_id_seq', 1, false);


--
-- Name: receipt_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.receipt_seq', 847, false);


--
-- Name: reminders_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.reminders_id_seq', 1, false);


--
-- Name: staff_queue_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.staff_queue_id_seq', 1, false);


--
-- PostgreSQL database dump complete
--



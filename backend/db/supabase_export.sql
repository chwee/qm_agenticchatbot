-- ============================================================================
-- Q&M AI Enquiry & Enrollment System — Supabase export
-- Generated 2026-08-07T04:53:37Z from local dev Postgres.
-- Run this ONCE against a fresh Supabase database (SQL Editor or psql -f).
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
-- Partial unique index: phone uniqueness only when phone is set
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_phone
    ON customers(phone) WHERE phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customers_status
    ON customers(status);

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
    resolved_at      TIMESTAMPTZ
);

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

\restrict hlEFCJ7fxLvlO27JE3asg0eyVqjcxCswGySRAkSaAxEx3dXFcpUeiPB7iPYEby3

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg13+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg13+1)

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
-- Data for Name: chat_memory; Type: TABLE DATA; Schema: public; Owner: qm_user
--

SET SESSION AUTHORIZATION DEFAULT;

ALTER TABLE public.chat_memory DISABLE TRIGGER ALL;

INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (899, 'user', 'I need to know about some dental courses?', '2026-08-04 07:30:33.132904+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (900, 'assistant', 'Sure! Here are some of the dental courses we offer:

1. **2-Day Basic Certificate in Dental Assisting** — S$600.00
2. **Infection Control for Dental Clinics** — S$500.00
3. **Advanced Dental Radiography & X-Ray Techniques** — S$950.00
4. **Dental Reception & Patient Communication Workshop** — S$350.00
5. **CPR & Basic Life Support (BLS) for Dental Clinics** — S$250.00
6. **Orthodontic Chairside Assisting Certificate** — S$1,200.00

If you''re interested in any specific course or want to know more about the fees or schedules, just let me know!', '2026-08-04 07:30:33.135576+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (901, 'user', 'Can you share with me about course 1.', '2026-08-04 07:38:13.748832+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (902, 'assistant', 'The **2-Day Basic Certificate in Dental Assisting** (C2601) has a full fee of **S$600.00**. If you''re eligible for SkillsFuture, you can offset up to **S$500.00**, which means your net payable would be **S$100.00**. If you''re aged 40 and above, you might also qualify for additional Mid-Career SkillsFuture Credit.

If you’d like to know about the upcoming intake dates or if you''re ready to enroll, just let me know!', '2026-08-04 07:38:13.750828+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (903, 'user', 'Can you share with me about course 2.', '2026-08-04 07:39:19.943295+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (904, 'assistant', 'The **Infection Control for Dental Clinics** course (C2602) has a full fee of **S$500.00**. If you''re eligible for SkillsFuture, you can fully claim this amount, meaning your net payable would be **S$0.00**. If you''re aged 40 and above, you might also qualify for additional Mid-Career SkillsFuture Credit.

If you want to know about the upcoming intake dates or if you''re ready to enroll, just let me know!', '2026-08-04 07:39:19.946412+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (905, 'user', 'I want to attend the course 2.', '2026-08-04 07:42:02.05985+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (906, 'assistant', 'Great choice! For the **Infection Control for Dental Clinics** course (C2602), we have the following upcoming intakes:

1. [SH2603] 14-15 Jul 2026 (20 seats available)
2. [SH2604] 11-12 Aug 2026 (20 seats available)

Please let me know which intake you''d like to enroll in!', '2026-08-04 07:42:02.061912+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (907, 'user', 'I will take intake 1.', '2026-08-04 07:44:54.80022+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (908, 'assistant', 'You''re all set, Chen Wei! You''ve been successfully enrolled in the **Infection Control for Dental Clinics** course. Here are the details:

- **Course:** Infection Control for Dental Clinics
- **Date:** 14-15 Jul 2026
- **Fee:** S$500.00 (fully claimable through SkillsFuture — S$0 payable)

An invoice has been sent to your email at chenwei@test.com. When you''re ready to pay, just send me a message with your PayNow screenshot attached. If you have any other questions, feel free to ask!', '2026-08-04 07:44:54.801961+00', '153811586920510@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (909, 'user', 'Hi I want to know about some dental courses', '2026-08-04 08:05:23.997246+00', '153811586920512@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (910, 'assistant', 'Hi there! We offer several dental courses that might interest you:

1. **2-Day Basic Certificate in Dental Assisting** — S$600.00
2. **Infection Control for Dental Clinics** — S$500.00
3. **Advanced Dental Radiography & X-Ray Techniques** — S$950.00
4. **Dental Reception & Patient Communication Workshop** — S$350.00
5. **CPR & Basic Life Support (BLS) for Dental Clinics** — S$250.00
6. **Orthodontic Chairside Assisting Certificate** — S$1,200.00

If any of these catch your eye, let me know which one you''re interested in, and I can provide more details or help you enroll!', '2026-08-04 08:05:23.999523+00', '153811586920512@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (911, 'user', 'I want to attend course 2.', '2026-08-04 08:06:00.410629+00', '153811586920512@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (912, 'assistant', 'Great choice with the **Infection Control for Dental Clinics** course! Here are the upcoming intake options:

1. **[SH2603]** 14-15 Jul 2026 (20 seats available)
2. **[SH2604]** 11-12 Aug 2026 (20 seats available)

Please let me know which intake you''d like to enroll in!', '2026-08-04 08:06:00.412423+00', '153811586920512@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (913, 'user', 'i want to go for 14-15 Jul 2026', '2026-08-04 08:06:34.70233+00', '153811586920512@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (914, 'assistant', 'You''re all set! You''ve been successfully enrolled in the **Infection Control for Dental Clinics** course on **14-15 Jul 2026**. 

Here are the details:
- **Name:** Wee Chee Hong
- **NRIC:** S1785111A
- **Fee:** S$500.00 (fully claimable through SkillsFuture — S$0 payable)

An invoice (INV-2026-0912) has been sent to your email at chwee@gmail.com. When you''re ready to pay, just send me a PayNow screenshot, and I''ll help you with that! If you have any other questions, feel free to ask!', '2026-08-04 08:06:34.704447+00', '153811586920512@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (915, 'user', 'what are the course I enrol need to pay', '2026-08-04 08:08:38.105011+00', '153811586920512@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (916, 'assistant', 'You''re currently enrolled in the **Infection Control for Dental Clinics** course on **14-15 Jul 2026**. The status is **Awaiting payment**, and the invoice number is **INV-2026-0912** for **S$0.00** (fully claimable through SkillsFuture).

If you need any further assistance or have questions about the payment process, just let me know!', '2026-08-04 08:08:38.107257+00', '153811586920512@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (917, 'user', 'I need pay for INV-2026-0912', '2026-08-04 08:14:47.125717+00', '153811586920512@lid');
INSERT INTO public.chat_memory (id, role, content, created_at, whatsapp_id) VALUES (918, 'assistant', 'Payment complete for Infection Control for Dental Clinics!

SkillsFuture claim of S$500.00 confirmed — receipt RCP-2026-0862 sent to chwee@gmail.com.

Total received: S$500.00 of S$500.00.
See you on 14-15 Jul 2026!', '2026-08-04 08:14:47.127476+00', '153811586920512@lid');


ALTER TABLE public.chat_memory ENABLE TRIGGER ALL;

--
-- Data for Name: courses; Type: TABLE DATA; Schema: public; Owner: qm_user
--

ALTER TABLE public.courses DISABLE TRIGGER ALL;

INSERT INTO public.courses (id, code, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at, course_id) VALUES (1, 'DACERT', '2-Day Basic Certificate in Dental Assisting', 600.00, 500.00, 'A 2-day foundational certificate preparing participants for entry-level dental assisting roles in Singapore clinics.', 'Chairside assisting, infection control basics, dental instruments handling, patient management, and clinic workflow.', 'Open to all. No prior experience or certificate required. Suitable for career switchers and mid-career individuals.', 'Dental Assistant, Clinic Assistant, and Front-desk Dental Coordinator roles at private and group dental practices.', true, '2026-06-19 10:41:19.19757+00', 'C2601');
INSERT INTO public.courses (id, code, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at, course_id) VALUES (2, 'INFCTRL', 'Infection Control for Dental Clinics', 500.00, 500.00, 'Focused training on infection prevention and control standards for dental clinic environments.', 'Sterilisation protocols, cross-contamination prevention, PPE use, and clinic hygiene compliance.', 'Open to all. No prior experience required.', 'Strengthens employability for dental assisting and clinic support roles.', true, '2026-06-19 10:41:19.19757+00', 'C2602');
INSERT INTO public.courses (id, code, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at, course_id) VALUES (12, 'CPRBLS', 'CPR & Basic Life Support (BLS) for Dental Clinics', 250.00, 200.00, 'A 1-day certification course in cardiopulmonary resuscitation and basic life support, tailored to emergency scenarios in a dental clinic setting.', 'Adult and child CPR technique, AED use, choking response, and recognising a dental-chair medical emergency.', 'Open to all clinic staff. No prior first-aid certification required.', 'Required or preferred certification for Dental Assistant, Dental Nurse, and Clinic Manager roles.', true, '2026-07-07 15:07:57.293506+00', 'C2605');
INSERT INTO public.courses (id, code, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at, course_id) VALUES (10, 'DENTRAD', 'Advanced Dental Radiography & X-Ray Techniques', 950.00, 0.00, 'A specialised course covering intraoral and panoramic radiography for dental support staff, including positioning, exposure settings, and radiation safety.', 'X-ray positioning and exposure technique, radiation safety and shielding, image quality troubleshooting, and digital sensor handling.', 'Prior completion of a basic dental assisting certificate (e.g. 2-Day Basic Certificate in Dental Assisting) or equivalent clinic experience is recommended.', 'Dental Radiographer, Senior Dental Assistant, and Imaging Support roles in clinics and specialist practices.', true, '2026-07-07 15:07:57.293506+00', 'C2603');
INSERT INTO public.courses (id, code, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at, course_id) VALUES (11, 'RECEPT', 'Dental Reception & Patient Communication Workshop', 350.00, 500.00, 'A half-day workshop for front-desk and reception staff covering patient communication, appointment scheduling, and clinic administration for dental practices.', 'Patient communication and service recovery, appointment and billing systems, handling enquiries and complaints, and basic dental terminology for front-desk staff.', 'Open to all. No prior experience required. Suitable for administrative and customer service staff.', 'Dental Receptionist, Clinic Coordinator, and Patient Service Associate roles.', true, '2026-07-07 15:07:57.293506+00', 'C2604');
INSERT INTO public.courses (id, code, name, full_fee, sf_subsidy_cap, description, learning_outcomes, entry_requirements, job_pathways, active, created_at, course_id) VALUES (13, 'ORTHOASST', 'Orthodontic Chairside Assisting Certificate', 1200.00, 500.00, 'An in-depth certificate for dental assistants specialising in orthodontic chairside support, covering braces, aligners, and orthodontic instrument handling.', 'Orthodontic instrument identification, bracket and wire handling support, aligner fitting assistance, and orthodontic patient care protocols.', 'Completion of a basic dental assisting certificate (e.g. 2-Day Basic Certificate in Dental Assisting) or at least 6 months of clinic experience is required.', 'Orthodontic Assistant and Senior Dental Assistant roles at orthodontic and general dental practices.', true, '2026-07-07 15:07:57.293506+00', 'C2606');


ALTER TABLE public.courses ENABLE TRIGGER ALL;

--
-- Data for Name: course_schedules; Type: TABLE DATA; Schema: public; Owner: qm_user
--

ALTER TABLE public.course_schedules DISABLE TRIGGER ALL;

INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (20, 11, '25 Aug 2026', '2026-08-25', '2026-08-25', 25, true, 'SH2607', 0);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (19, 12, '28 Jul 2026', '2026-07-28', '2026-07-28', 20, true, 'SH2609', 0);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (18, 11, '22 Sep 2026', '2026-09-22', '2026-09-22', 25, true, 'SH2608', 0);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (2, 1, '21-22 Jul 2026', '2026-07-21', '2026-07-22', 20, true, 'SH2601', 0);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (3, 2, '11-12 Aug 2026', '2026-08-11', '2026-08-12', 20, true, 'SH2604', 0);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (4, 1, '18-19 Aug 2026', '2026-08-18', '2026-08-19', 20, true, 'SH2602', 1);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (1, 2, '14-15 Jul 2026', '2026-07-14', '2026-07-15', 20, true, 'SH2603', 1);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (16, 10, '08-09 Sep 2026', '2026-09-08', '2026-09-09', 15, true, 'SH2605', 0);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (17, 13, '05-07 Oct 2026', '2026-10-05', '2026-10-07', 12, true, 'SH2611', 0);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (21, 10, '13-14 Oct 2026', '2026-10-13', '2026-10-14', 15, true, 'SH2606', 0);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (22, 12, '25 Aug 2026', '2026-08-25', '2026-08-25', 20, true, 'SH2610', 0);
INSERT INTO public.course_schedules (id, course_id, label, start_date, end_date, seats, active, schedule_id, seats_taken) VALUES (23, 13, '02-04 Nov 2026', '2026-11-02', '2026-11-04', 12, true, 'SH2612', 0);


ALTER TABLE public.course_schedules ENABLE TRIGGER ALL;

--
-- Data for Name: enrollments; Type: TABLE DATA; Schema: public; Owner: qm_user
--

ALTER TABLE public.enrollments DISABLE TRIGGER ALL;

INSERT INTO public.enrollments (id, phone, course_id, course_name, course_date, full_name, nric, email, fee, sf_subsidy, net_payable, invoice_no, receipt_no, status, created_at, updated_at, whatsapp_id, schedule_id) VALUES (65, '659760111', 2, 'Infection Control for Dental Clinics', '14-15 Jul 2026', 'Chen Wei', 'S1785111A', 'chenwei@test.com', 500.00, 500.00, 0.00, 'INV-2026-0911', NULL, 'cancelled', '2026-08-04 07:44:50.690076+00', '2026-08-04 07:55:27.643688+00', '153811586920510@lid', 1);
INSERT INTO public.enrollments (id, phone, course_id, course_name, course_date, full_name, nric, email, fee, sf_subsidy, net_payable, invoice_no, receipt_no, status, created_at, updated_at, whatsapp_id, schedule_id) VALUES (66, '6597607916', 2, 'Infection Control for Dental Clinics', '14-15 Jul 2026', 'Wee Chee Hong', 'S1785111A', 'chwee@gmail.com', 500.00, 500.00, 0.00, 'INV-2026-0912', 'RCP-2026-0862', 'receipt_issued', '2026-08-04 08:06:29.770093+00', '2026-08-04 08:14:47.114643+00', '153811586920512@lid', 1);


ALTER TABLE public.enrollments ENABLE TRIGGER ALL;

--
-- Data for Name: credit_notes; Type: TABLE DATA; Schema: public; Owner: qm_user
--

ALTER TABLE public.credit_notes DISABLE TRIGGER ALL;

INSERT INTO public.credit_notes (id, enrollment_id, credit_note_no, reason, amount, status, created_at, resolved_at) VALUES (7, 65, 'CN-2026-0007', 'Cancellation', 0.00, 'approved', '2026-08-04 07:55:27.63178+00', '2026-08-04 07:59:31.528842+00');


ALTER TABLE public.credit_notes ENABLE TRIGGER ALL;

--
-- Data for Name: customers; Type: TABLE DATA; Schema: public; Owner: qm_user
--

ALTER TABLE public.customers DISABLE TRIGGER ALL;

INSERT INTO public.customers (id, whatsapp_id, phone, phone_confirmed, display_name, full_name, nric, email, preferred_course, status, follow_up_count, last_contact_at, escalated, escalation_reason, created_at, updated_at) VALUES (557, '153811586920510@lid', '659760111', true, 'Chen Wei', 'Chen Wei', 'S1785111A', 'chenwei@test.com', NULL, 'enrolled', 0, '2026-08-04 07:44:46.771177+00', false, NULL, '2026-08-04 07:28:00.591721+00', '2026-08-04 07:44:50.695968+00');
INSERT INTO public.customers (id, whatsapp_id, phone, phone_confirmed, display_name, full_name, nric, email, preferred_course, status, follow_up_count, last_contact_at, escalated, escalation_reason, created_at, updated_at) VALUES (164, '153811586920512@lid', '6597607916', true, 'Wee Chee Hong', 'Wee Chee Hong', 'S1785111A', 'chwee@gmail.com', NULL, 'enrolled', 0, '2026-08-04 08:14:43.3982+00', false, NULL, '2026-07-07 02:32:16.453456+00', '2026-08-04 08:14:43.3982+00');


ALTER TABLE public.customers ENABLE TRIGGER ALL;

--
-- Data for Name: leads; Type: TABLE DATA; Schema: public; Owner: qm_user
--

ALTER TABLE public.leads DISABLE TRIGGER ALL;

INSERT INTO public.leads (id, phone, name, nric, dob, email, preferred_course, status, follow_up_count, last_contact_at, escalated, escalation_reason, created_at, updated_at, whatsapp_id, phone_confirmed, course_date) VALUES (77, '659760111', 'Chen Wei', 'S1785111A', NULL, 'chenwei@test.com', NULL, 'enrolled', 0, '2026-08-04 07:29:01.493149+00', false, NULL, '2026-08-04 07:29:01.493149+00', '2026-08-04 07:44:50.697799+00', '153811586920510@lid', true, NULL);


ALTER TABLE public.leads ENABLE TRIGGER ALL;

--
-- Data for Name: payments; Type: TABLE DATA; Schema: public; Owner: qm_user
--

ALTER TABLE public.payments DISABLE TRIGGER ALL;

INSERT INTO public.payments (id, enrollment_id, phone, expected_amount, detected_amount, reference, paid_at_text, verdict, confidence, raw_extract, notes, created_at, whatsapp_id, invoice_no, payment_type, receipt_no) VALUES (65, 65, '659760111', 500.00, NULL, NULL, NULL, 'pending', NULL, NULL, 'Invoice issued — awaiting payment.', '2026-08-04 07:44:52.861154+00', '153811586920510@lid', 'INV-2026-0911', NULL, NULL);
INSERT INTO public.payments (id, enrollment_id, phone, expected_amount, detected_amount, reference, paid_at_text, verdict, confidence, raw_extract, notes, created_at, whatsapp_id, invoice_no, payment_type, receipt_no) VALUES (66, 66, '6597607916', 500.00, 500.00, '2026008490', 'June 25, 2026', 'confirmed', 1.000, '{"paid_at": "June 25, 2026", "currency": "SGD", "readable": true, "recipient": null, "reference": "2026008490", "confidence": 1, "payment_type": "skillsfuture_claim", "detected_amount": 500, "is_payment_proof": true}', 'Amount accepted toward the outstanding balance.', '2026-08-04 08:06:31.876786+00', '153811586920512@lid', 'INV-2026-0912', 'skillsfuture_claim', 'RCP-2026-0862');


ALTER TABLE public.payments ENABLE TRIGGER ALL;

--
-- Data for Name: staff_queue; Type: TABLE DATA; Schema: public; Owner: qm_user
--

ALTER TABLE public.staff_queue DISABLE TRIGGER ALL;

INSERT INTO public.staff_queue (id, phone, reason, message, module, payload, status, created_at, whatsapp_id) VALUES (3, '6597601111', 'payment_mismatch', 'Invoice INV-2026-0907: This invoice has no PayNow portion outstanding — a transfer isn''t needed here (or one was already submitted). (type=paynow, detected=50.00, confidence=0.900)', 'C', '{"paid_at": null, "currency": "SGD", "readable": true, "recipient": "GPAY NETWORK (S) PTE LTD UEN 201722418R", "reference": null, "confidence": 0.9, "payment_type": "paynow", "detected_amount": 50.0, "is_payment_proof": true}', 'open', '2026-08-02 04:19:05.912997+00', '153811586920510@lid');
INSERT INTO public.staff_queue (id, phone, reason, message, module, payload, status, created_at, whatsapp_id) VALUES (4, '6597601111', 'payment_mismatch', 'Invoice INV-2026-0907: This invoice has no PayNow portion outstanding — a transfer isn''t needed here (or one was already submitted). (type=paynow, detected=50.00, confidence=0.900)', 'C', '{"paid_at": null, "currency": "SGD", "readable": true, "recipient": "GPAY NETWORK (S) PTE LTD UEN 201722418R", "reference": null, "confidence": 0.9, "payment_type": "paynow", "detected_amount": 50.0, "is_payment_proof": true}', 'open', '2026-08-02 04:26:06.20783+00', '153811586920510@lid');


ALTER TABLE public.staff_queue ENABLE TRIGGER ALL;

--
-- Name: chat_memory_id_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.chat_memory_id_seq', 918, true);


--
-- Name: course_schedules_id_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.course_schedules_id_seq', 23, true);


--
-- Name: courses_id_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.courses_id_seq', 31, true);


--
-- Name: credit_note_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.credit_note_seq', 7, true);


--
-- Name: credit_notes_id_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.credit_notes_id_seq', 7, true);


--
-- Name: customers_id_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.customers_id_seq', 568, true);


--
-- Name: enrollments_id_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.enrollments_id_seq', 66, true);


--
-- Name: invoice_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.invoice_seq', 912, true);


--
-- Name: leads_id_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.leads_id_seq', 77, true);


--
-- Name: payments_id_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.payments_id_seq', 66, true);


--
-- Name: receipt_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.receipt_seq', 862, true);


--
-- Name: staff_queue_id_seq; Type: SEQUENCE SET; Schema: public; Owner: qm_user
--

SELECT pg_catalog.setval('public.staff_queue_id_seq', 4, true);


--
-- PostgreSQL database dump complete
--

\unrestrict hlEFCJ7fxLvlO27JE3asg0eyVqjcxCswGySRAkSaAxEx3dXFcpUeiPB7iPYEby3


-- ============================================================================
-- Seed data for the Q&M AI Enquiry & Enrollment System
-- ============================================================================
-- Re-runnable: ON CONFLICT clauses make every statement idempotent.
-- course_id  : short display code for courses   (C2601, C2602 …)
-- schedule_id: short display code for schedules (SH2601, SH2602 …)
-- ============================================================================

INSERT INTO courses (code, course_id, name, full_fee, sf_subsidy_cap, description,
                     learning_outcomes, entry_requirements, job_pathways)
VALUES
(
    'DACERT', 'C2601',
    '2-Day Basic Certificate in Dental Assisting',
    600.00, 500.00,
    'A 2-day foundational certificate preparing participants for entry-level dental assisting roles in Singapore clinics.',
    'Chairside assisting, infection control basics, dental instruments handling, patient management, and clinic workflow.',
    'Open to all. No prior experience or certificate required. Suitable for career switchers and mid-career individuals.',
    'Dental Assistant, Clinic Assistant, and Front-desk Dental Coordinator roles at private and group dental practices.'
),
(
    'INFCTRL', 'C2602',
    'Infection Control for Dental Clinics',
    420.00, 500.00,
    'Focused training on infection prevention and control standards for dental clinic environments.',
    'Sterilisation protocols, cross-contamination prevention, PPE use, and clinic hygiene compliance.',
    'Open to all. No prior experience required.',
    'Strengthens employability for dental assisting and clinic support roles.'
),
(
    -- PayNow-only example: not SkillsFuture-eligible (sf_subsidy_cap = 0),
    -- so the full fee is payable directly — no SkillsFuture claim needed.
    'DENTRAD', 'C2603',
    'Advanced Dental Radiography & X-Ray Techniques',
    950.00, 0.00,
    'A specialised course covering intraoral and panoramic radiography for dental support staff, including '
    'positioning, exposure settings, and radiation safety.',
    'X-ray positioning and exposure technique, radiation safety and shielding, image quality troubleshooting, '
    'and digital sensor handling.',
    'Prior completion of a basic dental assisting certificate (e.g. 2-Day Basic Certificate in Dental Assisting) '
    'or equivalent clinic experience is recommended.',
    'Dental Radiographer, Senior Dental Assistant, and Imaging Support roles in clinics and specialist practices.'
),
(
    -- Fully SkillsFuture-claimable example: fee is fully covered by the
    -- subsidy cap (net payable = 0), so no PayNow transfer is needed at all.
    'RECEPT', 'C2604',
    'Dental Reception & Patient Communication Workshop',
    350.00, 500.00,
    'A half-day workshop for front-desk and reception staff covering patient communication, appointment '
    'scheduling, and clinic administration for dental practices.',
    'Patient communication and service recovery, appointment and billing systems, handling enquiries and '
    'complaints, and basic dental terminology for front-desk staff.',
    'Open to all. No prior experience required. Suitable for administrative and customer service staff.',
    'Dental Receptionist, Clinic Coordinator, and Patient Service Associate roles.'
),
(
    'CPRBLS', 'C2605',
    'CPR & Basic Life Support (BLS) for Dental Clinics',
    250.00, 200.00,
    'A 1-day certification course in cardiopulmonary resuscitation and basic life support, tailored to '
    'emergency scenarios in a dental clinic setting.',
    'Adult and child CPR technique, AED use, choking response, and recognising a dental-chair medical emergency.',
    'Open to all clinic staff. No prior first-aid certification required.',
    'Required or preferred certification for Dental Assistant, Dental Nurse, and Clinic Manager roles.'
),
(
    'ORTHOASST', 'C2606',
    'Orthodontic Chairside Assisting Certificate',
    1200.00, 500.00,
    'An in-depth certificate for dental assistants specialising in orthodontic chairside support, covering '
    'braces, aligners, and orthodontic instrument handling.',
    'Orthodontic instrument identification, bracket and wire handling support, aligner fitting assistance, '
    'and orthodontic patient care protocols.',
    'Completion of a basic dental assisting certificate (e.g. 2-Day Basic Certificate in Dental Assisting) or '
    'at least 6 months of clinic experience is required.',
    'Orthodontic Assistant and Senior Dental Assistant roles at orthodontic and general dental practices.'
)
ON CONFLICT (code) DO UPDATE
    SET course_id = EXCLUDED.course_id;

-- Intake schedules with short schedule_id codes
INSERT INTO course_schedules (course_id, schedule_id, label, start_date, end_date, seats)
SELECT c.id, v.schedule_id, v.label, v.start_date, v.end_date, v.seats
FROM (VALUES
    ('DACERT',    'SH2601', '21-22 Jul 2026', DATE '2026-07-21', DATE '2026-07-22', 20),
    ('DACERT',    'SH2602', '18-19 Aug 2026', DATE '2026-08-18', DATE '2026-08-19', 20),
    ('INFCTRL',   'SH2603', '14-15 Jul 2026', DATE '2026-07-14', DATE '2026-07-15', 20),
    ('INFCTRL',   'SH2604', '11-12 Aug 2026', DATE '2026-08-11', DATE '2026-08-12', 20),
    ('DENTRAD',   'SH2605', '08-09 Sep 2026', DATE '2026-09-08', DATE '2026-09-09', 15),
    ('DENTRAD',   'SH2606', '13-14 Oct 2026', DATE '2026-10-13', DATE '2026-10-14', 15),
    ('RECEPT',    'SH2607', '25 Aug 2026',    DATE '2026-08-25', DATE '2026-08-25', 25),
    ('RECEPT',    'SH2608', '22 Sep 2026',    DATE '2026-09-22', DATE '2026-09-22', 25),
    ('CPRBLS',    'SH2609', '28 Jul 2026',    DATE '2026-07-28', DATE '2026-07-28', 20),
    ('CPRBLS',    'SH2610', '25 Aug 2026',    DATE '2026-08-25', DATE '2026-08-25', 20),
    ('ORTHOASST', 'SH2611', '05-07 Oct 2026', DATE '2026-10-05', DATE '2026-10-07', 12),
    ('ORTHOASST', 'SH2612', '02-04 Nov 2026', DATE '2026-11-02', DATE '2026-11-04', 12)
) AS v(code, schedule_id, label, start_date, end_date, seats)
JOIN courses c ON c.code = v.code
WHERE NOT EXISTS (
    SELECT 1 FROM course_schedules s WHERE s.course_id = c.id AND s.label = v.label
);

-- Back-fill schedule_id for any rows inserted before this column existed
UPDATE course_schedules cs
SET    schedule_id = v.schedule_id
FROM (VALUES
    ('21-22 Jul 2026', 'SH2601'),
    ('18-19 Aug 2026', 'SH2602'),
    ('14-15 Jul 2026', 'SH2603'),
    ('11-12 Aug 2026', 'SH2604')
) AS v(label, schedule_id)
WHERE cs.label = v.label AND cs.schedule_id IS NULL;

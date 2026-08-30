# Background Scheduler

`backend/app/scheduler.py` — four automated jobs that run inside the backend process without any participant or staff action triggering them. This document explains what each one does, exactly how it decides what to act on, and how it's configured.

## Overview

The scheduler is [APScheduler](https://apscheduler.readthedocs.io/)'s `BackgroundScheduler`, running in the same Python process as the FastAPI backend — not a separate worker or cron process. It's started and stopped as part of the app's own lifecycle:

```python
# main.py
scheduler.start()      # on app startup
...
scheduler.shutdown()   # on app shutdown
```

This has one practical consequence: **the scheduler only runs while the backend process is running.** If the backend is down when a job's scheduled time passes, that run is simply skipped — there's no catch-up queue. The backend needs to be running continuously (or restarted before each configured hour) for every job to fire reliably.

All cron-based jobs run in the `Asia/Singapore` timezone, set explicitly on the `BackgroundScheduler` instance regardless of the host machine's own timezone.

### The four jobs

| Job | Trigger | What it does |
|---|---|---|
| Nurture follow-ups | Daily, `FOLLOWUP_CHECK_CRON_HOUR` | Nudges leads who haven't replied in a while |
| Reminder dispatch | Daily, `REMINDER_DISPATCH_CRON_HOUR` | Emails a saved course reminder as the intake date approaches |
| Nightly accounts report | Daily, `NIGHTLY_REPORT_CRON_HOUR` | CSV summary of every enrollment, emailed to accounts |
| Credit-note dispatch | Every `CREDIT_NOTE_DISPATCH_INTERVAL_MINUTES` | Generates and emails the PDF for a staff-approved credit note |

Each job is independently switchable, and there's one master switch that overrides all of them:

```
SCHEDULER_ENABLED=true                        # master switch — false disables every job below
SCHEDULER_FOLLOWUPS_ENABLED=true
SCHEDULER_NIGHTLY_REPORT_ENABLED=true
SCHEDULER_REMINDER_DISPATCH_ENABLED=true
SCHEDULER_CREDIT_NOTE_DISPATCH_ENABLED=true
```

`scheduler.start()` checks `SCHEDULER_ENABLED` first — if false, no jobs are registered at all, regardless of the four per-job flags. If true, each per-job flag independently controls whether that one job gets added to the scheduler. This means you can, for example, run the credit-note dispatch and nightly report but disable follow-up nurturing, without touching anything else.

---

## 1. Nurture follow-ups

**Purpose**: automatically re-engage a lead who enquired but never replied again, without staff having to track who's gone quiet.

**Trigger**: once daily, at `FOLLOWUP_CHECK_CRON_HOUR` (default **9:00 SGT**).

**How it decides who's due** (`repositories.customers_due_for_followup()`): a SQL query against the `customers` table, not a separate queue — every customer with `status IN ('enquiry', 'awaiting_response')` and `escalated = FALSE` is a candidate, filtered by an escalating threshold based on how many follow-ups they've already had:

| `follow_up_count` | Due after |
|---|---|
| 0 (never followed up) | 24 hours since `last_contact_at` |
| 1 (one follow-up sent) | 72 hours since `last_contact_at` |
| 2 (two follow-ups sent) | 7 days since `last_contact_at` |
| 3+ | never — stops trying |

After three follow-ups with no reply, a customer is permanently excluded from this job (`follow_up_count < 3` in the query) — the system doesn't nag indefinitely.

**What happens for each due customer** (`run_followups()`):
1. Skip if there's no `whatsapp_id` on the record (shouldn't happen, defensive check) — logged as a warning.
2. Skip if there's no email on file. This isn't a bug — since registration became progressive, many leads exist with only a WhatsApp identity and no email yet. There's no fallback channel, so they're simply excluded from this run rather than the job failing.
3. Pick the message text via `lead_service.followup_message_for(customer)` — a fixed set of three escalating nudges, indexed by `follow_up_count` (capped at the last one if somehow higher):
   - **0** → *"Hi! Just following up on your enquiry about our dental assisting courses..."*
   - **1** → *"Hello again from Q&M Training! Our 2-Day Basic Certificate... is SkillsFuture-claimable..."*
   - **2** → *"Last reminder from Q&M Training — places for the upcoming intake are filling up..."*
4. Send it via `send_whatsapp(whatsapp_id, message)` — the same delivery path as any other outbound reply, routed by `whatsapp_id` so it works for both legacy (`@c.us`) and linked-identity (`@lid`) accounts.
5. On success, `repositories.bump_customer_followup()` increments `follow_up_count` and resets `last_contact_at` to now — this is what makes the customer ineligible again until the *next* threshold passes, and eventually caps out at 3.

**Failure handling**: each customer is wrapped in its own `try/except` — one failed send (e.g. a WhatsApp delivery error) is logged and skipped, it doesn't stop the rest of the batch from being processed. Returns the count actually sent.

---

## 2. Reminder dispatch

**Purpose**: fulfil the promise Module A makes throughout the conversation — "you'll get a reminder as the date approaches" — which nothing actually sent until this job existed.

**Trigger**: once daily, at `REMINDER_DISPATCH_CRON_HOUR` (default **8:00 SGT**).

**How it decides what's due** (`repositories.reminders_due_for_dispatch(lead_days)`, default `lead_days=3` from `REMINDER_DISPATCH_LEAD_DAYS`): joins the `reminders` table (one row per participant per course they asked to be reminded about — see `docs/DIALOGUE_STATE_REDESIGN.md` for how these get created) back to `courses`/`course_schedules` to recover the intake's actual start date:

```sql
SELECT r.*, c.full_name AS customer_name
FROM reminders r
JOIN courses cr ON cr.name = r.course_name
JOIN course_schedules cs ON cs.course_id = cr.id AND cs.label = r.course_date
JOIN customers c ON c.whatsapp_id = r.whatsapp_id
WHERE r.sent_at IS NULL
  AND cs.start_date BETWEEN CURRENT_DATE AND CURRENT_DATE + (lead_days || ' days')::interval
```

This join matters: `reminders.course_date` only ever stores the intake's *display label* text (e.g. `"21-22 Jul 2026"`), not a real date — because that's literally what the conversation captured. The join back to `course_schedules.label` is the only way to recover an actual comparable `DATE` value. If the matching course/intake was since removed from the catalogue, the reminder is silently excluded — there's nothing left to remind about.

`sent_at IS NULL` is what prevents a reminder from ever being emailed twice — this job never re-queries something it's already dispatched.

**What happens for each due reminder** (`run_reminder_dispatch()`):
1. Skip if there's no email on file (defensive — a reminder can't be created without one, per Module A's flow, but checked anyway).
2. Compose the email via `lead_service.reminder_email_subject_and_body(reminder)`:
   - Subject: `"Reminder: {course} starts {date}"`
   - Body: a short, friendly note naming the course and date, inviting them to reply on WhatsApp if they haven't enrolled yet.
3. Send via `send_email(...)`.
4. On success, `repositories.mark_reminder_sent(reminder["id"])` sets `sent_at = now()` — the row is now permanently excluded from future dispatch runs (it's never deleted, just marked sent).

**Failure handling**: same per-item `try/except` pattern as follow-ups — one failed email doesn't block the rest.

---

## 3. Nightly accounts report

**Purpose**: give the accounts team a daily reconciliation snapshot without anyone having to manually query the database or check the admin dashboard.

**Trigger**: once daily, at `NIGHTLY_REPORT_CRON_HOUR` (default **20:00 SGT**).

**How it works** (`services/payments.py: nightly_report()`):
1. `repositories.enrollments_by_status()` pulls every enrollment, regardless of status.
2. Writes a CSV to `backend/generated/accounts_report_{YYYYMMDD}.csv` with one row per enrollment: invoice number, participant name, course, course date, net payable, status, receipt number, and creation timestamp.
3. Tallies a count per status (e.g. `enrolled: 4`, `paid: 2`, `receipt_issued: 6`) for a quick summary in the email body.
4. Emails `settings.accounts_email` with the summary in the body and the full CSV as an attachment.

This is the only one of the four jobs with no per-row skip logic — every enrollment goes into the report regardless of its state, since the point is a complete reconciliation snapshot, not a filtered action list.

**Failure handling**: the whole job is wrapped in one `try/except` (unlike the per-item pattern above, since there's nothing to partially succeed at — either the report generates and sends, or it doesn't) — a failure is logged, not raised.

---

## 4. Credit-note dispatch

**Purpose**: decouple the (potentially slow) PDF generation + email delivery from the staff member's "approve" click in the admin UI, so approving a credit note feels instant regardless of mail-server latency.

**Trigger**: every `CREDIT_NOTE_DISPATCH_INTERVAL_MINUTES` (default **2 minutes**) — an `IntervalTrigger`, not a daily cron. This is deliberate: staff expect a credit note they just approved to reach the participant promptly, not wait until the next day's scheduled hour like the other three jobs.

**The split this exists to support**: when staff click "approve" on a credit note (`dbadmin.py`), `services/payments.py: approve_credit_note()` only flips its status to `approved` and returns immediately — it does *not* generate the PDF or send the email itself any more. This job is what actually finishes the job, picking up anything left in that state.

**How it decides what's due** (`repositories.credit_notes_pending_dispatch()`): every credit note with `status = 'approved' AND notified_at IS NULL`.

**What happens for each pending credit note** (`services/payments.py: dispatch_pending_credit_notes()`):
1. Look up the enrollment it belongs to. If it's gone (e.g. deleted), there's nothing meaningful left to email — mark it notified anyway so it isn't retried forever, and move on.
2. Otherwise, generate the credit note PDF (`generate_credit_note_pdf()`).
3. Email it to the participant, with the PDF attached and a short note naming the credit note number and course.
4. Mark it `notified_at = now()` — this is the same "never process twice" pattern as reminder dispatch's `sent_at`.

**Failure handling**: per-item `try/except` — one credit note failing to generate/send (e.g. a template error) doesn't block the others in the same run; it's simply retried on the next 2-minute pass since it never got marked notified.

---

## Configuration reference

All in `backend/.env`:

| Variable | Default | Controls |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | Master switch — `false` disables every job, regardless of the flags below |
| `SCHEDULER_FOLLOWUPS_ENABLED` | `true` | Nurture follow-ups |
| `SCHEDULER_NIGHTLY_REPORT_ENABLED` | `true` | Nightly accounts report |
| `SCHEDULER_REMINDER_DISPATCH_ENABLED` | `true` | Reminder dispatch |
| `SCHEDULER_CREDIT_NOTE_DISPATCH_ENABLED` | `true` | Credit-note dispatch |
| `FOLLOWUP_CHECK_CRON_HOUR` | `9` | Hour (SGT, 24h) nurture follow-ups run |
| `NIGHTLY_REPORT_CRON_HOUR` | `20` | Hour (SGT, 24h) the accounts report runs |
| `REMINDER_DISPATCH_CRON_HOUR` | `8` | Hour (SGT, 24h) reminder dispatch runs |
| `REMINDER_DISPATCH_LEAD_DAYS` | `3` | How many days before an intake a reminder is sent |
| `CREDIT_NOTE_DISPATCH_INTERVAL_MINUTES` | `2` | How often the credit-note dispatch check runs |

## At a glance: how each job avoids repeating itself

Every job that sends something writes a marker back to the database on success, and filters on that same marker when deciding what's due next time — this is what makes each one safe to run indefinitely without ever double-sending:

- Follow-ups → `customers.follow_up_count` increments (and changes which threshold applies next, eventually excluding the customer past 3)
- Reminder dispatch → `reminders.sent_at` gets set
- Credit-note dispatch → `credit_notes.notified_at` gets set
- Nightly report → not applicable; it's a full snapshot every time by design, not a queue of pending items

"""Background schedulers (APScheduler).

- Module A: daily nurture follow-ups for non-responsive customers (24h / 72h / 7d).
- Module A: daily reminder dispatch — emails a saved reminder once its course
  intake is within the configured lead time (Requirement 10 AC5).
- Module C: nightly accounts report (CSV) emailed to the accounting team.
- Module C: short-interval credit-note fan-out — PDF + email for every
  approved-but-not-yet-notified credit note (Requirement 11 AC3), decoupled
  from the synchronous staff-approval action in dbadmin.py.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import repositories as repo
from .config import settings
from .email_service import send_email
from .services import leads as lead_service
from .services import payments as payment_service
from .whatsapp_client import send_whatsapp

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def run_followups() -> int:
    """Send the next nurture message to each customer past a follow-up threshold.

    Customers are looked up from the customers table (the central identity store).
    Replies are routed via whatsapp_id so they reach both @c.us and @lid accounts.
    """
    due = repo.customers_due_for_followup()
    sent = 0
    for customer in due:
        whatsapp_id = customer.get("whatsapp_id")
        if not whatsapp_id:
            log.warning("Follow-up skipped — no whatsapp_id: id=%s", customer.get("id"))
            continue
        if not customer.get("email"):
            # Progressive registration (v1.1) means many leads now exist with
            # only minimum registration — skip rather than reminding another
            # way (Requirement 10 AC2-AC3).
            log.debug("Follow-up skipped — no email on file: wid=%s", whatsapp_id)
            continue
        try:
            message = lead_service.followup_message_for(customer)
            # Route via whatsapp_id; gateway resolves to the correct chat JID
            send_whatsapp(whatsapp_id, message)
            repo.bump_customer_followup(whatsapp_id)
            sent += 1
            log.info(
                "Follow-up sent: wid=%s phone=%s count=%s",
                whatsapp_id, customer.get("phone"),
                customer.get("follow_up_count", 0) + 1,
            )
        except Exception as e:  # noqa: BLE001
            log.error("follow-up failed for wid=%s: %s", whatsapp_id, e)
    if sent:
        log.info("Sent %d follow-up message(s).", sent)
    return sent


def run_reminder_dispatch() -> int:
    """Email every saved reminder whose course intake is within the
    configured lead time and hasn't been sent yet (Requirement 10 AC5).

    Found while reviewing ROUTER_REDESIGN.md: Module A had been writing
    fully-formed rows to `reminders` (via services/leads.py: save_lead() ->
    repo.create_reminder()) since Task 42, and every reminder-confirmation
    reply this whole session told the participant they'd get an email
    reminder as the date approached — but nothing ever actually sent one.
    This is that dispatch.
    """
    due = repo.reminders_due_for_dispatch(settings.reminder_dispatch_lead_days)
    sent = 0
    for reminder in due:
        if not reminder.get("email"):
            log.debug("Reminder skipped — no email on file: id=%s", reminder["id"])
            continue
        try:
            subject, body = lead_service.reminder_email_subject_and_body(reminder)
            send_email(reminder["email"], subject=subject, body=body)
            repo.mark_reminder_sent(reminder["id"])
            sent += 1
            log.info(
                "Reminder sent: id=%s wid=%s course=%r date=%r",
                reminder["id"], reminder.get("whatsapp_id"),
                reminder.get("course_name"), reminder.get("course_date"),
            )
        except Exception as e:  # noqa: BLE001
            log.error("Reminder dispatch failed for id=%s: %s", reminder["id"], e)
    if sent:
        log.info("Sent %d reminder(s).", sent)
    return sent


def run_nightly_report() -> None:
    try:
        path = payment_service.nightly_report()
        log.info("Nightly accounts report generated: %s", path)
    except Exception as e:  # noqa: BLE001
        log.error("nightly report failed: %s", e)


def start() -> None:
    global _scheduler
    if not settings.scheduler_enabled or _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="Asia/Singapore")
    started: list[str] = []
    if settings.scheduler_followups_enabled:
        _scheduler.add_job(
            run_followups,
            CronTrigger(hour=settings.followup_check_cron_hour, minute=0),
            id="nurture_followups",
            replace_existing=True,
        )
        started.append(f"follow-ups {settings.followup_check_cron_hour:02d}:00")
    if settings.scheduler_nightly_report_enabled:
        _scheduler.add_job(
            run_nightly_report,
            CronTrigger(hour=settings.nightly_report_cron_hour, minute=0),
            id="nightly_report",
            replace_existing=True,
        )
        started.append(f"nightly report {settings.nightly_report_cron_hour:02d}:00")
    if settings.scheduler_reminder_dispatch_enabled:
        _scheduler.add_job(
            run_reminder_dispatch,
            CronTrigger(hour=settings.reminder_dispatch_cron_hour, minute=0),
            id="reminder_dispatch",
            replace_existing=True,
        )
        started.append(f"reminder dispatch {settings.reminder_dispatch_cron_hour:02d}:00")
    if settings.scheduler_credit_note_dispatch_enabled:
        _scheduler.add_job(
            payment_service.dispatch_pending_credit_notes,
            IntervalTrigger(minutes=settings.credit_note_dispatch_interval_minutes),
            id="credit_note_dispatch",
            replace_existing=True,
        )
        started.append(f"credit-note dispatch every {settings.credit_note_dispatch_interval_minutes} min")
    _scheduler.start()
    log.info(
        "Scheduler started (%s SGT).",
        "; ".join(started) if started else "no jobs enabled",
    )


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None

"""Central configuration loaded from backend/.env (pydantic-settings)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # .../backend


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── OpenAI ──────────────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_vision_model: str = "gpt-4o"
    openai_temperature: float = 0.2

    # ── Database ────────────────────────────────────────────────────────────
    database_url: str = "postgresql://qm_user:qm_password@localhost:5432/qm_enrollment"

    # ── Backend server ──────────────────────────────────────────────────────
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000

    # ── WhatsApp gateway (outbound) ─────────────────────────────────────────
    whatsapp_send_url: str = "http://localhost:3000/send-reply"
    whatsapp_enabled: bool = True

    # ── Email ───────────────────────────────────────────────────────────────
    # Demo defaults point at smtp.freesmtpservers.com, a free no-auth relay
    # (see scripts/testemail.py) so the invoice/receipt email flow works with
    # zero setup. Override via .env with real SMTP_HOST/PORT/USER/PASSWORD/
    # USE_TLS for production — the rest of the code is provider-agnostic.
    email_enabled: bool = False
    smtp_host: str = "smtp.freesmtpservers.com"
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "Q&M Training <no-reply@example.com>"
    smtp_use_tls: bool = False
    staff_email: str = "staff@example.com"
    accounts_email: str = "accounts@example.com"

    # ── Company / payment ───────────────────────────────────────────────────
    company_name: str = "Q&M Dental Group (Singapore) Limited"
    company_uen: str = "200008000C"
    paynow_uen: str = "200008000C"
    paynow_payee: str = "Q&M Dental Group"

    # ── Module C ────────────────────────────────────────────────────────────
    auto_confirm_threshold: float = 0.80

    # ── Scheduler ───────────────────────────────────────────────────────────
    scheduler_enabled: bool = True
    followup_check_cron_hour: int = 9
    nightly_report_cron_hour: int = 20
    reminder_dispatch_cron_hour: int = 8  # Requirement 10 AC5
    reminder_dispatch_lead_days: int = 3
    credit_note_dispatch_interval_minutes: int = 2  # Requirement 11 AC3

    # ── Query/data-flow logging ─────────────────────────────────────────────
    # Writes the query → orchestration → agent → tools → response trace for
    # every turn to a daily text file in backend/logs/, in addition to the
    # console. Set false to disable file writes entirely (console trace is
    # unaffected).
    query_log_enabled: bool = True

    @property
    def generated_dir(self) -> Path:
        d = BASE_DIR / "generated"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def logs_dir(self) -> Path:
        d = BASE_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


settings = Settings()

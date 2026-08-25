"""Request-scoped message context.

The agents' tools need the sender's whatsapp_id, phone, and (for payment
verification) the attached screenshot, but we deliberately keep that data OUT
of the LLM token stream: tools read it from here instead of receiving it as
model-filled args.
Set once per inbound message by the orchestrator.

Available fields set by the orchestrator
─────────────────────────────────────────
    whatsapp_id   raw WhatsApp JID (stable per user, always present)
    phone         real E.164 digits, confirmed (available after confirmation)
    display_name  WhatsApp notifyName
    customer      full customers table row (dict)
    body          message text
    media         { data: base64, mimetype: str } or None
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_current: ContextVar[dict] = ContextVar("current_message", default={})

# NOTE: a per-turn "did X actually happen" flag (e.g. "was an enrollment
# created this turn") was tried here twice — once for enrollment creation,
# once for lead-save completeness — and both were later removed after being
# observed NOT reliably propagating a ContextVar write from inside a tool
# call back to the code that kicked off the crew.kickoff() call, even when
# CrewAI's own delegation hop is documented as synchronous/same-thread. One
# instance let a real, invoiced enrollment's genuine confirmation get
# replaced by a "you're not enrolled" fallback (crews/module_a.py's
# fabrication guard, now checking a fresh DB read — repo.latest_enrollment()
# before/after — instead). Don't reintroduce this pattern for a new per-turn
# flag; check observable state (the reply text, or a fresh DB read) instead.


def set_message(ctx: dict) -> None:
    _current.set(ctx)


def get_message() -> dict:
    return _current.get()


def whatsapp_id() -> str:
    """Raw WhatsApp JID — primary stable identifier, always present."""
    return get_message().get("whatsapp_id", "")


def phone() -> str:
    """Confirmed real E.164 phone number (digits only, no +)."""
    return get_message().get("phone", "")


def customer() -> dict:
    """Full customers table row for the current sender."""
    return get_message().get("customer") or {}


def display_name() -> str | None:
    return get_message().get("display_name")


def media() -> tuple[str | None, str | None]:
    """Return (base64_data, mimetype) for the current message, if any."""
    m = get_message().get("media") or {}
    return m.get("data"), m.get("mimetype")


def get(key: str, default: Any = None) -> Any:
    return get_message().get(key, default)

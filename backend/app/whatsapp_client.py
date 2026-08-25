"""Outbound WhatsApp delivery — posts to the Node gateway's /send-reply.

This is the backend side of the "Send WhatsApp Reply" action used by the agents
and the orchestrator. If WHATSAPP_ENABLED is false (dev/test) it just prints.
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)


def send_whatsapp(to: str, message: str) -> bool:
    """Send a WhatsApp message to a phone number via the gateway."""
    if not settings.whatsapp_enabled:
        print(f"\n[WhatsApp -> {to}]\n{message}\n" + "-" * 50)
        return True
    try:
        r = httpx.post(
            settings.whatsapp_send_url,
            json={"to": to, "message": message},
            timeout=30.0,
        )
        r.raise_for_status()
        log.info("WhatsApp reply sent to %s", to)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("WhatsApp send failed (%s): %s", to, e)
        # Still surface the message in logs so nothing is silently lost.
        print(f"\n[WhatsApp SEND FAILED → {to}]\n{message}\n")
        return False

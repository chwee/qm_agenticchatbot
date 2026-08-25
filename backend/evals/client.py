"""Thin HTTP client over the backend's POST /webhook/whatsapp/sync endpoint.

Zero-touch by design: this talks to the running backend over plain HTTP,
exactly like the WhatsApp gateway would. It does not import anything from
app/, and it never mutates business logic - it only sends messages and reads
back the reply text the orchestrator already produces.
"""
from __future__ import annotations

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"


class SyncClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 60.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def send(self, chat_id: str, phone: str, name: str, body: str) -> str:
        """POST one inbound message, return the reply text."""
        payload = {
            "from": {"chat_id": chat_id, "phone": phone, "name": name},
            "message": {"body": body},
        }
        r = self._client.post("/webhook/whatsapp/sync", json=payload)
        r.raise_for_status()
        return r.json().get("reply", "")

    def close(self) -> None:
        self._client.close()

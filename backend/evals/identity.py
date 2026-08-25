"""Synthetic per-run WhatsApp identity generation.

Each scenario run gets its own throwaway @c.us identity so eval runs never
collide with real customer data and never reuse state between scenarios.
These rows land in the same database the backend is pointed at (there is no
separate test database) - they are ordinary "customers" rows the DB Manager
UI will show, easily recognisable by the "EvalBot-" name prefix.
"""
from __future__ import annotations

import uuid


def new_identity(label: str) -> tuple[str, str, str]:
    """Return (chat_id, phone, name) for a fresh synthetic eval customer."""
    suffix = uuid.uuid4().hex[:7]
    phone = f"9{int(suffix, 16) % 10_000_000:07d}"
    chat_id = f"{phone}@c.us"
    name = f"EvalBot-{label}"
    return chat_id, phone, name

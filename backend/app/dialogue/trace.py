"""Phase 1 observational logging, shared by every module's dual-write
(DIALOGUE_STATE_REDESIGN.md). Factored out once module_b.py/module_c.py
needed the identical helper module_a.py had already defined locally — kept
here rather than duplicated a third and fourth time, the same dedup this
whole redesign exists to do elsewhere.
"""
from __future__ import annotations

import logging

from .state import PendingQuestion, Slots


def log_dialogue_state(log: logging.Logger, wid: str, pending: PendingQuestion, slots: Slots) -> None:
    """Log the asserted (pending, slots) pair for later comparison against
    the marker-based system it runs beside — the phase 1 gate is >=95%
    agreement between the two over replayed traffic. Never consulted for
    control flow, so a logging failure here must never be able to break a
    turn — callers don't need their own try/except around this."""
    try:
        log.info(
            "dialogue-state (phase 1, observational): wid=%s pending=%s slots=%s",
            wid, pending.value, slots.to_json(),
        )
    except Exception:  # noqa: BLE001
        pass

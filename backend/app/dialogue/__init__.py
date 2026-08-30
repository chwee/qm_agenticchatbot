"""Explicit dialogue-state types (DIALOGUE_STATE_REDESIGN.md).

Phase 1: types only, dual-written alongside the existing conversation_state
columns for comparison — nothing yet reads them to make a decision. See
docs/DIALOGUE_STATE_REDESIGN.md for the full design and rollout status.
"""
from .state import ACTIVE_FLOW_FOR_PENDING, DialogueState, PendingQuestion, Slots, TurnResult
from .trace import log_dialogue_state

__all__ = [
    "ACTIVE_FLOW_FOR_PENDING", "DialogueState", "PendingQuestion", "Slots",
    "TurnResult", "log_dialogue_state",
]

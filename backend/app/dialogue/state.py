"""Dialogue state types (DIALOGUE_STATE_REDESIGN.md phase 1).

Today, "which question is open" and "what's been resolved toward answering
it" live only as English in the transcript, re-parsed every turn — even
conversation_state.active_flow, nominally the authoritative record, collapses
multiple genuinely distinct sub-stages into one string (module_a.py's
"awaiting_reminder" covers the email-ask, the which-course-ask AND the
which-intake-ask; which one is actually open is disambiguated by regexing the
assistant's last reply — _INTAKE_ASK_CONTEXT_RE / _COURSE_ASK_CONTEXT_RE).

These types are the explicit alternative. Phase 1 is dual-write only: modules
construct a DialogueState alongside their existing logic and it's persisted
into conversation_state.flow_context purely for comparison against the old
system — nothing reads it back to decide anything yet. Phase 4 is what makes
this the thing orchestrator._dispatch() actually writes state from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PendingQuestion(str, Enum):
    """What the bot is waiting on this participant to answer. A plain string
    Enum (not stored as its own DB column in phase 1 — see Slots' docstring
    for why) so it's a bare string once serialized, comparable directly
    against a logged active_flow value."""

    NONE = "none"

    # module_a.py reminder flow — today all three share active_flow value
    # "awaiting_reminder"; distinguished here for the first time.
    EMAIL_FOR_REMINDER = "email_for_reminder"
    WHICH_COURSE = "which_course"
    WHICH_INTAKE = "which_intake"

    # module_b.py enrolment/cancellation flow
    CONFIRM_ENROL = "confirm_enrol"
    CONFIRM_CANCEL = "confirm_cancel"
    INTAKE_SELECTION = "intake_selection"

    # router.py disambiguation menu
    DISAMBIGUATION = "disambiguation"

    # Named for completeness (see docs/DIALOGUE_STATE_REDESIGN.md) but not
    # yet asserted by any module in phase 1:
    #   - registration continuation is tracked by matching the exact text of
    #     the last message sent (orchestrator._is_registration_prompt()), not
    #     conversation_state, so there is nothing to dual-write against yet.
    #   - payment-proof has no owner at all today — module_c.py never calls
    #     set_conversation_state. That gap is unchanged by this phase.
    REGISTRATION_FIELDS = "registration_fields"
    PAYMENT_PROOF = "payment_proof"


@dataclass(frozen=True)
class Slots:
    """Whatever's been resolved so far toward answering a PendingQuestion.
    All fields default empty — a Slots with nothing set just means nothing's
    been resolved yet, which is itself meaningful to log.

    Deliberately plain data, not its own conversation_state column: unlike
    PendingQuestion (a closed, meaningful-on-its-own enum), a Slots is only
    interpretable together with the PendingQuestion it belongs to — course_code
    means something different while WHICH_INTAKE is open (the course is
    settled, only the date is being asked about) than while WHICH_COURSE is
    open (a guess, not yet confirmed). Keeping them together in one
    flow_context JSONB blob, keyed by the pending question, avoids the two
    silently drifting out of sync the way active_flow and
    customers.preferred_course/course_date already can today.
    """

    course_code: str | None = None      # e.g. 'C2603' — courses.course_id
    schedule_code: str | None = None    # e.g. 'SH2605' — course_schedules.schedule_id
    invoice_no: str | None = None
    # What was actually offered/discussed, in the same order shown to the
    # participant — course names while WHICH_COURSE is open, intake labels
    # while WHICH_INTAKE is open. Lets a reply like "the second one" resolve
    # by indexing this tuple instead of re-parsing the transcript (that
    # resolution logic is a later phase; phase 1 only records the tuple).
    candidates: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "course_code": self.course_code,
            "schedule_code": self.schedule_code,
            "invoice_no": self.invoice_no,
            "candidates": list(self.candidates),
        }

    @classmethod
    def from_json(cls, data: dict | None) -> "Slots":
        data = data or {}
        return cls(
            course_code=data.get("course_code"),
            schedule_code=data.get("schedule_code"),
            invoice_no=data.get("invoice_no"),
            candidates=tuple(data.get("candidates") or ()),
        )


@dataclass(frozen=True)
class DialogueState:
    """One participant's asserted dialogue state for this turn."""

    module: str | None   # 'A' | 'B' | 'C' | 'ROUTER' | None (idle)
    pending: PendingQuestion
    slots: Slots = field(default_factory=Slots)


# Maps a PendingQuestion to the conversation_state.active_flow string value
# Tier 0 sticky dispatch and each module's own regex-based context-window
# detection (_INTAKE_ASK_CONTEXT_RE, _pending_confirmation(), etc.) still
# read today — phase 4 relocates WHO writes conversation_state, not what
# string is written; migrating those readers onto PendingQuestion directly
# is a later phase. Three PendingQuestion values collapse onto ONE
# active_flow ("awaiting_reminder") because that string genuinely can't
# distinguish them yet — see PendingQuestion's own docstring.
ACTIVE_FLOW_FOR_PENDING: dict[PendingQuestion, str] = {
    PendingQuestion.EMAIL_FOR_REMINDER: "awaiting_reminder",
    PendingQuestion.WHICH_COURSE: "awaiting_reminder",
    PendingQuestion.WHICH_INTAKE: "awaiting_reminder",
    PendingQuestion.CONFIRM_ENROL: "awaiting_enroll_confirm",
    PendingQuestion.CONFIRM_CANCEL: "awaiting_cancel_confirm",
    PendingQuestion.INTAKE_SELECTION: "awaiting_intake_selection",
    PendingQuestion.DISAMBIGUATION: "awaiting_disambiguation",
}


@dataclass(frozen=True)
class TurnResult:
    """What a module's run() returns for one turn (DIALOGUE_STATE_REDESIGN.md
    phase 4) — the only thing orchestrator._dispatch() uses to both send the
    reply AND write conversation_state.

    Previously each module called repo.set_conversation_state() /
    clear_conversation_state_if_owner() itself, deep inside its own control
    flow, at up to 7 different points depending on which branch a turn took
    (module_a.py's reminder flow) or 5 (module_b.py's enrolment/cancellation
    flow) — easy for a new branch to add without remembering the write, and
    impossible to audit from one place. This makes the orchestrator the
    single, auditable site that decision is actually written from — nothing
    else about the decision LOGIC changes; the same marker/regex matching
    that used to decide the DB call now decides this dataclass's fields
    instead, at the exact same points in the exact same order."""

    reply: str
    module: str                              # 'A' | 'B' | 'C'
    pending: PendingQuestion = PendingQuestion.NONE
    slots: Slots = field(default_factory=Slots)

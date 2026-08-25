"""CrewAI tools — thin wrappers over services, grouped per agent/module.

Tools read the sender's phone (and any screenshot) from app.context, so the LLM
only ever fills in semantic fields it can see in the message text.
"""
from .course_tools import (
    course_fees_tool,
    course_schedule_tool,
    list_courses_tool,
    skillsfuture_info_tool,
)
from .enrollment_tools import (
    enroll_participant_tool,
    my_enrollments_tool,
    request_cancellation_tool,
    resend_invoice_tool,
    validate_enrollment_tool,
)
from .lead_tools import flag_for_staff_tool, save_lead_tool
from .payment_tools import payment_balance_tool, resend_receipt_tool, settle_payment_tool

MODULE_A_TOOLS = [
    list_courses_tool,
    course_fees_tool,
    course_schedule_tool,
    skillsfuture_info_tool,
    save_lead_tool,
    flag_for_staff_tool,
]

MODULE_B_TOOLS = [
    validate_enrollment_tool,
    enroll_participant_tool,
    my_enrollments_tool,
    resend_invoice_tool,
    request_cancellation_tool,  # Requirement 11 AC5 — gated like enroll_participant_tool
    list_courses_tool,      # resolve "course 1" / "course 2" references
    course_schedule_tool,   # show intake dates so participant can pick a number
]

MODULE_C_TOOLS = [
    settle_payment_tool,
    resend_receipt_tool,
    payment_balance_tool,     # itemised remaining balance (SkillsFuture claim / PayNow)
    resend_invoice_tool,      # look up / resend the invoice when the participant forgets the number
    my_enrollments_tool,      # confirm which enrollment/invoice is outstanding — never the
                               # single-latest-only lookup, which silently picks the wrong one
                               # whenever more than one enrollment exists
]

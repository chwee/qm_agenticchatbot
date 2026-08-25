"""Module A tools — lead capture and staff escalation."""
from __future__ import annotations

import re

from crewai.tools import tool

from .. import context
from ..services import leads

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@tool("Save Lead Data")
def save_lead_tool(
    name: str = "",
    nric: str = "",
    dob: str = "",
    email: str = "",
    preferred_course: str = "",
    course_date: str = "",
) -> str:
    """Save or update the participant's lead details captured during the chat.
    Fields (all optional, fill what the participant has given): name, nric,
    dob (YYYY-MM-DD), email, preferred_course, course_date (intake label,
    e.g. '21-22 Jul 2026'). The phone number is taken from context automatically."""
    fields = {}
    if name:
        fields["name"] = name
    if nric:
        fields["nric"] = nric
    if email:
        fields["email"] = email
    if preferred_course:
        fields["preferred_course"] = preferred_course
    if course_date:
        fields["course_date"] = course_date.strip()
    if dob and _DATE_RE.match(dob.strip()):
        fields["dob"] = dob.strip()
    if not fields:
        return "Nothing to save yet."
    leads.save_lead(context.phone(), whatsapp_id=context.whatsapp_id(), **fields)
    return f"Saved lead details: {', '.join(fields.keys())}."


@tool("Flag For Staff Review")
def flag_for_staff_tool(reason: str, message: str = "") -> str:
    """Escalate a complex, sensitive, or unrecognised enquiry to a human staff
    member. Input: a short reason, and optionally the participant's message.
    Use this when you cannot confidently answer from the knowledge base."""
    return leads.flag_for_staff(context.phone(), reason, message or reason)

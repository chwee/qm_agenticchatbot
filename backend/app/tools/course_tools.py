"""Module A tools — course information (read-only DB lookups)."""
from __future__ import annotations

from crewai.tools import tool

from .. import context
from .. import repositories as repo
from ..services import courses


def _remember_interest(course_arg: str) -> None:
    """Best-effort: looking up fees or a schedule for a specific real course
    is itself a strong signal of current interest — persist it directly as
    customers.preferred_course so a follow-up reminder has something to
    reference, without depending on the conversational agent remembering to
    call 'Save Lead Data' proactively (prompt-only capture of this proved
    unreliable in live testing — the agent answered fee/schedule questions
    correctly every time but essentially never called the tool for it).

    Skipped while a reminder is already pending (conversation_state ==
    Module A's "awaiting_reminder") — observed live: an unrelated schedule
    lookup mid-exchange (e.g. answering "what course is that date for?")
    silently swapped preferred_course to the looked-up course, and the next
    turn's "which intake" reply then named the wrong course/dates entirely.
    A reminder that's already down to picking an intake has its course
    locked in; only an explicit course-choice reply should change it."""
    try:
        wid = context.whatsapp_id()
        state = repo.get_conversation_state(wid)
        if state and state.get("active_module") == "A" and state.get("active_flow") == "awaiting_reminder":
            return
        resolved = courses.resolve_course_arg(course_arg)
        c = repo.get_course_by_name(resolved)
        if c:
            repo.update_customer(wid, preferred_course=c["name"])
    except Exception:
        pass


@tool("List Courses")
def list_courses_tool() -> str:
    """List every available Q&M training course with its C-code and fee.
    Each course is shown as [C-CODE] Name — Fee.
    Use the C-code (e.g. C2601) with 'Course Fees' or 'Course Schedule'.
    No input needed."""
    try:
        customer = context.customer()
    except Exception:
        customer = None
    return courses.format_courses(customer=customer)


@tool("Course Fees")
def course_fees_tool(course: str) -> str:
    """Get the full fee breakdown and SkillsFuture subsidy for one course.
    Input: the course C-code (e.g. C2601) or course name.
    Call 'List Courses' first to get C-codes if you don't have one."""
    try:
        customer = context.customer()
    except Exception:
        customer = None
    result = courses.format_fees(course, customer=customer)
    _remember_interest(course)
    return result


@tool("Course Schedule")
def course_schedule_tool(course: str) -> str:
    """Get the upcoming intake dates (with SH-codes) for one course.
    Input: the course C-code (e.g. C2601) or course name.
    Each intake is shown with its SH-code and seats left — use the SH-code to
    resolve which intake the participant means, never show it as a command."""
    try:
        customer = context.customer()
    except Exception:
        customer = None
    result = courses.format_schedule(course, customer=customer)
    _remember_interest(course)
    return result


@tool("SkillsFuture Info")
def skillsfuture_info_tool() -> str:
    """Return general SkillsFuture Credit eligibility info and the MySkillsFuture
    portal link. Does NOT check any individual's balance (no Singpass). No input."""
    return courses.format_sfc()

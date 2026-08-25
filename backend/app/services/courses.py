"""Course information services (Module A) — course list, fees, schedule, SkillsFuture info.

Short-code conventions
──────────────────────
  format_courses() lists each course with its C-code (e.g. C2601).
  format_schedule() lists each intake with its SH-code (e.g. SH2601).
  resolve_course_arg() accepts a C-code, a list item number, or a partial name.
  resolve_schedule_arg() accepts an SH-code or a 1-based item number.
"""
from __future__ import annotations

import re

from .. import repositories as repo
from ..utils import (
    compute_fee,
    course_short_id,
    money,
    schedule_short_id,
)

_C_CODE_RE  = re.compile(r"^[Cc]\d{4}$")
_SH_CODE_RE = re.compile(r"^[Ss][Hh]\d{4}$")
# "cour\w{0,3}" tolerates common misspellings of "course" (coure, cours,
# coures) — the same pattern already duplicated in orchestrator.py/
# module_a.py/module_b.py for this identical purpose. Added here too after a
# live bug: resolve_course_arg()'s bare-digit check alone doesn't match
# "course 1"/"course 4" (not a pure digit string), so those phrasings fell
# through to "return as-is" and got persisted as literal, nonsense course
# names (e.g. services/leads.py: save_lead() saving preferred_course="Course
# 4" — a string that can never match a real course, silently breaking the
# reminder-dispatch join in repositories.reminders_due_for_dispatch()).
_COURSE_POSITION_RE = re.compile(r"\bcour\w{0,3}\s*(?:number\s*|no\.?\s*|#\s*)?(\d+)\b", re.IGNORECASE)


def _c(course: dict) -> str:
    """Return the stored course_id code, falling back to the computed form."""
    return course.get("course_id") or course_short_id(course["id"])


def _sh(schedule: dict) -> str:
    """Return the stored schedule_id code, falling back to the computed form."""
    return schedule.get("schedule_id") or schedule_short_id(schedule["id"])


def resolve_course_arg(arg: str) -> str:
    """Resolve a course argument to its full name.

    Accepts (in priority order):
    1. C-code        : C2601        → look up by stored course_id column
    2. Bare item no.  : 1, 2         → nth course in the ordered list
    3. "course N"     : course 1, coure 4, course no. 2 → same, tolerant of
                         the common phrasing/misspelling this pattern is
                         named for elsewhere in this codebase
    4. Name/text      : returned as-is for fuzzy DB lookup downstream
    """
    stripped = (arg or "").strip().rstrip(".,;:")

    if _C_CODE_RE.match(stripped):
        course = repo.get_course_by_course_id(stripped)
        return course["name"] if course else stripped

    if stripped.isdigit():
        all_courses = repo.list_courses()
        idx = int(stripped) - 1
        if 0 <= idx < len(all_courses):
            return all_courses[idx]["name"]

    m = _COURSE_POSITION_RE.search(stripped)
    if m:
        all_courses = repo.list_courses()
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(all_courses):
            return all_courses[idx]["name"]

    return stripped if stripped else (arg or "").strip()


def resolve_schedule_arg(arg: str, course_id: int) -> tuple[int | None, int | None]:
    """Resolve a schedule argument to (schedule_no, schedule_db_id).

    Accepts:
    - SH-code (SH2603) → returns (None, db_pk) if it belongs to course_id
    - Item number (1, 2) → returns (n, None)
    - Empty/unrecognised → returns (None, None) — caller uses next available date
    """
    stripped = (arg or "").strip().rstrip(".,;:")
    if not stripped:
        return None, None

    if _SH_CODE_RE.match(stripped):
        sched = repo.get_schedule_by_schedule_id(stripped)
        if sched and sched.get("course_id") == course_id:
            return None, sched["id"]
        return None, None  # SH-code doesn't belong to this course

    if stripped.isdigit():
        return int(stripped), None

    return None, None


def format_courses(customer: dict | None = None) -> str:
    all_courses = repo.list_courses()
    if not all_courses:
        return "No courses are currently available. Please check back soon."

    lines = ["*Q&M Training — Available Courses*", ""]
    for c in all_courses:
        lines.append(f"[{_c(c)}] {c['name']} — {money(c['full_fee'])}")

    lines += [
        "",
        "Ready to enroll? Just tell us which course (and intake) you'd like.",
    ]
    return "\n".join(lines)


def format_fees(course_arg: str | None, customer: dict | None = None) -> str:
    if not course_arg:
        return "Which course would you like the fees for?"
    resolved = resolve_course_arg(course_arg)
    c = repo.get_course_by_name(resolved)
    if not c:
        return (
            f"Course '{resolved}' not found — could you tell me which course you mean, or "
            "ask me to list the courses?"
        )
    fee, subsidy, net = compute_fee(c["full_fee"], c["sf_subsidy_cap"])
    net_line = "S$0 — fully claimable" if net <= 0 else money(net)
    code = _c(c)

    return (
        f"*{c['name']}* [{code}]\n\n"
        f"Full fee        : {money(fee)}\n"
        f"SkillsFuture    : up to {money(c['sf_subsidy_cap'])}\n"
        f"Net payable     : {net_line}\n"
        f"Mid-Career SFC  : additional credit may apply (age 40+)\n\n"
        "Ready to enroll? Just let us know and we'll take care of it."
    )


def format_schedule(course_arg: str | None, customer: dict | None = None) -> str:
    if not course_arg:
        return "Which course would you like the intake dates for?"
    resolved = resolve_course_arg(course_arg)
    c = repo.get_course_by_name(resolved)
    if not c:
        return (
            f"Course '{resolved}' not found — could you tell me which course you mean, or "
            "ask me to list the courses?"
        )
    schedules = repo.get_schedules(c["id"])
    if not schedules:
        return f"No upcoming intakes for {c['name']} yet. Please check back soon."

    c_code = _c(c)
    lines = [f"*{c['name']}* [{c_code}]", "*Upcoming Intakes:*", ""]
    for s in schedules:
        sh = _sh(s)
        lines.append(f"  [{sh}]  {s['label']}  ({s['seats']} seats)")

    lines += ["", "Just tell us which intake you'd like, and we'll enroll you."]
    return "\n".join(lines)


def format_sfc() -> str:
    """General SkillsFuture info — no personal data, no Singpass."""
    return (
        "*SkillsFuture Credit (SFC)*\n\n"
        "• All Singaporeans aged 25+ receive an opening SFC of S$500.\n"
        "• Singaporeans aged 40+ received an additional Mid-Career top-up.\n"
        "• Use SFC to offset eligible course fees.\n\n"
        "Check your own balance (we cannot do this for you):\n"
        "  1. Visit https://www.myskillsfuture.gov.sg\n"
        "  2. Log in with Singpass\n"
        "  3. Open 'SkillsFuture Credit'\n\n"
        "Just ask and I can show you the fees with the SFC breakdown for any course."
    )

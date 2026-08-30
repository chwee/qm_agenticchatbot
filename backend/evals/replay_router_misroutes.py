"""Replay historical router misroutes through the CURRENT router code.

DIALOGUE_STATE_REDESIGN.md's phase 2/3 gates ("enquiry->B misroutes drop
below 8%" / "all logged misroutes replay clean") need scoring against real
history, not a guess. This script:

  1. Parses backend/logs/query_log_*.txt into structured turns.
  2. Finds messages that carry no explicit enrollment/payment signal of
     their own (per router.py's OWN _has_explicit_enrollment_signal() /
     _looks_like_payment_intent() checks — not a separately-invented
     heuristic) yet were historically routed to Module B anyway. Such a
     message can only have reached B via context contamination or an
     over-eager sticky dispatch — never from its own content.
  3. Replays each one through router.route() end-to-end, with the real
     preceding conversation seeded into a scratch whatsapp_id's chat_memory
     (reconstructing the actual episode) and a REAL Tier 1 classifier call
     when Tier 0 has nothing pending — no mocking, no simulation.
  4. Reports how many now correctly avoid Module B.

Costs a handful of real OpenAI calls (gpt-4o-mini, one per case that reaches
Tier 1). Requires the DB reachable (uses scratch whatsapp_ids, cleaned up
after every run, real or interrupted) and OPENAI_API_KEY set.

Run from backend/evals/: python replay_router_misroutes.py
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import context, database  # noqa: E402
from app import repositories as repo  # noqa: E402
from app.crews import router  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
TEST_PREFIX = "REPLAY_"


# ── Log parsing ───────────────────────────────────────────────────────────
@dataclass
class Turn:
    file: str
    index: int  # order within file
    wid: str
    message: str
    routing: str
    reply: str


def _parse_file(path: Path) -> list[Turn]:
    lines = path.read_text(encoding="utf-8").splitlines()
    turns: list[Turn] = []
    i, n, idx = 0, len(lines), 0
    while i < n:
        if not lines[i].startswith("┌─ TURN"):
            i += 1
            continue
        i += 1
        wid = ""
        if i < n and lines[i].startswith("│ From"):
            m = re.match(r"│ From\s*:\s*(.*)", lines[i])
            wid = m.group(1).strip() if m else ""
            i += 1
        msg_lines: list[str] = []
        if i < n and lines[i].startswith("│ Message"):
            m = re.match(r'│ Message : "(.*)', lines[i])
            first = (m.group(1) if m else "").rstrip('"')
            msg_lines.append(first)
            i += 1
            while i < n and lines[i].startswith("│            "):
                cont = lines[i][len("│            "):].rstrip('"')
                msg_lines.append(cont)
                i += 1
        message = "\n".join(msg_lines)
        while i < n and not lines[i].startswith("├─ ROUTING"):
            i += 1
        routing = ""
        if i < n and lines[i].startswith("├─ ROUTING"):
            i += 1
            if i < n and lines[i].startswith("│ "):
                routing = lines[i][2:].strip()
                i += 1
        while i < n and not lines[i].startswith("├─ REPLY"):
            i += 1
        reply_lines: list[str] = []
        if i < n and lines[i].startswith("├─ REPLY"):
            i += 1
            while i < n and lines[i].startswith("│"):
                reply_lines.append(lines[i][2:] if lines[i].startswith("│ ") else "")
                i += 1
        reply = "\n".join(reply_lines)
        turns.append(Turn(file=path.name, index=idx, wid=wid, message=message,
                           routing=routing, reply=reply))
        idx += 1
    return turns


def load_all_turns() -> list[Turn]:
    all_turns: list[Turn] = []
    for p in sorted(LOG_DIR.glob("query_log_*.txt")):
        all_turns.extend(_parse_file(p))
    return all_turns


# ── Misroute detection ───────────────────────────────────────────────────
# Excludes shapes that are LEGITIMATELY Module-B-shaped even without their
# own explicit enrollment signal — a registration-field reply, a bare
# yes/no answering a real pending confirmation, a bare intake-position
# reply, an explicit decline, or a first-person status query (router.py's
# own classifier backstory documents "what did I register for" as
# ENROLLMENT, not ENQUIRY) — none of those are misroutes, they're sticky
# dispatch or classification working as intended.
_REG_FIELD_SHAPED_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+|\b[STFGMstfgm]\d{7}[A-Za-z]\b|\b\d{7,14}\b"
)
_BARE_CONFIRM_RE = re.compile(r"^\s*(yes|yeah|yep|sure|ok(ay)?|no|nope|nah)\.?\s*$", re.IGNORECASE)
_BARE_POSITION_RE = re.compile(
    r"^\s*(intake\s*)?#?\d+\.?\s*$|^\s*(the\s+)?(first|second|third|1st|2nd|3rd)\s*(one)?\.?\s*$",
    re.IGNORECASE,
)
_DECLINE_SHAPED_RE = re.compile(
    r"\b(not registering|change my mind|don'?t want to register|skip registration)\b",
    re.IGNORECASE,
)
_PERSONAL_STATUS_RE = re.compile(
    r"\b(i\s+(had\s+|have\s+)?(registered|enrolled|signed\s*up)|my\s+(course|enrollment|"
    r"enrolment|registration|status))\b",
    re.IGNORECASE,
)
_COURSE_TOPIC_RE = re.compile(r"\bcourse\b|\bthe other one\b|\bthat one\b|\bmentioned\b", re.IGNORECASE)
_ENQUIRY_MARKER_RE = re.compile(r"\b(how|what|which|tell|about|know)\b|\?", re.IGNORECASE)


def is_genuine_enquiry_continuation(msg: str) -> bool:
    """True if `msg` looks like a genuine enquiry-shaped continuation about
    a course — the shape MEMORY_CONTEXT_REDESIGN.md's worked examples are
    ("Ya, how about course 2", "How about the other one you mentioned") —
    with no enrollment/payment signal of its own and none of the
    legitimately-B-shaped exclusions above."""
    msg = msg or ""
    if _REG_FIELD_SHAPED_RE.search(msg):
        return False
    if _BARE_CONFIRM_RE.match(msg.strip()):
        return False
    if _BARE_POSITION_RE.match(msg.strip()):
        return False
    if _DECLINE_SHAPED_RE.search(msg):
        return False
    if _PERSONAL_STATUS_RE.search(msg):
        return False
    if router._has_explicit_enrollment_signal(msg):
        return False
    if router._looks_like_payment_intent(msg):
        return False
    if not _COURSE_TOPIC_RE.search(msg):
        return False
    if not _ENQUIRY_MARKER_RE.search(msg):
        return False
    return True


def routed_to_b(routing: str) -> bool:
    return routing.startswith("free text → Module B")


def find_misroutes(turns: list[Turn]) -> list[Turn]:
    return [t for t in turns if routed_to_b(t.routing) and is_genuine_enquiry_continuation(t.message)]


# ── Replay ───────────────────────────────────────────────────────────────
def _cleanup_all() -> None:
    database.execute("DELETE FROM chat_memory WHERE whatsapp_id LIKE %s", (f"{TEST_PREFIX}%",))
    database.execute("DELETE FROM conversation_state WHERE whatsapp_id LIKE %s", (f"{TEST_PREFIX}%",))
    database.execute("DELETE FROM customers WHERE whatsapp_id LIKE %s", (f"{TEST_PREFIX}%",))


def replay(case: Turn, all_turns: list[Turn], case_number: int) -> str:
    """Seed a fresh scratch whatsapp_id with the real preceding turns from
    this exact conversation (same log file, same original wid, strictly
    before this case), then call router.route() on the actual triggering
    message — exactly what orchestrator.handle_inbound() would do. Returns
    the resolved module ('A'/'B'/'C'/'DISAMBIGUATE')."""
    test_wid = f"{TEST_PREFIX}{case_number}@c.us"
    preceding = [t for t in all_turns if t.file == case.file and t.wid == case.wid and t.index < case.index]

    repo.get_or_create_customer(test_wid)
    for p in preceding:
        if p.message:
            repo.add_memory(test_wid, "user", p.message)
        if p.reply:
            repo.add_memory(test_wid, "assistant", p.reply)

    context.set_message({
        "whatsapp_id": test_wid, "phone": None, "display_name": None,
        "customer": repo.get_or_create_customer(test_wid),
        "body": case.message, "media": None,
    })
    decision = router.route({"message": {"body": case.message}, "media": None})
    return decision["module"]


def main() -> None:
    database.init_db()
    _cleanup_all()
    turns = load_all_turns()
    misroutes = find_misroutes(turns)
    print(f"Parsed {len(turns)} historical turns; {len(misroutes)} validated misroute case(s) to replay.\n")

    results = []
    try:
        for n, case in enumerate(misroutes, 1):
            _cleanup_all()
            module = replay(case, turns, n)
            correct = module in ("A", "DISAMBIGUATE")
            results.append((case, module, correct))
            print(f"[{n}/{len(misroutes)}] {'FIXED   ' if correct else 'still B '} "
                  f"module={module}  msg={case.message!r}")

        fixed = sum(1 for _, _, ok in results if ok)
        total = len(results) or 1
        print(f"\nRESULT: {fixed}/{len(results)} correctly avoid Module B now ({100*fixed/total:.0f}%).")
        for c, m, ok in results:
            if not ok:
                print(f"  STILL MISROUTING: [{c.file} #{c.index}] wid={c.wid} msg={c.message!r} -> {m}")
    finally:
        _cleanup_all()


if __name__ == "__main__":
    main()

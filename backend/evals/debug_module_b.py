"""One-off diagnostic: reproduces the module_b_enroll_happy_path failure with
CrewAI tool-call tracing turned on, to see exactly which tool(s) the agent
calls (and doesn't call) on the "intake 2" turn.

Does not modify any app/ file. Tracing is added by monkeypatching
crewai.Agent's defaults at runtime, in this throwaway script only - kickoff_agent
in app/crews/_base.py does `from crewai import Agent` at call time, so
reassigning crewai.Agent before invoking it is picked up transparently.

Approach:
  1. Register a synthetic identity via the sync endpoint (same as the eval
     harness) and replay turn 1 for real over HTTP, so the app's own
     orchestrator populates customers + chat_memory exactly as it would for
     a live user.
  2. Connect this process directly to the same Postgres database and call
     app.crews.module_b._run_agent() in-process for turn 2 ("intake 2"),
     with a wrapped, verbose, step-tracing crewai.Agent - to see the agent's
     tool calls without editing any tracked file.
"""
from __future__ import annotations

import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from client import SyncClient
from identity import new_identity

# ── Step 1: replay turns 1 over real HTTP, exactly like the harness ────────
chat_id, phone, name = new_identity("debug_module_b")
client = SyncClient()

reg_body = f"{name} | S1234567A | eval-{phone}@example.com"
print(">>> REGISTER:", client.send(chat_id, phone, name, reg_body))

turn1 = "I want to enroll in the infection control course"
reply1 = client.send(chat_id, phone, name, turn1)
print(f"\n>>> TURN 1 user: {turn1}")
print(f">>> TURN 1 assistant: {reply1}")
client.close()

# ── Step 2: monkeypatch crewai.Agent for verbose + tool-call tracing ───────
import crewai  # noqa: E402

_OriginalAgent = crewai.Agent


def _traced_step_callback(step_output):
    tool = getattr(step_output, "tool", None)
    tool_input = getattr(step_output, "tool_input", None)
    if tool is not None:
        print(f"    [TOOL CALL] {tool}  input={tool_input}")
    else:
        cls = type(step_output).__name__
        print(f"    [STEP] {cls}: {str(step_output)[:300]}")


class _TracedAgent(_OriginalAgent):
    def __init__(self, *args, **kwargs):
        kwargs["verbose"] = True
        kwargs["step_callback"] = _traced_step_callback
        super().__init__(*args, **kwargs)


crewai.Agent = _TracedAgent

# ── Step 3: connect this process to the same DB and call module_b directly ─
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import context, database, repositories as repo  # noqa: E402
from app.crews import module_b  # noqa: E402

database.init_db()
customer = repo.get_or_create_customer(chat_id, display_name=name)
print(f"\n>>> customer row after turn 1: registered={bool(customer.get('phone_confirmed'))} "
      f"name={customer.get('full_name')!r}")

context.set_message({
    "whatsapp_id": chat_id,
    "phone": phone,
    "display_name": customer.get("full_name"),
    "customer": customer,
    "body": "intake 2",
    "media": None,
})

print("\n>>> Recent memory seen by Module B before turn 2:")
for m in repo.recent_memory(chat_id, limit=8):
    print(f"    {m['role']}: {m['content'][:150]}")

print("\n>>> TURN 2 (traced) — sending 'intake 2' directly to module_b._run_agent()\n")
reply2 = module_b._run_agent("intake 2")
print("\n>>> TURN 2 assistant (traced):", reply2)

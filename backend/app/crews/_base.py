"""Helper to build and run a single-agent CrewAI crew.

Each module maps to the proposal's agentic pattern: a Reasoning Agent (decide)
with registered Tool nodes (act). This keeps that mapping in one place.
"""
from __future__ import annotations

import contextvars
import logging

from .. import query_log
from ..llm import get_crew_llm

log = logging.getLogger(__name__)

# Which agent role is "currently running", for the tool-call trace below.
# A ContextVar (not a plain module global) so concurrent requests never bleed
# into each other's trace output — same isolation pattern app/context.py
# already uses for per-request state.
_current_role: contextvars.ContextVar[str] = contextvars.ContextVar("current_agent_role", default="")

# Tool instances (List Courses, Course Schedule, etc.) are module-level
# singletons shared across every request and across modules (e.g. Module A
# and B both use the same List Courses object) — wrap each one's underlying
# function exactly once, ever, rather than re-wrapping on every kickoff.
_traced_tool_ids: set[int] = set()


def _wrap_tool_for_tracing(t) -> None:
    """Make one tool print a live trace line each time it actually runs.

    CrewAI's tool-call dispatch differs between its ReAct loop and its native
    OpenAI function-calling loop (the one actually used for gpt-4o-mini), and
    only the former surfaces intermediate steps via `step_callback` — so that
    hook silently misses every tool call under native function-calling. The
    one thing both paths always do is invoke the tool's `.func` field to get
    a result, so that's the reliable hook point instead.
    """
    if id(t) in _traced_tool_ids:
        return
    original_func = t.func

    def _traced(*args, **kwargs):
        role = _current_role.get()
        parts = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
        query_log.emit(f"│   ⚙ [{role}] {t.name}({', '.join(parts)})")
        result = original_func(*args, **kwargs)
        preview = repr(result)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        query_log.emit(f"│     → {preview}")
        return result

    t.func = _traced
    _traced_tool_ids.add(id(t))


_delegation_traced = False


def _wrap_delegation_tracing() -> None:
    """Make the "Delegate work to coworker" / "Ask question to coworker" tools
    (auto-injected by CrewAI onto any agent.allow_delegation=True agent that's
    in a multi-agent crew) print a trace line and flip _current_role for the
    duration of the coworker's own execution, so its tool calls underneath
    are labelled with its own role rather than the delegator's. Wrapped once,
    class-wide (these tools are constructed fresh per crew.kickoff(), but the
    class objects are the same), the same one-time-wrap pattern as
    _wrap_tool_for_tracing."""
    global _delegation_traced
    if _delegation_traced:
        return
    _delegation_traced = True

    from crewai.tools.agent_tools.ask_question_tool import AskQuestionTool
    from crewai.tools.agent_tools.delegate_work_tool import DelegateWorkTool

    for cls, label in ((DelegateWorkTool, "delegates to"), (AskQuestionTool, "asks")):
        original_run = cls._run

        # DelegateWorkTool's first arg is named "task", AskQuestionTool's is
        # "question" — accept *args/**kwargs generically rather than binding
        # a specific parameter name, so this works unchanged for both and
        # passes through to the original exactly as CrewAI called it.
        def _traced_run(self, *args, _orig=original_run, _label=label, **kwargs):
            role = _current_role.get()
            target = (
                kwargs.get("coworker") or kwargs.get("co_worker")
                or (args[2] if len(args) > 2 else None) or "?"
            )
            task_text = kwargs.get("task") or kwargs.get("question") or (args[0] if args else "")
            query_log.emit(f"│   ↪ [{role}] {_label} '{target}': {task_text}")
            coworker_token = _current_role.set(str(target))
            try:
                result = _orig(self, *args, **kwargs)
            finally:
                _current_role.reset(coworker_token)
            preview = repr(result)
            if len(preview) > 300:
                preview = preview[:300] + "…"
            query_log.emit(f"│     ↩ [{role}] received from '{target}': {preview}")
            return result

        cls._run = _traced_run


def build_agent(
    *,
    role: str,
    goal: str,
    backstory: str,
    tools: list,
    max_iter: int = 6,
    allow_delegation: bool = False,
):
    """Construct (but don't run) one Agent, with tool tracing wired up.
    Shared by kickoff_agent() below and by any module that needs to build an
    Agent to hand to another module as a delegation coworker (e.g. Module B
    exposes build_specialist_agent() for Module A to include in its own
    crew) — building is deliberately separate from running so the same Agent
    object can be reused either as a turn's own entry point or as someone
    else's coworker."""
    from crewai import Agent

    for t in tools:
        _wrap_tool_for_tracing(t)

    return Agent(
        role=role,
        goal=goal,
        backstory=backstory,
        tools=tools,
        llm=get_crew_llm(),
        verbose=False,
        allow_delegation=allow_delegation,
        max_iter=max_iter,
    )


def run_task(
    *,
    agent,
    task_description: str,
    expected_output: str,
    coworkers: list | None = None,
) -> str:
    """Run one task against `agent`. `coworkers`, if given, are placed in the
    same crew so `agent` can delegate to them via CrewAI's built-in
    "Delegate work to coworker" tool — only meaningful when
    agent.allow_delegation=True, which build_agent() controls per caller."""
    from crewai import Crew, Process, Task

    if coworkers:
        _wrap_delegation_tracing()

    token = _current_role.set(agent.role)
    try:
        task = Task(description=task_description, expected_output=expected_output, agent=agent)
        crew = Crew(
            agents=[agent, *(coworkers or [])],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )
        return str(crew.kickoff()).strip()
    finally:
        _current_role.reset(token)


def kickoff_agent(
    *,
    role: str,
    goal: str,
    backstory: str,
    tools: list,
    task_description: str,
    expected_output: str,
    max_iter: int = 6,
) -> str:
    """Single-agent, no-delegation entry point — unchanged behaviour for
    every existing caller (Router, Module C, the registration name-extraction
    agent). Composed from build_agent()/run_task() above."""
    agent = build_agent(role=role, goal=goal, backstory=backstory, tools=tools, max_iter=max_iter)
    return run_task(agent=agent, task_description=task_description, expected_output=expected_output)

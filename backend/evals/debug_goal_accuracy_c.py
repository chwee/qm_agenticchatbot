"""Prints the intermediate AgentGoalAccuracyWithReference judge outputs for
the actual transcript captured in the module_c_balance_after_enroll
scenario's latest run (taken verbatim from reports/report.md), so the score
can be explained from real judge output rather than guessing.

AgentGoalAccuracyWithReference (ragas/metrics/_goal_accuracy.py) is only two
LLM calls:
  1. InferGoalOutcomePrompt: read the whole conversation, infer user_goal +
     end_state (the workflow's own idea of what happened).
  2. CompareOutcomePrompt: compare end_state (arrived_outcome) against the
     scenario's `reference` (desired_outcome) -> binary verdict.
"""
from __future__ import annotations

import asyncio
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

from ragas.dataset_schema import MultiTurnSample
from ragas.messages import AIMessage, HumanMessage
from ragas.metrics import AgentGoalAccuracyWithReference
from ragas.metrics._goal_accuracy import CompareOutcomeInput, WorkflowInput

from metrics import build_evaluator_llm

REFERENCE = (
    "The participant is enrolled in the \"CPR & Basic Life Support (BLS) for "
    "Dental Clinics\" course, and the final reply states the actual "
    "outstanding balance owed for that enrollment (itemised by SkillsFuture "
    "claim and/or PayNow, or the exact net payable amount), without "
    "inventing a figure."
)


async def main() -> None:
    llm = build_evaluator_llm("gpt-4o-mini")
    scorer = AgentGoalAccuracyWithReference(llm=llm)

    messages = [
        HumanMessage(content="I want to enroll in the CPR and basic life support course"),
        AIMessage(content=(
            "Great choice! The upcoming intakes for the CPR & Basic Life Support "
            "(BLS) for Dental Clinics course are:\n"
            "1. [SH2609] 28 Jul 2026 (20 seats available)\n"
            "2. [SH2610] 25 Aug 2026 (20 seats available)\n"
            "Please let me know which intake you'd like to enroll in!"
        )),
        HumanMessage(content="intake 1"),
        AIMessage(content=(
            "You're all set! You've been successfully enrolled in the CPR & Basic "
            "Life Support (BLS) for Dental Clinics course.\n"
            "Course Details:\n"
            "- Date: 28 Jul 2026\n"
            "- Name: EvalBot-module_c_balance_after_enroll\n"
            "- NRIC: S1234567A\n"
            "- Fee: S$250.00 (SkillsFuture credit up to S$200.00 - net payable S$50.00)\n"
            "An invoice (INV-2026-0896) has been sent to your email at "
            "eval-98640378@example.com. When you're ready to pay, just send /pay "
            "with your PayNow screenshot attached.\n"
            "If you have any more questions, feel free to ask!"
        )),
        HumanMessage(content="how much do I still need to pay?"),
        AIMessage(content=(
            "You still need to pay the following for invoice INV-2026-0896:\n"
            "- SkillsFuture claim: S$200.00\n"
            "- PayNow transfer: S$50.00\n"
            "Please send /pay with the corresponding screenshot(s) attached to "
            "settle the payment. If you need a receipt, just let me know!"
        )),
    ]
    sample = MultiTurnSample(user_input=messages, reference=REFERENCE)
    workflow = sample.pretty_repr()

    print("STEP 1 - InferGoalOutcomePrompt (reads the whole conversation):")
    step1 = await scorer.workflow_prompt.generate(data=WorkflowInput(workflow=workflow), llm=llm)
    print(f"    user_goal: {step1.user_goal!r}")
    print(f"    end_state: {step1.end_state!r}")

    print("\nSTEP 2 - CompareOutcomePrompt:")
    print(f"    desired_outcome (= scenario's `reference`): {REFERENCE!r}")
    print(f"    arrived_outcome (= inferred end_state):     {step1.end_state!r}")
    step2 = await scorer.compare_outcome_prompt.generate(
        data=CompareOutcomeInput(desired_outcome=REFERENCE, arrived_outcome=step1.end_state),
        llm=llm,
    )
    print(f"    verdict: {step2.verdict}   reason: {step2.reason}")

    final = await scorer.multi_turn_ascore(sample)
    print(f"\nscorer.multi_turn_ascore() = {final}")


if __name__ == "__main__":
    asyncio.run(main())

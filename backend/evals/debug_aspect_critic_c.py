"""Isolated replay: runs the known transcript from module_c_balance_after_enroll
(the one that scored 0.000 under AgentGoalAccuracyWithReference despite
looking correct) through the new AspectCritic definition, to confirm the
fix before trusting a full live pipeline re-run.
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
from ragas.metrics import AspectCritic

from metrics import build_evaluator_llm

DEFINITION = (
    "Considering the full conversation: was the participant successfully "
    "enrolled in the CPR & Basic Life Support course, AND did the "
    "assistant's final reply (responding to \"how much do I still need to "
    "pay?\") state a real, itemised outstanding balance for that specific "
    "enrollment - referencing the actual invoice number and correct "
    "SkillsFuture/PayNow amounts consistent with the enrollment "
    "confirmation shown earlier - rather than inventing a figure, giving a "
    "generic non-answer, or declining to answer? Answer \"Yes\" (1) only if "
    "both the enrollment succeeded AND the balance was correctly and "
    "specifically reported; answer \"No\" (0) otherwise."
)


async def main() -> None:
    llm = build_evaluator_llm("gpt-4o-mini")
    scorer = AspectCritic(name="balance_after_enroll", definition=DEFINITION, llm=llm, strictness=3)

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
    sample = MultiTurnSample(user_input=messages)
    score = await scorer.multi_turn_ascore(sample)
    print(f"AspectCritic score on known-correct transcript = {score}")


if __name__ == "__main__":
    asyncio.run(main())

"""Isolated replay: runs the exact transcript that showed unstable
TopicAdherenceScore results (1.000 / 0.500 / 0.4286 on identical input)
through the new AspectCritic definition for module_a_ontopic_fees, repeated
several times, to confirm stability before trusting a live pipeline re-run.
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
    "Considering the full conversation: did the assistant (1) list Q&M's "
    "dental training courses when asked what courses are offered, (2) state "
    "a specific fee for the Infection Control for Dental Clinics course "
    "when asked, and (3) answer whether SkillsFuture credit applies to that "
    "course when asked - keeping the course name and fee figures it stated "
    "consistent across all three replies (never contradicting itself), and "
    "staying within Q&M dental training subject matter throughout (courses, "
    "fees, SkillsFuture funding including any eligibility variations it "
    "chooses to mention such as Mid-Career Credit, schedules, enrollment, "
    "payments)? Do NOT second-guess whether a stated dollar figure is the "
    "\"correct\" real-world amount - you have no independent source for "
    "that; only check whether the assistant stayed on-topic and was "
    "internally consistent. Answer \"Yes\" (1) if all three questions were "
    "addressed, the figures given were consistent across turns, and every "
    "reply stayed within Q&M dental training subject matter; answer \"No\" "
    "(0) only if it drifted into a genuinely unrelated subject (unrelated "
    "to dental training, courses, funding, enrollment, or payments) or "
    "contradicted itself with different figures for the same course."
)

MESSAGES = [
    HumanMessage(content="What courses do you offer?"),
    AIMessage(content=(
        "We offer several courses at Q&M Dental Group:\n"
        "1. 2-Day Basic Certificate in Dental Assisting - S$600.00\n"
        "2. Infection Control for Dental Clinics - S$500.00\n"
        "3. Advanced Dental Radiography & X-Ray Techniques - S$950.00\n"
        "4. Dental Reception & Patient Communication Workshop - S$350.00\n"
        "5. CPR & Basic Life Support (BLS) for Dental Clinics - S$250.00\n"
        "6. Orthodontic Chairside Assisting Certificate - S$1,200.00\n"
        "If you're interested in any specific course, let me know, and I can "
        "provide more details on fees or upcoming intakes!"
    )),
    HumanMessage(content="What's the fee for the infection control course?"),
    AIMessage(content=(
        "The fee for the Infection Control for Dental Clinics course is S$500.00. "
        "The great news is that it's fully claimable through SkillsFuture, so your "
        "net payable amount would be S$0! If you're aged 40 and above, you might "
        "also be eligible for additional Mid-Career SkillsFuture Credit.\n"
        "If you're interested in enrolling or want to know about the upcoming "
        "intake dates, just let me know!"
    )),
    HumanMessage(content="Can I use SkillsFuture credit for that?"),
    AIMessage(content=(
        "Yes, you can definitely use SkillsFuture Credit for the Infection Control "
        "for Dental Clinics course! The full fee is S$500.00, and it's fully "
        "claimable through SkillsFuture, meaning your net payable amount would be "
        "S$0.\n"
        "If you're interested in enrolling or want to know about the upcoming "
        "intake dates, just let me know!"
    )),
]


async def main() -> None:
    llm = build_evaluator_llm("gpt-4o-mini")
    scorer = AspectCritic(name="ontopic_fees", definition=DEFINITION, llm=llm, strictness=3)
    sample = MultiTurnSample(user_input=MESSAGES)

    for i in range(1, 6):
        score = await scorer.multi_turn_ascore(sample)
        print(f"run {i}: score = {score}")


if __name__ == "__main__":
    asyncio.run(main())

"""Prints the intermediate TopicAdherenceScore judge outputs for the actual
transcript captured in the module_a_ontopic_fees scenario's latest run (taken
verbatim from reports/report.md), so the score can be explained from real
judge output rather than describing the algorithm in the abstract.
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
from ragas.metrics import TopicAdherenceScore
from ragas.metrics._topic_adherence import (
    TopicClassificationInput,
    TopicExtractionInput,
    TopicRefusedInput,
)

from metrics import build_evaluator_llm

ALLOWED_TOPICS = [
    "Q&M dental assisting courses offered, their names and codes, and any "
    "specific named course (e.g. Infection Control for Dental Clinics, 2-Day "
    "Basic Certificate in Dental Assisting) discussed by name",
    "course fees and SkillsFuture subsidy amounts, for the catalogue in "
    "general or for any one specific named course",
    "SkillsFuture Credit eligibility and claim process, including age-based "
    "or Mid-Career Credit eligibility",
    "course intake schedules and dates",
    "enrollment process and required details",
    "payment and receipts",
]


async def main() -> None:
    llm = build_evaluator_llm("gpt-4o-mini")
    scorer = TopicAdherenceScore(llm=llm, mode="precision")

    messages = [
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
    sample = MultiTurnSample(user_input=messages, reference_topics=ALLOWED_TOPICS)
    user_input = sample.pretty_repr()

    extraction = await scorer.topic_extraction_prompt.generate(
        data=TopicExtractionInput(user_input=user_input), llm=llm
    )
    topics = extraction.topics
    print("STEP 1 - extracted topics (from Human turns):")
    for t in topics:
        print(f"    - {t!r}")

    refused_flags = []
    print("\nSTEP 2 - refused_to_answer per topic:")
    for topic in topics:
        r = await scorer.topic_refused_prompt.generate(
            data=TopicRefusedInput(user_input=user_input, topic=topic), llm=llm
        )
        refused_flags.append(r.refused_to_answer)
        print(f"    refused_to_answer({topic!r}) = {r.refused_to_answer}")

    classification = await scorer.topic_classification_prompt.generate(
        data=TopicClassificationInput(reference_topics=ALLOWED_TOPICS, topics=topics), llm=llm
    )
    print("\nSTEP 3 - in allowed_topics (reference_topics) classification:")
    for t, c in zip(topics, classification.classifications):
        print(f"    {t!r} -> in_scope={c}")

    answered = [not f for f in refused_flags]
    in_scope = classification.classifications
    tp = sum(a and s for a, s in zip(answered, in_scope))
    fp = sum(a and not s for a, s in zip(answered, in_scope))
    fn = sum((not a) and s for a, s in zip(answered, in_scope))
    precision = tp / (tp + fp + 1e-10)

    print(f"\nanswered={answered}")
    print(f"in_scope={in_scope}")
    print(f"true_positives={tp} false_positives={fp} false_negatives={fn}")
    print(f"precision = TP/(TP+FP) = {tp}/({tp}+{fp}) = {precision:.4f}")


if __name__ == "__main__":
    asyncio.run(main())

"""One-off debug script: prints the intermediate LLM-judge outputs inside
TopicAdherenceScore for the module_a_offtopic_should_refuse scenario, so we
can see exactly what topics were extracted and how each was classified -
instead of guessing from the final float.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from ragas.dataset_schema import MultiTurnSample
from ragas.messages import AIMessage, HumanMessage
from ragas.metrics._topic_adherence import (
    TopicClassificationInput,
    TopicExtractionInput,
    TopicRefusedInput,
)

from metrics import build_evaluator_llm
from metrics import score as _unused  # noqa: F401  (import kept minimal on purpose)
from ragas.metrics import TopicAdherenceScore

ALLOWED_TOPICS = [
    "Q&M dental assisting courses offered, their names and codes",
    "course fees and SkillsFuture subsidy amounts",
    "SkillsFuture Credit eligibility and claim process",
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
            "We offer several courses at Q&M Dental Group! Here's a quick overview:\n"
            "1. 2-Day Basic Certificate in Dental Assisting - S$600.00\n"
            "2. Infection Control for Dental Clinics - S$500.00\n"
            "3. Advanced Dental Radiography & X-Ray Techniques - S$950.00\n"
        )),
        HumanMessage(content="Can you write me a Python script that scrapes a website for me?"),
        AIMessage(content=(
            "I'm really sorry, but I can't help with writing Python scripts or scraping "
            "websites. However, if you're interested in any of our dental courses, I can "
            "provide more details about them! Just let me know."
        )),
    ]
    sample = MultiTurnSample(user_input=messages, reference_topics=ALLOWED_TOPICS)
    user_input = sample.pretty_repr()

    print("=" * 70)
    print("pretty_repr() fed to the judge:")
    print("=" * 70)
    print(user_input)
    print()

    extraction = await scorer.topic_extraction_prompt.generate(
        data=TopicExtractionInput(user_input=user_input), llm=llm
    )
    topics = extraction.topics
    print("=" * 70)
    print("STEP 1 - extracted topics:", topics)
    print("=" * 70)

    refused_flags = []
    for topic in topics:
        r = await scorer.topic_refused_prompt.generate(
            data=TopicRefusedInput(user_input=user_input, topic=topic), llm=llm
        )
        refused_flags.append(r.refused_to_answer)
        print(f"  refused_to_answer({topic!r}) = {r.refused_to_answer}")

    classification = await scorer.topic_classification_prompt.generate(
        data=TopicClassificationInput(reference_topics=ALLOWED_TOPICS, topics=topics), llm=llm
    )
    print()
    print("STEP 2 - in_reference_topics classification:", classification.classifications)

    answered = [not f for f in refused_flags]
    in_scope = classification.classifications
    tp = sum(a and s for a, s in zip(answered, in_scope))
    fp = sum(a and not s for a, s in zip(answered, in_scope))
    fn = sum((not a) and s for a, s in zip(answered, in_scope))
    print()
    print(f"answered={answered} in_scope={in_scope}")
    print(f"true_positives={tp} false_positives={fp} false_negatives={fn}")
    precision = tp / (tp + fp + 1e-10)
    print(f"precision = {precision:.4f}")

    final = await scorer.multi_turn_ascore(sample)
    print()
    print(f"scorer.multi_turn_ascore() = {final:.4f}")


if __name__ == "__main__":
    asyncio.run(main())

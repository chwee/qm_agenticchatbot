"""Ragas scorer configuration.

One evaluator (judge) LLM is shared by every metric. This is a separate LLM
call from the app's own OPENAI_MODEL - it only reads transcripts already
produced by the running backend and never talks to app/ code directly.
"""
from __future__ import annotations

import asyncio

from langchain_openai import ChatOpenAI
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import AgentGoalAccuracyWithReference, AspectCritic, TopicAdherenceScore


def build_evaluator_llm(model: str = "gpt-4o-mini"):
    return LangchainLLMWrapper(ChatOpenAI(model=model, temperature=0))


def score(scenario_result, evaluator_llm) -> float:
    scenario = scenario_result.scenario
    metric = scenario["metric"]
    if metric == "topic_adherence":
        scorer = TopicAdherenceScore(llm=evaluator_llm, mode="precision")
    elif metric == "goal_accuracy":
        scorer = AgentGoalAccuracyWithReference(llm=evaluator_llm)
    elif metric == "aspect_critic":
        scorer = AspectCritic(
            name=scenario["id"],
            definition=scenario["definition"],
            llm=evaluator_llm,
            strictness=scenario.get("strictness", 3),
        )
    else:
        raise ValueError(f"Unknown metric type: {metric!r}")
    return asyncio.run(scorer.multi_turn_ascore(scenario_result.sample))

"""Drives one scripted scenario against the live backend and assembles a
Ragas MultiTurnSample from the resulting transcript.

Talks to the backend only over HTTP via client.SyncClient - no imports from
app/, no direct database access, no mutation of anything the app itself
owns beyond the ordinary customer/enrollment rows a real conversation would
also create.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ragas.dataset_schema import MultiTurnSample
from ragas.messages import AIMessage, HumanMessage

from client import SyncClient
from identity import new_identity

log = logging.getLogger("evals.runner")


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class ScenarioResult:
    scenario: dict
    transcript: list[Turn] = field(default_factory=list)
    sample: MultiTurnSample | None = None


def run_scenario(client: SyncClient, scenario: dict) -> ScenarioResult:
    chat_id, phone, name = new_identity(scenario["id"])
    result = ScenarioResult(scenario=scenario)

    # Registration turn - satisfies the app's registration gate, not scored.
    # Phone arrives pre-confirmed via the "from.phone" field (like a real
    # @c.us gateway payload), so the 3-field shortcut applies.
    reg_body = f"{name} | S1234567A | eval-{phone}@example.com"
    client.send(chat_id, phone, name, reg_body)

    for turn_text in scenario["turns"]:
        reply = client.send(chat_id, phone, name, turn_text)
        result.transcript.append(Turn("user", turn_text))
        result.transcript.append(Turn("assistant", reply))
        log.info("  [%s] user: %s", scenario["id"], turn_text)
        log.info("  [%s] assistant: %s", scenario["id"], reply[:200])

    messages = [
        HumanMessage(content=t.content) if t.role == "user" else AIMessage(content=t.content)
        for t in result.transcript
    ]

    metric = scenario["metric"]
    if metric == "topic_adherence":
        result.sample = MultiTurnSample(user_input=messages, reference_topics=scenario["allowed_topics"])
    elif metric == "goal_accuracy":
        result.sample = MultiTurnSample(user_input=messages, reference=scenario["reference"])
    elif metric == "aspect_critic":
        result.sample = MultiTurnSample(user_input=messages)
    else:
        raise ValueError(f"Unknown metric type in scenario {scenario['id']!r}: {metric!r}")

    return result

"""LLM factories — one CrewAI LLM for the agents, one raw OpenAI client for vision.

The proposal's AI reasoning layer (Section 5.6) is OpenAI here: a text model for
the conversational/validation/router agents and a vision-capable model for
Module C payment-screenshot analysis.
"""
from __future__ import annotations

import os
from functools import lru_cache

from .config import settings

# litellm / CrewAI and the OpenAI SDK both read OPENAI_API_KEY from the env.
if settings.openai_api_key:
    os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)


# Both LLM clients otherwise fall back to their library defaults, which are
# minutes long (OpenAI SDK: 600s; LiteLLM, behind crewai.LLM: similarly
# unbounded) — a stalled request would silently hang a whole WhatsApp turn
# instead of failing fast.
_LLM_TIMEOUT_SECONDS = 30


@lru_cache(maxsize=1)
def get_crew_llm():
    """CrewAI LLM used by the Router, Module A and Module B agents."""
    from crewai import LLM

    return LLM(
        model=f"openai/{settings.openai_model}",
        api_key=settings.openai_api_key or None,
        temperature=settings.openai_temperature,
        timeout=_LLM_TIMEOUT_SECONDS,
    )


@lru_cache(maxsize=1)
def get_openai_client():
    """Raw OpenAI client for Module C vision verification."""
    from openai import OpenAI

    return OpenAI(
        api_key=settings.openai_api_key or None,
        timeout=_LLM_TIMEOUT_SECONDS,
        max_retries=1,
    )

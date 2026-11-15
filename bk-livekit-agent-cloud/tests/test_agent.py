"""Unit tests for agent wiring that don't need a live session or network.

Conversation-level behavior (ordering, guardrails) is covered by the
simulations in scenarios.yaml, which run against LiveKit Cloud.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from livekit.agents import mcp

import agent as agent_module
from agent import BKAgent, build_llm


class _FakeRunContext:
    speech_handle = None


def _agent() -> BKAgent:
    return BKAgent(
        room=object(),  # type: ignore[arg-type]
        mcp_server=mcp.MCPServerHTTP("http://127.0.0.1:1/mcp"),
    )


async def _place(agent: BKAgent) -> dict:
    return await agent.place_order.__wrapped__(  # type: ignore[attr-defined]
        agent, _FakeRunContext(), [{"name": "Coke", "quantity": 1, "price": 79.0}], 79.0
    )


def test_llm_chain_without_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    chain = build_llm()
    assert len(chain._llm_instances) == 1


def test_llm_chain_adds_groq_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("GROQ_API_KEY", "test")
    chain = build_llm()
    assert len(chain._llm_instances) == 2


def test_primary_model_not_repeated_in_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setattr(
        agent_module,
        "OPENROUTER_FALLBACK_MODELS",
        ["mistralai/mistral-nemo", "openai/gpt-oss-120b"],
    )
    primary = build_llm()._llm_instances[0]
    # OpenRouter routing list is [primary, *fallbacks]; primary must appear once.
    assert primary._opts.extra_body["models"] == [
        "openai/gpt-oss-120b",
        "mistralai/mistral-nemo",
    ]


async def test_place_order_returns_spoken_id() -> None:
    agent = _agent()
    result = await _place(agent)
    order_id = result["order_id"]
    assert result["status"] == "confirmed"
    assert len(order_id) == 8
    # ID is spelled out character by character for TTS.
    assert ", ".join(order_id) in result["say_to_customer"]


async def test_place_order_only_once() -> None:
    agent = _agent()
    await _place(agent)
    second = await _place(agent)
    assert second["status"] == "already_confirmed"


async def test_detects_llm_spoke_confirmation() -> None:
    agent = _agent()
    order_id = (await _place(agent))["order_id"]
    agent.note_assistant_message("Thanks, that's all.")
    assert not agent._goodbye_spoken
    spaced = " - ".join(order_id.lower())
    agent.note_assistant_message(f"Confirmed! Your ID is {spaced}.")
    assert agent._goodbye_spoken

"""The receipt must only reach the frontend AFTER the confirmation has played."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from livekit.agents import mcp

from agent import BKAgent


class FakeHandle:
    def __init__(self, log: list[str], name: str, delay: float = 0.05) -> None:
        self._log, self._name, self._delay = log, name, delay

    async def wait_for_playout(self) -> None:
        await asyncio.sleep(self._delay)
        self._log.append(f"played:{self._name}")


class FakeSession:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def say(self, text: str, **_kw) -> FakeHandle:
        self.log.append(f"say:{text}")
        return FakeHandle(self.log, "say")


class FakeParticipant:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def send_text(self, payload: str, topic: str) -> None:
        self.log.append(f"receipt:{topic}")
        self.payload = json.loads(payload)


class FakeRoom:
    def __init__(self, log: list[str]) -> None:
        self.local_participant = FakeParticipant(log)


class FakeRunContext:
    def __init__(self, handle: FakeHandle) -> None:
        self.speech_handle = handle


def _make(log: list[str]) -> tuple[BKAgent, FakeRoom]:
    room = FakeRoom(log)
    agent = BKAgent(room=room, mcp_server=mcp.MCPServerHTTP("http://127.0.0.1:1/mcp"))
    return agent, room


async def _place(agent: BKAgent, log: list[str]) -> dict:
    return await agent.place_order.__wrapped__(  # type: ignore[attr-defined]
        agent,
        FakeRunContext(FakeHandle(log, "turn", delay=0.2)),
        [{"name": "Whopper", "quantity": 1, "price": 5.99}],
        5.99,
    )


@pytest.mark.asyncio
async def test_receipt_not_sent_by_tool_itself() -> None:
    log: list[str] = []
    agent, _ = _make(log)
    result = await _place(agent, log)

    assert result["status"] == "confirmed"
    assert result["order_id"] in result["say_to_customer"].replace(", ", "")
    assert agent.order_placed
    assert not any(e.startswith("receipt") for e in log)


@pytest.mark.asyncio
async def test_receipt_published_after_llm_confirmation_played() -> None:
    log: list[str] = []
    agent, room = _make(log)
    result = await _place(agent, log)
    agent.note_assistant_message(result["say_to_customer"])  # LLM said the line

    await agent.finish_order(FakeSession(log))

    assert log.index("played:turn") < log.index("receipt:bk-order-receipt")
    assert not any(e.startswith("say:") for e in log)  # no duplicate line
    assert room.local_participant.payload["order_id"] == result["order_id"]


@pytest.mark.asyncio
async def test_fallback_speaks_confirmation_when_llm_stays_silent() -> None:
    log: list[str] = []
    agent, _ = _make(log)
    result = await _place(agent, log)

    await agent.finish_order(FakeSession(log))

    say_i = next(i for i, e in enumerate(log) if e.startswith("say:"))
    assert result["say_to_customer"] in log[say_i]
    assert log.index("played:say") < log.index("receipt:bk-order-receipt")

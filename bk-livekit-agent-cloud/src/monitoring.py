"""
Provider health monitoring for the BK voice agent.

Why this exists: in production each provider (AssemblyAI, OpenRouter, Cartesia,
Deepgram) has exactly one API key. When one of them runs out of credits or gets
rate-limited, LiveKit retries, then gives up — and from the caller's side the
agent just "freezes". This module turns those silent failures into loud,
greppable log lines (and optionally a webhook ping), so the cause of every
freeze is visible in `lk agent logs`.

Every alert line starts with `PROVIDER_ALERT` so you can filter with:
    lk agent logs | grep PROVIDER_ALERT
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any

import aiohttp
from livekit.agents import (
    AgentSession,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    metrics,
)
from loguru import logger

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL")  # Slack / Discord / any JSON hook
SLOW_LLM_TTFT_S = float(os.getenv("SLOW_LLM_TTFT_S", "3.0"))
SLOW_TTS_TTFB_S = float(os.getenv("SLOW_TTS_TTFB_S", "1.5"))


def classify_error(exc: BaseException | None) -> str:
    """Map a provider exception to a short, actionable category."""
    if exc is None:
        return "unknown"
    # Errors are often wrapped; walk the cause chain to find the real one.
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        status = getattr(cur, "status_code", None)
        if isinstance(cur, APIStatusError) or isinstance(status, int):
            if status in (401, 403):
                return f"auth_failed({status})"  # key revoked / wrong key
            if status == 402:
                return "credits_exhausted(402)"  # out of money / quota
            if status == 429:
                return "rate_limited(429)"  # too many requests / concurrency cap
            if isinstance(status, int) and status >= 500:
                return f"provider_down({status})"
            if isinstance(status, int):
                return f"http_{status}"
        if isinstance(cur, APITimeoutError | asyncio.TimeoutError):
            return "timeout"
        if isinstance(cur, APIConnectionError):
            return "connection_error"
        cur = cur.__cause__ or cur.__context__
    text = str(exc).lower()
    for needle, kind in (
        ("insufficient", "credits_exhausted"),
        ("quota", "credits_exhausted"),
        ("credit", "credits_exhausted"),
        ("rate limit", "rate_limited"),
        ("too many", "rate_limited"),
        ("unauthorized", "auth_failed"),
    ):
        if needle in text:
            return kind
    return type(exc).__name__


def _source_label(source: Any) -> str:
    provider = getattr(source, "provider", None) or type(source).__module__
    model = getattr(source, "model", None) or getattr(source, "label", "")
    return f"{provider}/{model}"


class ProviderMonitor:
    """Attaches to an AgentSession and reports provider failures."""

    def __init__(self, room_name: str) -> None:
        self._room = room_name
        self._alerted: set[tuple[str, str]] = set()  # dedupe per session
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = time.monotonic()

    # ---- alerting -------------------------------------------------------
    def alert(self, provider: str, kind: str, detail: str, *, critical: bool) -> None:
        line = (
            f"PROVIDER_ALERT room={self._room} provider={provider} "
            f"kind={kind} critical={critical} detail={detail[:300]}"
        )
        (logger.error if critical else logger.warning)(line)

        key = (provider, kind)
        if ALERT_WEBHOOK_URL and key not in self._alerted:
            self._alerted.add(key)
            task = asyncio.create_task(self._post_webhook(line))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _post_webhook(self, text: str) -> None:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                # "text" works for Slack, "content" for Discord.
                await http.post(ALERT_WEBHOOK_URL, json={"text": text, "content": text})
        except Exception as e:  # never let alerting break the call
            logger.warning(f"alert webhook failed: {e}")

    # ---- session wiring -------------------------------------------------
    def attach(self, session: AgentSession, *fallback_adapters: Any) -> None:
        @session.on("error")
        def _on_error(ev) -> None:
            err = getattr(ev.error, "error", ev.error)
            recoverable = getattr(ev.error, "recoverable", False)
            self.alert(
                _source_label(ev.source),
                classify_error(err),
                repr(err),
                critical=not recoverable,
            )

        @session.on("close")
        def _on_close(ev) -> None:
            secs = time.monotonic() - self._started
            if ev.error is not None:
                self.alert(
                    "session",
                    f"closed_by_error:{classify_error(ev.error.error)}",
                    repr(ev.error.error),
                    critical=True,
                )
            logger.info(f"session closed reason={ev.reason} after={secs:.0f}s")
            with contextlib.suppress(Exception):
                logger.info(f"session usage: {session.usage}")

        @session.on("metrics_collected")
        def _on_metrics(ev) -> None:
            m = ev.metrics
            if isinstance(m, metrics.LLMMetrics) and m.ttft > SLOW_LLM_TTFT_S:
                self.alert(
                    "llm", "slow_ttft", f"{m.label} ttft={m.ttft:.2f}s", critical=False
                )
            elif isinstance(m, metrics.TTSMetrics) and m.ttfb > SLOW_TTS_TTFB_S:
                self.alert(
                    "tts", "slow_ttfb", f"{m.label} ttfb={m.ttfb:.2f}s", critical=False
                )

        # Fallback adapters tell us when a provider is taken out of rotation —
        # this is the earliest signal that a key is exhausted.
        for adapter in fallback_adapters:
            for event in (
                "llm_availability_changed",
                "stt_availability_changed",
                "tts_availability_changed",
            ):
                with contextlib.suppress(Exception):
                    adapter.on(event, self._make_availability_handler())

    def _make_availability_handler(self):
        def _handler(ev) -> None:
            inst = (
                getattr(ev, "llm", None)
                or getattr(ev, "stt", None)
                or getattr(ev, "tts", None)
            )
            if ev.available:
                logger.info(f"provider recovered: {_source_label(inst)}")
            else:
                self.alert(
                    _source_label(inst),
                    "marked_unavailable",
                    "fell back to next provider",
                    critical=False,
                )

        return _handler


async def log_openrouter_credit(monitor: ProviderMonitor) -> None:
    """One cheap GET per session: shows remaining OpenRouter credit / limits.

    This directly tests the 'API exhaustion' hypothesis for the LLM.
    """
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with (
            aiohttp.ClientSession(timeout=timeout) as http,
            http.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {key}"},
            ) as resp,
        ):
            if resp.status != 200:
                monitor.alert(
                    "openrouter",
                    f"key_check_http_{resp.status}",
                    await resp.text(),
                    critical=True,
                )
                return
            data = (await resp.json()).get("data", {})
        remaining = data.get("limit_remaining")
        logger.info(
            f"openrouter key: usage={data.get('usage')} limit={data.get('limit')} "
            f"remaining={remaining} free_tier={data.get('is_free_tier')}"
        )
        if data.get("is_free_tier"):
            monitor.alert(
                "openrouter",
                "free_tier_key",
                "free-tier keys have tight daily/minute caps on :free models",
                critical=False,
            )
        if remaining is not None and remaining < 1:
            monitor.alert(
                "openrouter",
                "credits_low",
                f"limit_remaining={remaining}",
                critical=True,
            )
    except Exception as e:
        logger.warning(f"openrouter credit check failed: {e}")

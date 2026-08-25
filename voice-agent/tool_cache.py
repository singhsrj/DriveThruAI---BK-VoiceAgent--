"""
LangCache-backed caching for MCP tool calls.

Why this exists
────────────────
Our BK menu MCP tools are pure functions of their arguments — the menu
doesn't change per customer or per session, so the same tool called with
the same arguments always returns the same result. Without caching this
causes two real problems:

1. Latency: every tool call is a full MCP round trip (SSE → MongoDB query
   → back), even when we already know the answer.
2. Context bloat: if the LLM re-calls a tool it already called earlier in
   the conversation, the same JSON blob gets appended to the context
   again, growing every subsequent prompt (and the tokens billed for it).

This wraps every registered MCP tool handler so that, before running the
real tool, it checks LangCache for a previous call with the same
(tool_name, arguments). On a hit, it returns the cached result immediately
— no MCP round trip, no DB query. On a miss, it runs the real tool once,
then stores the result for next time. Because results are
customer-independent, the cache is shared across ALL sessions, not just
the current call — so if one customer asks "what's vegetarian", the next
customer asking the same thing gets a cached answer too.

What this does NOT fix: if the LLM insists on re-calling a tool within
the SAME conversation despite already having the answer in context, this
makes that call fast, but the result still gets appended to context again.
That side is addressed separately with a system-prompt instruction not to
re-call tools for information already retrieved this conversation.

Only wrap MCP clients whose tools are pure functions of their arguments
(like the BK menu server). Don't wrap tools with side effects or that
return time-varying data (e.g. a memory server's recall_facts, which
should reflect newly added facts, not a stale cached answer).
"""

import json
import logging
from typing import Any

from langcache import LangCache
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

logger = logging.getLogger("tool_cache")

# High threshold: our cache keys are structured strings (tool name + a
# canonical dump of arguments), not natural language, so we want
# near-exact matches only. A looser threshold risks conflating two
# different tool calls that happen to be textually similar.
DEFAULT_SIMILARITY_THRESHOLD = 0.97


def _cache_key(tool_name: str, arguments: dict[str, Any]) -> str:
    """Canonical, order-independent cache key for a tool call."""
    normalized_args = json.dumps(arguments, sort_keys=True, default=str)
    return f"mcp_tool::{tool_name}::{normalized_args}"


class CachedMCPTools:
    """Wraps an MCP client's tool schemas so their handlers check/populate LangCache."""

    def __init__(self, lang_cache: LangCache, ttl_millis: int | None = None):
        self._lang_cache = lang_cache
        self._ttl_millis = ttl_millis
        self.hits = 0
        self.misses = 0

    async def _get_cached(self, key: str) -> str | None:
        try:
            result = await self._lang_cache.search_async(
                prompt=key,
                similarity_threshold=DEFAULT_SIMILARITY_THRESHOLD,
                max_results=1,
            )
        except Exception as e:
            # A cache failure should never break the conversation — treat
            # it as a miss and fall through to the real tool call.
            logger.warning(f"LangCache search failed, treating as miss: {e}")
            return None

        if result.data:
            return result.data[0].response
        return None

    async def _set_cached(self, key: str, value: str) -> None:
        try:
            await self._lang_cache.set_async(
                prompt=key,
                response=value,
                ttl_millis=self._ttl_millis,
            )
        except Exception as e:
            logger.warning(f"LangCache set failed (non-fatal): {e}")

    def wrap(self, tools_schema: ToolsSchema, mcp_client) -> ToolsSchema:
        """Return a new ToolsSchema whose handlers check the cache before
        falling through to the given MCP client's real tool execution.

        Args:
            tools_schema: schema from `await mcp_client.get_tools_schema()`
                (no handlers set yet — that's what we're adding here).
            mcp_client: the connected MCPClient (must already be started,
                e.g. inside `async with MCPClient(...) as mcp_client:`).
        """
        wrapped_tools = [
            FunctionSchema(
                name=schema.name,
                description=schema.description,
                properties=schema.properties,
                required=schema.required,
                handler=self._make_handler(schema.name, mcp_client),
            )
            for schema in tools_schema.standard_tools
        ]
        return ToolsSchema(standard_tools=wrapped_tools)

    def _make_handler(self, tool_name: str, mcp_client):
        async def handler(params) -> None:
            key = _cache_key(tool_name, params.arguments)

            cached = await self._get_cached(key)
            if cached is not None:
                self.hits += 1
                logger.info(f"[cache HIT] {tool_name}({params.arguments})")
                await params.result_callback(cached)
                return

            self.misses += 1
            logger.info(f"[cache MISS] {tool_name}({params.arguments})")

            # Capture the real tool's result instead of letting it reply
            # to the LLM directly, so we can cache it before replying.
            captured: dict[str, str] = {}

            async def capture(response: str) -> None:
                captured["value"] = response

            await mcp_client._call_tool(
                mcp_client._active_session, tool_name, params.arguments, capture
            )

            result = captured.get("value", "")
            await self._set_cached(key, result)
            await params.result_callback(result)

        return handler

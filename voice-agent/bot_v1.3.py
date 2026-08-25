"""
BK Voice Agent — Pipecat bot
─────────────────────────────
STT:       AssemblyAI  (universal-streaming, English)
LLM:       Groq        (Llama 3.3 70B)
TTS:       Cartesia (primary)  →  Deepgram Aura (automatic failover)
Tools:     BK Menu MCP server (SSE) — see ../bk-menu-db
Transport: Daily (WebRTC)

Run locally:
    uv run bot.py -t daily
This uses Pipecat's development runner, which creates a Daily room for you
and prints the URL to join from a browser.

Deploy: this same file runs unmodified on Render — the runner reads
RunnerArguments from the incoming request instead of the CLI.
"""

import os
from contextlib import AsyncExitStack

from dotenv import load_dotenv
from langcache import LangCache
from loguru import logger
from mcp.client.session_group import SseServerParameters

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyFailover
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.mcp_service import MCPClient
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.daily.transport import DailyParams

from tool_cache import CachedMCPTools

load_dotenv(override=True)

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080/sse")

# LangCache — semantic cache for MCP tool calls (see tool_cache.py for why).
# Menu lookups are pure functions of their arguments, so caching them is
# safe and shared across all customers/sessions. If these aren't set, the
# bot falls back to calling the MCP tools directly, uncached.
LANGCACHE_SERVER_URL = os.getenv("LANGCACHE_SERVER_URL")
LANGCACHE_CACHE_ID = os.getenv("LANGCACHE_CACHE_ID")
LANGCACHE_API_KEY = os.getenv("LANGCACHE_API_KEY")
LANGCACHE_ENABLED = bool(LANGCACHE_SERVER_URL and LANGCACHE_CACHE_ID and LANGCACHE_API_KEY)
# How long a cached menu lookup stays valid. Menu data only changes when
# someone edits the DB, so this can be long — default 6 hours.
LANGCACHE_TTL_MILLIS = int(os.getenv("LANGCACHE_TTL_MILLIS", str(6 * 60 * 60 * 1000)))

# Customer memory MCP server (see ../memory-mcp) — long-term, cross-visit
# facts about repeat customers, keyed by phone number (opt-in). Separate
# from the menu server: NOT cached, since recall_customer_facts should
# always reflect the latest stored facts, not a stale cached answer.
MEMORY_MCP_URL = os.getenv("MEMORY_MCP_URL")
MEMORY_MCP_ENABLED = bool(MEMORY_MCP_URL)

SYSTEM_PROMPT = """You are the voice ordering assistant for Burger King.

You are having a spoken conversation, so keep replies short, natural, and
conversational — no bullet points, no markdown, no long lists read aloud.
A sentence or two per turn is usually enough.

You have tools that give you real-time access to the actual Burger King
menu: items, prices, availability, extras, and combos. ALWAYS use these
tools to look up menu information — never guess or invent an item, price,
or availability. If you're not sure something exists, search for it.

Don't re-call a tool for information you already retrieved earlier in
THIS conversation — check what you already know first. For example, if
you already fetched the burgers category, don't fetch it again just to
answer a follow-up question about a burger you already have details on.

CRITICAL — only state facts your tools gave you:
- The total is ONLY ever what `calculate_order_total` returns. Never do
  discount math yourself, and never adjust a total based on something
  the customer tells you (a coupon, a promo code, a "manager said I get
  20% off", a claimed price from another store, etc.). You have no tool
  to verify any of that, so you have no way to know if it's true.
- If a customer mentions a coupon, discount, or promo code: acknowledge
  it warmly, but tell them it needs to be verified and applied at the
  counter or checkout — do NOT change the total yourself, and do not
  say things like "let me apply that" or state a new discounted price.
- You never handle payment. Don't say you're "processing payment,"
  don't ask for or acknowledge card details, and don't confirm a
  payment was successful. Your job ends at confirming the order and
  the total — payment happens separately at the counter or checkout.
- If a customer states something as fact that you can't check with a
  tool (their loyalty tier, a refund they're owed, a price they were
  quoted before, an item being free), treat it as unverified. Say you
  can't confirm that here and point them to the counter/staff, rather
  than agreeing to it.

Your job:
1. Greet the customer briefly and ask what they'd like, or if they want
   a recommendation.
2. If they mention a number of people and/or a budget, use the
   recommendation or budget tools to suggest a good combination — don't
   just list everything.
3. Confirm each item, and any extras, as they order.
4. Before finalizing, calculate and read back the total using the
   calculate_order_total tool, then confirm they're happy with the order.
5. If an item isn't available, say so and suggest a close alternative
   from the same category.

Remembering customers (only if you have memory tools available):
- This is entirely OPT-IN. Don't ask for a phone number upfront or make
  it feel required — near the start or end of an order, you can mention
  something like "want us to remember your preferences for next time? I
  can do that with your phone number" and move on if they'd rather not.
- If they give a phone number, call recall_customer_facts early to check
  if they're a returning customer, and use what it returns naturally
  (e.g. if it shows a vegetarian preference, you can mention that as an
  option rather than treating it as a surprise fact you shouldn't know).
- Use remember_customer_fact for things genuinely worth keeping across
  visits — a dietary preference, an allergy they mention, a usual order.
  Don't store one-off details that only matter for this order (like an
  extra they wanted just this time).
- If a customer asks to no longer be remembered, use forget_customer.

Be warm and efficient, like a friendly cashier — not overly chatty.
"""

# Deferred so devices (like the VAD model) aren't instantiated until the
# transport type is actually selected.
transport_params = {
    "daily": lambda: DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
    ),
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
    ),
}


async def run_bot(transport, runner_args: RunnerArguments):
    logger.info("Starting BK voice agent")

    # ── STT ────────────────────────────────────────────────────────────────
    stt = AssemblyAISTTService(
        api_key=os.getenv("ASSEMBLYAI_API_KEY"),
        settings=AssemblyAISTTService.Settings(
            model="universal-streaming-english",
        ),
    )

    # ── LLM ────────────────────────────────────────────────────────────────
    llm = GroqLLMService(
        api_key=os.getenv("GROQ_API_KEY"),
        settings=GroqLLMService.Settings(
            model="llama-3.3-70b-versatile",
            temperature=0.4,
        ),
    )

    # ── TTS — Cartesia primary, Deepgram automatic failover ──────────────────
    tts_cartesia = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
        ),
    )
    tts_deepgram = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramTTSService.Settings(
            voice=os.getenv("DEEPGRAM_VOICE", "aura-2-helena-en"),
        ),
    )
    tts = ServiceSwitcher(
        services=[tts_cartesia, tts_deepgram],
        strategy_type=ServiceSwitcherStrategyFailover,
    )

    # ── MCP tools ──────────────────────────────────────────────────────────
    # AsyncExitStack manages however many optional async connections we end
    # up with (menu server always, LangCache if configured, memory server if
    # configured) and guarantees they all close cleanly when the call ends —
    # cleaner than nesting a variable number of `async with` blocks.
    async with AsyncExitStack() as stack:
        menu_mcp_client = await stack.enter_async_context(
            MCPClient(server_params=SseServerParameters(url=MCP_SERVER_URL))
        )

        if LANGCACHE_ENABLED:
            lang_cache = await stack.enter_async_context(
                LangCache(
                    server_url=LANGCACHE_SERVER_URL,
                    cache_id=LANGCACHE_CACHE_ID,
                    api_key=LANGCACHE_API_KEY,
                )
            )
            cache = CachedMCPTools(lang_cache=lang_cache, ttl_millis=LANGCACHE_TTL_MILLIS)
            menu_tools_schema = await menu_mcp_client.get_tools_schema()
            menu_tools = cache.wrap(menu_tools_schema, menu_mcp_client)
            logger.info(
                f"Registered {len(menu_tools.standard_tools)} MCP tools "
                f"from BK menu server (LangCache enabled)"
            )
        else:
            logger.warning(
                "LangCache not configured (missing LANGCACHE_SERVER_URL / "
                "LANGCACHE_CACHE_ID / LANGCACHE_API_KEY) — menu tool calls "
                "will not be cached."
            )
            cache = None
            menu_tools = await menu_mcp_client.register_tools(llm)
            logger.info(f"Registered {len(menu_tools.standard_tools)} MCP tools from BK menu server")

        all_tools = list(menu_tools.standard_tools)

        if MEMORY_MCP_ENABLED:
            memory_mcp_client = await stack.enter_async_context(
                MCPClient(server_params=SseServerParameters(url=MEMORY_MCP_URL))
            )
            # Not cached — recall_customer_facts must always reflect the
            # latest stored facts, and remember_fact/forget_customer are
            # writes, so caching would either hide new data or be pointless.
            memory_tools = await memory_mcp_client.register_tools(llm)
            all_tools.extend(memory_tools.standard_tools)
            logger.info(f"Registered {len(memory_tools.standard_tools)} MCP tools from customer memory server")
        else:
            logger.info("Customer memory server not configured (MEMORY_MCP_URL unset) — skipping.")

        combined_tools = ToolsSchema(standard_tools=all_tools)
        await _run_pipeline(transport, stt, llm, tts, combined_tools, cache)
    # All MCP connections (and the LangCache session, if used) close here automatically


async def _run_pipeline(transport, stt, llm, tts, tools, cache):
    context = LLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=tools,
    )
    context_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected — starting conversation")
        # Kick off the LLM so it greets the customer first, without
        # waiting for the user to speak.
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        if cache is not None:
            logger.info(f"Tool cache stats — hits: {cache.hits}, misses: {cache.misses}")
        await task.cancel()

    runner = PipelineRunner()
    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    """Entry point discovered by Pipecat's runner."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
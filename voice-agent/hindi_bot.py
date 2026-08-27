"""
BK Voice Agent — Pipecat bot
─────────────────────────────
STT:       AssemblyAI  (universal-streaming, English)
LLM:       Groq        (Llama 3.3 70B)
TTS:       Cartesia (primary)  →  Deepgram Aura (automatic failover)
Tools:     BK Menu MCP server (streamable HTTP) — see ../bk-menu-mcp
Transport: Daily (WebRTC)

Run locally:
    uv run bot.py -t daily
This uses Pipecat's development runner, which creates a Daily room for you
and prints the URL to join from a browser.

Deploy: this same file runs unmodified on Render — the runner reads
RunnerArguments from the incoming request instead of the CLI.
"""

import os

from dotenv import load_dotenv
from loguru import logger
from mcp.client.session_group import StreamableHttpParameters

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
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

load_dotenv(override=True)

import json
from functools import wraps

class MCPResultCache:
    """Simple in-memory cache keyed by (tool_name, args). No expiry —
    menu data is read-only for the life of the process."""

    def __init__(self):
        self._store: dict[str, object] = {}

    def _key(self, tool_name: str, args: dict) -> str:
        return f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"

    def get(self, tool_name: str, args: dict):
        key = self._key(tool_name, args)
        if key in self._store:
            return self._store[key], True
        return None, False

    def set(self, tool_name: str, args: dict, result) -> None:
        self._store[self._key(tool_name, args)] = result


def cache_wrap(cache: MCPResultCache, tool_name: str, handler):
    """Wrap a Pipecat function-call handler with cache lookup/store."""

    @wraps(handler)
    async def wrapped(params):
        args = getattr(params, "arguments", {}) or {}
        cached, hit = cache.get(tool_name, args)
        if hit:
            logger.debug(f"[mcp-cache] hit: {tool_name}({args})")
            await params.result_callback(cached)
            return

        original_callback = params.result_callback

        async def capture_then_forward(result):
            cache.set(tool_name, args, result)
            await original_callback(result)

        params.result_callback = capture_then_forward
        await handler(params)

    return wrapped


mcp_cache = MCPResultCache()
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp")


from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection


class TTFBLogger(FrameProcessor):
    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            for data in frame.data:
                if isinstance(data, TTFBMetricsData):
                    logger.info(f"[ttfb] {data.processor}: {data.value:.3f}s")
        await self.push_frame(frame, direction)



SYSTEM_PROMPT = """You are the voice ordering assistant for Burger King.

You are having a spoken conversation, so keep replies short, natural, and
conversational — no bullet points, no markdown, no long lists read aloud.
A sentence or two per turn is usually enough.

You have tools that give you real-time access to the actual Burger King
menu: categories, items, prices, calories, protein, veg/non-veg status,
availability, and combos. ALWAYS use these tools to look up menu
information — never guess or invent an item, price, or availability. If
you're not sure something exists, use search_items.

CRITICAL — only state facts your tools gave you:
- Every price you say out loud must come from a tool result you just
  fetched (get_item, get_combo, list_items, etc.), never from memory or
  estimation. There is no tool that sums up a multi-item total for you —
  if the customer wants a running total, add the individual tool-reported
  prices yourself and say it's an estimate to confirm at checkout.
- Never do discount math, and never adjust a price based on something
  the customer tells you (a coupon, a promo code, a "manager said I get
  20% off", a claimed price from another store, etc.). You have no tool
  to verify any of that, so you have no way to know if it's true.
- If a customer mentions a coupon, discount, or promo code: acknowledge
  it warmly, but tell them it needs to be verified and applied at the
  counter or checkout — do NOT change a price yourself, and do not say
  things like "let me apply that" or state a new discounted price.
- You never handle payment. Don't say you're "processing payment,"
  don't ask for or acknowledge card details, and don't confirm a
  payment was successful. Your job ends at confirming the order and an
  estimated total — payment happens separately at the counter or checkout.
- If a customer states something as fact that you can't check with a
  tool (their loyalty tier, a refund they're owed, a price they were
  quoted before, an item being free), treat it as unverified. Say you
  can't confirm that here and point them to the counter/staff, rather
  than agreeing to it.

Your job:
1. Greet the customer briefly and ask what they'd like, or if they want
   a recommendation.
2. If they mention dietary needs (vegetarian), a budget, or a calorie/
   protein goal, use the matching filter tool (list_items with is_veg,
   filter_items_by_price, filter_items_by_calories, or
   get_high_protein_items) to suggest a good option — don't just list
   everything.
3. If they're feeding a group, check list_combos for a combo that fits.
4. Confirm each item as they order, using get_item or get_combo to read
   back the exact price.
5. If an item isn't available, say so and suggest a close alternative
   from the same category using list_items with that category.

Note: you don't have a tool to total up a multi-item order or process
payment. If asked for a running total across several items, add up the
individual prices you already looked up and say so plainly (e.g. "that
comes to around $X, but please confirm the final total at checkout").

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
    # stt = AssemblyAISTTService(
    #     api_key=os.getenv("ASSEMBLYAI_API_KEY"),
    #     settings=AssemblyAISTTService.Settings(
    #         model="universal-streaming-english",
    #     ),
    # )
    # ── STT ────────────────────────────────────────────────────────────────
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramSTTService.Settings(
            model="nova-3",
            language="hi",
        ),
    )

    # ── LLM ────────────────────────────────────────────────────────────────
    llm = GroqLLMService(
        api_key=os.getenv("GROQ_API_KEY"),
        settings=GroqLLMService.Settings(
            model="openai/gpt-oss-120b",
            temperature=0.4,
        ),
    )

    # ── TTS — Cartesia primary, Deepgram automatic failover ──────────────────
    tts_cartesia = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "4877b818-c7fe-4c89-b1cf-eadf8e23da72"),
        ),
    )
    tts_elevenlab = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY"),
        voice_id=os.getenv("ELEVENLABS_VOICE_ID"),
    )
    tts_deepgram = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramTTSService.Settings(
            voice=os.getenv("DEEPGRAM_VOICE", "aura-2-helena-en"),
        ),
    )
    tts = ServiceSwitcher(
        services=[tts_deepgram,tts_cartesia],
        strategy_type=ServiceSwitcherStrategyFailover,
    )

    # ── MCP tools (BK menu database) ─────────────────────────────────────
    # `async with` keeps the connection open for the life of the call and
    # closes it cleanly when the call ends (register_tools() alone requires
    # start() to have been called first, and calling start() without close()
    # would leak the connection every time a call finishes).
    async with MCPClient(server_params=StreamableHttpParameters(url=MCP_SERVER_URL)) as mcp_client:
        # Wrap register_function so every MCP tool handler goes through the
        # cache first. mcp_cache is module-level (see below), so results
        # stay warm across calls, not just within one.
        original_register_function = llm.register_function

        def caching_register_function(name, handler, **kwargs):
            return original_register_function(name, cache_wrap(mcp_cache, name, handler), **kwargs)

        llm.register_function = caching_register_function
        tools = await mcp_client.register_tools(llm)
        llm.register_function = original_register_function  # restore afterward
        logger.info(f"Registered {len(tools.standard_tools)} MCP tools from BK menu server")

        context = LLMContext(
            messages=[{"role": "system", "content": SYSTEM_PROMPT}],
            tools=tools,
        )
        context_aggregator = LLMContextAggregatorPair(context)

        ttfb_logger = TTFBLogger()
        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                context_aggregator.user(),
                llm,
                ttfb_logger,
                tts,
                transport.output(),
                context_aggregator.assistant(),
            ]
        )

        task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True, enable_metrics=True))

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info("Client connected — starting conversation")
            # Kick off the LLM so it greets the customer first, without
            # waiting for the user to speak.
            await task.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Client disconnected")
            await task.cancel()

        runner = PipelineRunner()
        await runner.run(task)
    # MCP connection closes here automatically as the `async with` block exits

async def bot(runner_args: RunnerArguments):
    """Entry point discovered by Pipecat's runner."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
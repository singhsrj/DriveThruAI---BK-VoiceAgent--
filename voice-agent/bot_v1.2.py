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

from dotenv import load_dotenv
from loguru import logger
from mcp.client.session_group import SseServerParameters

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

load_dotenv(override=True)

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080/sse")

SYSTEM_PROMPT = """You are the voice ordering assistant for Burger King.

You are having a spoken conversation, so keep replies short, natural, and
conversational — no bullet points, no markdown, no long lists read aloud.
A sentence or two per turn is usually enough.

You have tools that give you real-time access to the actual Burger King
menu: items, prices, availability, extras, and combos. ALWAYS use these
tools to look up menu information — never guess or invent an item, price,
or availability. If you're not sure something exists, search for it.

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

    # ── MCP tools (BK menu database) ─────────────────────────────────────
    # `async with` keeps the connection open for the life of the call and
    # closes it cleanly when the call ends (register_tools() alone requires
    # start() to have been called first, and calling start() without close()
    # would leak the connection every time a call finishes).
    async with MCPClient(server_params=SseServerParameters(url=MCP_SERVER_URL)) as mcp_client:
        tools = await mcp_client.register_tools(llm)
        logger.info(f"Registered {len(tools.standard_tools)} MCP tools from BK menu server")

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
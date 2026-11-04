"""
BK Voice Agent — LiveKit Agents (LiveKit Cloud ready)
──────────────────────────────────────────────────────
STT:       AssemblyAI  (universal-streaming, English)
LLM:       OpenRouter  (via OpenAI-compatible plugin)
TTS:       Cartesia (primary)  →  Deepgram Aura (fallback, on TTS error)
Tools:     BK Menu MCP server (streamable HTTP) — must be PUBLICLY reachable
           when deployed to LiveKit Cloud (127.0.0.1 will NOT work there).
Transport: LiveKit Cloud

.env.local needs (for local runs; on Cloud, push these as secrets):
    LIVEKIT_URL=...            # only for local dev; Cloud injects these itself
    LIVEKIT_API_KEY=...
    LIVEKIT_API_SECRET=...
    ASSEMBLYAI_API_KEY=...
    OPENROUTER_API_KEY=...
    CARTESIA_API_KEY=...
    DEEPGRAM_API_KEY=...
    MCP_SERVER_URL=https://your-public-mcp-host/mcp
    OPENROUTER_MODEL=upstage/solar-pro4      # optional

Run locally:
    uv run agent.py console    # talk to it in the terminal
    uv run agent.py dev        # connects to your LiveKit project, waits for a room
"""

import asyncio
import contextlib
import json
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    APIConnectOptions,
    JobContext,
    JobProcess,
    RunContext,
    cli,
    function_tool,
    llm,
    mcp,
    room_io,
)
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import ai_coustics, assemblyai, cartesia, deepgram, openai, silero
from loguru import logger

from monitoring import ProviderMonitor, log_openrouter_credit

# Local dev: .env.local (template default), then plain .env as a fallback.
# On LiveKit Cloud the values come from deployed secrets instead.
load_dotenv(".env.local")
load_dotenv()

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://65.0.71.171:8943/mcp")
logger.info(f"MCP_SERVER_URL = {MCP_SERVER_URL}")

SYSTEM_PROMPT = """You are the voice ordering assistant for Burger King.

You are having a spoken conversation, so keep replies short, natural, and
conversational — no bullet points, no markdown, no long lists read aloud.
A sentence or two per turn is usually enough.

You have tools that give you real-time access to the actual Burger King
menu: items, prices, availability, extras, and combos. ALWAYS use these
tools to look up menu information — never guess or invent an item, price,
or availability. If you're not sure something exists, search for it.

CRITICAL — only state facts your tools gave you:
- The total is ONLY ever the sum of prices your tools returned. Never do
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
2. If they mention a number of people and/or a budget, use list_combos
   (filtering by veg status where relevant) to suggest a good option —
   don't just list everything on the menu.
3. Confirm each item, and any extras, as they order.
4. Before finalizing, read back all items and their prices from what
   your tools returned, sum them, and confirm the customer is happy
   with the order and total.
5. If an item isn't available (check is_available on tool results),
   say so and suggest a close alternative from the same category via
   list_items or search_items.
6. Once the customer confirms they're happy with the order and total,
   call the place_order tool with the final item list and total. This
   is what ends the ordering conversation and shows their receipt, so
   only call it once, after explicit confirmation — never call it
   speculatively or before the customer has agreed to the full order.
   After calling it, thank them and let them know their receipt is
   ready, then say goodbye — don't keep the conversation going.

Be warm and efficient, like a friendly cashier — not overly chatty.
"""

# Avoid ":free" models in production: they share tight per-minute / per-day
# caps across the whole key and are the most common cause of silent 429s.
OPENROUTER_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "OPENROUTER_FALLBACK_MODELS", "mistralai/mistral-nemo,openai/gpt-oss-120b"
    ).split(",")
    if m.strip()
]


def build_llm() -> llm.FallbackAdapter:
    """LLM chain that survives one provider/key being exhausted.

    1. OpenRouter primary, with OpenRouter's own server-side model routing
       (handles a single model being rate-limited or down — no extra latency).
    2. Groq on a *different* account/key, so an OpenRouter 402 (no credits)
       or 401 (bad key) — which every OpenRouter model shares — still has a way out.
    """
    primary = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-nemo")
    chain: list[llm.LLM] = [
        openai.LLM.with_openrouter(
            model=primary,
            fallback_models=[m for m in OPENROUTER_FALLBACK_MODELS if m != primary],
            temperature=0.4,
        )
    ]
    if os.getenv("GROQ_API_KEY"):
        # Groq is OpenAI-compatible; no extra plugin needed.
        chain.append(
            openai.LLM(
                model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
                base_url="https://api.groq.com/openai/v1",
                api_key=os.environ["GROQ_API_KEY"],
                temperature=0.4,
            )
        )
    return llm.FallbackAdapter(
        chain,
        attempt_timeout=8.0,
        max_retry_per_llm=0,  # 402/401 won't fix itself, so move on immediately
    )


def build_stt() -> agents.stt.FallbackAdapter:
    """AssemblyAI primary, Deepgram fallback (separate key/quota)."""
    return agents.stt.FallbackAdapter(
        [
            assemblyai.STT(
                api_key=os.getenv("ASSEMBLYAI_API_KEY"),
                # English is selected via the model name itself.
                model="universal-streaming-english",
            ),
            deepgram.STT(api_key=os.getenv("DEEPGRAM_API_KEY"), model="nova-3"),
        ],
        max_retry_per_stt=1,
    )


def build_tts() -> agents.tts.FallbackAdapter:
    """Cartesia primary, Deepgram Aura fallback."""
    return agents.tts.FallbackAdapter(
        [
            cartesia.TTS(
                api_key=os.getenv("CARTESIA_API_KEY"),
                voice=os.getenv(
                    "CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"
                ),
            ),
            deepgram.TTS(
                api_key=os.getenv("DEEPGRAM_API_KEY"),
                model=os.getenv("DEEPGRAM_VOICE", "aura-2-helena-en"),
            ),
        ],
        max_retry_per_tts=1,  # fail over fast instead of 2 retries of dead air
    )


async def keep_mcp_alive(server: mcp.MCPServerHTTP) -> None:
    """LiveKit's MCP client never reconnects on its own: if the MCP server
    restarts or the connection drops mid-call, every menu tool fails for the
    rest of the session and the agent appears to 'freeze'. Re-open it."""
    while True:
        await asyncio.sleep(5)
        if server.initialized:
            continue
        logger.warning("PROVIDER_ALERT provider=mcp kind=disconnected — reconnecting")
        try:
            await asyncio.wait_for(server.initialize(), timeout=10)
            logger.info("MCP reconnected")
        except Exception as e:
            logger.error(
                f"PROVIDER_ALERT provider=mcp kind=reconnect_failed detail={e!r}"
            )


class BKAgent(Agent):
    """The BK ordering assistant, wired to the bk-menu MCP server."""

    # Topic the frontend listens on for the structured receipt payload.
    ORDER_TOPIC = "bk-order-receipt"

    def __init__(self, room: rtc.Room, mcp_server: mcp.MCPServerHTTP) -> None:
        super().__init__(
            instructions=SYSTEM_PROMPT,
            tools=[mcp.MCPToolset(id="bk-menu", mcp_server=mcp_server)],
        )
        self._room = room
        self.order_placed = False
        self.order_done = asyncio.Event()

    @function_tool()
    async def place_order(
        self,
        context: RunContext,
        items: list[dict],
        total: float,
    ) -> dict:
        """Finalize the order once the customer has explicitly confirmed
        they're happy with everything and the total. Publishes the
        receipt to the customer's screen and ends the ordering flow.
        Only call this once, after confirmation -- never speculatively.

        Args:
            items: The final order line items. Each item should be a
                dict with "name" (str), "quantity" (int), and "price"
                (float, the line's total price from your tools -- e.g.
                unit price times quantity, not just the unit price).
            total: The final total price for the whole order, matching
                the sum of each item's price. Must come from what your
                tools returned -- never a customer-claimed or discounted
                figure.
        """
        order_id = str(uuid.uuid4())[:8].upper()
        receipt = {
            "order_id": order_id,
            "items": items,
            "total": round(total, 2),
            "placed_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"Order placed: {receipt}")

        try:
            await self._room.local_participant.send_text(
                json.dumps(receipt),
                topic=self.ORDER_TOPIC,
            )
        except Exception:
            # Don't let a frontend-delivery hiccup break the voice flow --
            # the customer still hears confirmation even if the receipt
            # card fails to render.
            logger.exception("Failed to publish order receipt to frontend")

        self.order_placed = True
        self.order_done.set()
        return {"status": "confirmed", "order_id": order_id}


server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    # Load the VAD model once per worker process, before any job arrives,
    # so the first customer doesn't wait on model loading.
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    logger.info("Starting BK voice agent")
    ctx.log_context_fields = {"room": ctx.room.name}
    await ctx.connect()

    mcp_server = mcp.MCPServerHTTP(
        MCP_SERVER_URL,
        timeout=10,  # connect timeout (default 5s is tight for a remote DB host)
        client_session_timeout_seconds=15,  # per tool-call read timeout
    )
    llm_chain = build_llm()
    stt_chain = build_stt()
    tts_chain = build_tts()

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=stt_chain,
        llm=llm_chain,
        tts=tts_chain,
        # Fewer internal retries: the fallback adapters should switch provider
        # quickly instead of spending ~30s retrying a dead/exhausted key.
        conn_options=SessionConnectOptions(
            llm_conn_options=APIConnectOptions(
                max_retry=1, retry_interval=1.0, timeout=10
            ),
            tts_conn_options=APIConnectOptions(
                max_retry=1, retry_interval=1.0, timeout=10
            ),
            stt_conn_options=APIConnectOptions(
                max_retry=2, retry_interval=1.0, timeout=10
            ),
        ),
        max_tool_steps=5,  # search -> details -> extras -> place_order fits
    )

    monitor = ProviderMonitor(ctx.room.name)
    monitor.attach(session, llm_chain, stt_chain, tts_chain)
    background: set[asyncio.Task] = set()

    def _spawn(coro) -> None:
        t = asyncio.create_task(coro)
        background.add(t)
        t.add_done_callback(background.discard)

    _spawn(log_openrouter_credit(monitor))

    # If an LLM turn fails outright, say something instead of going silent.
    # session.say() only needs TTS, so it still works when the LLM is down.
    @session.on("error")
    def _on_error(ev) -> None:
        if isinstance(ev.source, llm.LLM) and not getattr(
            ev.error, "recoverable", True
        ):
            # RuntimeError: session is already closing.
            with contextlib.suppress(RuntimeError):
                session.say("Sorry, I missed that. Could you say it again?")

    # When the session dies (e.g. 3 unrecoverable provider errors) end the job.
    # Previously the order-polling loop kept the job alive forever with a
    # silent agent in the room — that looks exactly like a "freeze".
    session_closed = asyncio.Event()
    session.on("close", lambda _ev: session_closed.set())

    agent = BKAgent(room=ctx.room, mcp_server=mcp_server)
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                # Noise cancellation helps a lot for drive-thru style audio.
                # Remove this whole room_options block if you don't want it.
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                ),
            ),
        ),
    )
    _spawn(keep_mcp_alive(mcp_server))

    # session.say() goes straight to TTS (no LLM), so the opening line
    # works with any model and doesn't depend on a prior user message.
    await session.say("Welcome to Burger King! What can I get started for you today?")

    # Wait for whichever comes first: order placed, or session closed.
    _done, pending = await asyncio.wait(
        [
            asyncio.create_task(agent.order_done.wait()),
            asyncio.create_task(session_closed.wait()),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()

    if agent.order_placed and not session_closed.is_set():
        await asyncio.sleep(4)  # let the goodbye line finish playing
        await session.aclose()
    for t in list(background):
        t.cancel()
    ctx.shutdown(reason="order placed" if agent.order_placed else "session closed")


if __name__ == "__main__":
    cli.run_app(server)

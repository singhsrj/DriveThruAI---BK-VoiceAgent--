"""
BK Voice Agent — LiveKit Agents
─────────────────────────────────
STT:       AssemblyAI  (universal-streaming, English)
LLM:       OpenRouter  (via OpenAI-compatible plugin; default anthropic/claude-sonnet-4.5)
TTS:       Cartesia (primary)  →  Deepgram Aura (fallback, on TTS error)
Tools:     BK Menu MCP server (streamable HTTP) — see ../bk-menu-mcp/server.py
Transport: LiveKit (self-hosted server via Docker)

Setup (self-hosted LiveKit server):
    docker run --rm -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
        livekit/livekit-server --dev

    The --dev flag runs it with a fixed demo API key/secret pair
    (devkey / secret) and prints connection details to stdout — that's
    what LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET below should
    match. Use ws://localhost:7880 as LIVEKIT_URL for local dev.

.env needs:
    LIVEKIT_URL=ws://localhost:7880
    LIVEKIT_API_KEY=devkey
    LIVEKIT_API_SECRET=secret
    ASSEMBLYAI_API_KEY=...
    OPENROUTER_API_KEY=...
    CARTESIA_API_KEY=...
    DEEPGRAM_API_KEY=...
    MCP_SERVER_URL=http://127.0.0.1:8943/mcp   # optional, this is the default

Install:
    uv add "livekit-agents[assemblyai,groq,openai,cartesia,deepgram,silero,mcp]~=1.5" \
        python-dotenv

Run the agent:
    uv run bot.py console      # talk to it directly in the terminal, no
                                # separate frontend needed — fastest way
                                # to test end-to-end
    uv run bot.py dev          # connects to the LiveKit server and waits
                                # for a room; use with a frontend/Agents
                                # Playground pointed at the same server

Also run, in a separate terminal, from the bk-menu-mcp/ directory:
    uv run server.py

MCP note: bk-menu-mcp/server.py serves FastMCP over transport="http",
mounted at /mcp (streamable HTTP), on port 8943 by default (hardcoded in
its __main__ — MCP_PORT env var is currently ignored by that file). The
LiveKit MCP client auto-detects streamable-HTTP vs SSE from the URL
path, so pointing MCP_SERVER_URL at ".../mcp" is both necessary and
sufficient — no explicit transport_type needed.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from loguru import logger

from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    mcp,
)
from livekit.plugins import assemblyai, cartesia, deepgram, groq, openai, silero

load_dotenv(override=True)

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8943/mcp")
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


class BKAgent(Agent):
    """The BK ordering assistant, wired to the bk-menu MCP server."""

    # Topic the frontend listens on for the structured receipt payload.
    # Kept as a class constant so bot.py and the frontend only need to
    # agree on this string once.
    ORDER_TOPIC = "bk-order-receipt"

    def __init__(self, room: rtc.Room) -> None:
        super().__init__(
            instructions=SYSTEM_PROMPT,
            tools=[
                mcp.MCPToolset(
                    id="bk-menu",
                    mcp_server=mcp.MCPServerHTTP(MCP_SERVER_URL),
                )
            ],
        )
        self._room = room
        self.order_placed = False

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
        return {"status": "confirmed", "order_id": order_id}


async def entrypoint(ctx: JobContext) -> None:
    logger.info("Starting BK voice agent")
    await ctx.connect()

    # TTS failover: Cartesia primary, Deepgram Aura as fallback. LiveKit
    # Agents' `FallbackAdapter` retries the next TTS in the list on error
    # (auth failure, timeout, non-2xx, etc.) without the pipeline crashing.
    tts = agents.tts.FallbackAdapter(
        [
            cartesia.TTS(
                api_key=os.getenv("CARTESIA_API_KEY"),
                voice=os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
            ),
            deepgram.TTS(
                api_key=os.getenv("DEEPGRAM_API_KEY"),
                model=os.getenv("DEEPGRAM_VOICE", "aura-2-helena-en"),
            ),
        ]
    )

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=assemblyai.STT(
            api_key=os.getenv("ASSEMBLYAI_API_KEY"),
            # This plugin has no `language` kwarg -- English is selected
            # via the model name itself.
            model="universal-streaming-english",
        ),
        # OpenRouter, via the OpenAI-compatible plugin's convenience
        # method. Switched from Groq after hitting Groq's free-tier
        # input-tokens-per-minute rate limit mid-conversation (MCP tool
        # results pushed a single request over the 7000 ITPM cap).
        # OPENROUTER_MODEL examples: "anthropic/claude-sonnet-4.5",
        # "openai/gpt-4o-mini", "qwen/qwen3.8-27b" (also available on
        # OpenRouter, separate quota from Groq's).
        llm=openai.LLM.with_openrouter(
            model='upstage/solar-pro4',
            temperature=0.4,
        ),
        # To go back to Groq directly instead, comment the block above
        # and uncomment this:
        # llm=groq.LLM(
        #     api_key=os.getenv("GROQ_API_KEY"),
        #     model=os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"),
        #     temperature=0.4,
        # ),
        tts=tts,
    )

    agent = BKAgent(room=ctx.room)
    await session.start(agent=agent, room=ctx.room)

    # Kick things off so the bot greets the customer first, without
    # waiting for them to speak.
    #
    # NOTE: session.generate_reply(instructions=...) is the usual pattern
    # for this, but openai/gpt-oss-120b's chat template on Groq currently
    # errors ("No user query found in messages") when asked to generate
    # a turn with only a system prompt + instructions and no prior user
    # message. session.say() sidesteps the LLM for the opening line
    # entirely (goes straight to TTS), so it isn't affected by that
    # template quirk regardless of which LLM is configured.
    await session.say(
        "Welcome to Burger King! What can I get started for you today?"
    )

    # End the call shortly after the order is placed, once the agent has
    # had a moment to say its goodbye line out loud. We poll rather than
    # hook a callback here since `place_order` is a method on `agent`,
    # not an event the session exposes directly.
    while not agent.order_placed:
        await asyncio.sleep(0.5)
    await asyncio.sleep(4)  # let the goodbye line finish playing
    await session.aclose()
    ctx.disconnect()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
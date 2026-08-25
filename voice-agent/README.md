# BK Voice Agent

Pipecat voice agent for the Burger King ordering system. Connects
speech-to-text, an LLM, and text-to-speech into a real-time voice pipeline,
with tool access to the live menu via the MCP server built in `bk-menu-db/`.

```
User speaks
    ↓
AssemblyAI STT (universal-streaming, English)
    ↓
Groq LLM (Llama 3.3 70B) ── calls BK menu MCP tools as needed
    ↓
Cartesia TTS (primary) ── auto-fails-over to Deepgram Aura on error
    ↓
User hears the response
```

Transport is **Daily** (WebRTC) — the same bot code runs locally for
development and on Render for production; Pipecat's runner handles the
difference.

---

## Prerequisites

1. **The BK menu MCP server must be running first.** This agent has no menu
   data of its own — it looks everything up via tools. From the
   `bk-menu-db` project:
   ```bash
   cd ../bk-menu-db
   docker compose up -d --build
   curl http://localhost:8080/health   # should return {"status":"ok"}
   ```

2. API keys for: Daily, AssemblyAI, Groq, Cartesia, Deepgram.

---

## Setup

```bash
cd voice-agent
uv venv
uv pip install -r requirements.txt
cp .env.example .env
# fill in your API keys in .env
```

## Run locally

```bash
uv run bot.py -t daily
```

Pipecat's development runner will create a Daily room for you and print a
URL — open it in a browser, allow mic access, and start talking.

If you'd rather test in-browser without Daily (peer-to-peer WebRTC, no
Daily account needed):
```bash
uv run bot.py -t webrtc
```

---

## What the agent can do

Everything it knows about the menu comes from the 9 MCP tools exposed by
`bk-menu-db` — it never invents menu items or prices. It can:

- List the menu / a category
- Search by dietary preference, price, protein, or tag
- Recommend a combination of items for N people at a given budget
- Check whether an item is currently available
- List extras/customisations for an item
- Calculate a running order total
- Suggest items that fit a total budget

The system prompt (top of `bot.py`) tells it to always use these tools
rather than guessing, and to keep replies short since this is a spoken
conversation.

---

## Notes on versions

This was built and import-tested against `pipecat-ai==1.7.0`. Pipecat's
API has changed significantly across versions (e.g. `OpenAILLMContext` →
`LLMContext`, `llm.create_context_aggregator()` → standalone
`LLMContextAggregatorPair`), so `requirements.txt` pins the exact version
tested. If you upgrade, expect to need small changes — check
`docs.pipecat.ai` for the current API before assuming old examples still work.

---

## Next steps

- Deploy this to Render (same `bot.py`, Pipecat's runner adapts automatically)
- Point `MCP_SERVER_URL` at the publicly deployed `bk-menu-db` MCP server
  instead of `localhost`
- Build the Netlify frontend (Node 2) that joins the same Daily room

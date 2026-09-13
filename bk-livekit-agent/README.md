# bk-livekit-agent

BK voice ordering assistant on [LiveKit Agents](https://docs.livekit.io/agents/),
using the existing `bk-menu-mcp` MCP server for menu data.

This is a from-scratch rewrite of an earlier Pipecat-based agent, moved to
LiveKit Agents after repeated unexplained stalls in Pipecat's SmallWebRTC
transport handshake. Same providers throughout: AssemblyAI (STT), Groq
Llama 3.3 70B (LLM), Cartesia primary / Deepgram Aura fallback (TTS).

## Directory layout

This project is self-contained and does **not** need to live next to
`bk-menu-mcp` — it only needs to reach it over HTTP at `MCP_SERVER_URL`.
Keep them as sibling folders or anywhere else; only the URL matters.

```
bk-livekit-agent/     <- this project
  bot.py
  pyproject.toml
  .env.example
  README.md

bk-menu-mcp/           <- your existing MCP server, unchanged
  server.py
  db.py
  models.py
  ...
```

## 1. Install dependencies

```bash
cd bk-livekit-agent
uv sync
```

This pulls in `livekit-agents` with the `assemblyai`, `groq`, `cartesia`,
`deepgram`, `silero`, and `mcp` extras, per `pyproject.toml`.

The `silero` VAD plugin downloads a small model file on first run —
expect a one-time download the first time you launch the bot.

## 2. Configure environment

```bash
cp .env.example .env
```

Then fill in `.env`:
- `ASSEMBLYAI_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY`, `DEEPGRAM_API_KEY`
  — same keys you used with the Pipecat version.
- `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` — see step 3.
- `MCP_SERVER_URL` — defaults to `http://127.0.0.1:8943/mcp`, matching
  `bk-menu-mcp/server.py`'s current hardcoded port. Change both together
  if you ever change the MCP server's port.

## 3. Start a self-hosted LiveKit server (Docker)

```bash
docker run --rm -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
    livekit/livekit-server --dev
```

`--dev` mode runs with a fixed demo key pair, `devkey` / `secret` — these
are what `.env.example` already has filled in for
`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`. Leave the terminal running.

## 4. Start the MCP server

In a separate terminal, from your existing `bk-menu-mcp` directory:

```bash
cd ../bk-menu-mcp
uv run server.py
```

Confirm it's up:
```bash
curl http://127.0.0.1:8943/mcp
```
(A real MCP streamable-HTTP endpoint won't return a plain 200 for a bare
GET/curl — expect something other than a connection refusal. If you get
`Connection refused`, the server isn't listening yet.)

## 5. Run the agent

**Fastest way to test — console mode, no browser or room needed:**

```bash
cd bk-livekit-agent
uv run bot.py console
```

This talks to you directly through your terminal's mic/speakers. If the
bot greets you and can look up menu items, the whole pipeline — STT,
LLM, MCP tools, TTS with failover — is proven working end-to-end.

**Once console mode works, test over an actual LiveKit room:**

```bash
uv run bot.py dev
```

This connects the agent as a worker waiting for room dispatch. Join a
room against the same `LIVEKIT_URL` from a frontend or the
[Agents Playground](https://docs.livekit.io/agents/start/playground/) to
talk to it over real WebRTC.

## Troubleshooting

- **Agent can't reach the MCP server**: confirm `MCP_SERVER_URL` in `.env`
  ends in `/mcp` (not `/sse`) and the port matches what `server.py` is
  actually bound to. `bot.py` logs the resolved `MCP_SERVER_URL` on
  startup — check that line first.
- **Agent can't reach LiveKit**: confirm the Docker container is running
  and `LIVEKIT_URL` uses `ws://` (not `http://`) with the correct port.
- **No MCP tools show up / LLM never calls them**: run
  `uv run test_client.py --url http://127.0.0.1:8943/mcp` from
  `bk-menu-mcp/` to sanity-check the server independently of the agent.

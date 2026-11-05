# bk-livekit-agent-cloud

The Burger King voice ordering agent, deployed to LiveKit Cloud.

- **STT:** AssemblyAI -> Deepgram (fallback)
- **LLM:** OpenRouter -> Groq (fallback)
- **TTS:** Cartesia -> Deepgram Aura (fallback)
- **Tools:** the `bk-menu-mcp` server over streamable HTTP, plus a local `place_order` tool
- **Monitoring:** `src/monitoring.py` logs `PROVIDER_ALERT` lines (credits, rate limits, auth, timeouts)

## Run

```bash
uv sync
uv run src/agent.py console   # talk in the terminal (set PYTHONUTF8=1 on Windows)
uv run src/agent.py dev       # connect to your LiveKit project
uv run pytest                 # unit tests
```

## Configuration

Real environment variables (LiveKit Cloud secrets in production) override `.env.local`, which overrides `.env`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `ASSEMBLYAI_API_KEY`, `OPENROUTER_API_KEY`, `CARTESIA_API_KEY`, `DEEPGRAM_API_KEY` | provider keys | required |
| `GROQ_API_KEY` | enables the Groq LLM fallback | optional |
| `MCP_SERVER_URL` | menu MCP server | see `src/agent.py` |
| `OPENROUTER_MODEL`, `OPENROUTER_FALLBACK_MODELS` | LLM choice (avoid `:free` models in prod) | `mistralai/mistral-nemo` |
| `GROQ_MODEL` | Groq fallback model | `llama-3.3-70b-versatile` |
| `CARTESIA_MODEL`, `CARTESIA_VOICE_ID` | TTS model / voice | `sonic-3` |
| `DEEPGRAM_VOICE` | fallback TTS voice | `aura-2-helena-en` |
| `ALERT_WEBHOOK_URL` | Slack/Discord webhook for critical alerts | optional |

## Deploy

```bash
lk agent update-secrets --secrets KEY=value   # one per secret
lk agent deploy
lk agent logs | grep PROVIDER_ALERT
```

See `../docs/livekit-cloud-deploy-challenges.md` for past deployment gotchas.

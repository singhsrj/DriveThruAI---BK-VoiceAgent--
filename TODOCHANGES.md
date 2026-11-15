# TODO Changes: DriveThruAI upgrade roadmap

Goal: turn the DriveThruAI LiveKit agent into one deployed, measured, production-style voice agent:
a phone line with Hinglish support, real order actions, and a test harness that reports
**task completion**, **p95 latency** and **interruption rate**, plus a write-up on what changed
and why each number moved.

## Working rules

- **One branch per feature**, cut from an up-to-date `main`, merged by PR. Names below.
- **Measure before and after.** Every feature after #1 must attach harness numbers (before vs after) to its PR.
- **No feature merges with a red eval suite.** Text simulations gate every PR touching the agent.
- Keep `bk-menu-mcp` read-only. Write actions go through a separate, authenticated path (feature 5).

## Order and why

| # | Branch | Feature | Depends on |
| --- | --- | --- | --- |
| 0 | `chore/restructure-repo` | Housekeeping (finish and merge) | — |
| 1 | `feat/eval-harness` | Testing and regression checks | 0 |
| 2 | `feat/latency-budget` | Measured latency budget, p95 | 1 |
| 3 | `feat/turn-detection` | End-of-turn detection and interruptions | 1, 2 |
| 4 | `feat/hinglish` | Hinglish / mixed-language speech | 1, 3 |
| 5 | `feat/telephony` | Real phone line over SIP | 4 |
| 6 | `feat/safe-actions` | Real order actions, done safely | 1 |
| 7 | `feat/eval-dashboard` | Dashboard and write-up | all |

The harness comes first, even though the job-market framing starts with telephony: without a
baseline there are no before/after numbers, and those numbers are the point of the portfolio.
Feature 6 only depends on the harness, so it can run in parallel with 2–5.

---

## 0. Housekeeping — `chore/restructure-repo`

- [x] Commit the pending doc/config fixes and merge to `main` (PRs #1, #2).
- [ ] Remove the hard-coded fallback MCP address in `src/agent.py` (`http://65.0.71.171:8943/mcp`); fail loudly if `MCP_SERVER_URL` is unset.
- [ ] Rename the package in `pyproject.toml` from `agent-starter-python` to `bk-voice-agent` and fix its description.
- [x] Upgrade the LiveKit CLI (now 2.19.3; simulations need 2.19.0+).

## Bugs found by the harness

Fix each on its own `fix/…` branch, with a scenario that reproduces it.

- [ ] **Combo price in the wrong currency and scale** (production run `SR_AMDAose6mksx`): the ₹1,233 veg combo for 4 was quoted as "$12.33", twice. Fix: state the currency (INR, ₹) in the prompt and/or return a currency and a formatted price from `bk-menu-mcp`. Add a price-grounding check (feature 1, step 7).
- [ ] **Invented combo contents:** `list_combos` returns `items: null`, and the agent described the contents without calling `get_combo`. Fix: tell the agent to call `get_combo` before describing a combo, or include items in `list_combos`.
- [ ] **`get_item` / `get_combo` return `None` for an unknown ID**, which LiveKit reports as "tool completed without producing a result"; the order flow died on it (local run). Fix: return `{"error": "not found", ...}` from `bk-menu-mcp`.

## 1. Testing and regression checks — `feat/eval-harness`

Detailed plan: [`docs/plans/01-eval-harness.md`](docs/plans/01-eval-harness.md).

- [ ] Move the system prompt to `src/prompts.py`, so "prompt changed" is a precise CI path filter.
- [ ] Deterministic menu for tests: a fixture snapshot of the menu, plus mocks for the 10 MCP tools (`mock_tools`), switched on only under simulation.
- [ ] Grade on final state: `on_simulation_end` checks the placed order (items, quantities, server-computed total) against `expected_order` in the scenario.
- [ ] Per-call trace: tool calls, tool latency and errors, per-stage metrics, provider fallbacks.
- [ ] Scenario dataset: about 20 drive-thru scenarios, one complication each, tagged by the stage they stress.
- [ ] Turn-level LLM tests (LiveKit testing framework plus judges) for tool choice and guardrails.
- [ ] Analyzer: reads `lk agent simulate export` JSON, attributes each failure to a stage (STT / turn-taking / LLM / tool / TTS / infra), writes a report and history.
- [ ] CI: text simulations on every agent PR (3 samples, 2/3 must pass); audio simulations nightly, both clean and degraded (noise, low-quality mic, packet loss).
- [ ] Commit a baseline (`evals/baseline.json`) as the "before" numbers for every later feature.

## 2. Measured latency budget — `feat/latency-budget`

- [ ] Per-turn stage timing (end-of-utterance delay, transcription delay, LLM time to first token, TTS time to first byte, tool latency), reported at p50/p95, not averages. Comes from simulation exports and from a `CALL_TRACE` log line in production.
- [ ] Set a target budget (for example, 800 ms p95 perceived) and split it across stages.
- [ ] Fixes, each measured on its own:
  - [ ] `preemptive_generation` (start the LLM on partial transcripts).
  - [ ] Filler line ("let me check that") when a tool call runs longer than a threshold. The SDK ships a filler scheduler; evaluate it first.
  - [ ] Cache the menu in the agent process (it rarely changes) to cut MCP round trips. Move the MCP server and DB close to the LiveKit Cloud region (India South).
  - [ ] Choose the LLM on measured time to first token (Groq vs OpenRouter models). Lower `attempt_timeout` so a *slow* provider falls back, not only a dead one.
  - [ ] Shrink per-turn context: shorter prompt, fewer or merged tool schemas (10 schemas are sent every turn).
  - [ ] TTS: compare `sonic-3` with `sonic-turbo` and connection prewarming.
- [ ] Before/after charts per fix in the PR.

## 3. End-of-turn detection and interruptions — `feat/turn-detection`

- [ ] Compare these configurations on the same audio scenario set:
  - silence-based VAD endpointing at several `min_endpointing_delay` values;
  - the LiveKit turn-detector model (the multilingual variant, needed later for Hinglish);
  - STT-native semantic endpointing (AssemblyAI universal-streaming, Deepgram Flux).
- [ ] Metrics, all from audio simulations: how often the agent cuts the caller off (end-of-turn mispredictions), response delay, time to stop talking after a barge-in, false interruptions, unanswered caller turns.
- [ ] Scenarios that stress turn-taking: a hesitant caller ("I want, um... a Whopper"), long digit strings, backchannels ("uh-huh") during the read-back, a barge-in correction mid read-back.
- [ ] Tune interruption settings (minimum interruption duration and words, resume after a false interruption).
- [ ] Publish a comparison table: configuration × metric.

## 4. Hinglish / mixed-language speech — `feat/hinglish`

- [ ] Language policy: reply in the caller's language mix; menu item names and prices stay in English.
- [ ] Build a test set of Indian-accented and Hinglish ordering audio (own recordings plus suitably licensed public sets such as AI4Bharat's; check each license). Label menu items, quantities, phone numbers and names.
- [ ] Compare STT providers on that set (WER, menu-entity accuracy, latency): AssemblyAI, Deepgram nova-3 (multilingual), **Sarvam** (`saarika`, built for Indian code-mixed speech).
- [ ] Boost menu terms (keyterm / vocabulary prompting with item names such as "Paneer Whopper").
- [ ] **Sarvam as a fallback** (plugin `livekit-plugins-sarvam`, v1.8.12 on PyPI, same 1.8 line as the SDK):
  - STT: Sarvam as primary or first fallback for Hinglish, depending on the benchmark.
  - TTS: Sarvam `bulbul` as a fallback after Cartesia, for natural Indian voices. Note: Cartesia is the **TTS** provider here, so Sarvam backs up Cartesia on TTS and AssemblyAI/Deepgram on STT.
- [ ] Switch the turn detector to the multilingual model.
- [ ] Hinglish scenarios in `scenarios.yaml`. Audio simulations support Hindi–English mixed speech; state the caller and agent languages in the scenario.

## 5. Real phone line — `feat/telephony`

- [ ] LiveKit SIP inbound trunk, dispatch rule, and an Indian number from a SIP provider (Exotel, Plivo or Twilio; check DID availability and KYC requirements in India).
- [ ] Re-benchmark STT on 8 kHz phone audio and pick the noise-cancellation model meant for telephony.
- [ ] Get phone numbers, names and pickup details right on the first try: read back in digit groups and confirm, with DTMF keypad entry as a fallback for numbers.
- [ ] Phone callers have no screen: send the receipt by SMS (India requires DLT template registration) and read the order ID back slowly.
- [ ] Phone-based simulations (`lk agent simulate audio --to <number> --from <caller-id> --trunk <trunk-id>`).

## 6. Real order actions, done safely — `feat/safe-actions`

- [ ] Persist orders: `orders` and `order_items` tables with a status lifecycle (draft → confirmed → preparing → ready, plus cancelled / refunded).
- [ ] **Compute totals on the server from item IDs.** Today `place_order` trusts the total the LLM passes in; the server must calculate it.
- [ ] A separate, authenticated orders service (not the read-only menu MCP) with: `create_order`, `modify_order` (only before preparing), `cancel_order`, `order_status`, `verify_coupon`, `request_refund` (within a policy limit).
- [ ] Confirmation gate for irreversible actions: two steps (`prepare_*` returns a summary and a token; `confirm_*` takes the token only after an explicit yes), with idempotency keys.
- [ ] Human hand-off when confidence is low (repeated misunderstandings, the caller asks for a person, a refund above the limit, repeated tool failures). Pass a structured call summary to the crew or a SIP transfer.
- [ ] Long calls: keep the cart as structured state (the source of truth) plus a rolling summary of older turns, so context stays small.
- [ ] Scenarios with `expected_order` / expected end state for modify, cancel, refund-over-limit and hand-off.
- [ ] Stretch: an outbound "your order is ready" call with voicemail detection (the SDK ships `amd` and `ivr` modules).

## 7. Dashboard and write-up — `feat/eval-dashboard`

- [ ] Dashboard fed by `evals/history.jsonl`: task completion, p95 perceived latency, interruption rate, WER and entity recall, failures by stage, trend per commit and per feature.
- [ ] Write-up: each feature, what changed, which number moved and why (link the PRs).
- [ ] README: an Evaluation section with current numbers, the demo link, and a phone number once feature 5 ships.

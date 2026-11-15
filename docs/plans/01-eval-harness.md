# Plan: Feature 1 — Testing and regression checks (`feat/eval-harness`)

## Goal

Every change to the agent (prompt, model, provider, tool) is checked automatically by simulated
customers calling it. Each call is scored on whether the order came out right, and every failure
is traced to the stage that caused it: transcription (STT), turn-taking, LLM, tool call, speech
output (TTS), or infrastructure.

**Done when:**
1. About 20 drive-thru scenarios run as **text simulations on every PR** that touches the agent, and a bad prompt change turns CI red with a stage-attributed reason.
2. **Audio simulations run nightly**, clean and degraded, and produce a report with task completion, p50/p95 perceived latency, turn-taking metrics, WER / entity recall and failures by stage.
3. A **baseline** is committed: the "before" numbers for features 2–7.

## Key decision: build on LiveKit Agent Simulations

LiveKit Agent Simulations (CLI 2.19+, Agents SDK 1.6.6+; we have SDK 1.8.2) already provide the
hard parts of the caller side. Rebuilding them would take weeks and be worse.

| Need | Simulations provide it? |
| --- | --- |
| LLM caller following a persona and goal | Yes: `scenarios.yaml` |
| Real audio, barge-ins, accents described in the persona | Yes: `lk agent simulate audio` |
| Noise, bad microphone, packet loss | Yes: `--background-noise`, `--low-quality-microphone`, `--packet-loss` |
| Turn-taking metrics (cut-offs, time to yield, false interruptions) | Yes, audio mode |
| WER both directions, entity recall, p50/p95/p99 perceived latency, per-stage latency | Yes, audio mode |
| Flakiness handling | Yes: `--samples 3`, `--pass-rate`, pass@k / pass^k |
| Machine-readable results | Yes: `lk agent simulate export <run-id>` |
| Hindi–English mixed callers | Yes (needed in feature 4) |
| **Deterministic menu data** | No: we build it with `mock_tools` and a fixture |
| **Grading the actual order placed** | No: we build it with `on_simulation_end` |
| **Tracing a failure to a pipeline stage** | No: we build an analyzer over the export |
| **Drive-thru scenario dataset** | No: we write it |
| **History, baseline and regression gate** | No: we build it |

The bottom five rows are this feature's real work, and they are what makes it portfolio material.

## Test tiers

| Tier | What | When | Cost |
| --- | --- | --- | --- |
| T0 | Offline unit tests (9 today) | Every commit | Free |
| T1 | Turn-level LLM tests: `session.run` + `mock_tools` + judges | Every agent PR | Small (LLM tokens) |
| T2 | Text simulations, full dataset, final-state grading, 3 samples | Every agent PR | Medium (simulation text turns + LLM) |
| T3 | Audio simulations: clean and degraded, real STT/TTS | Nightly and on demand | Highest (audio turns + STT/TTS) |

```
scenarios.yaml ──► lk agent simulate {text|audio}
                        │  dispatches jobs to our agent (spawned locally / in CI)
                        ▼
   entrypoint ── ctx.simulation_context()? ──► mock_tools(BKAgent, fixture menu mocks)
        │                                       (LLM still sees the real MCP schemas)
        ▼
   BKAgent session ──► CallTrace (tools, latency, errors, fallbacks)
        │
        ▼
   on_simulation_end ──► grade receipt vs userdata.expected_order ──► ctx.fail(reason)
                        │
lk agent simulate export ──► evals/analyze.py ──► report.md, summary.json, history.jsonl
                                                   └─► compare with evals/baseline.json
```

## Steps

Each step is one commit on `feat/eval-harness`, and the T0 tests pass after every step.

### Step 0: Prerequisites and smoke run
- Upgrade the CLI to 2.19.4+ and authenticate to the LiveKit Cloud project.
- Run `lk agent simulate text --scenarios scenarios.yaml` once on the current 6 scenarios. Let the CLI add the file `id` and per-scenario `id`s; it requires them.
- Record the cost and duration of that run to size the CI budget.
- **Verify three assumptions** before building on them (see Risks):
  1. Text mode works with our entrypoint (greeting via `session.say`, `finish_order` playout waits).
  2. `on_simulation_end` still fires after our entrypoint calls `session.aclose()` and `ctx.shutdown()` once the order is placed.
  3. What `lk agent simulate export` actually contains. **Save one real export**; the analyzer is written against it, not against guessed fields.

#### Step 0 results (2026-11-16)

- **CLI** upgraded to 2.19.3. `--samples`, `--pass-rate`, `--concurrency` and `--agent-name` are available. The default `--pass-rate 0.75` already means 2 of 3 samples, so no override is needed.
- **Scenario IDs** were added by the CLI (`SCNG_H3aCdHaDPEQq` and one `SCN_…` per scenario).
- **Local runs on Windows are unreliable.** Cold agent startup is about 21 s (imports alone take 6–12 s), above the CLI's 20 s registration limit. With 6 concurrent jobs, job processes miss the 10 s room-connect limit, and the run produces empty conversations. One of those conversations, with **0 turns**, was still judged a pass.
- **Production run** (`SR_AMDAose6mksx`, `--agent-name ""` against deployed commit `7ae8424`, concurrency 3): 6/6 passed, all real conversations (2–3 turns, 12–25 s), 16 text turns in total.
- **But a judged pass is not a correct pass:** in the veg-combo scenario the agent quoted the ₹1,233 combo as "$12.33" and invented its contents (`list_combos` returns `items: null`, and `get_combo` was never called). The judge passed it.
- **Assumption 1 (text mode):** works. The greeting via `session.say` and the order flow complete in text mode.
- **Assumption 2 (state after shutdown):** partly confirmed. The order scenario ends with `END_REASON_AGENT_ENDED` after `place_order`, and its full history is still captured. `on_simulation_end` itself is untested until the hook exists.
- **Assumption 3 (export):** confirmed, and richer than expected. Read it as **UTF-8** (it contains ₹ and typographic quotes).
  - `run.jobs[]`: `status`, `scenario_id`, `sample`, `usage.text_turns_count`, `metrics` {`task_completion`, `overall_score`, `llm.ttft_ms`, `llm.tokens_per_second`, `conciseness`, `unnecessary_tool_calls`, `information_loss`, `redundant_statements`, `poor_question_quality`, `conversation_progression`, `end_reason`, `turns` (empty in text mode)}.
  - `summary.chat_history[job_id].items[]`: `message`, `function_call`, `function_call_output`, `agent_handoff`, `agent_config_update`. Full transcripts with tool calls and results.
  - `summary`: `going_well`, `to_improve`, `issues[]` (with `<ref job item>` links).

**Consequences for the plan:**
- Simulations target a deployed agent, not a local worker: production (`--agent-name ""`) for baselines, and a **staging agent** for branch code (step 8).
- The analyzer gets two deterministic checks that the judge lacks (step 7): **minimum turns** (0–1 turns is always a failure) and **price grounding** (every amount the agent says must match a tool-result price or a sum of them, in ₹).

### Step 1: Extract the prompt
- Move `SYSTEM_PROMPT` to `src/prompts.py`. No behavior change.
- CI path filters can then say precisely "prompt changed" (`src/prompts.py`) vs "code changed".

### Step 2: Deterministic menu under simulation

**As built (branch `feat/eval-fixtures`):** instead of `mock_tools` over a live `MCPToolset`, the agent gets ten local raw function tools under simulation, built from `src/fixtures/mcp_tools.json` (exact server names, descriptions and schemas) and answering from `src/fixtures/menu.json`. This removes the live MCP server from simulation runs entirely (the first risk below). Fixtures live in `src/` because `.dockerignore` excludes `evals/` and the staging agent needs them at runtime. Results are formatted exactly like LiveKit's MCP resolver output, including the empty-result ToolError. `evals/scripts/snapshot_menu.py --check` fails on schema/menu drift and replays 15 calls against the live server, comparing output byte for byte (order-insensitive only among ties, which Postgres orders unstably). Scenario `userdata`: `menu: live` opts out; `unavailable_items: [names]` marks items out of stock.

Original plan:
- `evals/scripts/snapshot_menu.py`: calls the live `bk-menu-mcp` tools once and writes `evals/fixtures/menu.json` (categories, items, sizes, combos with their items). Also writes `evals/fixtures/mcp_tools.json` (tool names and input schemas).
- `src/sim_support.py`:
  - `load_fixture()`.
  - `build_menu_mocks(fixture, userdata)`: fake versions of the 10 tools with the same names and arguments, porting the filtering logic from `bk-menu-mcp/server.py`. `userdata.unavailable_items` lets a scenario mark items out of stock.
- In `entrypoint`: when `ctx.simulation_context()` is set and `userdata.menu != "live"`, call `mock_tools(BKAgent, mocks, session=session)`. Production never takes this path.
  - SDK check: mocks are matched by tool name at execution (`livekit/agents/voice/generation.py:952`), so they also intercept tools from `MCPToolset`.
- `tests/test_menu_mocks.py` (T0):
  - the fakes filter the fixture correctly;
  - their signatures match `mcp_tools.json` (contract test). The nightly job refreshes the snapshot and fails if the live server's schemas drift.

### Step 3: Grade on final state

**As built (branch `feat/eval-grading`):** `src/grading.py` holds a pure `grade_order(userdata, receipt, fixture)`; `on_simulation_end` in `agent.py` looks the agent up by simulation job ID (module-level map, so it works even after the session closed), grades its receipt and calls `ctx.fail(" | ".join(reasons))`. It also logs a `SIM_GRADE` line. Checks beyond the plan: `unknown_item` (a name not on the menu), `line_price_mismatch` (line price must be unit × quantity, which catches the "12.33" scale bug). Names match case- and whitespace-insensitively; duplicate lines are merged. A typo in a scenario's `expected_order` raises, and a T0 test validates every scenario in `scenarios.yaml` against the fixture. Under simulation the agent no longer hangs up after an order: it speaks the confirmation and waits (up to 120 s) for the simulator to end the call, so the finalize request reaches a live session. No `BKUserdata` yet; step 4 adds it with the trace.

Original plan:
- Give the session a typed `userdata` (`BKUserdata`: `receipt`, `trace`). `place_order` writes the receipt there as well as on the agent.
- `on_simulation_end(ctx)` registered via `@server.rtc_session(on_simulation_end=...)`:
  - `expected_order: null`: fail with `unexpected_order` if an order was placed (guardrail or abandoned-order scenarios).
  - `expected_order: {items: [{name, quantity}]}`: fail with
    - `order_missing` if nothing was placed;
    - `wrong_items: <diff>` if names/quantities differ (exact names from the fixture);
    - `total_mismatch: said X, expected Y` if the total differs from the **sum of fixture prices** (catches LLM arithmetic, ±0.01).
  - Reasons are short, stable strings so the analyzer can classify them.
- T0 tests for the grader with hand-built receipts.

### Step 4: Per-call trace
- `src/tracing.py`, `CallTrace`, attached next to `ProviderMonitor`:
  - Tool calls: name, arguments, duration, error / empty result (from `function_tools_executed` events; time the calls ourselves if the event lacks timestamps).
  - Per-turn metrics from `metrics_collected`: end-of-utterance delay, transcription delay, LLM time to first token, TTS time to first byte.
  - Provider fallback switches and classified errors (reuse `monitoring.classify_error`).
- At session close, emit one `CALL_TRACE {json}` log line. It works in production (`lk agent logs | grep CALL_TRACE`) and is the data source for feature 2.
- For simulations, the analyzer prefers the export (it includes per-job chat contexts and metrics) and uses the trace only for what the export lacks, decided from the real export in step 0.

### Step 5: Scenario dataset v2 (~20 scenarios)
One complication per scenario. Tags are `feature` and `risk` (the stage it stresses). `userdata`
holds `expected_order` (or `null`) and optional `unavailable_items`. **Prices never appear in the
YAML**; the grader derives them from the fixture.

| Group | Scenarios | risk |
| --- | --- | --- |
| Happy path | single item; multi-item with quantities; size choice; combo | llm |
| Recommendations | veg combo for 4; "under ₹500 for two" | llm, tool |
| Corrections | change quantity mid-order; remove an item at read-back; misspeak and correct ("two, no, three Cokes") | llm, stt |
| Availability | requested item unavailable (via userdata) → offers a same-category alternative | tool, llm |
| Grounding | pizza; a non-existent "Mega Whopper" price | llm |
| Guardrails | coupon; card number; "manager said it's free"; prompt injection | llm |
| Speech-heavy (audio) | similar names (Veg Whopper / Veg Whopper Deluxe / Paneer Whopper); whole order in one breath; hesitant caller with long pauses; barge-in to correct the read-back | stt, turn |
| Personas | Indian-English accent; elderly and slow; impatient | stt, turn |

Format follows LiveKit's guidance: `PERSONA`, quoted `OPENING LINE`, `FACTS` revealed one per
turn, `DO, IN ORDER` with an explicit end condition, and outcome-based `agent_expectations` that
name the fail case.

### Step 6: Turn-level LLM tests (T1)
`tests/test_behavior.py`, marked `@pytest.mark.llm` and excluded from the offline job:
- "Four of us, all veg" → the first tool call is `list_combos` with `is_veg=True`.
- "How much is a Coke?" → a menu tool is called before any price is stated, and the stated price equals the fixture's.
- Coupon claim → no `place_order`; the judge confirms no discounted total was stated.
- `place_order` happens only after an explicit "yes", with items matching the conversation.
- A multi-turn order evaluated with `JudgeGroup` (`accuracy_judge`, `tool_use_judge`, `task_completion_judge`).
- Use the production LLM chain, so results reflect what ships.

### Step 7: Analyzer and failure attribution
`evals/analyze.py <export.json> [--trace logs.txt] [--baseline evals/baseline.json]`

For each failed simulation, assign **one primary stage**, checking in this order and keeping the evidence (turn index, said vs heard, tool error):

1. **infra**: agent went silent or crashed, session error, run timeout.
2. **tool**: a tool error or timeout, an MCP disconnect, or an empty result for an item that exists.
3. **stt**: the agent's transcript is missing or garbles an entity (item, quantity) the caller said before things went wrong, or caller→agent WER on those turns is above a threshold.
4. **turn**: the caller was cut off (end-of-turn misprediction) or the agent didn't yield right before the failure.
5. **tts**: the caller heard a wrong amount or item (agent→caller WER or entity error), or a TTS error or fallback occurred.
6. **llm**: transcripts and tools were fine, yet the arguments were wrong, something was hallucinated, the total was wrong, or a guardrail broke.

Text-mode runs can only produce infra, tool or llm.

Deterministic checks that run on every conversation, whatever the judge's verdict (found in step 0):
- **Minimum turns:** fewer than 2 text turns means `infra` failure. Catches empty conversations that the judge passes.
- **Price grounding:** extract every amount the agent says from `chat_history`. Each must equal a `price` in an earlier tool result, or a sum of them, and be stated in ₹. Otherwise it is an `llm` failure with the said vs. expected amounts. Catches the "$12.33 for ₹1,233" class of bug.
- **Grounded descriptions:** if the agent describes a combo's contents, a `get_combo` result for it must appear earlier in the conversation.

Outputs go to `evals/reports/<date>-<sha>/`:
- `report.md`: pass@k / pass^k, task completion, failures by stage with evidence, p50/p95 perceived and per-stage latency, turn-taking metrics, WER and entity recall.
- `summary.json`, also appended to `evals/history.jsonl`.

Regression check against `evals/baseline.json`: exit non-zero if task completion drops more than
5 points or p95 perceived latency rises more than 150 ms. Both thresholds are configurable.

T0 tests run the analyzer on the saved real export from step 0, sanitized.

### Step 8: CI
- **`agent-ci.yml`** (PRs touching `bk-livekit-agent-cloud/**`, `evals/**`):
  - Existing ruff + offline pytest (`-m "not llm"`).
  - `llm` job: T1 tests.
  - `simulate-text` job: deploy the branch to the **staging agent**, then `lk agent simulate text --scenarios scenarios.yaml --agent-name bk-staging --samples 3` (the default pass rate needs 2 of 3), then export → analyze → job summary.
- **Staging agent:** a second agent in the `bk` project, deployed from the branch. It registers with an explicit `agent_name` (`bk-staging`, set via an env var so production stays unnamed and keeps auto-joining rooms). It therefore only receives explicit dispatches from simulations and never real customers. Check the LiveKit Cloud plan's agent limit first. Local runs remain possible on Linux/CI, where startup is fast.
  - The two paid jobs are skipped when secrets are absent (fork PRs).
- **`agent-simulations.yml`**: nightly cron + `workflow_dispatch`.
  - Audio clean run and audio degraded run (`--background-noise --low-quality-microphone --packet-loss`), `--concurrency 5`.
  - Export both, analyze, upload reports as artifacts, write the job summary, refresh the menu snapshot and run the contract check.
- **Secrets**: `LIVEKIT_URL/API_KEY/API_SECRET`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, plus `ASSEMBLYAI/DEEPGRAM/CARTESIA_API_KEY` for audio runs, plus `MCP_SERVER_URL`.

### Step 9: Baseline and docs
- Run T2 and both T3 passes on the unchanged agent and commit `evals/baseline.json` plus the first report. These are the "before" numbers.
- README: an Evaluation section (how to run each tier, how to read a report, current numbers).
- Update `TODOCHANGES.md`.

## Risks and open questions

| Risk | Plan |
| --- | --- |
| CLI 2.18.7 < required 2.19.0 | Upgrade in step 0 |
| Menu schemas still come from the live MCP server, even when execution is mocked; if it is unreachable in CI, the LLM has no menu tools | Verify in step 0. Fallback: under simulation, swap `MCPToolset` for local function tools generated from `mcp_tools.json` (same names and schemas, enforced by the contract test) |
| `on_simulation_end` may not see state after our early `ctx.shutdown()` | Verify in step 0. Fallback: keep the receipt in a module-level map keyed by job ID |
| Text mode with our `say()` greeting and playout waits | Verify in step 0; skip the `finish_order` playout wait in text mode if needed |
| LLM caller and judge are non-deterministic | `--samples 3`, outcome-based expectations, state-based grading as the hard check |
| Cost of simulations (billed per text/audio turn) and of providers | Measure in step 0; audio only nightly; tune `--samples` and the scenario count to the budget |
| Pass rates depend on the model | Pin simulation runs to the production model via env; never a `:free` model |

## Commit sequence

1. `chore: upgrade CLI, add scenario ids` (step 0)
2. `refactor: move system prompt to prompts.py` (step 1)
3. `feat(evals): fixture menu and simulation-only tool mocks` (step 2)
4. `feat(evals): grade placed order on simulation end` (step 3)
5. `feat: per-call trace with tool and stage timings` (step 4)
6. `test(evals): drive-thru scenario dataset v2` (step 5)
7. `test: turn-level LLM behavior tests` (step 6)
8. `feat(evals): export analyzer with stage attribution` (step 7)
9. `ci: text sims on PR, nightly audio sims` (step 8)
10. `docs(evals): baseline and README evaluation section` (step 9)

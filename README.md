<div align="center">

# 🍔🔊 DriveThruAI
### *The BK Voice Agent that thinks before it talks*

**A voice-native drive-thru ordering agent that queries only what it needs —**
**max-info-gain slot filling, composite-key combo modeling, and a fully async stack.**

<br/>

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://neon.tech/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0_async-D71F00?style=for-the-badge&logo=python&logoColor=white)](https://www.sqlalchemy.org/)
[![Alembic](https://img.shields.io/badge/Alembic-migrations-6BA81E?style=for-the-badge&logo=alembic&logoColor=white)](https://alembic.sqlalchemy.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-agent_orchestration-1C3C3C?style=for-the-badge&logo=langchain&logoColor=white)](https://langchain-ai.github.io/langgraph/)
[![Redis](https://img.shields.io/badge/Redis-Upstash-DC382D?style=for-the-badge&logo=redis&logoColor=white)](https://upstash.com/)
[![arq](https://img.shields.io/badge/arq-async_workers-FF6F00?style=for-the-badge&logo=python&logoColor=white)](https://arq-docs.helpmanual.io/)
[![asyncpg](https://img.shields.io/badge/asyncpg-driver-336791?style=for-the-badge&logo=postgresql&logoColor=white)](https://magicstack.github.io/asyncpg/)

[![Pipecat](https://img.shields.io/badge/Pipecat-voice_pipeline-8A2BE2?style=for-the-badge)](https://www.pipecat.ai/)
[![LiveKit](https://img.shields.io/badge/LiveKit-voice_pipeline-FF3B00?style=for-the-badge&logo=livekit&logoColor=white)](https://livekit.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](./LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=for-the-badge)](https://github.com/singhsrj/DriveThruAI---BK-VoiceAgent--/pulls)

<br/>

[**Quick Start**](#setup) · [**Architecture**](#architecture) · [**Core Ideas**](#core-ideas) · [**Schema**](#database-schema) · [**Roadmap**](#roadmap)

</div>

<br/>

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [Core ideas](#core-ideas)
  - [Max-info-gain slot filling](#1-max-info-gain-slot-filling)
  - [Composite-key combos](#2-composite-key-combos-veg--non-veg-as-first-class-variants)
  - [Redis caching layer](#3-redis-caching-layer)
  - [Async everything](#4-async-everything)
  - [Background workers](#5-background-workers)
- [Tech stack](#tech-stack)
- [Database schema](#database-schema)
- [Project structure](#project-structure)
- [Setup](#setup)
- [Environment variables](#environment-variables)
- [Running locally](#running-locally)
- [Testing](#testing)
- [Roadmap](#roadmap)
- [License](#license)

---

## Why this exists

Most "AI drive-thru" demos work by stuffing the whole menu into a system prompt and hoping the model picks the right combo of items out of a hundred-line wall of text. That's slow, expensive, and gets worse the bigger the menu gets.

DriveThruAI flips the order of operations: **ask first, query second.** A LangGraph state machine fills a small set of constraint slots (budget, veg/non-veg preference, party size) by asking whichever question narrows the menu the most — measured against the real database, not guessed — and only queries Postgres once it has enough to return a tight, relevant candidate set. The LLM downstream sees 5–10 rows, not the whole catalog, every single turn.

## Architecture

```mermaid
flowchart TB
    subgraph Voice["Voice Layer (Pipecat / LiveKit)"]
        STT[Speech-to-Text]
        TTS[Text-to-Speech]
    end

    subgraph Agent["LangGraph Order Agent"]
        Route{Enough info<br/>to query?}
        Ask[Ask next best<br/>question]
        InfoGain[Max-info-gain<br/>slot selector]
        Query[Query candidate<br/>items + combos]
        Present[Present options]
    end

    subgraph Data["Data Layer"]
        Cache[(Redis Cache)]
        DB[(Neon Postgres<br/>via async SQLAlchemy)]
    end

    subgraph Background["Background Workers"]
        Worker[arq worker]
        Jobs[confirm_order /<br/>recompute_combo_prices]
    end

    STT --> Route
    Route -->|no| Ask
    Ask --> InfoGain
    InfoGain -.->|checks candidate<br/>counts| DB
    Ask -->|interrupt, wait for answer| STT
    Route -->|yes| Query
    Query --> Cache
    Cache -.->|cache miss| DB
    Query --> Present
    Present --> TTS
    Present -.->|order confirmed| Worker
    Worker --> Jobs
    Jobs --> DB
```

## Core ideas

### 1. Max-info-gain slot filling

Instead of a scripted question order, the agent measures — against the live database — how much each unfilled slot (`veg_pref`, `budget_total`, `num_people`) would shrink the candidate item set if answered, and asks whichever one shrinks it the most. Concretely: for each candidate answer to a slot, count how many items would remain, average across answers, and compare that to the current count. The slot with the biggest expected drop gets asked first.

This means the question order isn't fixed — it adapts to what's actually on the menu. Add or remove items and the agent re-derives which question is most useful, without anyone touching the prompt.

A hard cap (2 questions) prevents interrogating the customer forever if answers are vague or slots keep tying.

### 2. Composite-key combos: veg / non-veg as first-class variants

Combos aren't just "an item bundle with a price." Every combo *family* (Classic Combo, Value Combo, Feast Combo, Party Combo) exists as **two rows sharing one `id`**, differentiated by `is_veg` — a genuine composite primary key `(id, is_veg)` — rather than treating veg and non-veg versions as unrelated menu entries. `combo_items` carries the matching composite foreign key, so each variant resolves to its own correct set of items (a non-veg Feast Combo can never accidentally serve a veg-flagged burger — this is enforced at the schema level, not just in application code).

Combos also scale by `num_people`: item quantities and price scale together, generated programmatically from real item prices with a bundle discount applied.

### 3. Redis caching layer

The same Redis instance backing the job queue also caches the results of candidate-item/candidate-combo lookups. Menu data changes rarely — item prices and availability don't shift mid-shift — so once a constraint combination (`veg_pref=nonveg, budget_total=200`) has been queried, the filtered result is cached with a short TTL. Repeat callers asking similar questions (a very common pattern at a real drive-thru — "cheap non-veg combo" is asked constantly) skip the database entirely and hit Redis, cutting both latency and Postgres load during peak order volume. Cache keys are derived from a hash of the filled constraint slots, so different customers with the same constraints share a cache entry; any admin-side price or availability update invalidates the relevant keys rather than waiting out the TTL.

### 4. Async everything

Every layer — database access, ORM sessions, the LangGraph nodes, Alembic migrations themselves — is async. `asyncpg` instead of `psycopg2`, `AsyncSession` instead of `Session`, `app.ainvoke()` instead of `app.invoke()`. A voice agent lives or dies on latency, and a single blocking DB call in a sync codepath stalls every concurrent call on the line. Alembic's core migration runner is sync by design, so migrations bridge into the async engine via SQLAlchemy's `run_sync()` pattern rather than falling back to a separate sync driver.

### 5. Background workers

Anything that doesn't need to happen before the agent can keep talking gets handed off to an `arq` worker — order confirmation (SMS/kitchen display/printer webhook), periodic combo price recalculation, and similar. `arq` was chosen over Celery specifically because it's natively `asyncio`-based: worker jobs `await` the exact same `AsyncSession` code the rest of the app uses, with no sync/async bridging or extra event-loop tricks.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Database | [Neon](https://neon.tech) (serverless Postgres) | Branching, autoscaling, generous free tier |
| ORM | SQLAlchemy 2.0 (async) | Composite keys, typed models, mature async support |
| Migrations | Alembic (async) | Standard, autogenerate-capable, version-controlled schema |
| Driver | `asyncpg` | Fastest async Postgres driver for Python |
| Agent orchestration | LangGraph | Native support for interrupts, checkpointed state, conditional routing — a real fit for slot-filling dialogue |
| Cache + queue broker | Redis (Upstash) | One instance, two jobs: response caching and `arq`'s job queue |
| Background jobs | `arq` | Natively async, no sync/async bridging |
| Voice I/O | Pipecat / LiveKit Agents | STT/TTS pipelines, barge-in, turn-taking |

## Database schema

```mermaid
erDiagram
    categories ||--o{ items : contains
    items ||--o{ item_sizes : has
    items ||--o{ combo_items : "included in"
    combos ||--o{ combo_items : contains

    categories {
        int id PK
        string name
    }
    items {
        int id PK
        string name
        int category_id FK
        float price
        int calories
        float protein_g
        int piece_count
        bool is_veg
        bool is_available
    }
    item_sizes {
        int id PK
        int item_id FK
        string size
        float price
    }
    combos {
        int id PK "composite w/ is_veg"
        bool is_veg PK "composite w/ id"
        int num_people
        string name
        float price
        bool is_available
    }
    combo_items {
        int combo_id PK
        bool combo_is_veg PK
        int item_id PK
        int quantity
    }
```

`combos(id, is_veg)` and `combo_items(combo_id, combo_is_veg, item_id)` are genuine composite keys enforced by Postgres — not just convention. See [`test_schema.py`](./test_schema.py) for the checks that prove this (duplicate `(id, is_veg)` pairs are rejected, veg/non-veg item sets never cross-contaminate, combo prices scale correctly with `num_people`).

## Project structure

```
bk_neon/
├── database.py            # async engine/session, Neon URL normalization
├── models.py               # SQLAlchemy models (composite-key combos included)
├── constraints.py          # slot schema for the ordering agent
├── info_gain.py            # max-info-gain question selector
├── query_builder.py        # async candidate-item/combo queries
├── cache.py                 # Redis cache-aside layer for candidate queries
├── agent_graph.py           # async LangGraph ordering state machine
├── seed.py                  # idempotent async data seeding
├── worker.py                 # arq background worker (order confirmation, price sync)
├── enqueue_test_job.py      # example: enqueue a job onto the worker
├── requirements.txt
├── alembic.ini
└── alembic/
    ├── env.py                # async migration runner
    └── versions/
        └── ..._initial_schema.py
```

## Setup

### 1. Prerequisites
- Python 3.12+
- A [Neon](https://neon.tech) Postgres project (grab both the **pooled** and **direct** connection strings)
- A [Upstash](https://upstash.com) Redis database (grab the **TCP** `rediss://` connection string — *not* the REST `https://` one, they're different products)

### 2. Install

```bash
git clone https://github.com/singhsrj/DriveThruAI---BK-VoiceAgent--.git
cd DriveThruAI---BK-VoiceAgent--
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment

Copy `.env.example` → `.env` and fill in:

```dotenv
DATABASE_URL=postgresql://user:pass@ep-xxx-pooler.region.aws.neon.tech/dbname?sslmode=require
DATABASE_URL_DIRECT=postgresql://user:pass@ep-xxx.region.aws.neon.tech/dbname?sslmode=require
REDIS_URL=rediss://default:password@your-db.upstash.io:6379
```

`database.py` and `alembic/env.py` auto-normalize Neon's default `sslmode=`/`channel_binding=` query params into what `asyncpg` actually expects — paste Neon's connection string as-is, no manual edits needed.

### 4. Create the schema and seed data

```bash
alembic upgrade head
python seed.py
```

## Environment variables

| Variable | Used by | Notes |
|---|---|---|
| `DATABASE_URL` | App runtime | Use the **pooled** Neon connection string |
| `DATABASE_URL_DIRECT` | Alembic migrations | Use the **direct** (non-pooled) string — DDL through a pooler can deadlock |
| `REDIS_URL` | `arq` worker + cache layer | `rediss://` TCP protocol, not Upstash's REST API |

## Running locally

**Start the worker** (separate terminal — this is a standalone service, not part of the request path):
```bash
arq worker.WorkerSettings
```

**Run the agent:**
```bash
python agent_graph.py
```

**Enqueue a test background job:**
```bash
python enqueue_test_job.py
```

## Testing

```bash
python test_schema.py
```

Runs the full pipeline against a throwaway database and checks, among other things:
- All expected tables/columns exist
- The `(id, is_veg)` composite PK on `combos` is actually enforced (not just present)
- Every generated combo has exactly one veg + one non-veg variant
- No non-veg combo variant ever resolves to a veg-flagged item
- Combo price strictly increases with `num_people`
- Realistic query patterns (budget filters, protein filters, group orders) return sane results

## Roadmap

- [ ] Wire `present_options` to a live LLM call (currently a stub that just returns the candidate set)
- [ ] Full cart/order-total tracking in `AgentState` so the graph loops through a full order instead of stopping after one query
- [ ] Real order persistence (`orders` table) instead of the current stubbed `confirm_order` job
- [ ] Cache invalidation hooks on item/price updates instead of TTL-only expiry
- [ ] Pipecat/LiveKit integration examples for full voice I/O

## License

MIT

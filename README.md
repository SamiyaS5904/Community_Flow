# CommunityFlow

**A multi-tenant AI content engine that plans, writes, designs and publishes
educational posts for Telegram communities — automatically, on a schedule, with one
human approval.**

---

## Use case

An education business runs several Telegram communities. Each needs a few posts a day,
in its own voice, on subjects it has not already covered — many of them as designed
graphics rather than plain text. By hand that is a full-time job per community.

CommunityFlow runs all of them from one codebase. A new community is two configuration
files and an environment variable: no Python, no new templates.

---

## Problems it solves

| Problem | How |
|---|---|
| **Topics repeat.** A fixed calendar replays, and a model asked for "a topic about interviews" returns the same handful in different words. | A living topic pool. Every candidate is embedded and compared against everything already scheduled or published; ≥ 0.86 cosine similarity is rejected. Two consecutive 77-slot cycles were generated with zero overlap. |
| **Topics go stale.** Someone has to keep thinking of new ones. | Scheduled web discovery admits new topics automatically, constrained by per-community editorial guardrails rather than by manual approval. |
| **Every community sounds the same.** | Voice, audience and guardrails are per-tenant configuration, not a prompt convention. No group name, colour or CTA exists anywhere in Python. |
| **Every community looks the same.** | Five shared HTML archetypes, themed per tenant from `config.yaml` as design tokens. Different palettes and fonts, zero template copies. |
| **Failures are silent.** A failed render or a rate-limited API produces a post that never publishes. | Every failure is a post state visible in the dashboard, with the real reason attached. |
| **Posts miss their slot.** | A stateless reconciler polls the database every 30 seconds and sends what is due. Restarts lose nothing. |

---

## Stack

| Layer | Technology | Why |
|---|---|---|
| Language | Python 3.11+ | — |
| Web / UI | Flask + Jinja2, Bootstrap 5 (self-hosted), vanilla JS | No build step; the tool works offline |
| Content generation | OpenAI `gpt-4o-mini` | Seven prompt-chained agents, per-agent token and temperature budgets |
| Deduplication | OpenAI `text-embedding-3-small` (1536-dim) | Catches rephrased duplicates that string matching cannot |
| Database | PostgreSQL + `pgvector`, SQLAlchemy 2.0, psycopg 3 | Indexed similarity search; survives ephemeral container storage |
| Rendering | Playwright / headless Chromium | Real CSS layout and typography; PNG and PDF from one source, no API cost |
| Web search | Serper | Research briefs and topic discovery |
| Publishing | Telegram Bot API | — |
| Scheduling | APScheduler + Postgres advisory lock | One reconciler across all workers |
| Testing | pytest — 159 tests | Runs without live API keys |

---

## Main flow

```
  groups/<id>/config.yaml + strategy.json        ← the only per-community input
        │
        ▼
  1. PLAN        Posting rhythm (strategy) + Topic Pool
                 → a 15-day Cycle Plan of slots, each with a topic
        │
        ▼
  2. GENERATE    Planner → Researcher → Writer → QA Editor
                         → PDF Writer → Asset Planner → Asset Mapper
        │
        ▼
  3. RENDER      HTML archetype + tenant design tokens
                 → headless Chromium → PNG 1080×1350  |  multi-page PDF
        │
        ▼
  4. REVIEW      Dashboard — edit the copy and the points, adjust the
                 design live, then approve.        ← the one human step
        │
        ▼
  5. PUBLISH     A reconciler polls Postgres every 30s and delivers
                 each post to its own community's chat, on time.
```

**Post lifecycle**

```
draft ─► rendering ─┬─► asset_failed
                    └─► needs_review ─► approved ─► publishing ─┬─► published
                            │                                   └─► publish_failed
                            └────────► rejected
```

A post reaches `approved` only from `needs_review`, which it reaches only once every
declared asset exists — so "approved before its image finished rendering" is
unrepresentable rather than merely unlikely.

---

## Running it

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env          # OPENAI_API_KEY, DATABASE_URL, TELEGRAM_BOT_TOKEN, …

python -m engine.health       # checks every dependency before booting
python run.py                 # dashboard on :5000
pytest                        # 159 tests
```

---

Design decisions and the reasoning behind them are recorded in the commit history —
each structural change documents the defect that motivated it.

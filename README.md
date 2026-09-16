# PolicyRadar · Policy Intelligence Agent

> Policy collection for enterprises → AI scoring and cleaning → intelligent report generation

## Project Structure

```
policy-radar/
├── app/
│   ├── main.py                # FastAPI entrypoint (port 8001)
│   ├── config.py              # Settings (fully isolated from ShillGuard)
│   ├── db/
│   │   ├── init.py            # Schema setup (pgvector extension, 5 tables)
│   │   ├── postgres.py        # asyncpg connection pool
│   │   └── schemas.py         # Pydantic data models
│   ├── agents/
│   │   └── report/
│   │       ├── graph.py       # LangGraph report agent (retrieve → format → stream)
│   │       └── prompts.py     # System prompts
│   ├── crawler/
│   │   ├── base.py            # Crawler base class (Playwright + LLM extraction)
│   │   ├── sources.py         # Site crawlers (MIIT / MOST / MOF)
│   │   └── scheduler.py       # APScheduler job (daily at 03:00)
│   ├── rag/
│   │   ├── es_client.py       # Elasticsearch BM25 retrieval
│   │   └── retriever.py       # Hybrid retrieval (pgvector + BM25 + RRF)
│   └── api/
│       ├── categories.py      # Category tree API
│       ├── policies.py        # Policy list / detail API
│       ├── report.py          # Report generation (SSE streaming)
│       └── crawl.py           # Manual crawl trigger
├── scripts/
│   └── seed_data.py           # Seed category tree + sample policies
├── requirements.txt
├── .env.example
└── README.md
```

## Quick Start

### 1. Create the PostgreSQL database

```sql
CREATE DATABASE policy_radar;
-- Switch to the policy_radar database
\c policy_radar
CREATE EXTENSION IF NOT EXISTS vector;
```

### 2. Install dependencies

```
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure environment variables

```
copy .env.example .env
```

Edit `.env` and fill in the LLM API key, PostgreSQL password, and other secrets.

### 4. Initialize the database and seed data

```
python scripts/seed_data.py
```

### 5. Start the service

```
python -m app.main
```

The service listens on http://localhost:8001. Swagger docs: http://localhost:8001/docs

## API Endpoints

| Method | Path | Description |
|------|------|------|
| GET  | /api/v1/categories | Full category tree (4 levels) |
| GET  | /api/v1/policies | Policy list (filter by region / l1 / l2) |
| GET  | /api/v1/policies/{id} | Policy detail |
| POST | /api/v1/report/generate | Stream a generated report (SSE) |
| POST | /api/v1/crawl/trigger | Trigger a crawl job manually |
| GET  | /health | Health check |

## Isolation from ShillGuard

| Resource | ShillGuard | PolicyRadar | Isolation |
|------|-----------|-------------|---------|
| Service port | 8000 | 8001 | Different ports |
| PostgreSQL database | shillguard | policy_radar | Different databases |
| Elasticsearch index | shillguard_kg | policy_radar_* | Different prefixes |
| Redis database | db=0 | db=1 | Different DB numbers |
| Python environment | agent venv | can reuse agent venv | Shared |

## Frontend Access

In the `shill-guard-frontend` project, open the admin sidebar group **Policy Intelligence** and click **Policy Intelligence Agent**.

Vite proxy: `/policy-api/*` → `http://localhost:8001/*`

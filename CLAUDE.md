# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Pre-implementation: the only source of truth is `PRD.md` (Order-to-ERP Integration Simulator, a portfolio project). Treat it as authoritative, implement its milestones (§10) **in order**, and don't build "future enhancements" (§13) until MVP acceptance criteria (§11–12) pass. Before each milestone, state the files to change and the verification commands; after it, run tests + lint and summarize what works and what remains. When something is underspecified, pick the simplest safe approach and document the tradeoff in README.

## Commands (planned stack: Python 3.12 + uv)

```bash
docker compose up -d                       # PostgreSQL 16 + Redis 7 (bind to 127.0.0.1 only)
uv run alembic upgrade head                # apply migrations
uv run uvicorn app.main:app --reload --port 8000        # Orders API
uv run uvicorn mock_erp.main:app --reload --port 8001   # Mock ERP (separate app)
uv run celery -A app.workers.celery_app:celery_app worker --loglevel=info

uv run pytest                              # all tests
uv run pytest tests/test_worker.py::test_name   # single test
uv run ruff check .                        # lint (must pass along with pytest)
```

Config comes from `.env` via pydantic-settings; `.env.example` must define `DATABASE_URL`, `CELERY_BROKER_URL`, `ERP_BASE_URL`, `ERP_HTTP_TIMEOUT_SECONDS`, `ERP_MAX_ATTEMPTS`, and the mock ERP failure mode.

## Architecture

Three processes sharing PostgreSQL: **Orders API** (FastAPI) → persists order as `PENDING`, enqueues a Celery task, returns `202` → **Celery worker** (Redis broker) claims the order and calls → **Mock ERP** (FastAPI, own `erp_orders` table). Order status is always read from the DB, never from the Celery result backend.

Layering (see PRD §8): `app/api` (routes) → `app/services` (business logic) → `app/models` (SQLAlchemy) / `app/schemas` (Pydantic v2); `app/integrations/erp_client.py` (HTTPX) and `app/workers` (Celery) are kept separate from routes.

Key design rules that span multiple modules:

- **Sync everywhere**: synchronous SQLAlchemy 2.x sessions (psycopg 3) and `httpx.Client`. Don't call blocking DB/HTTP from `async def` endpoints.
- **Status machine**: `PENDING → PROCESSING → SUCCESS | FAILED | UNKNOWN | PENDING` (PRD §4). `SUCCESS` is terminal; manual retry (`POST /orders/{id}/retry`) is only allowed from `FAILED`/`UNKNOWN`, else `409`.
- **Tasks take only an order ID** and reload from the DB. Claim work via conditional status update or row lock so concurrent tasks/retries can't send twice.
- **Stable `external_order_id`** is sent on every ERP attempt and never reset; the mock ERP is idempotent on it (same payload → same ERP order, different payload → `409`).
- **Error classification**: 429/503 → transient, retry with exponential backoff + jitter, bounded by `ERP_MAX_ATTEMPTS`; 400/422 → `FAILED`, no auto-retry; timeout/connection loss → `UNKNOWN`.
- **Never retry an uncertain side effect without reconciliation**: `orders.needs_reconciliation` is committed `True` *before* every ERP create call (write-ahead intent) and cleared only on a certain outcome (success, 4xx, 429/503/connect error). When it is set, the worker first does `GET /erp/orders/by-external-id/{id}`: found+same data → `SUCCESS`, found+different → `FAILED`, absent → create, lookup error → stay `UNKNOWN`. Manual retry leaves the flag untouched. Retry scheduling is represented by status (`PENDING` = certainly not processed, `UNKNOWN` = maybe); the task re-enqueues itself with `countdown` from `ProcessOutcome.retry_in`. An uncertain outcome is never turned into `FAILED`, even when the budget is exhausted.
- **Client idempotency**: optional `Idempotency-Key` header, unique at DB level, paired with a `request_hash` of the normalized payload. Same key + same payload → original order (no second task); different payload → `409`.
- **Mock ERP failure modes** (dev-only): `normal`, `http_503`, `http_429`, `http_400`, `timeout_before_create`, `timeout_after_create` (must commit the ERP order *before* sleeping past the client timeout).
- Known MVP limitation to document, not silently "fix": DB commit and Celery publish aren't atomic (no outbox), so a crash can strand a `PENDING` order. Don't claim exactly-once.
- Structured logs include `order_id`, `external_order_id`, `attempt`, `event`, error category; `last_error` is sanitized; never log secrets.

## Testing

`tests/conftest.py` creates a separate `<db>_test` database, rebuilds it with Alembic (`downgrade base` + `upgrade head`) once per session, and `TRUNCATE`s tables before each test; `test_migrations.py` runs `alembic check` so model changes without a migration fail. Ruff is configured to treat `alembic` as third-party (the local `alembic/` folder would shadow it). ERP client unit tests use `httpx.MockTransport`; integration tests use the `erp_base_url` fixture, which serves the real mock ERP via uvicorn in a background thread (real sockets and timeouts), and `erp_failures` to set failure modes. Fixtures that touch `app.dependency_overrides` / `erp_app.dependency_overrides` must restore previous overrides, not just clear them (the session-scoped server relies on its own). `tests/test_end_to_end.py` starts a real Celery worker in-process with the **configured** pool (`threads`; don't switch tests to `solo`, it once hid a prefork-only crash) on Redis db 15, and the `client` fixture replaces task publishing with a recording fake (`dispatched`). Mock outbound HTTP in unit tests; integration/end-to-end tests run against the Docker services. Use short deterministic timeouts and tiny retry delays to keep the suite fast. The required test cases are enumerated in PRD §11.

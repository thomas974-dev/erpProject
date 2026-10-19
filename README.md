# Order-to-ERP Integration Simulator

Work in progress — see `PRD.md`.

## Local setup (macOS)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker Desktop.

```bash
cp .env.example .env
uv sync
docker compose up -d          # PostgreSQL 16 + Redis 7 on 127.0.0.1
docker compose ps             # wait until both are "healthy"
uv run alembic upgrade head   # create/upgrade the schema

uv run ruff check .
uv run pytest                 # unit + integration tests (uses a separate `orders_test` DB)
uv run pytest -m "not integration"   # without Docker
```

Run the API: `uv run uvicorn app.main:app --reload --port 8000`, then open
http://127.0.0.1:8000/docs (Swagger UI).

Run the mock ERP: `uv run uvicorn mock_erp.main:app --reload --port 8001`
(docs at http://127.0.0.1:8001/docs).

Run the worker: `uv run celery -A app.workers.celery_app:celery_app worker --loglevel=info`
(thread pool by default: tasks are I/O-bound, and Celery's prefork pool currently fails on
macOS because billiard starts children with `spawn` there).

## Order lifecycle

```
PENDING ──claim──▶ PROCESSING ──▶ SUCCESS                       (terminal)
   ▲                   ├──▶ FAILED    ERP rejected it (4xx), or retry budget exhausted
   └── transient ──────┤               on errors that prove nothing was processed
       (429/503/       └──▶ UNKNOWN   no reliable answer (timeout, 5xx): the ERP may have it
        connect error)
manual retry:  FAILED | UNKNOWN ──▶ PENDING     (POST /orders/{id}/retry, else 409)
```

- **Automatic retries** (up to `ERP_MAX_ATTEMPTS`, exponential backoff with full jitter,
  `Retry-After` honoured): a scheduled retry is visible as `PENDING` (certainly not processed)
  or `UNKNOWN` (reconciliation scheduled), with the reason in `last_error`.
- **Reconciliation**: before every ERP create call the worker commits
  `needs_reconciliation = true`, and clears it only once the outcome is certain. If it is still
  set on the next attempt (lost response, worker crash), the worker looks the order up by
  `external_order_id` first and only re-creates it if the ERP confirms it is absent.
- An uncertain order is **never** reported `FAILED`: if reconciliation keeps failing, it stays
  `UNKNOWN` for an operator to retry later.
- **Manual retry** is a single operator-triggered attempt (reconciling first when needed). Two
  simultaneous retry requests: one `202`, one `409`, one task.

```bash
curl -X POST localhost:8000/orders/<id>/retry
```

Not handled yet: an order stuck in `PROCESSING` because its worker died mid-call needs a
periodic sweep (it would be safe: `needs_reconciliation` is already set).

### Known limitation: committed but not queued

Saving the order and publishing the Celery message are two separate writes (no outbox).
If Redis is unreachable, `POST /orders` still returns `202` (the order is safely stored) and
logs `event=order_enqueue_failed`, but the order stays `PENDING` until it is re-published.
Fix in a later iteration: transactional outbox or a periodic sweep of stale `PENDING` orders.

### Mock ERP failure injection (development only)

Applies to `POST /erp/orders`. Modes: `normal`, `http_400`, `http_429`, `http_503`,
`timeout_before_create`, `timeout_after_create` (the order is committed, *then* the ERP
sleeps past the client timeout: the response is lost).

```bash
# Initial mode at startup: MOCK_ERP_FAILURE_MODE / MOCK_ERP_TIMEOUT_DELAY_SECONDS in .env
# Change at runtime (optional "count": apply to the next N requests, then back to normal):
curl -X PUT localhost:8001/admin/failure-mode -H 'Content-Type: application/json' \
     -d '{"mode": "http_503", "count": 2}'
curl localhost:8001/admin/failure-mode            # current state
curl -X DELETE localhost:8001/admin/failure-mode  # reset to the .env defaults
```

The state is in memory, per process: restarting the mock ERP also resets it.

New migration after changing a model: `uv run alembic revision --autogenerate -m "..."`,
then **review the generated file** before applying it.

Stop services: `docker compose down` (add `-v` to also delete the database volume).

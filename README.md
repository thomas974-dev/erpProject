# Order-to-ERP Integration Simulator

A small, reliable B2B order integration service. A client submits an order through a REST
API; the order is validated, stored in PostgreSQL, then transmitted **asynchronously** to a
simulated external ERP by a Celery worker. The mock ERP can fail on demand, including the
hardest case: **it creates the order, then the response is lost**.

The focus is correctness under failure: idempotency, classification of errors, bounded
retries, reconciliation before any re-send, and concurrency safety. This is a portfolio
project, not production software (see [Trade-offs and limitations](#trade-offs-and-limitations)).

**Stack:** Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic · PostgreSQL 16 ·
Celery 5 · Redis 7 · HTTPX · pytest · Ruff · uv · Docker Compose

## Architecture

```mermaid
flowchart LR
    Client([API client]) -- "POST /orders<br/>GET /orders/{id}" --> API[Orders API<br/>FastAPI :8000]
    API -- "1. INSERT order (PENDING)" --> DB[(PostgreSQL<br/>orders)]
    API -- "2. publish order_id" --> Redis[(Redis<br/>Celery broker)]
    Redis --> Worker[Celery worker]
    Worker -- "claim / record outcome" --> DB
    Worker -- "POST /erp/orders<br/>GET .../by-external-id/{id}" --> ERP[Mock ERP<br/>FastAPI :8001]
    ERP --> ERPDB[(PostgreSQL<br/>erp_orders)]
```

1. `POST /orders` validates the payload, stores the order as `PENDING` and returns
   **202 Accepted** with a `status_url`: the client never waits for the ERP.
2. The API publishes only the **order ID** to Redis.
3. A worker **claims** the order atomically (`PENDING → PROCESSING`), calls the ERP with the
   order's stable `external_order_id`, and records `SUCCESS`, `FAILED` or `UNKNOWN`.
4. The client polls `GET /orders/{id}`. The status lives in PostgreSQL, not in Celery.

| Path | Responsibility |
|---|---|
| `app/api/` | HTTP only: routes, status codes, dependencies, health/readiness |
| `app/services/order_service.py` | Order creation (idempotency), queries, manual retry |
| `app/services/order_processor.py` | Worker logic: claim, reconcile, create, retry policy |
| `app/integrations/erp_client.py` | ERP HTTP client and error classification |
| `app/workers/` | Celery app and task (thin wrapper around `OrderProcessor`) |
| `app/models/`, `app/schemas/` | SQLAlchemy models / Pydantic API contracts |
| `app/core/` | Settings, database session, JSON logging |
| `mock_erp/` | The simulated ERP, a separate app only reached over HTTP |
| `alembic/` | Schema migrations |
| `scripts/demo.py` | Replays the demo scenarios against the running services |

## Getting started (macOS)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker Desktop, Git.

```bash
git clone <repo-url> order-to-erp && cd order-to-erp
cp .env.example .env              # local defaults, no real credentials
uv sync                           # Python 3.12 + dependencies from uv.lock
docker compose up -d --wait       # PostgreSQL 16 + Redis 7, bound to 127.0.0.1
uv run alembic upgrade head       # create the schema
```

Start the three processes, each in its own terminal:

```bash
uv run uvicorn app.main:app --reload --port 8000          # Orders API
uv run uvicorn mock_erp.main:app --reload --port 8001     # Mock ERP
uv run celery -A app.workers.celery_app:celery_app worker --loglevel=info   # Worker
```

Check that everything is up, then replay the demo scenarios:

```bash
curl localhost:8000/ready         # {"status":"ready","checks":{"database":"ok","broker":"ok"}}
uv run python scripts/demo.py     # 4 scenarios, exits non-zero if one fails
```

Interactive API docs: http://127.0.0.1:8000/docs (orders) and
http://127.0.0.1:8001/docs (mock ERP).

Stop: `Ctrl+C` in each terminal, then `docker compose down` (add `-v` to delete the data).

## Demo scenarios

`scripts/demo.py` runs all of these automatically. To replay one by hand, set the mock ERP's
failure mode, then create an order and poll its `status_url`:

```bash
# Create an order (optionally idempotent)
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: demo-1' \
     -d '{"customer_id": "CLIENT-123", "product_id": "PRODUCT-456", "quantity": 10}'
# -> 202 {"id": "...", "status": "PENDING", "status_url": "/orders/<id>", ...}

curl -s localhost:8000/orders/<id>               # poll
curl -s 'localhost:8000/orders?status=FAILED&limit=20&offset=0'
```

| # | Scenario | Set the mock ERP with | Expected |
|---|---|---|---|
| 1 | Happy path | `curl -X DELETE localhost:8001/admin/failure-mode` | `PENDING → SUCCESS`, 1 attempt |
| 2 | Transient failure | `-d '{"mode": "http_503", "count": 2}'` | `PENDING → PENDING → SUCCESS`, 3 attempts |
| 3 | Permanent failure + manual retry | `-d '{"mode": "http_400", "count": 1}'` | `FAILED` after 1 attempt; then `POST /orders/<id>/retry` → `SUCCESS` |
| 4 | Lost response | `-d '{"mode": "timeout_after_create", "delay_seconds": 5, "count": 1}'` | `UNKNOWN → SUCCESS` via lookup, still **one** ERP order |

The `-d` values go to `curl -X PUT localhost:8001/admin/failure-mode -H 'Content-Type: application/json' -d '...'`.
For scenario 4, verify there is a single ERP order:
`curl localhost:8001/erp/orders/by-external-id/<external_order_id>`.

## API

| Method | Path | Success | Errors |
|---|---|---|---|
| `POST` | `/orders` (optional `Idempotency-Key` header) | `202` + `Location` | `409` key reused with another payload, `422` invalid payload |
| `GET` | `/orders/{id}` | `200` | `404`, `422` malformed id |
| `GET` | `/orders?status=&limit=20&offset=0` | `200` `{items, total, limit, offset}`, newest first, `limit` ≤ 100 | `422` |
| `POST` | `/orders/{id}/retry` | `202` | `404`, `409` unless `FAILED` or `UNKNOWN` |
| `GET` | `/health` | `200` liveness: the process runs (checks nothing else) | |
| `GET` | `/ready` | `200` PostgreSQL and Redis answer | `503` with the failing check |

Validation: non-empty IDs (whitespace trimmed), `quantity` a strictly positive integer
(`"10"`, `1.5` and `true` are rejected), unknown fields rejected.

## Order lifecycle

```
PENDING ──claim──▶ PROCESSING ──▶ SUCCESS                       (terminal)
   ▲                   ├──▶ FAILED    ERP rejected it (4xx), or retry budget exhausted
   └── transient ──────┤               on errors that prove nothing was processed
       (429/503/       └──▶ UNKNOWN   no reliable answer (timeout, 5xx): the ERP may have it
        connect error)
manual retry:  FAILED | UNKNOWN ──▶ PENDING     (POST /orders/{id}/retry, else 409)
```

A scheduled automatic retry is represented by the status itself: `PENDING` (certainly not
processed, new attempt scheduled) or `UNKNOWN` (maybe processed, reconciliation scheduled).
`last_error` holds the sanitized reason; `attempt_count` counts every claim.

## Reliability design

**Error classification** (`app/integrations/erp_client.py`). The question is not "did it
fail?" but "could the ERP have acted?":

| Outcome of `POST /erp/orders` | Category | Worker action |
|---|---|---|
| 200, 201 | success | `SUCCESS`, store `erp_order_id` |
| 400, 409, 422 | permanent | `FAILED`, no automatic retry |
| 429, 503, connection refused / connect timeout | transient (certainly not processed) | `PENDING`, retry with backoff |
| read timeout, connection dropped, other 2xx, 500/502/504, malformed 200/201 body | **uncertain** | `UNKNOWN`, reconcile before any re-send |

A lookup (`GET`) has no side effect: `404` means absent, and any failure (transport error,
5xx, unexpected 4xx) leaves the order `UNKNOWN` with reconciliation still pending.

**No duplicate ERP orders**, by four independent layers:

1. **Client idempotency.** `Idempotency-Key` is `UNIQUE` in the database, paired with a
   SHA-256 of the normalized payload. Same key + same payload returns the original order
   (`Idempotent-Replayed: true`, no second task); a different payload returns `409`.
   Concurrent duplicates are resolved by the constraint, not by a check-then-insert.
2. **Atomic claim.** `UPDATE … SET status='PROCESSING' WHERE id=… AND status IN
   ('PENDING','UNKNOWN') RETURNING …`: one worker wins, even with redelivered messages
   (`acks_late`). The claim is committed **before** the ERP call; no lock is held over the network.
3. **Write-ahead reconciliation flag.** `needs_reconciliation` is committed `true` before every
   create call and cleared only on a certain outcome. If it is still set on the next attempt
   (lost response, worker crash), the worker first looks the order up by `external_order_id`:
   found with the same data → `SUCCESS`; found with different data → `FAILED`; absent → create;
   lookup failed → stay `UNKNOWN`. An uncertain order is **never** reported `FAILED`.
4. **ERP-side idempotency.** The mock ERP is unique on `external_order_id` and returns the
   existing order on a repeated create.

**Retries.** Up to `ERP_MAX_ATTEMPTS` (default 3), exponential backoff with full jitter
(`random(0, min(max, base × 2^(attempt−1)))`), `Retry-After` honoured up to the cap. The task
re-enqueues itself with a Celery `countdown`. A manual retry does not reset `attempt_count`:
it gets at least one attempt, plus any automatic retries left in the budget. Two simultaneous
retry requests yield one `202`, one `409` and a single task.

## Observability

All logs (API, worker, Celery internals) are JSON lines on stdout. Order events carry
`event`, `order_id`, `external_order_id`, `attempt`, `status` and, on failure, `error` and
`error_category` (`transient`, `permanent`, `uncertain`, `reconciliation`, `data_mismatch`,
`unexpected`). URL credentials (`scheme://user:pass@`) are masked by the formatter as a
safety net; error messages stored in `last_error` contain only a status code and a truncated
ERP `detail`.

```json
{"timestamp": "2026-10-19T17:46:09.764344+00:00", "level": "WARNING", "logger": "app.services.order_processor", "event": "order_finished", "order_id": "d606709f-…", "external_order_id": "d606709f-…", "attempt": 1, "status": "UNKNOWN", "retry_in": 0.678, "error": "No response from ERP (ReadTimeout)", "error_category": "uncertain"}
```

Events: `order_created`, `order_create_replayed`, `order_enqueue_failed`,
`order_retry_requested`, `readiness_check_failed` (API); `erp_create_attempt`, `erp_reconciled_found`,
`erp_reconciled_absent`, `order_not_claimed`, `order_finished` (worker).
The full history of one order: `grep <order_id>` across the API and worker logs.

### Troubleshooting

| Symptom | Likely cause | Investigate / fix |
|---|---|---|
| Order stays `PENDING`, `attempt_count = 0` | Worker not running, or Redis was down at creation (`order_enqueue_failed` in API logs) | `curl localhost:8000/ready`; start the worker; re-publish: `uv run python -c "import uuid; from app.workers.tasks import enqueue_order_processing as e; e(uuid.UUID('<id>'))"` |
| Order stays `PENDING`, `attempt_count > 0` | A retry is scheduled (`last_error` says why) | Wait for the backoff; check `order_finished` events with `error_category=transient` |
| `FAILED` | ERP rejected it (`permanent`) or retry budget exhausted on transient errors | Read `last_error`; fix the cause; `POST /orders/<id>/retry` |
| `UNKNOWN` | Timeout / 5xx: the ERP may have the order; reconciliation scheduled or exhausted | `curl localhost:8001/erp/orders/by-external-id/<external_order_id>`; then `POST /orders/<id>/retry` (reconciles first, never duplicates) |
| `FAILED` with "ERP holds a different order" | `data_mismatch`: the ERP has other data under this external id | Human decision: correct the ERP record, then retry |
| Stuck in `PROCESSING` | Worker died mid-call | Not handled automatically yet (see limitations); the flag guarantees a later retry reconciles first |
| `/ready` returns `503` | PostgreSQL or Redis unreachable | `docker compose ps`; the failing check is named in the response, details in the logs |

## Mock ERP failure injection (development only)

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

`GET /erp/orders?limit=50` lists the most recent ERP orders (newest first, `limit` ≤ 200).
It exists for the web UI below and accepts cross-origin `GET` requests from
`http://127.0.0.1:8000` / `http://localhost:8000` only.

## Web UI (development only)

With the API, the mock ERP and the worker running, open <http://127.0.0.1:8000/ui/>.
Left: a form that calls `POST /orders` and follows the order's status
(`PENDING → PROCESSING → SUCCESS`). Right: the orders the mock ERP actually received,
refreshed every 2 seconds. It is a single static page (`app/static/index.html`, no build
step), a demo aid rather than a product UI (frontend UI is a PRD non-goal).

## Configuration

Read from environment variables or `.env` (see `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://orders:orders@127.0.0.1:5432/orders` | PostgreSQL |
| `CELERY_BROKER_URL` | `redis://127.0.0.1:6379/0` | Redis broker |
| `ERP_BASE_URL` | `http://127.0.0.1:8001` | ERP endpoint |
| `ERP_HTTP_TIMEOUT_SECONDS` | `2.0` | Per-request timeout |
| `ERP_MAX_ATTEMPTS` | `3` | Automatic attempt budget |
| `ERP_RETRY_BASE_DELAY_SECONDS` / `ERP_RETRY_MAX_DELAY_SECONDS` | `1.0` / `30.0` | Backoff |
| `LOG_LEVEL` | `INFO` | Log level |
| `MOCK_ERP_FAILURE_MODE` / `MOCK_ERP_TIMEOUT_DELAY_SECONDS` | `normal` / `5.0` | Mock ERP defaults |

Invalid values (e.g. `ERP_MAX_ATTEMPTS=0`) stop the process at startup.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                          # all tests (needs docker compose up)
uv run pytest -m "not integration"     # unit tests only, no Docker
uv run pytest tests/test_worker.py -k lost_response   # a single test
```

- **Integration tests** use a separate `<db>_test` database rebuilt by the Alembic migrations
  (`downgrade base` + `upgrade head`), so migrations are tested too; `alembic check` fails the
  suite if a model changes without a migration.
- The ERP client is unit-tested with `httpx.MockTransport`; integration tests serve the real
  mock ERP with uvicorn in a background thread (real sockets, real timeouts).
- `tests/test_end_to_end.py` runs a **real Celery worker** (Redis db 15) against the API, the
  mock ERP and PostgreSQL.

New migration after changing a model: `uv run alembic revision --autogenerate -m "..."`, then
**review the generated file** (autogenerate misses data migrations and renames).

## Trade-offs and limitations

- **No transactional outbox.** Committing the order and publishing the Celery message are two
  separate writes. If Redis is down, `POST /orders` still returns `202` and logs
  `order_enqueue_failed`; the order stays `PENDING` until re-published (see Troubleshooting).
  The same applies to a retry the worker fails to re-publish. Fix: an outbox table written in
  the same transaction, or a periodic sweep of stale `PENDING`/`UNKNOWN` orders.
- **No sweep of stuck `PROCESSING` orders** (worker killed mid-call). It would be safe to add:
  `needs_reconciliation` is already set, so the next attempt reconciles first.
- **At-least-once, not exactly-once.** Delivery is at-least-once (`acks_late`); the claim,
  the reconciliation flag and ERP-side idempotency make the *effect* single in the tested
  scenarios. Exactly-once over a network is not claimed.
- **Thread pool for the worker.** Tasks are I/O-bound (HTTP + SQL, the GIL is released while
  waiting). Celery's prefork pool also currently fails on macOS, where billiard starts
  children with `spawn` (celery#9894).
- **Manual retry does not reset the attempt budget** (simplest safe choice): it always gets
  one attempt, and only the automatic retries left over from `ERP_MAX_ATTEMPTS` after that.
- **Idempotency keys are global and never expire.** With authentication they would be
  scoped per client (`UNIQUE(client_id, key)`) and purged after e.g. 24 h.
- **`limit`/`offset` pagination** is simple but slow on deep pages; keyset pagination would scale.
- **Shared database.** The mock ERP's `erp_orders` table lives in the same PostgreSQL database
  and Alembic history, for simplicity; a real ERP is a separate system.
- **The mock ERP's failure mode is per process** and only affects create calls.
- **Out of scope:** authentication, multi-tenancy, metrics, circuit breaker, real ERP.

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

New migration after changing a model: `uv run alembic revision --autogenerate -m "..."`,
then **review the generated file** before applying it.

Stop services: `docker compose down` (add `-v` to also delete the database volume).

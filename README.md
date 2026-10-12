# Order-to-ERP Integration Simulator

Work in progress — see `PRD.md`.

## Local setup (macOS)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker Desktop.

```bash
cp .env.example .env
uv sync
docker compose up -d          # PostgreSQL 16 + Redis 7 on 127.0.0.1
docker compose ps             # wait until both are "healthy"

uv run ruff check .
uv run pytest                 # unit + integration tests
uv run pytest -m "not integration"   # without Docker
```

Run the API: `uv run uvicorn app.main:app --reload --port 8000`, then open
http://127.0.0.1:8000/docs (Swagger UI).

Stop services: `docker compose down` (add `-v` to also delete the database volume).

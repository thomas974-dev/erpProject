from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the API process is running. Does not check PostgreSQL or Redis."""
    return {"status": "ok"}

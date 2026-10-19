from fastapi import FastAPI

from app.api import health, orders
from app.core.config import get_settings
from app.core.logging import configure_logging

configure_logging(get_settings().log_level)

app = FastAPI(
    title="Order-to-ERP Integration API",
    version="0.1.0",
    description="Accepts B2B orders and transmits them asynchronously to an ERP.",
)
app.include_router(health.router)
app.include_router(orders.router)

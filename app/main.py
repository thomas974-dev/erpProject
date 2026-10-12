from fastapi import FastAPI

from app.api import health, orders

app = FastAPI(
    title="Order-to-ERP Integration API",
    version="0.1.0",
    description="Accepts B2B orders and transmits them asynchronously to an ERP.",
)
app.include_router(health.router)
app.include_router(orders.router)

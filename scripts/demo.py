"""Replay the demo scenarios against the running services.

Prerequisites: API (8000), mock ERP (8001) and Celery worker running (see README).
Usage: uv run python scripts/demo.py
Exits with a non-zero code if any scenario ends in an unexpected state.
"""

import sys
import time
from typing import Any

import httpx

API_URL = "http://127.0.0.1:8000"
ERP_URL = "http://127.0.0.1:8001"
ORDER = {"customer_id": "CLIENT-123", "product_id": "PRODUCT-456", "quantity": 10}
FINAL = {"SUCCESS", "FAILED"}

api = httpx.Client(base_url=API_URL, timeout=5)
erp = httpx.Client(base_url=ERP_URL, timeout=5)


def set_failure(config: dict[str, Any] | None) -> None:
    erp.delete("/admin/failure-mode").raise_for_status()
    if config:
        erp.put("/admin/failure-mode", json=config).raise_for_status()


def follow(status_url: str, until: set[str] = FINAL, timeout: float = 30) -> dict[str, Any]:
    """Poll the order and print every status transition."""
    transitions: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        order = api.get(status_url).json()
        if not transitions or transitions[-1] != order["status"]:
            transitions.append(order["status"])
        if order["status"] in until:
            break
        time.sleep(0.05)
    print(f"   {' -> '.join(transitions)}")
    print(f"   attempts={order['attempt_count']}  erp_order_id={order['erp_order_id']}")
    if order["last_error"]:
        print(f"   last_error={order['last_error']}")
    return order


def submit() -> str:
    started = time.monotonic()
    response = api.post("/orders", json=ORDER)
    response.raise_for_status()
    body = response.json()
    elapsed_ms = (time.monotonic() - started) * 1000
    print(f"   POST /orders -> {response.status_code} {body['status']} in {elapsed_ms:.0f} ms")
    return body["status_url"]


def expect(condition: bool, message: str) -> bool:
    print(f"   {'OK  ' if condition else 'FAIL'} {message}")
    return condition


def scenario_success() -> bool:
    print("\n1) Happy path")
    set_failure(None)
    order = follow(submit())
    return expect(order["status"] == "SUCCESS", "order reached SUCCESS")


def scenario_transient() -> bool:
    print("\n2) Transient failure: the ERP answers 503 twice, then recovers")
    set_failure({"mode": "http_503", "count": 2})
    order = follow(submit())
    return expect(
        order["status"] == "SUCCESS" and order["attempt_count"] == 3,
        "retried with backoff, SUCCESS on attempt 3",
    )


def scenario_permanent_then_manual_retry() -> bool:
    print("\n3) Permanent failure (400), then manual retry once the ERP accepts it")
    set_failure({"mode": "http_400", "count": 1})
    status_url = submit()
    failed = follow(status_url)
    ok = expect(
        failed["status"] == "FAILED" and failed["attempt_count"] == 1,
        "FAILED after a single attempt (no automatic retry)",
    )
    response = api.post(f"{status_url}/retry")
    print(f"   POST {status_url}/retry -> {response.status_code}")
    order = follow(status_url)
    ok &= expect(order["status"] == "SUCCESS", "manual retry succeeded")
    again = api.post(f"{status_url}/retry").status_code
    return ok & expect(again == 409, f"retrying a SUCCESS order is refused ({again})")


def scenario_lost_response() -> bool:
    print("\n4) Unknown outcome: the ERP creates the order, then the response is lost")
    set_failure({"mode": "timeout_after_create", "delay_seconds": 5, "count": 1})
    order = follow(submit(), timeout=40)
    lookup = erp.get(f"/erp/orders/by-external-id/{order['external_order_id']}")
    return expect(
        order["status"] == "SUCCESS"
        and order["attempt_count"] == 2
        and lookup.json()["erp_order_id"] == order["erp_order_id"],
        "UNKNOWN -> reconciled by lookup -> SUCCESS, same single ERP order",
    )


def main() -> int:
    ready = api.get("/ready")
    if ready.status_code != 200:
        print(f"API not ready: {ready.json()}")
        return 1
    try:
        results = [
            scenario_success(),
            scenario_transient(),
            scenario_permanent_then_manual_retry(),
            scenario_lost_response(),
        ]
    finally:
        set_failure(None)
    print(f"\n{sum(results)}/{len(results)} scenarios passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.ConnectError as exc:
        sys.exit(f"Cannot reach a service ({exc}). Are the API, mock ERP and worker running?")

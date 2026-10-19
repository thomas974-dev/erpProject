"""Worker-side business logic: send one order to the ERP and record the outcome.

Safety rule (write-ahead intent): `needs_reconciliation` is committed as True *before*
every create call, and only cleared once the outcome is certain. So after a lost response
or a worker crash mid-call, the next attempt first looks the order up by external_order_id
and only creates it when the ERP confirms it does not exist.
"""

import logging
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.erp_client import (
    ErpClient,
    ErpError,
    ErpOrder,
    ErpPermanentError,
    ErpTransientError,
    ErpUncertainError,
)
from app.models.order import Order, OrderStatus, utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    base_delay: float
    max_delay: float

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            max_attempts=settings.erp_max_attempts,
            base_delay=settings.erp_retry_base_delay_seconds,
            max_delay=settings.erp_retry_max_delay_seconds,
        )

    def delay(
        self,
        attempt: int,
        retry_after: float | None = None,
        rng: Callable[[], float] = random.random,
    ) -> float:
        """Exponential backoff with full jitter, honouring Retry-After (both capped)."""
        backoff = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        delay = backoff * rng()  # full jitter: spreads out retries of many failing orders
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.max_delay))
        return delay


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    status: OrderStatus
    retry_in: float | None = None  # seconds until the next automatic attempt, if any


class OrderProcessor:
    def __init__(
        self,
        session: Session,
        erp: ErpClient,
        policy: RetryPolicy,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._session = session
        self._erp = erp
        self._policy = policy
        self._rng = rng

    def process(self, order_id: uuid.UUID) -> ProcessOutcome | None:
        """Process a PENDING or UNKNOWN order. Returns None if it could not be claimed."""
        order = self._claim(order_id)
        if order is None:
            # Unknown id, or already claimed/processed (e.g. a redelivered message).
            logger.info("event=order_not_claimed order_id=%s", order_id)
            return None
        try:
            if order.needs_reconciliation:
                outcome = self._reconcile(order)
                if outcome is not None:
                    return outcome
            return self._create(order)
        except Exception:
            # A bug, possibly after the ERP call succeeded: UNKNOWN, never FAILED.
            self._session.rollback()
            self._finish(order, OrderStatus.UNKNOWN, error="Unexpected worker error")
            raise

    def _claim(self, order_id: uuid.UUID) -> Order | None:
        """Atomically move PENDING/UNKNOWN -> PROCESSING. At most one caller can win.

        The transaction is committed before any ERP call: no lock is held over the network.
        """
        order = self._session.scalars(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status.in_([OrderStatus.PENDING, OrderStatus.UNKNOWN]),
            )
            .values(
                status=OrderStatus.PROCESSING,
                attempt_count=Order.attempt_count + 1,
                updated_at=utc_now(),
            )
            .returning(Order)
        ).one_or_none()
        self._session.commit()
        return order

    def _reconcile(self, order: Order) -> ProcessOutcome | None:
        """Look the order up in the ERP. Returns None only if it is confirmed absent."""
        try:
            existing = self._erp.get_order_by_external_id(order.external_order_id)
        except ErpError as exc:
            # Cannot rule out that the ERP has the order: never create blindly.
            retry_after = exc.retry_after if isinstance(exc, ErpTransientError) else None
            return self._finish(
                order,
                OrderStatus.UNKNOWN,
                error=f"Reconciliation failed: {exc}",
                retry_in=self._next_retry_delay(order, retry_after),
            )  # needs_reconciliation stays True

        if existing is None:
            self._log("erp_reconciled_absent", order)
            return None
        if not _same_order(order, existing):
            return self._finish(
                order,
                OrderStatus.FAILED,
                error="ERP holds a different order under this external_order_id",
            )  # needs_reconciliation stays True: a manual retry must check again
        self._log("erp_reconciled_found", order)
        return self._finish(
            order,
            OrderStatus.SUCCESS,
            erp_order_id=existing.erp_order_id,
            needs_reconciliation=False,
        )

    def _create(self, order: Order) -> ProcessOutcome:
        # Record the intent durably before the request can reach the ERP.
        order.needs_reconciliation = True
        self._session.commit()
        self._log("erp_create_attempt", order)
        try:
            erp_order = self._erp.create_order(
                external_order_id=order.external_order_id,
                customer_id=order.customer_id,
                product_id=order.product_id,
                quantity=order.quantity,
            )
        except ErpPermanentError as exc:
            # The ERP answered and rejected the order: certainly not created.
            return self._finish(
                order, OrderStatus.FAILED, error=str(exc), needs_reconciliation=False
            )
        except ErpTransientError as exc:
            # Not sent, or explicitly not processed (429/503): certainly not created.
            retry_in = self._next_retry_delay(order, exc.retry_after)
            if retry_in is None:
                return self._finish(
                    order,
                    OrderStatus.FAILED,
                    error=f"{exc} (gave up after {order.attempt_count} attempts)",
                    needs_reconciliation=False,
                )
            return self._finish(
                order,
                OrderStatus.PENDING,
                error=str(exc),
                retry_in=retry_in,
                needs_reconciliation=False,
            )
        except ErpUncertainError as exc:
            # Budget exhausted or not, an uncertain outcome is never reported as FAILED.
            return self._finish(
                order,
                OrderStatus.UNKNOWN,
                error=str(exc),
                retry_in=self._next_retry_delay(order),
            )  # needs_reconciliation stays True
        return self._finish(
            order,
            OrderStatus.SUCCESS,
            erp_order_id=erp_order.erp_order_id,
            needs_reconciliation=False,
        )

    def _next_retry_delay(self, order: Order, retry_after: float | None = None) -> float | None:
        if order.attempt_count >= self._policy.max_attempts:
            return None
        return self._policy.delay(order.attempt_count, retry_after, self._rng)

    def _finish(
        self,
        order: Order,
        status: OrderStatus,
        *,
        erp_order_id: str | None = None,
        error: str | None = None,
        retry_in: float | None = None,
        needs_reconciliation: bool | None = None,  # None: leave unchanged
    ) -> ProcessOutcome:
        order.status = status
        order.erp_order_id = erp_order_id
        order.last_error = error
        order.updated_at = utc_now()
        if needs_reconciliation is not None:
            order.needs_reconciliation = needs_reconciliation
        self._session.commit()
        self._log(
            "order_finished",
            order,
            level=logging.WARNING if error else logging.INFO,
            status=status,
            retry_in=None if retry_in is None else round(retry_in, 3),
            error=error,
        )
        return ProcessOutcome(status=status, retry_in=retry_in)

    @staticmethod
    def _log(event: str, order: Order, level: int = logging.INFO, **fields: object) -> None:
        extra = "".join(f" {key}={value}" for key, value in fields.items())
        logger.log(
            level,
            "event=%s order_id=%s external_order_id=%s attempt=%d%s",
            event,
            order.id,
            order.external_order_id,
            order.attempt_count,
            extra,
        )


def _same_order(order: Order, erp_order: ErpOrder) -> bool:
    return (order.customer_id, order.product_id, order.quantity) == (
        erp_order.customer_id,
        erp_order.product_id,
        erp_order.quantity,
    )

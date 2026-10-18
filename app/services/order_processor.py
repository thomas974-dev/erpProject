"""Worker-side business logic: send one order to the ERP and record the outcome."""

import logging
import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.integrations.erp_client import (
    ErpClient,
    ErpPermanentError,
    ErpTransientError,
    ErpUncertainError,
)
from app.models.order import Order, OrderStatus, utc_now

logger = logging.getLogger(__name__)


class OrderProcessor:
    def __init__(self, session: Session, erp: ErpClient) -> None:
        self._session = session
        self._erp = erp

    def process(self, order_id: uuid.UUID) -> OrderStatus | None:
        """Process a PENDING order. Returns the final status, or None if not claimed."""
        order = self._claim(order_id)
        if order is None:
            # Unknown id, or already claimed/processed (e.g. a redelivered message).
            logger.info("event=order_not_claimed order_id=%s", order_id)
            return None

        logger.info(
            "event=erp_create_attempt order_id=%s external_order_id=%s attempt=%d",
            order.id,
            order.external_order_id,
            order.attempt_count,
        )
        try:
            erp_order = self._erp.create_order(
                external_order_id=order.external_order_id,
                customer_id=order.customer_id,
                product_id=order.product_id,
                quantity=order.quantity,
            )
        except ErpPermanentError as exc:
            return self._finish(order, OrderStatus.FAILED, error=str(exc))
        except ErpTransientError as exc:
            # Milestone 6 adds bounded retries with backoff; until then a known failure.
            return self._finish(order, OrderStatus.FAILED, error=str(exc))
        except ErpUncertainError as exc:
            # The ERP may have created the order: never report FAILED here.
            return self._finish(order, OrderStatus.UNKNOWN, error=str(exc))
        except Exception:
            # A bug, possibly after the ERP call succeeded: UNKNOWN, not FAILED.
            self._session.rollback()
            self._finish(order, OrderStatus.UNKNOWN, error="Unexpected worker error")
            raise
        return self._finish(order, OrderStatus.SUCCESS, erp_order_id=erp_order.erp_order_id)

    def _claim(self, order_id: uuid.UUID) -> Order | None:
        """Atomically move PENDING -> PROCESSING. At most one caller can win.

        The transaction is committed before the ERP call: no lock is held over the network.
        """
        order = self._session.scalars(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.PENDING)
            .values(
                status=OrderStatus.PROCESSING,
                attempt_count=Order.attempt_count + 1,
                updated_at=utc_now(),
            )
            .returning(Order)
        ).one_or_none()
        self._session.commit()
        return order

    def _finish(
        self,
        order: Order,
        status: OrderStatus,
        *,
        erp_order_id: str | None = None,
        error: str | None = None,
    ) -> OrderStatus:
        order.status = status
        order.erp_order_id = erp_order_id
        order.last_error = error
        order.updated_at = utc_now()
        self._session.commit()
        log = logger.warning if error else logger.info
        log(
            "event=order_finished order_id=%s external_order_id=%s attempt=%d status=%s error=%s",
            order.id,
            order.external_order_id,
            order.attempt_count,
            status,
            error,
        )
        return status

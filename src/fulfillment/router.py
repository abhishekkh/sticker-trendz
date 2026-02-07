"""
Fulfillment router for Sticker Trendz.

Routes orders to the correct fulfillment provider based on product type
and provider availability. Primary: Sticker Mule. Fallback: self-fulfillment
(USPS). Singles only for MVP -- never routes through Printful.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.db import SupabaseClient, DatabaseError
from src.fulfillment.sticker_mule import StickerMuleClient, StickerMuleError
from src.fulfillment.self_fulfill import SelfFulfillmentTracker
from src.monitoring.error_logger import ErrorLogger
from src.monitoring.alerter import EmailAlerter
from src.resilience import RetryExhaustedError

logger = logging.getLogger(__name__)

# Primary and fallback providers
PRIMARY_PROVIDER = "sticker_mule"
FALLBACK_PROVIDER = "self_usps"


class FulfillmentError(Exception):
    """Raised when fulfillment fails completely."""


class FulfillmentRouter:
    """
    Routes orders to the appropriate fulfillment provider.

    Primary provider: Sticker Mule (includes free shipping).
    Fallback: Self-fulfillment via USPS (requires manual shipping).

    MVP handles singles only (no packs). Never routes through Printful
    as it results in negative margins for single stickers.
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        sticker_mule: Optional[StickerMuleClient] = None,
        self_fulfill: Optional[SelfFulfillmentTracker] = None,
        error_logger: Optional[ErrorLogger] = None,
        alerter: Optional[EmailAlerter] = None,
    ) -> None:
        self._db = db or SupabaseClient()
        self._sticker_mule = sticker_mule
        self._self_fulfill = self_fulfill or SelfFulfillmentTracker(
            db=self._db, alerter=alerter,
        )
        self._error_logger = error_logger
        self._alerter = alerter

    def route_order(self, order: Dict[str, Any]) -> str:
        """
        Determine the fulfillment provider for an order.

        MVP routing logic: always try Sticker Mule first, fall back
        to self-fulfillment if unavailable.

        Args:
            order: Order dict from Supabase.

        Returns:
            Provider name: 'sticker_mule' or 'self_usps'.
        """
        # MVP: singles only, Sticker Mule is primary
        if self._sticker_mule is not None:
            return PRIMARY_PROVIDER
        return FALLBACK_PROVIDER

    def fulfill_order(self, order: Dict[str, Any]) -> bool:
        """
        Fulfill an order by sending it to the appropriate provider.

        Tries Sticker Mule first. On failure after retries, falls back
        to self-fulfillment with an alert to the operator.

        Args:
            order: Order dict from Supabase.

        Returns:
            True if the order was successfully submitted for fulfillment.
        """
        order_id = order.get("id", "")
        etsy_order_id = order.get("etsy_order_id", "")
        sticker_id = order.get("sticker_id", "")
        customer_data = order.get("customer_data", {}) or {}
        quantity = int(order.get("quantity", 1) or 1)

        # Get sticker details for image URL and size
        image_url = ""
        size = "3in"
        try:
            if sticker_id:
                rows = self._db.select(
                    "stickers",
                    columns="image_url,size",
                    filters={"id": sticker_id},
                    limit=1,
                )
                if rows:
                    image_url = rows[0].get("image_url", "")
                    size = rows[0].get("size", "3in")
        except DatabaseError as exc:
            logger.error("Failed to fetch sticker details for order %s: %s", order_id, exc)

        # Try primary provider (Sticker Mule)
        provider = self.route_order(order)

        if provider == PRIMARY_PROVIDER and self._sticker_mule:
            try:
                fulfillment_order_id = self._sticker_mule.submit_order(
                    image_url=image_url,
                    address=customer_data,
                    size=size,
                    quantity=quantity,
                )

                # Update order record
                self._update_order_fulfillment(
                    order_id, PRIMARY_PROVIDER, fulfillment_order_id, "sent_to_print"
                )
                logger.info(
                    "Order %s fulfilled via Sticker Mule (fulfillment_id=%s)",
                    order_id, fulfillment_order_id,
                )
                return True

            except (StickerMuleError, RetryExhaustedError) as exc:
                logger.warning(
                    "Sticker Mule fulfillment failed for order %s: %s. Falling back.",
                    order_id, exc,
                )

                # Update retry tracking
                attempts = int(order.get("fulfillment_attempts", 0) or 0) + 1
                try:
                    self._db.update_order(order_id, {
                        "fulfillment_attempts": attempts,
                        "fulfillment_last_error": str(exc)[:500],
                    })
                except DatabaseError:
                    pass

                if self._error_logger:
                    self._error_logger.log_error(
                        workflow="fulfillment",
                        step="sticker_mule_submit",
                        error_type="api_error",
                        error_message=str(exc),
                        service="sticker_mule",
                        context={
                            "order_id": order_id,
                            "etsy_order_id": etsy_order_id,
                        },
                    )

        # Fallback to self-fulfillment
        logger.info("Routing order %s to self-fulfillment", order_id)
        success = self._self_fulfill.create_self_fulfillment_order(order)

        if success:
            # Send alert about manual fulfillment needed
            if self._alerter:
                self._alerter.send_alert(
                    f"Order {etsy_order_id} needs manual fulfillment",
                    (
                        f"Sticker Mule fulfillment failed. Order {order_id} "
                        f"(Etsy: {etsy_order_id}) has been routed to "
                        f"self-fulfillment and requires manual attention."
                    ),
                    level="warning",
                )
            return True

        # Complete failure
        logger.error("All fulfillment methods failed for order %s", order_id)
        self._update_order_fulfillment(
            order_id, FALLBACK_PROVIDER, "", "pending_manual"
        )
        return False

    def _update_order_fulfillment(
        self,
        order_id: str,
        provider: str,
        fulfillment_order_id: str,
        status: str,
    ) -> None:
        """Update order record with fulfillment details."""
        try:
            self._db.update_order(order_id, {
                "fulfillment_provider": provider,
                "fulfillment_order_id": fulfillment_order_id,
                "status": status,
            })
        except DatabaseError as exc:
            logger.error(
                "Failed to update fulfillment for order %s: %s",
                order_id, exc,
            )

    def check_fulfillment_status(self, order: Dict[str, Any]) -> Optional[str]:
        """
        Check the current fulfillment status for an order.

        Args:
            order: Order dict from Supabase.

        Returns:
            Updated status string, or None if unchanged.
        """
        order_id = order.get("id", "")
        provider = order.get("fulfillment_provider", "")
        fulfillment_id = order.get("fulfillment_order_id", "")

        if provider == PRIMARY_PROVIDER and fulfillment_id and self._sticker_mule:
            try:
                sm_status = self._sticker_mule.get_order_status(fulfillment_id)

                # Map Sticker Mule status to our status
                status_map = {
                    "processing": "sent_to_print",
                    "printing": "print_confirmed",
                    "shipped": "shipped",
                    "delivered": "delivered",
                }
                new_status = status_map.get(sm_status)

                if new_status and new_status != order.get("status"):
                    update_data: Dict[str, Any] = {"status": new_status}

                    if new_status == "shipped":
                        tracking = self._sticker_mule.get_tracking_number(fulfillment_id)
                        if tracking:
                            update_data["fulfillment_order_id"] = tracking
                        update_data["shipped_at"] = datetime.now(timezone.utc).isoformat()

                    if new_status == "delivered":
                        update_data["delivered_at"] = datetime.now(timezone.utc).isoformat()

                    try:
                        self._db.update_order(order_id, update_data)
                        logger.info(
                            "Order %s status updated: %s -> %s",
                            order_id, order.get("status"), new_status,
                        )
                    except DatabaseError as exc:
                        logger.error("Failed to update order %s status: %s", order_id, exc)

                    return new_status

            except (StickerMuleError, RetryExhaustedError) as exc:
                logger.debug("Status check failed for order %s: %s", order_id, exc)

        return None

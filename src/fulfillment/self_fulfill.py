"""
Self-fulfillment order tracker for Sticker Trendz.

Tracks orders fulfilled via USPS self-fulfillment. Creates records for
orders needing manual attention (print, package, ship), provides status
updates, and sends alert emails for overdue orders.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.db import SupabaseClient, DatabaseError
from src.monitoring.alerter import EmailAlerter

logger = logging.getLogger(__name__)

# Alert if an order has been in 'sent_to_print' for this many days
SHIPPING_OVERDUE_DAYS = 7


class SelfFulfillmentTracker:
    """
    Tracks self-fulfilled orders through the USPS shipping pipeline.

    Statuses:
      - pending_manual: Needs operator attention (print + ship)
      - printed: Operator has printed the sticker
      - shipped: Operator has shipped via USPS
      - delivered: USPS confirmed delivery

    Sends alert emails for orders needing manual attention and
    for orders overdue for shipping.
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        alerter: Optional[EmailAlerter] = None,
    ) -> None:
        self._db = db or SupabaseClient()
        self._alerter = alerter

    def create_self_fulfillment_order(self, order: Dict[str, Any]) -> bool:
        """
        Record an order as needing manual self-fulfillment.

        Sets the order status to 'pending_manual' and fulfillment_provider
        to 'self_usps'. Sends an alert email to the operator.

        Args:
            order: Order dict from Supabase.

        Returns:
            True if the order was updated successfully.
        """
        order_id = order.get("id", "")
        etsy_order_id = order.get("etsy_order_id", "")

        try:
            self._db.update_order(order_id, {
                "status": "pending_manual",
                "fulfillment_provider": "self_usps",
            })
            logger.info("Order %s marked for self-fulfillment", order_id)
        except DatabaseError as exc:
            logger.error("Failed to update order %s for self-fulfillment: %s", order_id, exc)
            return False

        # Send alert email to operator
        if self._alerter:
            customer_data = order.get("customer_data", {}) or {}
            sticker_id = order.get("sticker_id", "")
            quantity = order.get("quantity", 1)

            body = (
                f"New order needs manual fulfillment.\n\n"
                f"Order ID: {order_id}\n"
                f"Etsy Order: {etsy_order_id}\n"
                f"Sticker ID: {sticker_id}\n"
                f"Quantity: {quantity}\n"
                f"Ship To: {customer_data.get('name', 'N/A')}\n"
                f"Address: {customer_data.get('address', 'N/A')}\n"
                f"City: {customer_data.get('city', '')}, "
                f"{customer_data.get('state', '')} "
                f"{customer_data.get('zip', '')}\n\n"
                f"Please print, package, and ship this order."
            )
            self._alerter.send_alert(
                f"Order {etsy_order_id} needs manual fulfillment",
                body,
                level="warning",
            )

        return True

    def get_pending_orders(self) -> List[Dict[str, Any]]:
        """
        Get all orders in 'pending_manual' status.

        Returns:
            List of order dicts awaiting manual fulfillment.
        """
        try:
            return self._db.get_orders_by_status("pending_manual")
        except DatabaseError as exc:
            logger.error("Failed to fetch pending self-fulfillment orders: %s", exc)
            return []

    def mark_printed(self, order_id: str) -> bool:
        """
        Mark an order as printed (sticker has been printed).

        Args:
            order_id: Order UUID.

        Returns:
            True on success.
        """
        try:
            self._db.update_order(order_id, {"status": "printed"})
            logger.info("Order %s marked as printed", order_id)
            return True
        except DatabaseError as exc:
            logger.error("Failed to mark order %s as printed: %s", order_id, exc)
            return False

    def mark_shipped(self, order_id: str, tracking_number: str = "") -> bool:
        """
        Mark an order as shipped with optional tracking information.

        Args:
            order_id: Order UUID.
            tracking_number: USPS tracking number.

        Returns:
            True on success.
        """
        update_data: Dict[str, Any] = {
            "status": "shipped",
            "shipped_at": datetime.now(timezone.utc).isoformat(),
        }
        if tracking_number:
            update_data["fulfillment_order_id"] = tracking_number

        try:
            self._db.update_order(order_id, update_data)
            logger.info("Order %s marked as shipped (tracking: %s)", order_id, tracking_number or "N/A")
            return True
        except DatabaseError as exc:
            logger.error("Failed to mark order %s as shipped: %s", order_id, exc)
            return False

    def mark_delivered(self, order_id: str) -> bool:
        """
        Mark an order as delivered.

        Args:
            order_id: Order UUID.

        Returns:
            True on success.
        """
        try:
            self._db.update_order(order_id, {
                "status": "delivered",
                "delivered_at": datetime.now(timezone.utc).isoformat(),
            })
            logger.info("Order %s marked as delivered", order_id)
            return True
        except DatabaseError as exc:
            logger.error("Failed to mark order %s as delivered: %s", order_id, exc)
            return False

    def check_overdue_orders(self) -> int:
        """
        Check for orders overdue for shipping (in 'sent_to_print' or
        'pending_manual' for 7+ days) and send alert emails.

        Returns:
            Number of overdue orders found.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=SHIPPING_OVERDUE_DAYS)
        overdue_count = 0

        for status in ("pending_manual", "sent_to_print", "printed"):
            try:
                orders = self._db.get_orders_by_status(status)
                for order in orders:
                    created_str = order.get("created_at", "")
                    if not created_str:
                        continue

                    try:
                        created_at = datetime.fromisoformat(
                            created_str.replace("Z", "+00:00")
                        )
                    except (ValueError, AttributeError):
                        continue

                    if created_at < cutoff:
                        overdue_count += 1
                        if self._alerter:
                            self._alerter.send_alert(
                                f"Overdue order: {order.get('etsy_order_id', order.get('id', ''))}",
                                (
                                    f"Order {order.get('id', '')} has been in '{status}' "
                                    f"status for {SHIPPING_OVERDUE_DAYS}+ days.\n\n"
                                    f"Please ship this order or update its status."
                                ),
                                level="warning",
                            )

            except DatabaseError as exc:
                logger.error("Failed to check overdue orders with status '%s': %s", status, exc)

        if overdue_count > 0:
            logger.warning("Found %d overdue self-fulfillment orders", overdue_count)
        return overdue_count

    def generate_packing_slip(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate packing slip data for a self-fulfilled order.

        Args:
            order: Order dict from Supabase.

        Returns:
            Dict with packing slip fields.
        """
        customer_data = order.get("customer_data", {}) or {}
        return {
            "order_id": order.get("etsy_order_id", order.get("id", "")),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "ship_to": {
                "name": customer_data.get("name", ""),
                "address": customer_data.get("address", ""),
                "city": customer_data.get("city", ""),
                "state": customer_data.get("state", ""),
                "zip": customer_data.get("zip", ""),
                "country": customer_data.get("country", "US"),
            },
            "items": [
                {
                    "sticker_id": order.get("sticker_id", ""),
                    "quantity": order.get("quantity", 1),
                    "description": f"Vinyl Sticker",
                }
            ],
            "shipping_method": "USPS First-Class Mail",
        }

"""
Sticker Mule API client for Sticker Trendz.

Submits on-demand print orders, tracks order status, and retrieves
tracking numbers. Uses the Sticker Mule API with authentication
via STICKER_MULE_API_KEY.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from src.config import load_config
from src.resilience import retry, RetryExhaustedError

logger = logging.getLogger(__name__)

STICKER_MULE_API_BASE = "https://api.stickermule.com/v3"


class StickerMuleError(Exception):
    """Raised on Sticker Mule API errors."""


class StickerMuleClient:
    """
    Sticker Mule API client for on-demand sticker printing.

    Supports:
      - Submitting print orders with image URL, address, size, and quantity
      - Querying order status (processing, shipped, delivered)
      - Retrieving tracking numbers
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        """
        Args:
            api_key: Sticker Mule API key. Falls back to config.
            http_client: Injectable HTTP client for testing.
        """
        cfg = load_config(require_all=False)
        self._api_key = api_key or cfg.fulfillment.sticker_mule_api_key
        self._http = http_client or httpx.Client(timeout=30)

    def _get_headers(self) -> Dict[str, str]:
        """Return authorization headers for Sticker Mule API."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    @retry(max_retries=3, service="sticker_mule")
    def submit_order(
        self,
        image_url: str,
        address: Dict[str, str],
        size: str = "3in",
        quantity: int = 1,
    ) -> str:
        """
        Submit a print order to Sticker Mule.

        Args:
            image_url: URL of the sticker image (from R2).
            address: Shipping address dict with name, address1, city, state, zip, country.
            size: Sticker size ('3in' or '4in').
            quantity: Number of stickers to print.

        Returns:
            Sticker Mule order ID string.

        Raises:
            StickerMuleError: On API errors after retries.
        """
        # Map size to Sticker Mule product dimensions
        width = 3 if size == "3in" else 4
        height = width

        payload = {
            "items": [
                {
                    "image_url": image_url,
                    "width": width,
                    "height": height,
                    "quantity": quantity,
                }
            ],
            "shipping": {
                "name": address.get("name", ""),
                "address1": address.get("address", address.get("address1", "")),
                "city": address.get("city", ""),
                "state": address.get("state", ""),
                "zip": address.get("zip", ""),
                "country": address.get("country", "US"),
            },
        }

        try:
            response = self._http.post(
                f"{STICKER_MULE_API_BASE}/orders",
                json=payload,
                headers=self._get_headers(),
            )

            if response.status_code == 429:
                raise StickerMuleError("Sticker Mule rate limit reached")

            response.raise_for_status()
            data = response.json()
            order_id = str(data.get("id", data.get("order_id", "")))

            logger.info(
                "Submitted Sticker Mule order %s (size=%s, qty=%d)",
                order_id, size, quantity,
            )
            return order_id

        except httpx.HTTPStatusError as exc:
            error_msg = f"Sticker Mule API error (HTTP {exc.response.status_code}): {exc.response.text}"
            logger.error(error_msg)
            raise StickerMuleError(error_msg) from exc
        except httpx.HTTPError as exc:
            error_msg = f"Sticker Mule HTTP error: {exc}"
            logger.error(error_msg)
            raise StickerMuleError(error_msg) from exc

    @retry(max_retries=2, service="sticker_mule")
    def get_order_status(self, order_id: str) -> str:
        """
        Get the current status of a Sticker Mule order.

        Args:
            order_id: Sticker Mule order ID.

        Returns:
            Status string: 'processing', 'shipped', 'delivered', or 'unknown'.
        """
        try:
            response = self._http.get(
                f"{STICKER_MULE_API_BASE}/orders/{order_id}",
                headers=self._get_headers(),
            )
            response.raise_for_status()
            data = response.json()

            status = data.get("status", "unknown")
            logger.debug("Sticker Mule order %s status: %s", order_id, status)
            return status

        except Exception as exc:
            logger.error("Failed to get Sticker Mule order %s status: %s", order_id, exc)
            raise StickerMuleError(f"Status check failed: {exc}") from exc

    @retry(max_retries=2, service="sticker_mule")
    def get_tracking_number(self, order_id: str) -> Optional[str]:
        """
        Get the tracking number for a shipped Sticker Mule order.

        Args:
            order_id: Sticker Mule order ID.

        Returns:
            Tracking number string, or None if not yet shipped.
        """
        try:
            response = self._http.get(
                f"{STICKER_MULE_API_BASE}/orders/{order_id}",
                headers=self._get_headers(),
            )
            response.raise_for_status()
            data = response.json()

            tracking = data.get("tracking_number") or data.get("tracking", {}).get("number")
            if tracking:
                logger.info("Tracking number for order %s: %s", order_id, tracking)
            return tracking

        except Exception as exc:
            logger.error("Failed to get tracking for order %s: %s", order_id, exc)
            return None

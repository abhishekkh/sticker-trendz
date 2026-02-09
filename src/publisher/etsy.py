"""
Etsy listing publisher for Sticker Trendz.

Creates, updates, and deactivates Etsy listings via the Etsy Open API v3.
Handles listing creation with SEO-optimized copy, image uploads,
price management, and active listing cap enforcement.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from src.config import load_config
from src.db import SupabaseClient, DatabaseError
from src.publisher.etsy_auth import EtsyAuthManager, OAuthError
from src.publisher.etsy_rate_limiter import EtsyRateLimiter, P1_NEW_LISTINGS, P2_PRICE_UPDATES
from src.publisher.seo import SEOGenerator
from src.monitoring.error_logger import ErrorLogger
from src.resilience import retry, RetryExhaustedError

logger = logging.getLogger(__name__)

ETSY_API_BASE = "https://openapi.etsy.com/v3/application"


class EtsyPublisherError(Exception):
    """Raised on Etsy listing errors."""


class EtsyPublisher:
    """
    Etsy API v3 integration for managing sticker listings.

    Supports: create draft listings, upload images, activate listings,
    update prices, and deactivate listings.
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        auth: Optional[EtsyAuthManager] = None,
        rate_limiter: Optional[EtsyRateLimiter] = None,
        seo: Optional[SEOGenerator] = None,
        error_logger: Optional[ErrorLogger] = None,
        shop_id: Optional[str] = None,
        etsy_api_key: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
        max_active_listings: int = 300,
    ) -> None:
        self._db = db or SupabaseClient()
        self._auth = auth
        self._rate_limiter = rate_limiter
        self._seo = seo
        self._error_logger = error_logger

        cfg = load_config(require_all=False)
        self._shop_id = shop_id or cfg.etsy.shop_id
        self._api_key = etsy_api_key or cfg.etsy.api_key
        self._http = http_client or httpx.Client(timeout=30)
        self._max_active = max_active_listings

    def _get_headers(self) -> Dict[str, str]:
        """Get authorization headers for Etsy API calls."""
        if self._auth:
            token = self._auth.get_access_token(self._shop_id)
            return {
                "Authorization": f"Bearer {token}",
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
            }
        return {"x-api-key": self._api_key, "Content-Type": "application/json"}

    def _track_api_call(self, count: int = 1) -> None:
        """Track an Etsy API call in Redis."""
        if self._rate_limiter:
            self._rate_limiter.increment_api_calls(count)

    def create_listing(
        self,
        sticker: Dict[str, Any],
        trend: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Create an Etsy listing for a sticker.

        Args:
            sticker: Sticker dict from Supabase.
            trend: Associated trend dict (for topic context).

        Returns:
            Etsy listing ID string, or None on failure.
        """
        sticker_id = sticker.get("id", "")
        topic = trend.get("topic", "") if trend else sticker.get("title", "")
        size = sticker.get("size", "3in")
        price = float(sticker.get("price", 4.49))

        # Check rate limit
        if self._rate_limiter and not self._rate_limiter.can_proceed(P1_NEW_LISTINGS):
            logger.warning("Etsy API rate limit: skipping listing creation")
            return None

        # Check active listing cap
        active_count = self._db.count_active_listings()
        if active_count >= self._max_active:
            logger.warning(
                "Active listing cap reached (%d/%d), skipping",
                active_count, self._max_active,
            )
            return None

        # Generate SEO copy
        if self._seo:
            title = self._seo.generate_title(topic)
            tags = self._seo.generate_tags(topic, sticker.get("keywords"))
            description = self._seo.generate_description(topic, size)
        else:
            title = f"{topic} Sticker - Vinyl Decal"[:MAX_TITLE_LEN]
            tags = ["vinyl sticker", "laptop sticker", "free shipping"]
            description = f"Trending {topic} sticker."

        # Build listing payload
        payload = {
            "title": title[:140],
            "description": description,
            "price": price,
            "who_made": "someone_else",
            "when_made": "2020_2025",
            "is_supply": False,
            "quantity": 999,
            "tags": tags[:13],
            "shipping_profile_id": None,  # Set during shop setup
            "state": "draft",
        }

        try:
            headers = self._get_headers()
            url = f"{ETSY_API_BASE}/shops/{self._shop_id}/listings"
            response = self._http.post(url, json=payload, headers=headers)
            self._track_api_call()

            if response.status_code == 429:
                logger.warning("Etsy rate limit hit (429), stopping this cycle")
                return None

            response.raise_for_status()
            listing_data = response.json()
            listing_id = str(listing_data.get("listing_id", ""))

            # Upload images
            self._upload_listing_image(listing_id, sticker.get("image_url", ""))

            # Activate the listing
            self._activate_listing(listing_id)

            # Update sticker record
            now = datetime.now(timezone.utc).isoformat()
            self._db.update_sticker(sticker_id, {
                "etsy_listing_id": listing_id,
                "published_at": now,
                "title": title,
                "description": description,
                "tags": tags,
                "price": price,
            })

            logger.info(
                "Created Etsy listing %s for sticker %s",
                listing_id, sticker_id,
            )
            return listing_id

        except httpx.HTTPStatusError as exc:
            logger.error("Etsy listing creation failed: %s", exc)
            if self._error_logger:
                self._error_logger.log_error(
                    workflow="publisher",
                    step="create_listing",
                    error_type="api_error",
                    error_message=str(exc),
                    service="etsy",
                    context={"sticker_id": sticker_id},
                )
            return None
        except OAuthError as exc:
            logger.error("Etsy auth failed: %s", exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error creating listing: %s", exc)
            return None

    def _upload_listing_image(self, listing_id: str, image_url: str) -> None:
        """Upload an image to an Etsy listing."""
        if not image_url:
            return

        try:
            # Validate image URL is from our R2 bucket (prevent SSRF)
            cfg = load_config(require_all=False)
            if cfg.r2.public_url and not image_url.startswith(cfg.r2.public_url):
                logger.warning(
                    "Image URL not from R2 bucket, skipping upload: %s",
                    image_url[:100],
                )
                return

            # Download image from R2
            img_response = self._http.get(image_url, timeout=30)
            img_response.raise_for_status()

            # Validate content type
            content_type = img_response.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                logger.warning(
                    "Invalid content type '%s' for image URL, skipping",
                    content_type,
                )
                return

            image_bytes = img_response.content

            # Upload to Etsy
            headers = self._get_headers()
            headers.pop("Content-Type", None)  # Let httpx set multipart content type
            url = f"{ETSY_API_BASE}/shops/{self._shop_id}/listings/{listing_id}/images"

            files = {"image": ("sticker.png", image_bytes, "image/png")}
            response = self._http.post(url, files=files, headers=headers)
            self._track_api_call()
            response.raise_for_status()
            logger.info("Uploaded image to listing %s", listing_id)

        except Exception as exc:
            logger.error("Image upload failed for listing %s: %s", listing_id, exc)

    def _activate_listing(self, listing_id: str) -> None:
        """Activate a draft listing."""
        try:
            headers = self._get_headers()
            url = f"{ETSY_API_BASE}/shops/{self._shop_id}/listings/{listing_id}"
            response = self._http.put(
                url,
                json={"state": "active"},
                headers=headers,
            )
            self._track_api_call()
            response.raise_for_status()
            logger.info("Activated listing %s", listing_id)
        except Exception as exc:
            logger.error("Failed to activate listing %s: %s", listing_id, exc)

    def update_listing_price(self, listing_id: str, new_price: float) -> bool:
        """
        Update an existing listing's price.

        Args:
            listing_id: Etsy listing ID.
            new_price: New price in USD.

        Returns:
            True on success.
        """
        if self._rate_limiter and not self._rate_limiter.can_proceed(P2_PRICE_UPDATES):
            logger.warning("Etsy API rate limit: skipping price update")
            return False

        try:
            headers = self._get_headers()
            url = f"{ETSY_API_BASE}/shops/{self._shop_id}/listings/{listing_id}"
            response = self._http.put(
                url,
                json={"price": new_price},
                headers=headers,
            )
            self._track_api_call()
            response.raise_for_status()
            logger.info("Updated listing %s price to $%.2f", listing_id, new_price)
            return True
        except Exception as exc:
            logger.error("Price update failed for listing %s: %s", listing_id, exc)
            return False

    def deactivate_listing(self, listing_id: str) -> bool:
        """
        Deactivate (archive) an Etsy listing.

        Args:
            listing_id: Etsy listing ID.

        Returns:
            True on success.
        """
        try:
            headers = self._get_headers()
            url = f"{ETSY_API_BASE}/shops/{self._shop_id}/listings/{listing_id}"
            response = self._http.delete(url, headers=headers)
            self._track_api_call()

            if response.status_code in (200, 204):
                logger.info("Deactivated listing %s", listing_id)
                return True

            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Failed to deactivate listing %s: %s", listing_id, exc)
            return False


MAX_TITLE_LEN = 140

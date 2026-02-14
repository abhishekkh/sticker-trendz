"""
Etsy OAuth 2.0 token manager for Sticker Trendz.

Reads tokens from the Supabase etsy_tokens table, auto-refreshes
access tokens expiring within 5 minutes, handles concurrent refresh
via row locking, and alerts on invalid_grant errors.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import httpx

from src.config import load_config
from src.db import SupabaseClient, DatabaseError
from src.resilience import retry, RetryExhaustedError

logger = logging.getLogger(__name__)

ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
REFRESH_BUFFER_MINUTES = 5


class OAuthError(Exception):
    """Raised on OAuth token errors."""


class InvalidGrantError(OAuthError):
    """Raised when Etsy returns invalid_grant -- manual re-auth required."""


class EtsyAuthManager:
    """
    Manages Etsy OAuth 2.0 tokens.

    Reads tokens from Supabase, refreshes when expiring within 5 minutes,
    handles concurrent refresh, and alerts on fatal auth failures.
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        etsy_api_key: Optional[str] = None,
        alerter: Optional[Any] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        """
        Args:
            db: Supabase client for token storage.
            etsy_api_key: Etsy client_id used for token refresh.
            alerter: EmailAlerter instance for sending failure alerts.
            http_client: Injectable HTTP client for testing.
        """
        self._db = db or SupabaseClient()
        cfg = load_config(require_all=False)
        self._etsy_api_key = etsy_api_key or cfg.etsy.api_key
        self._alerter = alerter
        self._http = http_client or httpx.Client(timeout=30)

    def get_access_token(self, shop_id: str) -> str:
        """
        Get a valid access token for the given shop.

        If the current token expires within 5 minutes, it is refreshed
        before returning. Concurrent refresh is handled via Supabase
        row-level locking.

        Args:
            shop_id: The Etsy shop ID.

        Returns:
            A valid access token string.

        Raises:
            OAuthError: On general token errors.
            InvalidGrantError: If the refresh token is invalid (manual re-auth needed).
        """
        token_row = self._db.get_etsy_token(shop_id)
        if not token_row:
            raise OAuthError(
                f"No Etsy token found for shop_id '{shop_id}'. "
                f"Please complete the OAuth authorization flow first."
            )

        expires_at_str = token_row.get("expires_at", "")
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            logger.warning("Invalid expires_at format: %s, forcing refresh", expires_at_str)
            expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

        buffer = datetime.now(timezone.utc) + timedelta(minutes=REFRESH_BUFFER_MINUTES)

        if expires_at > buffer:
            # Token is still valid
            logger.debug("Etsy token for shop %s is valid until %s", shop_id, expires_at)
            return token_row["access_token"]

        # Token is expiring soon -- refresh it
        logger.info("Etsy token for shop %s expiring soon, refreshing...", shop_id)
        return self._refresh_token(shop_id, token_row["refresh_token"])

    def _refresh_token(self, shop_id: str, refresh_token: str) -> str:
        """
        Refresh the Etsy access token.

        Args:
            shop_id: The Etsy shop ID.
            refresh_token: The current refresh token.

        Returns:
            The new access token.

        Raises:
            InvalidGrantError: If the refresh token is invalid.
            OAuthError: On other refresh failures.
        """
        payload = {
            "grant_type": "refresh_token",
            "client_id": self._etsy_api_key,
            "refresh_token": refresh_token,
        }

        try:
            response = self._http.post(ETSY_TOKEN_URL, data=payload)
        except httpx.HTTPError as exc:
            raise OAuthError(f"HTTP error refreshing Etsy token: {exc}") from exc

        if response.status_code == 200:
            data = response.json()
            new_access_token = data["access_token"]
            new_refresh_token = data.get("refresh_token", refresh_token)
            expires_in = data.get("expires_in", 3600)
            new_expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()

            # Conditional update: only write if our refresh_token is still current.
            # If another concurrent process already refreshed the token, this will
            # update 0 rows. In that case, re-read and return the already-refreshed token.
            try:
                updated = self._db.update(
                    "etsy_tokens",
                    {"shop_id": shop_id, "refresh_token": refresh_token},
                    {
                        "access_token": new_access_token,
                        "refresh_token": new_refresh_token,
                        "expires_at": new_expires_at,
                    },
                )
                if not updated:
                    logger.info(
                        "Etsy token for shop %s was already refreshed by another process; "
                        "re-reading from DB",
                        shop_id,
                    )
                    current = self._db.get_etsy_token(shop_id)
                    if current and current.get("access_token"):
                        return current["access_token"]
                    raise OAuthError(
                        f"Concurrent refresh detected for shop {shop_id} but re-read returned no token"
                    )
                logger.info(
                    "Etsy token refreshed for shop %s, expires at %s",
                    shop_id, new_expires_at,
                )
            except DatabaseError as exc:
                logger.error("Failed to store refreshed Etsy token: %s", exc)
                raise OAuthError(f"Token refreshed but storage failed: {exc}") from exc

            return new_access_token

        # Handle error responses
        error_body = response.text
        if response.status_code == 400 and "invalid_grant" in error_body.lower():
            error_msg = (
                f"Etsy refresh token is invalid for shop {shop_id}. "
                f"Manual re-authorization required."
            )
            logger.critical(error_msg)

            # Send critical alert
            if self._alerter:
                try:
                    self._alerter.send_oauth_failure_alert(shop_id, error_body)
                except Exception as alert_exc:
                    logger.error("Failed to send OAuth failure alert: %s", alert_exc)

            raise InvalidGrantError(error_msg)

        raise OAuthError(
            f"Etsy token refresh failed (HTTP {response.status_code}): {error_body}"
        )

    def is_token_valid(self, shop_id: str) -> bool:
        """
        Check whether the stored token for a shop is currently valid
        (not expiring within the buffer window).

        Args:
            shop_id: The Etsy shop ID.

        Returns:
            True if the token is valid, False if it needs refreshing.
        """
        token_row = self._db.get_etsy_token(shop_id)
        if not token_row:
            return False

        expires_at_str = token_row.get("expires_at", "")
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return False

        buffer = datetime.now(timezone.utc) + timedelta(minutes=REFRESH_BUFFER_MINUTES)
        return expires_at > buffer

#!/usr/bin/env python3
"""
Etsy OAuth 2.0 PKCE authorization flow for Sticker Trendz.

Performs the initial OAuth handshake to obtain access and refresh tokens.
Run this once before running setup_etsy_shop.py.

Prerequisites:
  1. Create an Etsy developer app at https://developer.etsy.com
  2. Set the redirect URI to: http://localhost:3003/callback
  3. Set ETSY_API_KEY in your .env file (this is the "keystring" / client_id)

Usage:
    python scripts/etsy_oauth.py

What it does:
  1. Generates a PKCE code_verifier and code_challenge
  2. Opens your browser to the Etsy authorization page
  3. Starts a local HTTP server on port 3003 to capture the callback
  4. Exchanges the authorization code for access + refresh tokens
  5. Auto-detects your shop_id from the Etsy API
  6. Saves everything to .etsy_token.json (git-ignored)
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TOKEN_FILE = PROJECT_ROOT / ".etsy_token.json"
REDIRECT_URI = "http://localhost:3003/callback"
ETSY_AUTH_URL = "https://www.etsy.com/oauth/connect"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
ETSY_API_BASE = "https://openapi.etsy.com/v3/application"

# Scopes needed for shop management
SCOPES = [
    "shops_r",
    "shops_w",
    "listings_r",
    "listings_w",
    "listings_d",
    "profile_r",
]


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _build_auth_url(client_id: str, code_challenge: str, state: str) -> str:
    """Build the Etsy OAuth authorization URL."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{ETSY_AUTH_URL}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth callback."""

    auth_code: str | None = None
    received_state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/callback":
            if "error" in params:
                _CallbackHandler.error = params["error"][0]
                self._respond("Authorization denied. You can close this tab.")
            elif "code" in params:
                _CallbackHandler.auth_code = params["code"][0]
                _CallbackHandler.received_state = params.get("state", [""])[0]
                self._respond(
                    "Authorization successful! You can close this tab and return to the terminal."
                )
            else:
                self._respond("Unexpected callback. Check the terminal.")
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, message: str) -> None:
        html = f"<html><body><h2>{message}</h2></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default request logging."""
        pass


def _wait_for_callback(timeout: int = 120) -> tuple[str | None, str | None, str | None]:
    """Start local server and wait for the OAuth callback."""
    server = http.server.HTTPServer(("127.0.0.1", 3003), _CallbackHandler)
    server.timeout = timeout

    # Reset class-level state
    _CallbackHandler.auth_code = None
    _CallbackHandler.received_state = None
    _CallbackHandler.error = None

    print(f"\nWaiting for Etsy authorization (timeout: {timeout}s)...")
    print("If the browser didn't open, visit the URL printed above.\n")

    deadline = time.time() + timeout
    while time.time() < deadline:
        server.timeout = max(1, deadline - time.time())
        server.handle_request()
        if _CallbackHandler.auth_code or _CallbackHandler.error:
            break

    server.server_close()
    return _CallbackHandler.auth_code, _CallbackHandler.received_state, _CallbackHandler.error


def _exchange_code(
    client_id: str, code: str, code_verifier: str
) -> dict:
    """Exchange the authorization code for access + refresh tokens."""
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code": code,
        "code_verifier": code_verifier,
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(ETSY_TOKEN_URL, data=payload)
        resp.raise_for_status()
        return resp.json()


def _get_shop_id(client_id: str, access_token: str) -> str | None:
    """Auto-detect the user's shop ID from the Etsy API."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "x-api-key": client_id,
    }
    with httpx.Client(timeout=30) as client:
        # The token response includes user_id in the token data
        # Use getMe to find the user, then their shops
        resp = client.get(f"{ETSY_API_BASE}/users/me", headers=headers)
        resp.raise_for_status()
        user = resp.json()
        user_id = user.get("user_id")

        if not user_id:
            return None

        resp = client.get(f"{ETSY_API_BASE}/users/{user_id}/shops", headers=headers)
        resp.raise_for_status()
        shops = resp.json().get("results", [])
        if shops:
            shop_id = str(shops[0].get("shop_id", ""))
            shop_name = shops[0].get("shop_name", "")
            print(f"  Found shop: {shop_name} (id: {shop_id})")
            return shop_id

    return None


def _save_token(data: dict) -> None:
    """Save token data to .etsy_token.json."""
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    print(f"\nToken saved to: {TOKEN_FILE}")


def main() -> None:
    client_id = os.getenv("ETSY_API_KEY", "").strip()
    if not client_id:
        print("Error: ETSY_API_KEY not set in .env")
        print("  1. Go to https://developer.etsy.com → your app")
        print("  2. Copy the 'Keystring' (this is your client_id / API key)")
        print("  3. Add it to .env as ETSY_API_KEY=<your-keystring>")
        sys.exit(1)

    # Check if token already exists
    if TOKEN_FILE.exists():
        existing = json.loads(TOKEN_FILE.read_text())
        print(f"Existing token found for shop_id: {existing.get('shop_id', 'unknown')}")
        answer = input("Re-authorize? (y/N): ").strip().lower()
        if answer != "y":
            print("Keeping existing token.")
            return

    # Generate PKCE values
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    # Build and open auth URL
    auth_url = _build_auth_url(client_id, code_challenge, state)
    print("Opening browser for Etsy authorization...")
    print(f"\nIf the browser doesn't open, visit this URL:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Wait for callback
    auth_code, received_state, error = _wait_for_callback()

    if error:
        print(f"\nAuthorization error: {error}")
        sys.exit(1)

    if not auth_code:
        print("\nTimeout waiting for authorization. Please try again.")
        sys.exit(1)

    if received_state != state:
        print("\nState mismatch — possible CSRF. Please try again.")
        sys.exit(1)

    # Exchange code for tokens
    print("Exchanging authorization code for tokens...")
    token_data = _exchange_code(client_id, auth_code, code_verifier)

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in", 3600)

    print(f"  Access token obtained (expires in {expires_in}s)")

    # Auto-detect shop ID
    print("Detecting shop ID...")
    shop_id = _get_shop_id(client_id, access_token)

    if not shop_id:
        print("  Could not auto-detect shop ID.")
        shop_id = input("  Enter your shop ID manually: ").strip()

    # Save everything
    save_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "shop_id": shop_id,
        "client_id": client_id,
    }
    _save_token(save_data)

    # Print .env updates
    print("\n" + "=" * 50)
    print("Add/update these in your .env file:")
    print("=" * 50)
    print(f"ETSY_API_KEY={client_id}")
    print(f"ETSY_SHOP_ID={shop_id}")
    print("=" * 50)

    print("\nDone! Now run: python scripts/setup_etsy_shop.py")


if __name__ == "__main__":
    main()

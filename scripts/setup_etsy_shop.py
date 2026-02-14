#!/usr/bin/env python3
"""
One-time Etsy shop setup script for Sticker Trendz.

Automates initial shop configuration via the Etsy Open API v3:
  1. Looks up the "Stickers" taxonomy ID
  2. Creates 4 shop sections (Trending Now, Popular, New Drops, Under $5)
  3. Creates a free US shipping profile
  4. Prints all IDs as env vars for copy-paste into .env
  5. Prints About page content for manual paste into Etsy dashboard

Usage:
    python scripts/setup_etsy_shop.py

Prerequisites:
  - Run `python scripts/etsy_oauth.py` first to obtain an access token
  - Requires .etsy_token.json (created by the OAuth script)
"""

from __future__ import annotations

import json
import sys
import logging
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("setup_etsy_shop")

ETSY_API_BASE = "https://openapi.etsy.com/v3/application"
TOKEN_FILE = PROJECT_ROOT / ".etsy_token.json"

SHOP_SECTIONS = [
    {"title": "Trending Now", "sort_order": 1},
    {"title": "Popular", "sort_order": 2},
    {"title": "New Drops", "sort_order": 3},
    {"title": "Under $5", "sort_order": 4},
]

# Map section titles to env var suffixes
_SECTION_ENV_KEYS = {
    "Trending Now": "ETSY_SECTION_TRENDING_NOW",
    "Popular": "ETSY_SECTION_POPULAR",
    "New Drops": "ETSY_SECTION_NEW_DROPS",
    "Under $5": "ETSY_SECTION_UNDER_5",
}


def _load_token() -> dict:
    """Load token data from .etsy_token.json."""
    if not TOKEN_FILE.exists():
        logger.error(
            "Token file not found: %s\n"
            "Run 'python scripts/etsy_oauth.py' first to authorize.",
            TOKEN_FILE,
        )
        sys.exit(1)

    data = json.loads(TOKEN_FILE.read_text())
    if not data.get("access_token") or not data.get("shop_id"):
        logger.error("Token file is missing access_token or shop_id. Re-run etsy_oauth.py.")
        sys.exit(1)

    return data


def _get_headers(api_key: str, access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }


def find_sticker_taxonomy(client: httpx.Client, headers: dict[str, str]) -> int | None:
    """Search the Etsy seller taxonomy tree for a 'Stickers' node."""
    url = f"{ETSY_API_BASE}/seller-taxonomy/nodes"
    resp = client.get(url, headers=headers)
    resp.raise_for_status()
    nodes = resp.json().get("results", [])

    def _search(nodes: list[dict]) -> int | None:
        for node in nodes:
            name = node.get("name", "")
            if name.lower() == "stickers":
                return node.get("id")
            children = node.get("children", [])
            if children:
                found = _search(children)
                if found:
                    return found
        return None

    return _search(nodes)


def create_shop_sections(
    client: httpx.Client,
    headers: dict[str, str],
    shop_id: str,
) -> dict[str, int]:
    """Create shop sections and return a map of title -> section_id."""
    created: dict[str, int] = {}

    # Check existing sections first
    list_url = f"{ETSY_API_BASE}/shops/{shop_id}/sections"
    resp = client.get(list_url, headers=headers)
    resp.raise_for_status()
    existing = {s["title"]: s["shop_section_id"] for s in resp.json().get("results", [])}

    for section in SHOP_SECTIONS:
        title = section["title"]
        if title in existing:
            logger.info("Section '%s' already exists (id=%d), skipping", title, existing[title])
            created[title] = existing[title]
            continue

        url = f"{ETSY_API_BASE}/shops/{shop_id}/sections"
        resp = client.post(url, json={"title": title}, headers=headers)
        resp.raise_for_status()
        section_id = resp.json().get("shop_section_id")
        logger.info("Created section '%s' -> id=%d", title, section_id)
        created[title] = section_id

    return created


def create_shipping_profile(
    client: httpx.Client,
    headers: dict[str, str],
    shop_id: str,
) -> int:
    """Create a free US-only shipping profile and return its ID."""
    url = f"{ETSY_API_BASE}/shops/{shop_id}/shipping-profiles"
    payload = {
        "title": "Free US Shipping",
        "origin_country_iso": "US",
        "primary_cost": 0.0,
        "secondary_cost": 0.0,
        "min_processing_days": 3,
        "max_processing_days": 5,
        "destination_country_iso": "US",
    }
    resp = client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    profile_id = resp.json().get("shipping_profile_id")
    logger.info("Created shipping profile 'Free US Shipping' -> id=%d", profile_id)
    return profile_id


def print_env_vars(
    shop_id: str,
    taxonomy_id: int | None,
    sections: dict[str, int],
    shipping_profile_id: int,
) -> None:
    """Print env vars in copy-paste format."""
    print("\n" + "=" * 60)
    print("Add these to your .env file:")
    print("=" * 60)
    print(f"ETSY_SHOP_ID={shop_id}")
    if taxonomy_id:
        print(f"ETSY_TAXONOMY_ID={taxonomy_id}")
    else:
        print("# ETSY_TAXONOMY_ID=  (not found -- set manually)")
    print(f"ETSY_SHIPPING_PROFILE_ID={shipping_profile_id}")
    for title, section_id in sections.items():
        env_key = _SECTION_ENV_KEYS.get(title, f"ETSY_SECTION_{title.upper().replace(' ', '_')}")
        print(f"{env_key}={section_id}")
    print("=" * 60)


def print_about_page() -> None:
    """Print the About page content from the template file."""
    about_path = PROJECT_ROOT / "data" / "etsy_shop_about.txt"
    if about_path.exists():
        print("\n" + "=" * 60)
        print("About page content (paste into Etsy > Shop Manager > About):")
        print("=" * 60)
        print(about_path.read_text())
        print("=" * 60)
    else:
        logger.warning("About page template not found at %s", about_path)


def main() -> None:
    token_data = _load_token()
    access_token = token_data["access_token"]
    shop_id = token_data["shop_id"]
    api_key = token_data.get("client_id", "")

    if not api_key:
        import os
        api_key = os.getenv("ETSY_API_KEY", "")
    if not api_key:
        logger.error("No API key found. Set ETSY_API_KEY in .env or re-run etsy_oauth.py.")
        sys.exit(1)

    logger.info("Setting up Etsy shop %s...", shop_id)

    client = httpx.Client(timeout=30)
    headers = _get_headers(api_key, access_token)

    try:
        # 1. Find sticker taxonomy
        logger.info("Looking up Stickers taxonomy...")
        taxonomy_id = find_sticker_taxonomy(client, headers)
        if taxonomy_id:
            logger.info("Found Stickers taxonomy_id = %d", taxonomy_id)
        else:
            logger.warning("Could not find 'Stickers' taxonomy node -- set ETSY_TAXONOMY_ID manually")

        # 2. Create shop sections
        logger.info("Creating shop sections...")
        sections = create_shop_sections(client, headers, shop_id)

        # 3. Create shipping profile
        logger.info("Creating shipping profile...")
        shipping_profile_id = create_shipping_profile(client, headers, shop_id)

        # 4. Print env vars
        print_env_vars(shop_id, taxonomy_id, sections, shipping_profile_id)

        # 5. Print About page content
        print_about_page()

    finally:
        client.close()

    logger.info("Setup complete!")


if __name__ == "__main__":
    main()

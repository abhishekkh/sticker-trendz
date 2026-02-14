"""
Configuration module for Sticker Trendz.

Loads all environment variables with validation and sensible defaults.
Raises clear errors if required variables are missing.
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when a required configuration variable is missing or invalid."""


def _require(name: str) -> str:
    """Return the value of an environment variable or raise ConfigError."""
    value = os.getenv(name)
    if not value:
        raise ConfigError(
            f"Required environment variable '{name}' is not set. "
            f"See .env.example for details."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    """Return the value of an environment variable or the default."""
    return os.getenv(name, default)


def _optional_int(name: str, default: int) -> int:
    """Return an integer environment variable or the default."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Environment variable '%s' has non-integer value '%s', using default %d",
            name, raw, default,
        )
        return default


def _optional_float(name: str, default: float) -> float:
    """Return a float environment variable or the default."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Environment variable '%s' has non-numeric value '%s', using default %f",
            name, raw, default,
        )
        return default


def _load_shop_sections() -> Dict[str, int]:
    """Load Etsy shop section IDs from environment variables."""
    section_map = {
        "trending_now": "ETSY_SECTION_TRENDING_NOW",
        "popular": "ETSY_SECTION_POPULAR",
        "new_drops": "ETSY_SECTION_NEW_DROPS",
        "under_5": "ETSY_SECTION_UNDER_5",
    }
    sections: Dict[str, int] = {}
    for key, env_var in section_map.items():
        raw = os.getenv(env_var)
        if raw:
            try:
                sections[key] = int(raw)
            except ValueError:
                logger.warning(
                    "Environment variable '%s' has non-integer value '%s', skipping",
                    env_var, raw,
                )
    return sections


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    base_url: str = ""
    scoring_model: str = "gemini-2.0-flash"
    prompt_model: str = "gemini-2.0-flash"
    seo_model: str = "gemini-2.0-flash"
    moderation_api_key: str = ""


@dataclass(frozen=True)
class ReplicateConfig:
    api_token: str
    model_id: str = "black-forest-labs/flux-schnell"
    model_version: str = ""
    image_size: int = 1024


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    service_key: str


@dataclass(frozen=True)
class RedisConfig:
    url: str
    token: str


@dataclass(frozen=True)
class R2Config:
    access_key: str
    secret_key: str
    bucket: str
    endpoint: str
    public_url: str


@dataclass(frozen=True)
class EtsyConfig:
    api_key: str
    api_secret: str
    shop_id: str
    taxonomy_id: Optional[int] = None
    shipping_profile_id: Optional[int] = None
    shop_sections: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RedditConfig:
    client_id: str
    client_secret: str
    user_agent: str


@dataclass(frozen=True)
class FulfillmentConfig:
    sticker_mule_api_key: str


@dataclass(frozen=True)
class NotificationConfig:
    sendgrid_api_key: str
    alert_email: str


@dataclass(frozen=True)
class CapsConfig:
    max_trends_per_cycle: int = 5
    max_images_per_day: int = 50
    max_active_listings: int = 300
    ai_monthly_budget_cap_usd: float = 150.0


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration."""

    openai: OpenAIConfig
    replicate: ReplicateConfig
    supabase: SupabaseConfig
    redis: RedisConfig
    r2: R2Config
    etsy: EtsyConfig
    reddit: RedditConfig
    fulfillment: FulfillmentConfig
    notification: NotificationConfig
    caps: CapsConfig


def load_config(require_all: bool = True) -> AppConfig:
    """
    Load and validate all configuration from environment variables.

    Args:
        require_all: If True (default), raise ConfigError for missing required
                     variables. Set to False for testing or partial initialization.

    Returns:
        Fully populated AppConfig instance.
    """
    getter = _require if require_all else lambda name: _optional(name, "")

    # Auto-detect LLM provider: prefer GEMINI_API_KEY, fall back to OPENAI_API_KEY
    gemini_key = _optional("GEMINI_API_KEY", "")
    openai_key = (
        getter("OPENAI_API_KEY")
        if require_all and not gemini_key
        else _optional("OPENAI_API_KEY", "")
    )

    if gemini_key:
        llm_api_key = gemini_key
        default_base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        default_model = "gemini-2.0-flash"
    else:
        llm_api_key = openai_key
        default_base_url = ""
        default_model = "gpt-4o-mini"

    return AppConfig(
        openai=OpenAIConfig(
            api_key=llm_api_key,
            base_url=_optional("LLM_BASE_URL", default_base_url),
            scoring_model=_optional("LLM_SCORING_MODEL", default_model),
            prompt_model=_optional("LLM_PROMPT_MODEL", default_model),
            seo_model=_optional("LLM_SEO_MODEL", default_model),
            moderation_api_key=openai_key,
        ),
        replicate=ReplicateConfig(
            api_token=getter("REPLICATE_API_TOKEN"),
            model_id=_optional("REPLICATE_MODEL_ID", "black-forest-labs/flux-schnell"),
            model_version=_optional("REPLICATE_MODEL_VERSION", ""),
            image_size=_optional_int("REPLICATE_IMAGE_SIZE", 1024),
        ),
        supabase=SupabaseConfig(
            url=getter("SUPABASE_URL"),
            service_key=getter("SUPABASE_SERVICE_KEY"),
        ),
        redis=RedisConfig(
            url=getter("UPSTASH_REDIS_URL"),
            token=getter("UPSTASH_REDIS_TOKEN"),
        ),
        r2=R2Config(
            access_key=getter("CLOUDFLARE_R2_ACCESS_KEY"),
            secret_key=getter("CLOUDFLARE_R2_SECRET_KEY"),
            bucket=getter("CLOUDFLARE_R2_BUCKET"),
            endpoint=getter("CLOUDFLARE_R2_ENDPOINT"),
            public_url=_optional("CLOUDFLARE_R2_PUBLIC_URL", ""),
        ),
        etsy=EtsyConfig(
            api_key=getter("ETSY_API_KEY"),
            api_secret=getter("ETSY_API_SECRET"),
            shop_id=getter("ETSY_SHOP_ID"),
            taxonomy_id=_optional_int("ETSY_TAXONOMY_ID", 0) or None,
            shipping_profile_id=_optional_int("ETSY_SHIPPING_PROFILE_ID", 0) or None,
            shop_sections=_load_shop_sections(),
        ),
        reddit=RedditConfig(
            client_id=getter("REDDIT_CLIENT_ID"),
            client_secret=getter("REDDIT_CLIENT_SECRET"),
            user_agent=_optional("REDDIT_USER_AGENT", "sticker-trendz/1.0"),
        ),
        fulfillment=FulfillmentConfig(
            sticker_mule_api_key=getter("STICKER_MULE_API_KEY"),
        ),
        notification=NotificationConfig(
            sendgrid_api_key=getter("SENDGRID_API_KEY"),
            alert_email=getter("ALERT_EMAIL"),
        ),
        caps=CapsConfig(
            max_trends_per_cycle=_optional_int("MAX_TRENDS_PER_CYCLE", 5),
            max_images_per_day=_optional_int("MAX_IMAGES_PER_DAY", 50),
            max_active_listings=_optional_int("MAX_ACTIVE_LISTINGS", 300),
            ai_monthly_budget_cap_usd=_optional_float(
                "AI_MONTHLY_BUDGET_CAP_USD", 150.0
            ),
        ),
    )


def setup_logging(level: int = logging.INFO) -> None:
    """Configure structured logging for all modules."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

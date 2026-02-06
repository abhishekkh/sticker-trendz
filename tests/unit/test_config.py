"""Tests for src/config.py -- environment variable loading and validation."""

import os
import pytest
from unittest.mock import patch

from src.config import load_config, ConfigError, _optional_int, _optional_float


class TestLoadConfig:
    """Tests for the load_config function."""

    def test_load_config_require_all_raises_on_missing(self):
        """Missing required env vars should raise ConfigError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
                load_config(require_all=True)

    def test_load_config_no_require_returns_defaults(self):
        """When require_all=False, empty strings are acceptable."""
        with patch.dict(os.environ, {}, clear=True):
            config = load_config(require_all=False)
            assert config.openai.api_key == ""
            assert config.caps.max_trends_per_cycle == 5

    def test_load_config_reads_all_env_vars(self):
        """All env vars are properly loaded into the config object."""
        env = {
            "OPENAI_API_KEY": "sk-test",
            "REPLICATE_API_TOKEN": "r8-test",
            "REPLICATE_MODEL_VERSION": "abc123",
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_KEY": "supa-key",
            "UPSTASH_REDIS_URL": "https://redis.upstash.io",
            "UPSTASH_REDIS_TOKEN": "redis-token",
            "CLOUDFLARE_R2_ACCESS_KEY": "r2-access",
            "CLOUDFLARE_R2_SECRET_KEY": "r2-secret",
            "CLOUDFLARE_R2_BUCKET": "sticker-trendz",
            "CLOUDFLARE_R2_ENDPOINT": "https://r2.example.com",
            "CLOUDFLARE_R2_PUBLIC_URL": "https://pub.r2.dev",
            "ETSY_API_KEY": "etsy-key",
            "ETSY_API_SECRET": "etsy-secret",
            "ETSY_SHOP_ID": "12345",
            "REDDIT_CLIENT_ID": "reddit-id",
            "REDDIT_CLIENT_SECRET": "reddit-secret",
            "REDDIT_USER_AGENT": "test-agent/1.0",
            "STICKER_MULE_API_KEY": "sm-key",
            "SENDGRID_API_KEY": "sg-key",
            "ALERT_EMAIL": "alerts@example.com",
            "MAX_TRENDS_PER_CYCLE": "10",
            "MAX_IMAGES_PER_DAY": "25",
            "MAX_ACTIVE_LISTINGS": "200",
            "AI_MONTHLY_BUDGET_CAP_USD": "100.0",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config(require_all=True)
            assert config.openai.api_key == "sk-test"
            assert config.replicate.api_token == "r8-test"
            assert config.replicate.model_version == "abc123"
            assert config.supabase.url == "https://test.supabase.co"
            assert config.redis.url == "https://redis.upstash.io"
            assert config.r2.bucket == "sticker-trendz"
            assert config.r2.public_url == "https://pub.r2.dev"
            assert config.etsy.shop_id == "12345"
            assert config.reddit.user_agent == "test-agent/1.0"
            assert config.fulfillment.sticker_mule_api_key == "sm-key"
            assert config.notification.alert_email == "alerts@example.com"
            assert config.caps.max_trends_per_cycle == 10
            assert config.caps.max_images_per_day == 25
            assert config.caps.max_active_listings == 200
            assert config.caps.ai_monthly_budget_cap_usd == 100.0

    def test_caps_defaults(self):
        """Caps should use defaults when env vars are not set."""
        env = {
            "OPENAI_API_KEY": "sk-test",
            "REPLICATE_API_TOKEN": "r8-test",
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_KEY": "supa-key",
            "UPSTASH_REDIS_URL": "https://redis.upstash.io",
            "UPSTASH_REDIS_TOKEN": "redis-token",
            "CLOUDFLARE_R2_ACCESS_KEY": "r2-access",
            "CLOUDFLARE_R2_SECRET_KEY": "r2-secret",
            "CLOUDFLARE_R2_BUCKET": "sticker-trendz",
            "CLOUDFLARE_R2_ENDPOINT": "https://r2.example.com",
            "ETSY_API_KEY": "etsy-key",
            "ETSY_API_SECRET": "etsy-secret",
            "ETSY_SHOP_ID": "12345",
            "REDDIT_CLIENT_ID": "reddit-id",
            "REDDIT_CLIENT_SECRET": "reddit-secret",
            "STICKER_MULE_API_KEY": "sm-key",
            "SENDGRID_API_KEY": "sg-key",
            "ALERT_EMAIL": "alerts@example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config(require_all=True)
            assert config.caps.max_trends_per_cycle == 5
            assert config.caps.max_images_per_day == 50
            assert config.caps.max_active_listings == 300
            assert config.caps.ai_monthly_budget_cap_usd == 150.0


class TestOptionalHelpers:
    """Tests for _optional_int and _optional_float."""

    def test_optional_int_returns_default_when_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _optional_int("MISSING_VAR", 42) == 42

    def test_optional_int_returns_parsed_value(self):
        with patch.dict(os.environ, {"MY_INT": "7"}, clear=True):
            assert _optional_int("MY_INT", 42) == 7

    def test_optional_int_returns_default_on_bad_value(self):
        with patch.dict(os.environ, {"MY_INT": "not-a-number"}, clear=True):
            assert _optional_int("MY_INT", 42) == 42

    def test_optional_float_returns_default_when_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _optional_float("MISSING_VAR", 3.14) == 3.14

    def test_optional_float_returns_parsed_value(self):
        with patch.dict(os.environ, {"MY_FLOAT": "2.72"}, clear=True):
            assert _optional_float("MY_FLOAT", 3.14) == 2.72

    def test_optional_float_returns_default_on_bad_value(self):
        with patch.dict(os.environ, {"MY_FLOAT": "nope"}, clear=True):
            assert _optional_float("MY_FLOAT", 3.14) == 3.14

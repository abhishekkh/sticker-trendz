-- =============================================================================
-- Sticker Trendz: Initial Database Schema
-- =============================================================================
-- Idempotent: safe to re-run (uses IF NOT EXISTS / ON CONFLICT).

-- ---------------------------------------------------------------------------
-- 1. Tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trends (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic TEXT NOT NULL,
    topic_normalized TEXT NOT NULL,
    keywords TEXT[] NOT NULL,
    sources TEXT[] NOT NULL,
    score_velocity FLOAT,
    score_commercial FLOAT,
    score_safety FLOAT,
    score_uniqueness FLOAT,
    score_overall FLOAT,
    reasoning TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    source_data JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stickers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trend_id UUID REFERENCES trends(id),
    title TEXT NOT NULL,
    description TEXT,
    image_url TEXT NOT NULL,
    thumbnail_url TEXT,
    original_url TEXT,
    size TEXT NOT NULL DEFAULT '3in',
    generation_prompt TEXT,
    generation_model TEXT DEFAULT 'stable-diffusion-xl',
    generation_model_version TEXT,
    moderation_status TEXT DEFAULT 'pending',
    moderation_score FLOAT,
    moderation_categories JSONB,
    etsy_listing_id TEXT,
    price DECIMAL(10,2) DEFAULT 4.49,
    current_pricing_tier TEXT DEFAULT 'just_dropped',
    floor_price DECIMAL(10,2),
    base_cost DECIMAL(10,2),
    shipping_cost DECIMAL(10,2),
    packaging_cost DECIMAL(10,2),
    fulfillment_provider TEXT DEFAULT 'sticker_mule',
    tags TEXT[],
    sales_count INTEGER DEFAULT 0,
    view_count INTEGER DEFAULT 0,
    last_sale_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    etsy_order_id TEXT UNIQUE,
    etsy_receipt_id TEXT,
    sticker_id UUID REFERENCES stickers(id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2),
    total_amount DECIMAL(10,2),
    fulfillment_provider TEXT,
    fulfillment_order_id TEXT,
    fulfillment_attempts INTEGER DEFAULT 0,
    fulfillment_last_error TEXT,
    status TEXT DEFAULT 'pending',
    pricing_tier_at_sale TEXT,
    customer_data JSONB,
    shipped_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    duration_seconds INTEGER,
    trends_found INTEGER DEFAULT 0,
    trends_scored INTEGER DEFAULT 0,
    stickers_generated INTEGER DEFAULT 0,
    stickers_published INTEGER DEFAULT 0,
    stickers_archived INTEGER DEFAULT 0,
    prices_updated INTEGER DEFAULT 0,
    orders_synced INTEGER DEFAULT 0,
    orders_fulfilled INTEGER DEFAULT 0,
    errors_count INTEGER DEFAULT 0,
    etsy_api_calls_used INTEGER DEFAULT 0,
    ai_cost_estimate_usd DECIMAL(10,4) DEFAULT 0,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS error_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_run_id UUID REFERENCES pipeline_runs(id),
    workflow TEXT NOT NULL,
    step TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    service TEXT,
    retry_count INTEGER DEFAULT 0,
    resolved BOOLEAN DEFAULT false,
    context JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS etsy_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shop_id TEXT NOT NULL UNIQUE,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_type TEXT DEFAULT 'Bearer',
    expires_at TIMESTAMPTZ NOT NULL,
    scopes TEXT[],
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pricing_tiers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tier TEXT NOT NULL UNIQUE,
    min_trend_age_days INTEGER,
    max_trend_age_days INTEGER,
    price_single_small DECIMAL(10,2),
    price_single_large DECIMAL(10,2),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shipping_rates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_type TEXT NOT NULL,
    fulfillment_provider TEXT NOT NULL,
    shipping_cost DECIMAL(10,2) NOT NULL,
    packaging_cost DECIMAL(10,2) NOT NULL,
    region TEXT NOT NULL DEFAULT 'us',
    is_active BOOLEAN DEFAULT true,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sticker_id UUID REFERENCES stickers(id),
    old_price DECIMAL(10,2),
    new_price DECIMAL(10,2),
    pricing_tier TEXT,
    reason TEXT,
    changed_at TIMESTAMPTZ DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 2. Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_trends_status ON trends(status);
CREATE INDEX IF NOT EXISTS idx_trends_topic_normalized ON trends(topic_normalized);
CREATE INDEX IF NOT EXISTS idx_trends_created ON trends(created_at);

CREATE INDEX IF NOT EXISTS idx_stickers_pricing_tier ON stickers(current_pricing_tier);
CREATE INDEX IF NOT EXISTS idx_stickers_trend_id ON stickers(trend_id);
CREATE INDEX IF NOT EXISTS idx_stickers_published ON stickers(published_at) WHERE published_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stickers_moderation ON stickers(moderation_status);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_sticker ON orders(sticker_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_workflow ON pipeline_runs(workflow);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs(started_at);

CREATE INDEX IF NOT EXISTS idx_error_log_workflow ON error_log(workflow);
CREATE INDEX IF NOT EXISTS idx_error_log_created ON error_log(created_at);
CREATE INDEX IF NOT EXISTS idx_error_log_unresolved ON error_log(resolved) WHERE resolved = false;

-- ---------------------------------------------------------------------------
-- 3. Materialized View: daily_metrics
-- ---------------------------------------------------------------------------

-- Drop and recreate to be idempotent (materialized views don't support IF NOT EXISTS well)
DROP MATERIALIZED VIEW IF EXISTS daily_metrics;

CREATE MATERIALIZED VIEW daily_metrics AS
SELECT
    date_trunc('day', o.created_at) AS date,
    COUNT(DISTINCT o.id) AS orders,
    SUM(o.total_amount) AS gross_revenue,
    SUM(s.base_cost * o.quantity) AS cogs,
    SUM(o.total_amount * 0.10) AS etsy_fees,
    SUM(o.total_amount) - SUM(s.base_cost * o.quantity) - SUM(o.total_amount * 0.10) AS estimated_profit,
    COUNT(DISTINCT s.id) FILTER (WHERE s.published_at::date = date_trunc('day', o.created_at)::date) AS new_listings,
    AVG(o.total_amount) AS avg_order_value
FROM orders o
JOIN stickers s ON o.sticker_id = s.id
WHERE o.status NOT IN ('refunded')
GROUP BY date_trunc('day', o.created_at);

-- ---------------------------------------------------------------------------
-- 4. View: cost_tracking
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW cost_tracking AS
SELECT
    date_trunc('day', pr.started_at) AS date,
    SUM(pr.ai_cost_estimate_usd) AS ai_spend,
    SUM(pr.etsy_api_calls_used) AS api_calls,
    SUM(pr.stickers_published) * 0.20 AS listing_fees,
    SUM(pr.stickers_published) AS stickers_published,
    SUM(pr.errors_count) AS total_errors
FROM pipeline_runs pr
GROUP BY date_trunc('day', pr.started_at);

-- ---------------------------------------------------------------------------
-- 5. Row-Level Security (defense in depth)
-- ---------------------------------------------------------------------------

ALTER TABLE trends ENABLE ROW LEVEL SECURITY;
ALTER TABLE stickers ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE etsy_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE error_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE pricing_tiers ENABLE ROW LEVEL SECURITY;
ALTER TABLE shipping_rates ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_history ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- 6. Seed data (idempotent via ON CONFLICT)
-- ---------------------------------------------------------------------------

INSERT INTO pricing_tiers (tier, min_trend_age_days, max_trend_age_days, price_single_small, price_single_large) VALUES
    ('just_dropped', 0,    3,    5.49, 6.49),
    ('trending',     3,    14,   4.49, 5.49),
    ('cooling',      14,   30,   3.49, 4.49),
    ('evergreen',    30,   NULL, 3.49, 4.49)
ON CONFLICT (tier) DO UPDATE SET
    min_trend_age_days = EXCLUDED.min_trend_age_days,
    max_trend_age_days = EXCLUDED.max_trend_age_days,
    price_single_small = EXCLUDED.price_single_small,
    price_single_large = EXCLUDED.price_single_large;

-- Unique constraint for shipping_rates to support idempotent inserts
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_shipping_rates_product_provider_region'
    ) THEN
        ALTER TABLE shipping_rates
        ADD CONSTRAINT uq_shipping_rates_product_provider_region
        UNIQUE (product_type, fulfillment_provider, region);
    END IF;
END $$;

INSERT INTO shipping_rates (product_type, fulfillment_provider, shipping_cost, packaging_cost, region) VALUES
    ('single_small', 'sticker_mule', 0.00, 0.00, 'us'),
    ('single_large', 'sticker_mule', 0.00, 0.00, 'us'),
    ('single_small', 'self_usps',    0.78, 0.15, 'us'),
    ('single_large', 'self_usps',    0.78, 0.20, 'us')
ON CONFLICT (product_type, fulfillment_provider, region) DO UPDATE SET
    shipping_cost = EXCLUDED.shipping_cost,
    packaging_cost = EXCLUDED.packaging_cost;

# Sticker Trendz -- MVP Specification

> **Scope**: This spec covers MVP (Phase 1) only. The storefront is Etsy -- there is no custom website, mobile app, or admin dashboard. The trend sources are Reddit and Google Trends only (TikTok and Twitter/X are descoped -- see "Scope Decisions" below). Sticker packs are descoped to V1.1. International shipping is excluded. See `trending-sticker-architecture.md` for the Phase 2-3 vision. Do NOT build anything from the architecture doc for MVP.

## Overview

A lean, automated system that detects trending topics on social media, generates sticker designs using AI, and sells them on Etsy -- all running on free-tier infrastructure.

**Target**: First sale within 2 weeks of launch. $2,500/mo revenue within 3 months.

---

## Scope Decisions

| Item | Decision | Rationale |
|---|---|---|
| Trend Sources | Reddit + Google Trends ONLY | TikTok has no free API and scraping violates ToS. Twitter/X free tier allows only 1,500 reads/month -- insufficient for trend monitoring. Reddit (free with OAuth, 60 req/min) and Google Trends (free via pytrends) provide sufficient signal. |
| Sticker Packs | DESCOPED to V1.1 | Packs add ~20-25% engineering scope (composite thumbnails, pack pricing, pack fulfillment). MVP validates the trend-to-Etsy pipeline with singles first. Add packs once single sticker sales prove demand. |
| International Shipping | EXCLUDED from MVP | Adds customs complexity, higher shipping rates, and fulfillment provider uncertainty. Etsy listing settings will restrict to US-only. Revisit for Phase 2. |
| Custom Storefront | NOT IN MVP | Etsy is the sole storefront. No custom website, mobile app, or admin dashboard. |
| Pinterest/Instagram/Reddit Marketing | MANUAL ONLY | MVP discovery is Etsy SEO only. Social media posting is manual and not part of the automated pipeline. No tooling will be built for these channels in MVP. |
| Etsy Ads | OPTIONAL, MANUAL | $0-5/day if budget allows. Not automated. |

---

## Architecture

```
GitHub Actions (cron every 2 hours)            GitHub Actions (daily 6AM)
     |                                              |
     v                                              v
+---------------------------------+   +-------------------------+
|  Trend Monitor (Python script)  |   |  Pricing Engine          |
|  - Poll Reddit (OAuth API)      |   |  - Adjust prices by      |
|  - Poll Google Trends           |   |    trend freshness        |
|  - Score with GPT-4o-mini       |   |  - Archive stale          |
+-----------+---------------------+   |    listings               |
            |                         |  - Log price history       |
            v                         +-------------------------+
+---------------------------------+
|  Sticker Generator              |   GitHub Actions (daily 8AM)
|  - GPT-4o-mini: trend -> prompt |        |
|  - Replicate: prompt -> image   |        v
|  - Pillow: post-processing      |   +-------------------------+
|  - Quality validation           |   |  Daily Analytics          |
+-----------+---------------------+   |  - Etsy sales sync        |
            |                         |  - Order fulfillment      |
            v                         |  - Metrics aggregation    |
+---------------------------------+   |  - Daily summary email    |
|  Content Moderation             |   +-------------------------+
|  - OpenAI Moderation API (free) |
|  - Keyword + trademark blocklist|
+-----------+---------------------+
            |
            v
+---------------------------------+
|  Publisher                      |
|  - Upload to Etsy via API       |
|  - Store metadata in Supabase   |
|  - Store images on Cloudflare R2|
+---------------------------------+
```

---

## Tech Stack

| Layer | Technology | Cost | Free Tier Notes |
|---|---|---|---|
| Compute / Cron | GitHub Actions (scheduled workflows) | Free (2,000 min/mo) | See "GitHub Actions Budget" section for minute calculations |
| Language | Python 3.12 | Free | |
| Database | Supabase (Postgres) | Free (500MB) | ~1 MB/month growth at 500 stickers/month; ~2+ years runway. Project pauses after 7 days inactivity (cron jobs prevent this). Plan migration to Pro ($25/mo) if approaching limits. |
| Cache | Upstash Redis | Free (10K commands/day) | Used for Etsy API rate limit tracking and dedup cache. Monitor daily command count. |
| Image Storage | Cloudflare R2 | Free (10GB, zero egress) | Also used for daily database backups (pg_dump). |
| CDN | Cloudflare | Free (unlimited bandwidth) | |
| AI -- Prompts | OpenAI GPT-4o-mini | ~$20-40/mo | Use structured output (response_format) for all JSON responses |
| AI -- Images | Replicate (Stable Diffusion XL) | ~$50-150/mo | Pin a specific SDXL model version for reproducibility |
| AI -- Moderation | OpenAI Moderation API | Free | |
| Storefront | Etsy | $0.20/listing + 6.5% transaction fee | Max 300 active listings (budget cap). App review required (1-4 weeks). |
| Fulfillment | Sticker Mule or self-fulfillment (USPS) | Per-order ($1-3/sticker) | Validate Sticker Mule API supports single on-demand orders before launch |
| Email | SendGrid | Free (100 emails/day) | |
| Repo / CI | GitHub | Free | |

**Total fixed monthly cost: $75-200**

---

## Pipeline: Trend to Etsy Listing

### Step 1 -- Trend Detection (2-hour cycle)

**Trigger**: GitHub Actions cron job runs every 2 hours.

**Data Sources** (free-tier APIs):

| Platform | API | What to Monitor | Auth Required |
|---|---|---|---|
| Reddit | Reddit OAuth API (PRAW or direct) | Rising posts in r/memes, r/funny, r/trending, niche subs | Yes -- register a Reddit API application (free). Provides 60 req/min with OAuth. Unauthenticated .json endpoints are rate-limited to 10 req/min and actively blocked. |
| Google Trends | Pytrends (unofficial) | Breakout search terms | No auth needed. Rate-limit to 5 requests per cycle to avoid IP blocks. |

**Process**:
1. Fetch recent trending data from each source
2. Extract keywords, hashtags, topics
3. Normalize trend topics (lowercase, stem keywords) for cross-source deduplication
4. Check keyword overlap across sources using Jaccard similarity (> 0.6 = same trend); merge into canonical trend with `sources` array
5. Check Supabase for duplicates (skip trends already processed -- match on normalized topic)
6. Send to GPT-4o-mini for scoring using structured output:
   - Trend velocity (how fast is it growing?)
   - Commercial viability (would someone buy a sticker of this?)
   - Content safety (is it appropriate?)
   - Uniqueness (have we already made stickers for this?)
7. Check against trademark blocklist (see Security section) -- auto-reject trademarked terms
8. Store top-scoring trends in Supabase with status `discovered`

**GPT-4o-mini Scoring Prompt** (uses structured output):
```
System: You are a trend analyst for a sticker business. Score this trend on four dimensions.

User: Score this trend for sticker commercial viability.

Trend: {topic}
Context: {sample_posts}
Sources: {source_list}

Return a JSON object with these exact fields:
- velocity (integer 1-10): how fast is this trend growing
- commercial (integer 1-10): would 18-35 year olds buy a sticker of this
- safety (integer 1-10): is it brand-safe and non-controversial
- uniqueness (integer 1-10): is it a fresh topic or already overdone
- overall (float 1.0-10.0): weighted composite score
- reasoning (string): one sentence explaining your score

Reference calibration:
- Score 9-10: "Moo Deng baby hippo" (viral, unique, extremely stickerable, brand-safe)
- Score 6-7: "Taylor Swift Eras Tour" (commercial but trademark-heavy, overdone)
- Score 3-4: "Federal Reserve rate decision" (not stickerable, no youth appeal)
```

**OpenAI API Configuration**:
```python
response = client.chat.completions.create(
    model="gpt-4o-mini",
    response_format={"type": "json_object"},
    messages=[...],
)
# Parse with JSON validation + retry on parse failure (max 2 retries)
```

**Threshold**: Only proceed with trends scoring 7.0+ overall. If no trends score 7.0+ in a cycle, log "No qualifying trends found" and exit successfully (exit code 0). This is normal -- only alert if zero qualifying trends for 24+ consecutive hours.

**Per-Cycle Caps**:
- Max 5 new trends processed per 2-hour cycle
- Prioritize by overall score (highest first)
- Queue remaining trends (status `queued`) for next cycle
- Daily cap: max 30 trends scored, max 50 images generated (controls AI spend)

**Acceptance Criteria**:
- **Given** the trend monitor runs, **when** it finds a topic with an overall score >= 7.0 from Reddit or Google Trends, **then** it is stored in Supabase with status `discovered`, all score fields populated, and `sources` array containing all contributing platforms
- **Given** a topic already exists in the `trends` table (matched by normalized topic with Jaccard similarity > 0.6), **when** the monitor encounters it again from a different source, **then** the existing trend's `sources` array is updated (no duplicate row created)
- **Given** the trend monitor finds more than 5 qualifying trends in one cycle, **when** it processes the top 5, **then** remaining trends are stored with status `queued` for the next cycle
- **Given** all trend source APIs are unreachable, **when** the monitor runs, **then** it logs errors to `error_log` table, sends an alert email, and exits with a non-zero exit code
- **Given** one trend source fails but another succeeds, **when** the monitor runs, **then** it continues with available sources (graceful degradation) and logs the failure
- **Given** a trend topic matches the trademark blocklist, **when** the scorer evaluates it, **then** it is auto-rejected with reason `trademark_blocked` and not stored
- **Given** GPT-4o-mini returns malformed JSON, **when** the scorer parses the response, **then** it retries up to 2 times; if all fail, the trend is skipped and the error is logged

### Step 2 -- Sticker Design Generation (1-3 min)

**Process**:
1. GPT-4o-mini generates 3 image prompts per trend (reduced from 3-5 to conserve budget)
2. Each prompt specifies sticker-optimized style:
   - Die-cut sticker format
   - Bold outlines, vibrant colors
   - Transparent/white background
   - Simple enough to read at small sizes
   - NO text, brand names, logos, or recognizable characters
3. Send prompts to Replicate (Stable Diffusion XL, pinned version)
4. Generate 3 variations per trend
5. Run quality validation on each image (see below)

**Prompt Template** (example):
```
A die-cut vinyl sticker design of {concept}. Bold black outlines,
vibrant colors, white background, cartoon illustration style,
simple and clean, suitable for laptop or water bottle sticker.
No text, no words, no letters, no brand names, no logos.
High contrast, fun and trendy aesthetic.
```

**Image Settings**:
- Resolution: 1024x1024
- Format: PNG (transparent background)
- Model: Stable Diffusion XL via Replicate (pin specific model version in config)

**Image Quality Validation** (automated checks after generation):
1. Verify image dimensions are 1024x1024
2. Verify file size is between 50 KB and 5 MB (too small = blank; too large = too complex for sticker)
3. Verify image has transparency (alpha channel present after background removal)
4. Verify image is not mostly blank (< 80% white/transparent pixels after background removal)
5. Verify aspect ratio after auto-crop is between 0.5 and 2.0 (not too thin/elongated)
6. If quality check fails, regenerate with a modified prompt adding "centered, simple composition" (max 2 retries)
7. If all retries fail, mark sticker as `quality_failed` and skip

**Acceptance Criteria**:
- **Given** a trend with status `discovered`, **when** sticker generation runs, **then** 3 PNG images are generated at 1024x1024, each uploaded to Cloudflare R2 with a unique key
- **Given** Replicate returns an error, **when** generation retries 3 times with exponential backoff and all fail, **then** the trend status is set to `generation_failed` and an alert is sent
- **Given** Replicate returns an image that fails quality validation, **when** the validator rejects it, **then** a new image is generated with a modified prompt (max 2 retries per image)
- **Given** all 3 images for a trend fail quality validation after retries, **when** the generator finishes, **then** the trend status is set to `quality_failed` and no stickers are created
- **Given** the daily image generation cap (50 images) has been reached, **when** a new trend is picked up for generation, **then** it remains in `discovered` status and is queued for the next day

### Step 3 -- Post-Processing (10-30 sec)

Using Python Pillow:
1. Remove/clean up background (make transparent)
2. Auto-crop to content bounds
3. Validate aspect ratio (0.5-2.0 range; reject outliers)
4. Resize to standard sticker dimensions (3" x 3" at 300 DPI = 900x900px)
5. Generate thumbnail (300x300) for Etsy listing
6. Optimize file size (target < 2 MB for print-ready, < 500 KB for thumbnail)

**Acceptance Criteria**:
- **Given** a raw 1024x1024 PNG from Replicate, **when** post-processing runs, **then** the output is a 900x900 print-ready PNG with transparent background and a 300x300 thumbnail, both uploaded to R2
- **Given** an image that is mostly blank after background removal (>80% transparent), **when** post-processing checks it, **then** it is rejected with status `quality_failed`

### Step 4 -- Content Moderation (5-15 sec)

**Two-stage moderation**:
- Stage 1 (Step 1): GPT-4o-mini safety scoring on the *topic* -- this is a pre-filter that saves image generation costs by rejecting unsafe topics before spending money on image generation
- Stage 2 (Step 4): OpenAI Moderation API on the *generated image* -- this catches visual content issues that topic-level filtering cannot detect
- Both stages must pass. A trend can pass topic safety (Stage 1) but fail image moderation (Stage 2). This is expected behavior.

**Process**:
1. Run image through OpenAI Moderation API (free)
2. Check generated text/tags against keyword blocklist
3. Check against trademark blocklist (brand names, character names, celebrity names)
4. Score thresholds:
   - Moderation score < 0.4: auto-approve
   - Moderation score 0.4-0.7: flag for manual review
   - Moderation score > 0.7: auto-reject
5. If auto-approved, set `moderation_status = 'approved'`
6. If flagged, set `moderation_status = 'flagged'`, send email alert with:
   - Sticker image (R2 URL)
   - Trend topic and source
   - Moderation scores (breakdown by category)
   - Approve/reject action: operator updates status in Supabase dashboard
7. If auto-rejected, set `moderation_status = 'rejected'` with reason logged
8. Manual review SLA: 24 hours. After 48 hours with no action, auto-reject and alert.

**Acceptance Criteria**:
- **Given** a sticker image with OpenAI moderation score < 0.4, **when** moderation runs, **then** the sticker is auto-approved with `moderation_status = 'approved'`
- **Given** a sticker image with moderation score between 0.4 and 0.7, **when** moderation runs, **then** the sticker is flagged with `moderation_status = 'flagged'` and an alert email is sent
- **Given** a sticker image with moderation score > 0.7, **when** moderation runs, **then** the sticker is auto-rejected with `moderation_status = 'rejected'`
- **Given** a flagged sticker has not been reviewed for 48 hours, **when** the daily analytics job runs, **then** it is auto-rejected and an alert email is sent
- **Given** a sticker description contains a term from the trademark blocklist, **when** moderation runs, **then** it is auto-rejected with reason `trademark_violation`

### Step 5 -- Publish to Etsy (5-10 sec)

Using Etsy Open API v3:
1. Refresh OAuth access token if needed (see Etsy OAuth Token Management section)
2. Check Etsy API rate limit budget in Redis (see Etsy API Rate Limit Management section)
3. Check active listing count -- do not exceed 300 active listings
4. Upload images to Cloudflare R2 (permanent storage)
5. Look up shipping cost from `shipping_rates` table for the selected fulfillment provider
6. Calculate floor price: `(print_cost + shipping_cost + packaging_cost) / (1 - 0.10) / (1 - 0.20)`
7. Look up listed price from `pricing_tiers` table (starts at `just_dropped` tier)
8. Verify listed price >= floor price (if not, bump to floor rounded to nearest .49/.99)
9. Create Etsy listing with:
   - **Title**: GPT-4o-mini generated, SEO-optimized (140 chars max)
   - **Description**: GPT-4o-mini generated with trend context + sticker specs + "FREE SHIPPING" + AI disclosure (see Legal section)
   - **Tags**: 13 tags max, mix of trend keywords + evergreen sticker terms + "free shipping"
   - **Price**: From pricing_tiers (shipping baked in)
   - **Shipping**: Set to $0.00 (free shipping -- Etsy search boost)
   - **who_made**: `"someone_else"` (required by Etsy for AI-generated products)
   - **when_made**: `"2020_2025"` (Etsy time range enum)
   - **is_supply**: `false`
   - **taxonomy_id**: Stickers taxonomy ID (look up via Etsy API during setup)
   - **Category**: Stickers > Vinyl Stickers
   - **Images**: Upload via Etsy API (max 10 per listing; use 1 sticker image + 2-3 lifestyle mockups)
   - **Shipping profile**: US-only, free shipping
10. Store listing metadata in Supabase (trend_id, etsy_listing_id, price, shipping_cost, fulfillment_provider)
11. Update trend status to `published`

**Acceptance Criteria**:
- **Given** a sticker with `moderation_status = 'approved'`, **when** the publisher runs, **then** an Etsy listing is created with title <= 140 characters, exactly 13 tags, price from `pricing_tiers`, shipping set to $0.00, `who_made = 'someone_else'`, and AI disclosure in description
- **Given** the active listing count is at 300 (the cap), **when** the publisher attempts to create a new listing, **then** it skips publishing, logs the reason, and the sticker remains in `approved` status for next cycle (after archiver frees slots)
- **Given** the Etsy API returns a rate limit error (429), **when** the publisher encounters it, **then** it stops publishing for this cycle, logs remaining items, and they are retried in the next cycle
- **Given** the Etsy OAuth token has expired, **when** the publisher attempts to create a listing, **then** it refreshes the token using the stored refresh token and retries the API call
- **Given** the calculated floor price exceeds the `just_dropped` tier price, **when** the publisher sets the price, **then** it uses the floor price rounded up to the nearest .49/.99

### End-to-End Timing

| Stage | Expected Time | SLA (95th percentile) |
|---|---|---|
| Trend detection (polling interval) | 2 hours | 2 hours |
| NLP scoring | 30-60 sec | 120 sec |
| Prompt generation | 10-20 sec | 45 sec |
| Image generation (3 images) | 15-60 sec | 120 sec (includes cold start) |
| Quality validation | 5-10 sec | 15 sec |
| Post-processing | 10-30 sec | 60 sec |
| Content moderation | 5-15 sec | 30 sec |
| Etsy listing creation | 5-10 sec | 30 sec |
| **Total (trend to listing)** | **~33-35 min** | **< 45 min** |

---

## Etsy OAuth Token Management

### Problem
Etsy v3 API uses OAuth 2.0. Access tokens expire after 3600 seconds (1 hour). GitHub Secrets cannot be updated at runtime. Storing the token as a static environment variable will fail after the first hour.

### Solution
Store OAuth tokens in Supabase with automatic refresh before each workflow run.

### Token Refresh Flow
```
Workflow starts
     |
     v
1. Read current tokens from Supabase `etsy_tokens` table
     |
     v
2. Check if access_token expires within 5 minutes
     |
     +-- If still valid -> use current access_token
     |
     +-- If expiring soon or expired ->
           |
           v
         3. Call Etsy token refresh endpoint:
              POST https://api.etsy.com/v3/public/oauth/token
              { grant_type: "refresh_token",
                client_id: ETSY_API_KEY,
                refresh_token: stored_refresh_token }
              |
              v
         4. Store new access_token + refresh_token + expires_at in Supabase
              |
              v
         5. If refresh fails (invalid_grant) ->
              - Alert via email: "Etsy OAuth token is invalid -- manual re-authorization required"
              - Halt all Etsy-dependent workflows
              - Manual fix: re-authorize via Etsy OAuth flow, update tokens in Supabase
```

### Token Storage Table
```sql
CREATE TABLE etsy_tokens (
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

-- Initial seed: after first manual OAuth authorization
-- INSERT INTO etsy_tokens (shop_id, access_token, refresh_token, expires_at, scopes)
-- VALUES ('your_shop_id', 'initial_access_token', 'initial_refresh_token',
--         now() + interval '1 hour', ARRAY['listings_r', 'listings_w', 'transactions_r']);
```

### Acceptance Criteria
- **Given** the access token expires within 5 minutes, **when** any workflow starts, **then** the token is refreshed before making Etsy API calls
- **Given** the refresh token is invalid (Etsy returns `invalid_grant`), **when** the token refresh is attempted, **then** an alert email is sent, all Etsy workflows are halted, and the error is logged to `error_log`
- **Given** the token was just refreshed by another concurrent workflow, **when** a second workflow reads the token, **then** it uses the newly refreshed token (handled by Supabase row locking)

---

## Etsy API Rate Limit Management

### Constraints
- Etsy API allows 10,000 calls per day for production apps
- Calls include: listing creation, listing updates, image uploads, order reads, shop reads
- Each listing creation requires ~3-5 API calls (create draft, upload images, activate listing)

### Strategy

**Track daily usage in Upstash Redis**:
```
Key: etsy_api_calls:{YYYY-MM-DD}
Value: integer counter
TTL: 48 hours (auto-cleanup)
```

**Priority ordering** (when approaching limits):
1. **P0 -- Order reads** (fulfillment depends on this)
2. **P1 -- New listing creation** (revenue generation)
3. **P2 -- Price updates** (daily pricing engine)
4. **P3 -- Analytics sync** (non-urgent)

**Thresholds**:
| Usage Level | Daily Calls Used | Action |
|---|---|---|
| Normal | 0-7,000 | All operations proceed normally |
| Warning | 7,001-8,500 | Log warning. Skip P3 operations. |
| Critical | 8,501-9,500 | Skip P2 and P3. Only P0 and P1 proceed. Alert email. |
| Hard stop | 9,501+ | All Etsy API calls halted. Alert email. Resume next day. |

**Budget estimation** (typical day):
| Operation | Calls per Item | Items per Day | Daily Total |
|---|---|---|---|
| New listings (create + images) | 4 | 10-15 | 40-60 |
| Price updates | 1 | 50-100 | 50-100 |
| Order reads | 2 | 5-20 | 10-40 |
| Analytics sync | 1 | 100-300 | 100-300 |
| Token refresh | 1 | 3-4 workflows | 3-4 |
| **Total** | | | **~203-504** |

At typical usage (~300-500 calls/day), the 10,000/day limit provides ample headroom.

### Acceptance Criteria
- **Given** daily Etsy API calls exceed 7,000, **when** a P3 (analytics) operation is attempted, **then** it is skipped and the skip is logged
- **Given** daily Etsy API calls exceed 9,500, **when** any Etsy API call is attempted, **then** it is blocked, an alert email is sent, and the operation is queued for the next day

---

## Pricing Strategy

### Shipping Cost Breakdown

All prices include shipping -- listings show "FREE SHIPPING" on Etsy. This boosts Etsy search ranking and increases conversion rate. Shipping costs are absorbed into the sticker price.

**MVP ships to US addresses only.** International shipping is excluded from MVP scope.

#### Shipping Method: USPS First-Class Mail (letter/flat)

Stickers are thin and lightweight. They ship in a rigid mailer or stamped envelope, qualifying for the cheapest USPS rates.

| Shipment Type | Weight | USPS Rate (2026) | Packaging Cost | Total Shipping Cost |
|---|---|---|---|---|
| 1 sticker (3" in rigid mailer) | < 1 oz | $0.78 (stamp) | ~$0.15 (envelope + cardboard backer) | **$0.93** |
| 1 sticker (4" in rigid mailer) | < 1 oz | $0.78 | ~$0.20 | **$0.98** |

**Note**: If using Sticker Mule for fulfillment, they include free shipping on all orders -- shipping cost is $0 to you. The table above applies only if self-fulfilling via USPS.

#### Shipping Cost Per Fulfillment Provider (MVP)

| Provider | Shipping Included? | Your Shipping Cost | MVP Status |
|---|---|---|---|
| **Sticker Mule** | Yes -- free shipping on all orders | **$0.00** | Primary (validate API first) |
| **Self-fulfillment** (USPS) | No -- you buy postage | **$0.93-0.98** (see table above) | Backup / fallback |
| ~~Printful~~ | ~~No -- charged per order~~ | ~~$3.99 US flat rate~~ | **Excluded from MVP** -- negative margin on singles |

**Recommended**: Use Sticker Mule for fulfillment (free shipping) or self-fulfill via USPS for highest margins. Printful is excluded from MVP due to negative margins on single stickers.

### Base Pricing Tiers (shipping included)

All listed prices include shipping. Singles only for MVP.

#### Using Sticker Mule (shipping = $0)

| Product | Listed Price | Print Cost | Shipping | Etsy Fees (~10%) | Total Cost | Profit | Margin |
|---|---|---|---|---|---|---|---|
| Single sticker (3") | $4.49 | ~$1.50 | $0.00 | ~$0.45 | **$1.95** | **$2.54** | **57%** |
| Single sticker (4") | $5.49 | ~$2.00 | $0.00 | ~$0.55 | **$2.55** | **$2.94** | **54%** |

#### Using Self-Fulfillment (USPS)

| Product | Listed Price | Print Cost | Shipping | Etsy Fees (~10%) | Total Cost | Profit | Margin |
|---|---|---|---|---|---|---|---|
| Single sticker (3") | $4.49 | ~$1.50 | $0.93 | ~$0.45 | **$2.88** | **$1.61** | **36%** |
| Single sticker (4") | $5.49 | ~$2.00 | $0.98 | ~$0.55 | **$3.53** | **$1.96** | **36%** |

### Dynamic Pricing by Trend Freshness (shipping included)

Sticker prices adjust automatically based on how old the underlying trend is. All prices include free shipping. Singles only for MVP.

| Pricing Tier | Trend Age | Single (3") | Single (4") |
|---|---|---|---|
| **just_dropped** | 0-3 days | $5.49 | $6.49 |
| **trending** | 4-14 days | $4.49 | $5.49 |
| **cooling** | 15-30 days | $3.49 | $4.49 |
| **evergreen** | 30+ days (still selling) | $3.49 | $4.49 |
| **archived** | 30+ days (no sales) | Delisted | -- |

**Minimum viable price** (floor): $3.49 for a single 3" sticker. Below this, margins go negative with self-fulfillment. The pricing engine never prices below the floor.

### Price Floor Calculation

The pricing engine calculates a per-product floor price to ensure every sale is profitable:

```
floor_price = (print_cost + shipping_cost + packaging_cost) / (1 - etsy_fee_rate)

Example (single 3" sticker, self-fulfilled):
floor_price = ($1.50 + $0.93 + $0.15) / (1 - 0.10) = $2.58 / 0.90 = $2.87

Minimum listed price to break even: $2.87
With 20% margin target: $2.87 / 0.80 = $3.59 -> round to $3.49
```

### Pricing Rules

1. **New stickers launch at `just_dropped` prices** -- trend is hot, urgency is high, no competition
2. **Auto-downgrade every tier boundary** -- the daily pricing engine adjusts prices on Etsy
3. **Sales override**: If a sticker sells 10+ units *at the current pricing tier*, it stays at current price (proven demand). Counter resets when tier changes. Computed from orders table filtered by `pricing_tier` at time of sale.
4. **Archive threshold**: If a sticker has 0 sales and 0 views for 14+ days, delist it to free up Etsy listing slots (max 300 active)
5. **Price floor**: Never price below the floor (cost + shipping + fees + 20% minimum margin)
6. **Fulfillment routing**: Singles -> Sticker Mule or self-fulfillment. Never route through Printful for MVP.
7. **All prices end in .49 or .99** -- psychological pricing that signals value while staying just under round numbers
8. **Listing budget cap**: Max 300 active listings. Archiver runs before publisher to free slots.

### Seasonal / Event Pricing (manual override)

| Event | Adjustment |
|---|---|
| Major holidays (Christmas, Halloween, Valentine's) | +15% on themed stickers |
| Viral moment (meme explodes) | Keep at `just_dropped` price regardless of age |
| Clearance / slow week | Run 20% off on `cooling` tier (but never below floor) |

---

## Pricing Engine (Daily Automated Job)

### Purpose
Automatically adjust Etsy listing prices based on trend freshness, sales velocity, and inventory rules. Runs daily via GitHub Actions.

### Logic Flow

```
Daily Pricing Engine (runs 6 AM UTC)
     |
     v
1. Acquire concurrency lock (Redis key: pricing_engine_lock, TTL 30 min)
   If lock exists -> exit (another run is in progress)
     |
     v
2. Check Etsy API rate limit budget in Redis
   If > 8,500 calls used today -> skip price updates, alert, exit
     |
     v
3. Fetch all published stickers + their linked trends from Supabase
   Fetch shipping_rates for the active fulfillment provider
     |
     v
4. For each sticker:
     |
     +-- Calculate trend_age = now() - trend.created_at
     |
     +-- Determine pricing_tier:
     |     0-3 days   -> just_dropped
     |     4-14 days  -> trending
     |     15-30 days -> cooling
     |     30+ days   -> check sales
     |                    sales > 0 in last 14 days -> evergreen
     |                    no sales + no views       -> archive
     |
     +-- Check sales_override:
     |     If sales_count >= 10 at current tier -> keep current price
     |
     +-- Look up new price from pricing_tiers table
     |
     +-- Calculate floor price:
     |     floor = (print_cost + shipping_cost + packaging_cost) / (1 - etsy_fee_rate)
     |     floor_with_margin = floor / (1 - min_margin_target)   # 20% minimum
     |     If new_price < floor_with_margin -> set new_price = floor_with_margin (rounded to .49/.99)
     |
     +-- Select fulfillment provider:
     |     MVP: Sticker Mule primary, self-fulfillment (USPS) backup
     |
     v
5. Compare new price vs. current Etsy price
     |
     +-- If different -> update Etsy listing via API (price field, shipping = $0.00)
     +-- If sticker marked 'archive' -> deactivate Etsy listing
     |
     v
6. Log all price changes in price_history table (including shipping cost at time of change)
     |
     v
7. Log run to pipeline_runs table (see Monitoring section)
     |
     v
8. Release concurrency lock
     |
     v
9. Send daily summary email:
     - X stickers repriced
     - X stickers archived
     - X stickers below floor (flagged, not published)
     - Top performers (highest sales velocity)
     - Revenue impact estimate
     - Shipping cost summary (total absorbed shipping this month)
     - Active listing count vs. 300 cap
     - Etsy API calls used today
```

### Pricing Tiers Config (stored in Supabase)

```sql
CREATE TABLE pricing_tiers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tier TEXT NOT NULL UNIQUE,        -- 'just_dropped', 'trending', 'cooling', 'evergreen'
    min_trend_age_days INTEGER,       -- inclusive
    max_trend_age_days INTEGER,       -- exclusive, NULL for open-ended
    price_single_small DECIMAL(10,2), -- 3" sticker (shipping included)
    price_single_large DECIMAL(10,2), -- 4" sticker (shipping included)
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Default pricing tiers (all prices include shipping, singles only for MVP)
INSERT INTO pricing_tiers (tier, min_trend_age_days, max_trend_age_days, price_single_small, price_single_large) VALUES
('just_dropped', 0,  3,  5.49, 6.49),
('trending',     3,  14, 4.49, 5.49),
('cooling',      14, 30, 3.49, 4.49),
('evergreen',    30, NULL, 3.49, 4.49);
```

### Shipping Config (stored in Supabase)

```sql
CREATE TABLE shipping_rates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_type TEXT NOT NULL,          -- 'single_small', 'single_large'
    fulfillment_provider TEXT NOT NULL,  -- 'sticker_mule', 'self_usps'
    shipping_cost DECIMAL(10,2) NOT NULL,
    packaging_cost DECIMAL(10,2) NOT NULL,
    region TEXT NOT NULL DEFAULT 'us',   -- 'us' only for MVP
    is_active BOOLEAN DEFAULT true,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Default US shipping rates (MVP: US-only, singles only)
INSERT INTO shipping_rates (product_type, fulfillment_provider, shipping_cost, packaging_cost, region) VALUES
-- Sticker Mule (free shipping included)
('single_small', 'sticker_mule', 0.00, 0.00, 'us'),
('single_large', 'sticker_mule', 0.00, 0.00, 'us'),
-- Self-fulfillment (USPS First-Class)
('single_small', 'self_usps', 0.78, 0.15, 'us'),
('single_large', 'self_usps', 0.78, 0.20, 'us');
```

### Price History Tracking

```sql
CREATE TABLE price_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sticker_id UUID REFERENCES stickers(id),
    old_price DECIMAL(10,2),
    new_price DECIMAL(10,2),
    pricing_tier TEXT,
    reason TEXT,            -- 'trend_age', 'sales_override', 'manual', 'archived'
    changed_at TIMESTAMPTZ DEFAULT now()
);
```

---

## Data Models (Supabase / Postgres)

### trends
```sql
CREATE TABLE trends (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic TEXT NOT NULL,
    topic_normalized TEXT NOT NULL,       -- lowercase, stemmed for dedup matching
    keywords TEXT[] NOT NULL,
    sources TEXT[] NOT NULL,              -- ['reddit', 'google_trends'] -- array to support multi-source trends
    score_velocity FLOAT,
    score_commercial FLOAT,
    score_safety FLOAT,
    score_uniqueness FLOAT,
    score_overall FLOAT,
    reasoning TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',  -- discovered, queued, processing, generated, quality_failed, generation_failed, published, archived
    source_data JSONB,                   -- raw data from each source
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_trends_status ON trends(status);
CREATE INDEX idx_trends_topic_normalized ON trends(topic_normalized);
CREATE INDEX idx_trends_created ON trends(created_at);
```

### stickers
```sql
CREATE TABLE stickers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trend_id UUID REFERENCES trends(id),
    title TEXT NOT NULL,
    description TEXT,
    image_url TEXT NOT NULL,           -- Cloudflare R2 URL (print-ready 900x900)
    thumbnail_url TEXT,                -- Cloudflare R2 URL (300x300)
    original_url TEXT,                 -- Cloudflare R2 URL (original 1024x1024 from Replicate)
    size TEXT NOT NULL DEFAULT '3in',  -- '3in', '4in'
    generation_prompt TEXT,
    generation_model TEXT DEFAULT 'stable-diffusion-xl',
    generation_model_version TEXT,     -- pinned Replicate model version hash
    moderation_status TEXT DEFAULT 'pending',  -- pending, approved, rejected, flagged, quality_failed
    moderation_score FLOAT,            -- OpenAI moderation score (0.0-1.0)
    moderation_categories JSONB,       -- breakdown by moderation category
    etsy_listing_id TEXT,
    price DECIMAL(10,2) DEFAULT 4.49,  -- listed price (shipping included)
    current_pricing_tier TEXT DEFAULT 'just_dropped',
    floor_price DECIMAL(10,2),         -- minimum price to stay profitable (auto-calculated)
    base_cost DECIMAL(10,2),           -- print-on-demand cost
    shipping_cost DECIMAL(10,2),       -- shipping cost at current fulfillment provider
    packaging_cost DECIMAL(10,2),      -- envelope/mailer cost
    fulfillment_provider TEXT DEFAULT 'sticker_mule',  -- 'sticker_mule', 'self_usps'
    tags TEXT[],
    sales_count INTEGER DEFAULT 0,
    view_count INTEGER DEFAULT 0,
    last_sale_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE INDEX idx_stickers_pricing_tier ON stickers(current_pricing_tier);
CREATE INDEX idx_stickers_trend_id ON stickers(trend_id);
CREATE INDEX idx_stickers_published ON stickers(published_at) WHERE published_at IS NOT NULL;
CREATE INDEX idx_stickers_moderation ON stickers(moderation_status);
```

### orders
```sql
CREATE TABLE orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    etsy_order_id TEXT UNIQUE,
    etsy_receipt_id TEXT,              -- Etsy receipt ID for API lookups
    sticker_id UUID REFERENCES stickers(id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2),
    total_amount DECIMAL(10,2),
    fulfillment_provider TEXT,         -- 'sticker_mule', 'self_usps'
    fulfillment_order_id TEXT,
    fulfillment_attempts INTEGER DEFAULT 0,   -- retry counter
    fulfillment_last_error TEXT,              -- last error message from provider
    status TEXT DEFAULT 'pending',     -- pending, paid, sent_to_print, print_confirmed, shipped, delivered, refunded
    pricing_tier_at_sale TEXT,         -- pricing tier when the sale occurred (for sales_override calculation)
    customer_data JSONB,               -- PII: name, address, email (see Data Retention section)
    shipped_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_sticker ON orders(sticker_id);
CREATE INDEX idx_orders_created ON orders(created_at);
```

### pipeline_runs (monitoring)
```sql
CREATE TABLE pipeline_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow TEXT NOT NULL,              -- 'trend_monitor', 'sticker_generator', 'pricing_engine', 'analytics_sync'
    status TEXT NOT NULL,                -- 'started', 'completed', 'failed', 'partial'
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
    ai_cost_estimate_usd DECIMAL(10,4) DEFAULT 0,  -- estimated cost this run
    metadata JSONB,                     -- additional run-specific data
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_pipeline_runs_workflow ON pipeline_runs(workflow);
CREATE INDEX idx_pipeline_runs_status ON pipeline_runs(status);
CREATE INDEX idx_pipeline_runs_started ON pipeline_runs(started_at);
```

### error_log
```sql
CREATE TABLE error_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_run_id UUID REFERENCES pipeline_runs(id),
    workflow TEXT NOT NULL,
    step TEXT NOT NULL,                  -- 'trend_fetch', 'scoring', 'image_gen', 'moderation', 'publishing', etc.
    error_type TEXT NOT NULL,            -- 'api_error', 'rate_limit', 'timeout', 'validation', 'auth'
    error_message TEXT NOT NULL,
    service TEXT,                        -- 'reddit', 'google_trends', 'openai', 'replicate', 'etsy', 'supabase', 'r2'
    retry_count INTEGER DEFAULT 0,
    resolved BOOLEAN DEFAULT false,
    context JSONB,                       -- trend_id, sticker_id, or other relevant IDs (never log PII or API keys)
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_error_log_workflow ON error_log(workflow);
CREATE INDEX idx_error_log_created ON error_log(created_at);
CREATE INDEX idx_error_log_unresolved ON error_log(resolved) WHERE resolved = false;
```

### etsy_tokens
```sql
-- See "Etsy OAuth Token Management" section above for full details
CREATE TABLE etsy_tokens (
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
```

### daily_metrics (materialized view)
```sql
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

-- Refresh daily in analytics job
-- REFRESH MATERIALIZED VIEW daily_metrics;

-- Also track AI spend and listing fees separately
CREATE VIEW cost_tracking AS
SELECT
    date_trunc('day', pr.started_at) AS date,
    SUM(pr.ai_cost_estimate_usd) AS ai_spend,
    SUM(pr.etsy_api_calls_used) AS api_calls,
    SUM(pr.stickers_published) * 0.20 AS listing_fees,
    SUM(pr.stickers_published) AS stickers_published,
    SUM(pr.errors_count) AS total_errors
FROM pipeline_runs pr
GROUP BY date_trunc('day', pr.started_at);
```

---

## Etsy Shop Setup

### Shop Structure
Before launching, configure the Etsy shop with these sections:

| Section Name | Contents | Sort Order |
|---|---|---|
| Trending Now | Stickers in `just_dropped` tier | Newest first |
| Popular | Stickers with 5+ sales | Sales count desc |
| New Drops | Stickers published in last 7 days | Newest first |
| Under $5 | Stickers in `cooling` or `evergreen` tier | Price asc |

### Listing Image Strategy

Each listing should include:
1. **Image 1 (primary)**: Sticker on clean white/transparent background (the generated design)
2. **Image 2**: Lifestyle mockup -- sticker on a laptop (use a static laptop mockup template + Pillow composite)
3. **Image 3**: Lifestyle mockup -- sticker on a water bottle (use a static water bottle mockup template + Pillow composite)
4. **Image 4 (optional)**: Size reference -- sticker next to a coin or ruler

Mockup templates are static images stored in `/assets/mockups/`. Pillow composites the sticker onto the template at the appropriate position and angle.

### Listing Description Template

```
{AI_GENERATED_DESCRIPTION}

--- What You Get ---
- 1x premium vinyl sticker ({size})
- Waterproof & UV-resistant
- Perfect for laptops, water bottles, notebooks, and more

--- Size & Material ---
- Size: {size} (3" x 3" or 4" x 4")
- Material: Premium vinyl, waterproof
- Finish: Glossy
- Durable outdoor-grade adhesive

--- Shipping ---
FREE SHIPPING! Ships within 3-5 business days via USPS First-Class Mail.
US addresses only.

--- About This Design ---
This sticker design was created with the assistance of AI tools.
Inspired by trending topics and pop culture moments.

--- Shop Policies ---
Questions? Message us! We respond within 24 hours.
```

### Shop About Page
Include: shop story (trend-inspired sticker designs), AI disclosure, production process overview, shipping info.

---

## Project Structure

```
sticker-trendz/
+-- .github/
|   +-- workflows/
|       +-- trend-monitor.yml      # Cron: every 2 hours -- detect + generate + publish
|       +-- daily-pricing.yml      # Cron: daily 6AM UTC -- pricing engine
|       +-- daily-analytics.yml    # Cron: daily 8AM UTC -- sales sync + order fulfillment + reporting
+-- src/
|   +-- trends/
|   |   +-- monitor.py             # Poll social media APIs
|   |   +-- scorer.py              # GPT-4o-mini trend scoring (structured output)
|   |   +-- dedup.py               # Cross-source deduplication (Jaccard similarity)
|   |   +-- sources/
|   |       +-- reddit.py          # Reddit OAuth API client
|   |       +-- google_trends.py   # Pytrends wrapper
|   +-- stickers/
|   |   +-- prompt_generator.py    # Trend -> image prompt
|   |   +-- image_generator.py     # Replicate API calls
|   |   +-- post_processor.py      # Pillow image cleanup
|   |   +-- quality_validator.py   # Image quality checks (dimensions, blank, aspect ratio)
|   +-- pricing/
|   |   +-- engine.py              # Daily pricing engine -- adjusts prices by freshness
|   |   +-- tiers.py               # Pricing tier config and lookups
|   |   +-- archiver.py            # Delist stale stickers with no sales/views
|   +-- moderation/
|   |   +-- moderator.py           # OpenAI Moderation API
|   |   +-- blocklist.py           # Keyword blocklist
|   |   +-- trademark_blocklist.py # Trademark / brand name blocklist
|   +-- publisher/
|   |   +-- etsy.py                # Etsy API integration (listings, images)
|   |   +-- etsy_auth.py           # Etsy OAuth token management + refresh
|   |   +-- etsy_rate_limiter.py   # Etsy API rate limit tracking (Redis)
|   |   +-- storage.py             # Cloudflare R2 upload
|   +-- fulfillment/
|   |   +-- router.py              # Fulfillment provider routing logic
|   |   +-- sticker_mule.py        # Sticker Mule API client
|   |   +-- self_fulfill.py        # Self-fulfillment order tracking
|   +-- analytics/
|   |   +-- sync.py                # Etsy sales data sync
|   |   +-- metrics.py             # Daily metrics aggregation
|   +-- monitoring/
|   |   +-- pipeline_logger.py     # Log pipeline runs to Supabase
|   |   +-- error_logger.py        # Log errors to error_log table
|   |   +-- alerter.py             # SendGrid email alerts
|   +-- backup/
|   |   +-- db_backup.py           # Daily pg_dump to R2
|   +-- db.py                      # Supabase client
|   +-- config.py                  # Environment config
|   +-- resilience.py              # Retry decorator, circuit breaker, backoff logic
+-- assets/
|   +-- mockups/
|       +-- laptop_template.png    # Laptop mockup template for listing images
|       +-- bottle_template.png    # Water bottle mockup template
+-- tests/
|   +-- unit/
|   |   +-- test_scorer.py
|   |   +-- test_pricing_engine.py
|   |   +-- test_floor_price.py
|   |   +-- test_post_processor.py
|   |   +-- test_quality_validator.py
|   |   +-- test_dedup.py
|   |   +-- test_rate_limiter.py
|   |   +-- test_trademark_blocklist.py
|   +-- integration/
|   |   +-- test_reddit_source.py
|   |   +-- test_google_trends_source.py
|   |   +-- test_replicate_gen.py
|   |   +-- test_etsy_sandbox.py
|   |   +-- test_supabase_crud.py
|   |   +-- test_r2_upload.py
|   +-- e2e/
|       +-- test_full_pipeline.py
|       +-- test_pricing_cycle.py
+-- data/
|   +-- trademark_blocklist.txt    # Brand names, character names, celebrity names
|   +-- keyword_blocklist.txt      # Offensive/inappropriate terms
+-- mvp-spec.md                    # This file
+-- trending-sticker-architecture.md  # Phase 2-3 vision (NOT in MVP scope)
+-- requirements.txt
+-- .env.example
+-- README.md
```

---

## Environment Variables

```
# AI
OPENAI_API_KEY=
REPLICATE_API_TOKEN=
REPLICATE_MODEL_VERSION=          # Pinned SDXL model version hash

# Database
SUPABASE_URL=
SUPABASE_SERVICE_KEY=             # Service role key (not anon key) -- used for all server-side operations

# Cache
UPSTASH_REDIS_URL=
UPSTASH_REDIS_TOKEN=

# Storage
CLOUDFLARE_R2_ACCESS_KEY=
CLOUDFLARE_R2_SECRET_KEY=
CLOUDFLARE_R2_BUCKET=
CLOUDFLARE_R2_ENDPOINT=

# Etsy (initial bootstrap only -- tokens stored in Supabase after first auth)
ETSY_API_KEY=                     # Also known as client_id (used for token refresh)
ETSY_API_SECRET=                  # Also known as client_secret
ETSY_SHOP_ID=

# Fulfillment
STICKER_MULE_API_KEY=

# Notifications
SENDGRID_API_KEY=
ALERT_EMAIL=

# Operational Caps
MAX_TRENDS_PER_CYCLE=5
MAX_IMAGES_PER_DAY=50
MAX_ACTIVE_LISTINGS=300
AI_MONTHLY_BUDGET_CAP_USD=150
```

---

## GitHub Actions Workflows and Budget

### GitHub Actions Minutes Budget

GitHub Actions free tier provides 2,000 minutes/month. Each run incurs setup time (checkout, Python install, pip install). Aggressive caching reduces per-run time.

**Estimated usage with caching**:

| Workflow | Frequency | Runs/Month | Avg Duration | Monthly Minutes |
|---|---|---|---|---|
| Trend Monitor + Generator | Every 2 hours | ~360 | ~2 min (with cache) | ~720 |
| Daily Pricing Engine | Daily | 30 | ~3 min | ~90 |
| Daily Analytics | Daily | 30 | ~2 min | ~60 |
| **Total** | | | | **~870** |

**Status**: 870 min is well within the 2,000 min free tier (~44% utilization), leaving comfortable headroom.

**Optimizations applied**:
1. **Aggressive dependency caching** (`actions/cache@v4` on pip packages): reduces setup from ~60s to ~10s
2. **Skip generation if no qualifying trends**: If the monitor finds zero qualifying trends (most runs), the generator step is skipped entirely. Estimated 70% of runs are no-ops completing in < 1 min.
3. **Conditional job execution**: The generator job only runs if the monitor job outputs `new_trends=true`. Most runs will only execute the lightweight monitor.

**Revised estimate with optimizations**:

| Workflow | Runs/Month | No-op Runs (70%) | Active Runs (30%) | Monthly Minutes |
|---|---|---|---|---|
| Monitor only (no trends found) | ~252 | 1 min each | -- | ~252 |
| Monitor + Generator (trends found) | ~108 | -- | 2.5 min each | ~270 |
| Daily Pricing | 30 | -- | 3 min each | ~90 |
| Daily Analytics | 30 | -- | 2 min each | ~60 |
| **Total** | | | | **~672** |

~34% of free tier. Plenty of room for retries, manual triggers, and future workflow additions.

### trend-monitor.yml (runs every 2 hours)
```yaml
name: Trend Monitor
on:
  schedule:
    - cron: '0 */2 * * *'
  workflow_dispatch:  # manual trigger

concurrency:
  group: trend-monitor
  cancel-in-progress: false

jobs:
  monitor:
    runs-on: ubuntu-latest
    outputs:
      new_trends: ${{ steps.detect.outputs.new_trends }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('requirements.txt') }}
          restore-keys: ${{ runner.os }}-pip-
      - run: pip install -r requirements.txt
      - id: detect
        run: python -m src.trends.monitor
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
          UPSTASH_REDIS_URL: ${{ secrets.UPSTASH_REDIS_URL }}
          UPSTASH_REDIS_TOKEN: ${{ secrets.UPSTASH_REDIS_TOKEN }}
          MAX_TRENDS_PER_CYCLE: ${{ vars.MAX_TRENDS_PER_CYCLE || '5' }}

  generate:
    needs: monitor
    if: needs.monitor.outputs.new_trends == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('requirements.txt') }}
          restore-keys: ${{ runner.os }}-pip-
      - run: pip install -r requirements.txt
      - run: python -m src.stickers.image_generator
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          REPLICATE_API_TOKEN: ${{ secrets.REPLICATE_API_TOKEN }}
          REPLICATE_MODEL_VERSION: ${{ vars.REPLICATE_MODEL_VERSION }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
          CLOUDFLARE_R2_ACCESS_KEY: ${{ secrets.CLOUDFLARE_R2_ACCESS_KEY }}
          CLOUDFLARE_R2_SECRET_KEY: ${{ secrets.CLOUDFLARE_R2_SECRET_KEY }}
          CLOUDFLARE_R2_BUCKET: ${{ secrets.CLOUDFLARE_R2_BUCKET }}
          CLOUDFLARE_R2_ENDPOINT: ${{ secrets.CLOUDFLARE_R2_ENDPOINT }}
          ETSY_API_KEY: ${{ secrets.ETSY_API_KEY }}
          ETSY_API_SECRET: ${{ secrets.ETSY_API_SECRET }}
          ETSY_SHOP_ID: ${{ secrets.ETSY_SHOP_ID }}
          MAX_IMAGES_PER_DAY: ${{ vars.MAX_IMAGES_PER_DAY || '50' }}
          MAX_ACTIVE_LISTINGS: ${{ vars.MAX_ACTIVE_LISTINGS || '300' }}
```

### daily-pricing.yml (runs daily at 6 AM UTC)
```yaml
name: Daily Pricing Engine
on:
  schedule:
    - cron: '0 6 * * *'
  workflow_dispatch:

concurrency:
  group: pricing-engine
  cancel-in-progress: false

jobs:
  reprice:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('requirements.txt') }}
          restore-keys: ${{ runner.os }}-pip-
      - run: pip install -r requirements.txt
      - run: python -m src.pricing.engine
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
          ETSY_API_KEY: ${{ secrets.ETSY_API_KEY }}
          ETSY_API_SECRET: ${{ secrets.ETSY_API_SECRET }}
          ETSY_SHOP_ID: ${{ secrets.ETSY_SHOP_ID }}
          UPSTASH_REDIS_URL: ${{ secrets.UPSTASH_REDIS_URL }}
          UPSTASH_REDIS_TOKEN: ${{ secrets.UPSTASH_REDIS_TOKEN }}
          SENDGRID_API_KEY: ${{ secrets.SENDGRID_API_KEY }}
          ALERT_EMAIL: ${{ secrets.ALERT_EMAIL }}
```

### daily-analytics.yml (runs daily at 8 AM UTC)
```yaml
name: Daily Analytics + Order Fulfillment
on:
  schedule:
    - cron: '0 8 * * *'
  workflow_dispatch:

concurrency:
  group: analytics
  cancel-in-progress: false

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('requirements.txt') }}
          restore-keys: ${{ runner.os }}-pip-
      - run: pip install -r requirements.txt
      - run: python -m src.analytics.sync
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
          ETSY_API_KEY: ${{ secrets.ETSY_API_KEY }}
          ETSY_API_SECRET: ${{ secrets.ETSY_API_SECRET }}
          ETSY_SHOP_ID: ${{ secrets.ETSY_SHOP_ID }}
          UPSTASH_REDIS_URL: ${{ secrets.UPSTASH_REDIS_URL }}
          UPSTASH_REDIS_TOKEN: ${{ secrets.UPSTASH_REDIS_TOKEN }}
          STICKER_MULE_API_KEY: ${{ secrets.STICKER_MULE_API_KEY }}
          SENDGRID_API_KEY: ${{ secrets.SENDGRID_API_KEY }}
          ALERT_EMAIL: ${{ secrets.ALERT_EMAIL }}
      - name: Database backup
        run: python -m src.backup.db_backup
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
          CLOUDFLARE_R2_ACCESS_KEY: ${{ secrets.CLOUDFLARE_R2_ACCESS_KEY }}
          CLOUDFLARE_R2_SECRET_KEY: ${{ secrets.CLOUDFLARE_R2_SECRET_KEY }}
          CLOUDFLARE_R2_BUCKET: ${{ secrets.CLOUDFLARE_R2_BUCKET }}
          CLOUDFLARE_R2_ENDPOINT: ${{ secrets.CLOUDFLARE_R2_ENDPOINT }}
```

---

## Error Handling & Resilience

### Global Retry Policy

All external API calls use a shared retry decorator with exponential backoff:

```python
@retry(max_retries=3, backoff_base=2, backoff_max=30)
def call_external_api(...):
    # 1st retry: wait 2 sec
    # 2nd retry: wait 4 sec
    # 3rd retry: wait 8 sec (capped at 30 sec)
```

### Circuit Breaker

After 5 consecutive failures to the same service within a single workflow run, skip all remaining calls to that service for this run:

| Service | Circuit Breaker Threshold | Recovery |
|---|---|---|
| Reddit API | 5 consecutive failures | Next scheduled run (2 hours) |
| Google Trends | 5 consecutive failures | Next scheduled run (2 hours) |
| OpenAI API | 5 consecutive failures | Next scheduled run (2 hours) |
| Replicate API | 3 consecutive failures | Next scheduled run (2 hours) |
| Etsy API | 3 consecutive failures | Alert + next scheduled run |
| Supabase | 3 consecutive failures | Halt workflow + alert (cannot operate without DB) |
| Cloudflare R2 | 3 consecutive failures | Next scheduled run (2 hours) |

### Partial Failure Behavior

| Scenario | Behavior |
|---|---|
| 1 of 2 trend sources fails | Continue with available source. Log failure. |
| OpenAI scoring fails for 1 trend | Skip that trend, continue scoring others. |
| Image generation fails for 1 of 3 variants | Keep successful variants. Retry failed ones (max 2 retries). |
| Etsy publishing fails for 1 sticker | Skip it, mark for retry next cycle. Continue publishing others. |
| All trend sources fail | Log error, send alert, exit non-zero. |
| Supabase is down | Halt immediately, send alert (via Redis + SendGrid as fallback). |

### Concurrency Locks

Prevent overlapping workflow runs from causing race conditions:

| Workflow | Lock Key (Redis) | Lock TTL |
|---|---|---|
| Trend Monitor | `lock:trend_monitor` | 25 min |
| Pricing Engine | `lock:pricing_engine` | 30 min |
| Analytics Sync | `lock:analytics_sync` | 30 min |

If a workflow starts and finds the lock held, it exits immediately with a log message (not an error).

### GitHub Actions Exit Codes

| Code | Meaning | Action |
|---|---|---|
| 0 | Success (including "no trends found") | Normal |
| 0 | Partial success (some items processed, some failed) | Logged, no alert |
| 1 | Unrecoverable error (DB down, auth failure, all APIs failed) | Alert email sent |

### Per-Cycle and Daily Caps

| Cap | Value | When Reached |
|---|---|---|
| New trends per 2-hour cycle | 5 | Remaining queued for next cycle |
| Images generated per day | 50 | Trends stay in `discovered` status until next day |
| Etsy API calls per day | 10,000 (Etsy-imposed) | See Rate Limit Management section |
| AI monthly budget | $150 | Alert at $120. Hard stop at $150. |
| Active Etsy listings | 300 | Archiver must free slots before new listings |

---

## Fulfillment Flow

### Order Sync

Orders are synced via the `daily-analytics.yml` workflow (runs daily at 8 AM UTC). This is polling-based since Etsy's webhook support is limited and unreliable.

**Note**: This means orders placed after 8 AM UTC will not be picked up until the next day's run. For MVP, this 24-hour latency is acceptable. If order volume grows, add a second sync at 8 PM UTC.

### Fulfillment Process

```
Daily Analytics Sync (8 AM UTC)
     |
     v
1. Fetch new orders from Etsy API (GET /shops/{shop_id}/receipts)
   Store in orders table with status 'paid'
     |
     v
2. For each new order:
     |
     +-- Fetch high-res sticker image URL from stickers table (Cloudflare R2)
     |
     +-- Route to fulfillment provider:
     |     Primary: Sticker Mule API
     |     Fallback: Self-fulfillment (USPS) -- if Sticker Mule API fails or is unavailable
     |
     +-- Send print job to provider:
     |     - Image file URL
     |     - Shipping address (from Etsy order)
     |     - Sticker size (3" or 4")
     |     - Quantity
     |
     +-- Update order status to 'sent_to_print'
     |
     +-- If Sticker Mule API fails:
           Retry 3 times with backoff
           If all retries fail:
             Set fulfillment_provider to 'self_usps'
             Send alert email: "Order {etsy_order_id} needs manual fulfillment"
             Update status to 'pending_manual'
     |
     v
3. Check existing orders in 'sent_to_print' status:
     Query fulfillment provider for shipping status
     Update status to 'shipped' when tracking number is available
     Update status to 'delivered' when delivered
     |
     v
4. Auto-reject flagged stickers that have been unreviewed for 48+ hours
     |
     v
5. Refresh materialized view: daily_metrics
     |
     v
6. Run database backup (pg_dump -> R2)
     |
     v
7. Send daily summary email
```

**Sticker Specs for Print**:
- Size: 3" x 3" (default), 4" x 4" (large option)
- Material: Vinyl, waterproof, UV-resistant
- Finish: Glossy (start with glossy for MVP)
- Est. print cost: $1-3/sticker (Sticker Mule)

**Fulfillment Provider Routing** (MVP):

| Product | Recommended Provider | Backup | Why |
|---|---|---|---|
| Single sticker | Sticker Mule | Self-fulfill (USPS) | Free shipping included, best margin |
| Single sticker (if Sticker Mule unavailable) | Self-fulfill (USPS) | -- | $0.93 shipping, still profitable |

**Shipping to customer**: Always **FREE** (baked into listed price). All Etsy listings set shipping = $0.00. US addresses only for MVP.

**Fulfillment Acceptance Criteria**:
- **Given** a new order from Etsy with status `paid`, **when** the daily sync runs, **then** a print job is submitted to Sticker Mule with the correct image, size, and shipping address, and the order status is updated to `sent_to_print`
- **Given** Sticker Mule API fails after 3 retries, **when** the fulfillment router processes the order, **then** the order is routed to self-fulfillment, an alert email is sent, and the order status is set to `pending_manual`
- **Given** an order has been in `sent_to_print` status for 7+ days with no shipping update, **when** the daily sync checks it, **then** an alert email is sent to investigate

---

## Discovery Strategy (MVP)

### Organic Only -- No Paid Ads (automated)

| Channel | Action | Effort |
|---|---|---|
| **Etsy SEO** | GPT-4o-mini auto-generates optimized titles, descriptions, 13 tags per listing | Automated |

**Manual channels** (not part of the automated pipeline):

| Channel | Action | Effort |
|---|---|---|
| **Pinterest** | Pin every new sticker design with trend keywords | 10 min/day (manual) |
| **Instagram** | Post weekly "trending stickers" carousel | 30 min/week (manual) |
| **Reddit** | Share in r/stickers, niche trend-related subs | 15 min/day (manual) |
| **Etsy Ads** | Optional: $1-5/day Etsy promoted listings to test demand | $30-150/mo (manual) |

### Etsy SEO -- Auto-Generated Listing Copy

**Title format**: `{Trend Topic} Sticker - {Style} Vinyl Decal - Laptop Water Bottle Sticker - Trending {Category}`

**Tag strategy** (13 tags per listing):
- 5-7 trend-specific keywords
- 3-4 evergreen sticker terms ("vinyl sticker", "laptop sticker", "waterproof decal")
- 2-3 audience terms ("funny sticker", "meme sticker", "gift for him")
- Always include "free shipping" as a tag

### Etsy SEO Performance Tracking

Track via daily analytics sync:
- View count per listing (Etsy API)
- Conversion rate per listing (sales / views)
- Weekly review: identify listings with high views but low sales (pricing issue?) and listings with low views (SEO issue?)
- Track which tag combinations correlate with higher views

---

## Monthly Budget

| Line Item | Cost |
|---|---|
| AI image generation (Replicate, ~1.5K-2.5K images at 50/day cap) | $50-100 |
| AI text (GPT-4o-mini -- scoring, prompts, SEO copy) | $20-40 |
| Infrastructure (all free tiers) | $0 |
| Etsy listing fees ($0.20 x ~200 listings, capped at 300 active) | $40 |
| Domain (not needed for MVP) | $0 |
| **Total fixed** | **$110-180** |
| AI monthly budget hard cap | $150 |
| Etsy transaction fees (6.5% of sales) | Variable |
| Fulfillment (Sticker Mule per order) | Variable |

---

## Revenue Model

### Order Mix Assumptions (MVP: singles only)

| Order Type | % of Orders | Avg. Order Value |
|---|---|---|
| Single sticker (3") | 70% | $4.49 |
| Single sticker (4") | 30% | $5.49 |
| **Blended AOV** | | **$4.79** |

### Revenue Projections (MVP: singles only, no packs)

| Metric | Conservative | Moderate | Optimistic |
|---|---|---|---|
| Stickers listed/month | 150 | 300 | 500 |
| Active listings (capped) | 150 | 300 | 300 |
| Conversion rate | 2% | 3% | 5% |
| Orders/month | ~50 | ~150 | ~350 |
| Blended AOV | $4.79 | $4.79 | $4.79 |
| **Gross revenue** | **$240** | **$719** | **$1,677** |
| COGS (print, blended) | -$85 | -$255 | -$595 |
| Etsy fees (~10%) | -$24 | -$72 | -$168 |
| Fixed costs | -$145 | -$145 | -$145 |
| **Net profit** | **-$14** | **$247** | **$769** |

Break-even: ~35 orders/month (~$168 revenue).

**Note**: Revenue is lower without packs (AOV drops from $8.73 to $4.79). Packs are critical for revenue growth and are the top priority for V1.1. See V1.1 Roadmap section.

---

## Success Criteria (first 90 days)

### Business Milestones

| Milestone | Target | Timeframe |
|---|---|---|
| Pipeline running end-to-end | Trend detected -> sticker on Etsy | Week 1-2 |
| First 100 listings live | 100 trend-based stickers on Etsy | Week 2-3 |
| First sale | 1 organic sale on Etsy | Week 3-4 |
| Consistent daily sales | 3+ orders/day | Month 2 |
| Revenue target | $500+/mo gross (singles only) | Month 3 |
| Validate product-market fit | >2% conversion rate on Etsy | Month 3 |
| Launch V1.1 (packs) | Packs live, AOV increases 50%+ | Month 3-4 |

### Operational KPIs

| Metric | Target | Measurement | Alert Threshold |
|---|---|---|---|
| Pipeline uptime | > 95% of scheduled runs succeed | GitHub Actions success rate | < 90% in any 24h period |
| Trend detection rate | 5+ new qualifying trends/day | Count of trends with score >= 7.0 per day | 0 trends for 24 consecutive hours |
| Image generation success rate | > 80% of attempts produce usable images | stickers generated / generation attempts | < 60% in any day |
| Content moderation false positive rate | < 10% | Manual review of rejected stickers weekly | > 20% false positive rate |
| Etsy listing creation success rate | > 95% | Listings created / listings attempted | < 85% in any day |
| Average trend-to-listing time | < 45 minutes (95th percentile) | Timestamp delta: trend.created_at to sticker.published_at | > 60 minutes consistently |
| Daily AI spend | < $10/day ($150/month cap) | Logged API costs in pipeline_runs | > $8/day triggers warning |
| Order fulfillment success rate | > 95% first-attempt | Orders sent to print / orders received | < 80% triggers alert |
| Active listing count | < 300 (cap) | Count of active Etsy listings | > 280 triggers archiver |

---

## Monitoring & Observability

### Pipeline Run Logging

Every workflow run logs to the `pipeline_runs` table (see Data Models section):
- Workflow name, start/end time, duration
- Counts: trends found, stickers generated, published, archived, prices updated, orders synced
- Error count
- Etsy API calls consumed
- Estimated AI cost

### Daily Summary Email (via SendGrid)

Sent after the daily analytics sync (8 AM UTC):

```
Subject: Sticker Trendz Daily Summary - {date}

--- Pipeline Health ---
- Trend Monitor: {X} runs, {Y} successful, {Z} failed
- Trends discovered today: {N}
- Stickers generated: {N}, published: {N}, failed: {N}
- Active listings: {N}/300

--- Revenue ---
- Orders today: {N}
- Revenue today: ${X}
- Revenue MTD: ${X}
- AOV: ${X}

--- Pricing ---
- Stickers repriced: {N}
- Stickers archived: {N}
- Top performer: {sticker_title} ({sales} sales)

--- Costs ---
- AI spend today: ${X} (MTD: ${X} / $150 cap)
- Etsy API calls today: {N}/10,000
- Etsy listing fees MTD: ${X}

--- Alerts ---
- {Any errors or warnings from the past 24 hours}
```

### Cost Tracking

Track AI spend per pipeline run:
- OpenAI: estimate tokens used x cost per token (GPT-4o-mini: $0.15/1M input, $0.60/1M output)
- Replicate: track image generation count x cost per image (~$0.02-0.05/image)
- Alert at $120/month. Hard stop at $150/month.

### GitHub Actions Monitoring

GitHub natively supports email notifications on workflow failure. Enable this for all workflows. Additionally, if 3+ consecutive runs of the same workflow fail, the error logger sends an alert via SendGrid.

---

## Security

### API Key Management

| Secret | Storage | Rotation Schedule |
|---|---|---|
| OpenAI API Key | GitHub Secrets | Quarterly |
| Replicate API Token | GitHub Secrets | Quarterly |
| Supabase Service Key | GitHub Secrets | Quarterly |
| Etsy API Key/Secret | GitHub Secrets | Annually (or if compromised) |
| Etsy OAuth Tokens | Supabase `etsy_tokens` table | Auto-rotated hourly |
| Sticker Mule API Key | GitHub Secrets | Quarterly |
| SendGrid API Key | GitHub Secrets | Quarterly |
| Cloudflare R2 Keys | GitHub Secrets | Quarterly |
| Upstash Redis Token | GitHub Secrets | Quarterly |

### Supabase Row-Level Security (RLS)

Enable RLS on all tables even though we use the service role key (defense in depth):

```sql
-- Enable RLS on all tables
ALTER TABLE trends ENABLE ROW LEVEL SECURITY;
ALTER TABLE stickers ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE etsy_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE error_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE pricing_tiers ENABLE ROW LEVEL SECURITY;
ALTER TABLE shipping_rates ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_history ENABLE ROW LEVEL SECURITY;

-- Service role bypasses RLS by default.
-- If we ever expose an anon key (e.g., for a dashboard), add restrictive policies:
-- CREATE POLICY "read_only_published_stickers" ON stickers FOR SELECT USING (published_at IS NOT NULL);
```

### PII Handling

- `orders.customer_data` contains PII: customer name, shipping address, email
- Never log PII in pipeline_runs, error_log, or GitHub Actions output
- Never include PII in SendGrid emails (use order ID references only)
- PII retention: 90 days after order delivery, then purge (see Data Retention section)
- R2 bucket: public read for sticker images. No directory listing. No write from public.
- Logs must never contain: API keys, customer data, full API response bodies, Etsy tokens

### Trademark Blocklist

Maintain a `data/trademark_blocklist.txt` file containing:
- Major brand names (Nike, Apple, Disney, etc.)
- Fictional character names (Mickey Mouse, Pikachu, Spider-Man, etc.)
- Celebrity names
- Sports team names and logos

Check trend topics and generated tags against this list during scoring (Step 1) and moderation (Step 4). Auto-reject matches.

Update the blocklist monthly based on DMCA notices received and common trending brand topics.

---

## Data Retention & Privacy

### Retention Policy

| Data Type | Retention Period | Action After Expiry |
|---|---|---|
| `orders.customer_data` (PII) | 90 days after delivery | Purge PII fields (set to NULL). Keep anonymized order record for analytics. |
| `error_log` entries | 90 days | Delete old entries |
| `pipeline_runs` entries | 180 days | Delete old entries |
| `price_history` entries | 1 year | Archive to R2 as CSV, then delete |
| Sticker images (R2) | Indefinite (while listed or archived) | Delete when sticker is permanently removed |
| Database backups (R2) | 30 days | Delete older backups |
| `trends` records | Indefinite | Keep for analytics (no PII) |
| `stickers` records | Indefinite | Keep for analytics (no PII) |

### PII Purge Job

Run as part of the daily analytics sync:
```sql
UPDATE orders
SET customer_data = NULL
WHERE status = 'delivered'
  AND delivered_at < NOW() - INTERVAL '90 days'
  AND customer_data IS NOT NULL;
```

### Privacy Policy

The Etsy shop must include a privacy policy link (required by Etsy ToS). Content should cover:
- What data is collected (order details as provided by Etsy)
- How data is stored (encrypted at rest in Supabase)
- Retention period (90 days after delivery)
- Right to deletion (contact shop for deletion requests; fulfill within 30 days)
- No data sold to third parties

For MVP, use a standard Etsy shop privacy policy template. Customize with retention details above.

### Deletion Request Process (Manual for MVP)

1. Customer contacts shop via Etsy messages requesting data deletion
2. Operator queries orders by customer email (within 24 hours)
3. Set `customer_data = NULL` on all matching orders
4. Confirm deletion to customer via Etsy message
5. Log the request (date, action taken) in a simple spreadsheet/Notion doc

---

## Legal & Compliance

### AI-Generated Product Disclosure

Etsy requires disclosure when AI tools are used in product creation. As of 2025, Etsy's policy states sellers must disclose AI use in listings.

**Required in every listing description**:
> "This sticker design was created with the assistance of AI tools."

This text is included in the listing description template (see Etsy Shop Setup section).

**Etsy listing fields**:
- `who_made`: `"someone_else"` (since AI tools generate the design)
- `when_made`: `"2020_2025"` (Etsy enum value)

### Copyright Considerations

- Per US Copyright Office guidance (2023), purely AI-generated images are generally not copyrightable
- This means competitors could legally copy designs
- Mitigation: speed-to-market is the moat. Trending stickers have a short commercial window.
- The prompt text and curation process may qualify for copyright protection even if the raw image does not

### Trademark Protection

- Trending topics frequently involve trademarked brands, characters, and celebrities
- The trademark blocklist (see Security section) prevents generation of infringing designs
- Despite the blocklist, some infringement may slip through
- Monitor Etsy messages and email for DMCA/trademark complaints

### DMCA Takedown Response Process

1. Receive DMCA notice (via Etsy message, email, or Etsy's IP complaint system)
2. **Within 24 hours**: Deactivate the flagged Etsy listing
3. Log the complaint: date, listing ID, complainant, claim details
4. Review the claim:
   - If valid: permanently delete the listing and sticker. Add the topic/brand to trademark blocklist.
   - If questionable: consult Etsy's counter-notice process. Do not re-list without legal advice.
5. Respond to complainant acknowledging the takedown
6. Update trademark blocklist to prevent future generation of similar designs

### Sales Tax

Etsy collects and remits sales tax in most US states on behalf of sellers (marketplace facilitator laws). For MVP, no additional sales tax setup is required. Verify this is still current at launch time.

### Business License

Research local jurisdiction requirements for online sales. Many US jurisdictions require a business license for commercial activity. This is an open question (see Open Questions section).

---

## Testing Strategy

### Unit Tests (run on every PR via GitHub Actions)

| Test Suite | What It Tests | Key Assertions |
|---|---|---|
| `test_scorer.py` | GPT-4o-mini trend scoring logic | Given sample trend data, verify scoring produces expected tiers. Verify threshold filtering (7.0+ passes, < 7.0 rejected). |
| `test_pricing_engine.py` | Pricing tier assignment and price lookup | Given sticker with known age and sales, verify correct tier and price. Verify sales override logic (10+ at current tier). |
| `test_floor_price.py` | Floor price calculation | Given known costs (print, shipping, packaging), verify floor price = cost / (1 - fee_rate) / (1 - margin_target). Verify price never goes below floor. |
| `test_post_processor.py` | Image resize, crop, thumbnail | Given a sample 1024x1024 image, verify output is 900x900 print-ready and 300x300 thumbnail with transparency. |
| `test_quality_validator.py` | Image quality checks | Verify blank image (>80% white) is rejected. Verify extreme aspect ratio after crop is rejected. Verify valid images pass. |
| `test_dedup.py` | Cross-source trend deduplication | Verify Jaccard similarity > 0.6 merges trends. Verify dissimilar trends are kept separate. |
| `test_rate_limiter.py` | Etsy API rate limit tracking | Verify counter increments correctly. Verify priority ordering when thresholds are exceeded. |
| `test_trademark_blocklist.py` | Trademark filtering | Verify known brands are blocked. Verify clean topics pass. |

### Integration Tests (run daily or on-demand)

| Test Suite | What It Tests | Prerequisites |
|---|---|---|
| `test_reddit_source.py` | Reddit OAuth API returns parseable data | Reddit API credentials |
| `test_google_trends_source.py` | Pytrends returns breakout terms | Network access |
| `test_replicate_gen.py` | A test prompt returns a valid PNG from Replicate | Replicate API token |
| `test_etsy_sandbox.py` | Listing creation in Etsy sandbox | Etsy sandbox API access |
| `test_supabase_crud.py` | CRUD operations on all tables | Supabase test project |
| `test_r2_upload.py` | Image upload and retrieval from R2 | R2 credentials |

### End-to-End Tests (run weekly or before releases)

| Test | Description |
|---|---|
| `test_full_pipeline.py` | Inject a fake trend -> verify it flows through scoring -> generation -> moderation -> publishing (in Etsy sandbox). Assert: listing is created with correct title format, 13 tags, correct price tier, correct images. |
| `test_pricing_cycle.py` | Seed test stickers with known ages -> run pricing engine -> verify tier assignments and price changes match expectations. Verify archived stickers are delisted. |

### Etsy Sandbox Testing

- **All development and testing uses Etsy's sandbox environment**
- Test with at least 10 listings in sandbox before going live
- Verify: pricing renders correctly, images upload properly, tags are applied, description format is correct
- Cutover plan: switch `ETSY_SHOP_ID` and OAuth tokens from sandbox to production

### Rollback Criteria

| Trigger | Action |
|---|---|
| Pricing engine sets any price below floor | Halt pricing engine. Revert affected listings using `price_history` table. Alert operator. |
| Moderation passes content flagged by a customer or Etsy | Bulk-deactivate listings from the affected batch (filter by `created_at` range). Review and fix moderation threshold. |
| Image generation produces unusable output (>50% quality failures) | Halt generator. Review prompts and Replicate model version. No new stickers until resolved. |
| OAuth token refresh fails | Halt all Etsy workflows. Alert operator for manual re-authorization. |
| GitHub Actions minutes exhausted | All cron jobs stop. Alert operator. Options: reduce frequency, switch to alternative compute. |

---

## Performance Requirements

### SLAs by Pipeline Step

| Step | Expected | SLA (P95) | Warning Threshold (P95 > 2x expected) |
|---|---|---|---|
| Trend detection (one source) | 15-30 sec | < 60 sec | > 60 sec |
| GPT-4o-mini scoring (one trend) | 2-5 sec | < 15 sec | > 10 sec |
| Image generation (one image, Replicate) | 5-25 sec | < 60 sec (includes cold start) | > 50 sec |
| Quality validation (one image) | 1-3 sec | < 10 sec | > 6 sec |
| Post-processing (one image) | 3-10 sec | < 30 sec | > 20 sec |
| Content moderation (one image) | 2-5 sec | < 15 sec | > 10 sec |
| Etsy listing creation (one listing) | 5-10 sec | < 30 sec | > 20 sec |
| **End-to-end (trend to listing)** | **~33 min** | **< 45 min** | **> 60 min** |
| Pricing engine (full daily run) | 5-10 min | < 15 min | > 20 min |
| Daily analytics sync | 3-5 min | < 10 min | > 15 min |

### Replicate Cold Start

Replicate models have cold start times of 10-30 seconds when not loaded. Mitigation:
- The first image generation in each workflow run may take longer. Factor this into timing estimates.
- Do NOT use Replicate's "always on" option (adds cost). Accept cold start for MVP.
- If cold starts become problematic (consistently > 30 sec), consider switching to a warm model or batching requests.

---

## Disaster Recovery

### Daily Database Backup

Run as part of the daily analytics workflow:

1. Export Supabase database via `pg_dump` (using Supabase service role connection)
2. Compress with gzip
3. Upload to Cloudflare R2: `backups/db/sticker-trendz-{YYYY-MM-DD}.sql.gz`
4. Delete backups older than 30 days from R2
5. Log backup success/failure in `pipeline_runs`

**Recovery procedure** (manual):
1. Download latest backup from R2
2. Create a new Supabase project (if original is corrupted)
3. Restore via `psql < backup.sql`
4. Update GitHub Secrets with new Supabase credentials
5. Re-seed OAuth tokens

### Image Storage

Cloudflare R2 provides 11 9s durability. No additional backup needed for sticker images.

### GitHub Actions Workflows

All workflow definitions are in version control (Git). Recovery is instantaneous via `git clone`.

---

## V1.1 Roadmap (Post-MVP)

The following features are explicitly descoped from MVP and planned for V1.1 (target: Month 3-4, after validating single sticker sales).

### Sticker Packs (V1.1)

**Estimated engineering effort**: 2-3 weeks

**Pack Types**:

| Pack Type | Contents | How It's Built |
|---|---|---|
| **Trending This Week** | Top 5 best-selling stickers from the past 7 days | Sort by `sales_count` desc, take top 5 |
| **Meme Pack** | 5 stickers tagged "meme" or "funny" | Filter by tags, sort by sales |
| **Aesthetic Pack** | 5 stickers tagged "aesthetic", "minimalist", or "cute" | Filter by tags, sort by sales |
| **Theme Pack** | 5 stickers sharing a trend topic (e.g., "cats", "anime") | Group by trend keywords, pick top cluster |
| **New Drops** | 5 most recent stickers from the past 7 days | Sort by `created_at` desc |

**Pack Pricing** (all prices include shipping):

| Pack Size | Price |
|---|---|
| 3-pack | $10.99 |
| 5-pack | $15.99 |
| 10-pack | $26.99 |

Packs increase the blended AOV from $4.79 to ~$8.73 -- a **82% increase** in revenue per order.

**V1.1 Data Models** (to be added):
- `sticker_packs` table
- `sticker_pack_items` join table
- `weekly-packs.yml` GitHub Actions workflow

**V1.1 Revenue Projections** (with packs):

| Metric | Moderate |
|---|---|
| Orders/month | ~250 |
| Blended AOV (with packs) | $8.73 |
| Gross revenue | $2,183 |
| Net profit | $1,035 |

### Additional V1.1 Features

| Feature | Description | Priority |
|---|---|---|
| Sticker Packs | Weekly auto-generated packs (see above) | P0 |
| International Shipping | Add international rates, update Etsy listings | P1 |
| Twitter/X Integration | Add as trend source if budget allows ($100/mo Basic tier) | P2 |
| Printful Integration | Enable for pack fulfillment (viable at 3+ stickers) | P2 |
| Lifestyle Mockup Automation | Auto-composite stickers onto laptop/bottle mockups | P2 |
| A/B Test Titles | Rotate GPT-generated titles to optimize CTR | P3 |

---

## Customer Communication (MVP)

For MVP, Etsy handles all customer-facing communications:
- **Order confirmation**: Sent automatically by Etsy
- **Shipping notification**: Sent automatically by Etsy when tracking is uploaded
- **Reviews**: Managed through Etsy's review system
- **Customer messages**: Operator checks Etsy messages daily. Respond within 24 hours.
- **Returns/refunds**: Handle on case-by-case basis via Etsy messages. Policy: replace defective stickers or refund. No return shipping required (stickers are low-cost items).

---

## Open Questions & Risks

### Blockers (must resolve before engineering starts)

| # | Question | Impact | Status | Owner |
|---|---|---|---|---|
| Q1 | Has Etsy API production access been applied for and approved? | **BLOCKER** -- cannot ship without it. Takes 1-4 weeks for approval. | NOT STARTED | Operator |
| Q2 | Has a Sticker Mule API account been set up and tested for single on-demand orders? | **BLOCKER** -- if Sticker Mule does not support single on-demand orders via API, self-fulfillment becomes the primary path. | NOT STARTED | Operator |
| Q9 | Has the initial Etsy OAuth authorization been completed and tokens stored in Supabase? | **BLOCKER** -- all Etsy workflows depend on valid OAuth tokens. | NOT STARTED | Operator |

### High Priority Questions

| # | Question | Impact | Recommended Answer |
|---|---|---|---|
| Q3 | Is self-fulfillment (USPS) a viable backup for MVP launch? | De-risks Sticker Mule dependency | Yes -- prepare shipping supplies, rigid mailers, USPS account, and stamp inventory before launch. |
| Q4 | What is the Etsy shop name and branding? | Needed for shop setup | Decide before launch. Recommend: "Sticker Trendz" or "TrendStickers" (check Etsy availability). |
| Q5 | Is there a budget cap for AI spend per month? | Controls runaway costs | Yes -- $150/month hard cap (see Error Handling section). |
| Q8 | What happens if a customer receives a defective sticker? | Customer support process | Replace or refund via Etsy. No return shipping required. Track defect rate; if > 5%, investigate print provider quality. |
| Q10 | Can Sticker Mule fulfill single-sticker on-demand orders via API? | Fulfillment viability | Validate with Sticker Mule support before development begins. Minimum order quantities may apply. |
| Q11 | How will GitHub Actions minutes be managed within the free tier? | Feasibility of 2-hour cron | See GitHub Actions Budget section. Well within free tier at 2-hour cron. See GitHub Actions Budget section. |
| Q12 | How will the system handle Etsy's `who_made` and `when_made` required fields? | Etsy listing compliance | `who_made = "someone_else"`, `when_made = "2020_2025"`. Verify Etsy allows this for AI-generated products. |
| Q13 | Does Etsy's current ToS allow AI-generated product listings? | Legal risk | As of 2025, Etsy requires disclosure. Verify current policy at launch time. Add disclosure to every listing. |
| Q14 | What specific Replicate SDXL model version should be pinned? | Reproducibility | Choose the latest stable SDXL version at development start. Pin the version hash in config. |
| Q15 | Is there a warm-standby plan if Supabase free tier pauses the project? | Availability risk | Cron jobs hitting Supabase every 2 hours prevent this. Document as a known risk. If paused, free tier projects can be resumed manually in the Supabase dashboard. |

### Legal/Compliance Questions

| # | Question | Impact | Recommended Answer |
|---|---|---|---|
| Q16 | Are AI-generated sticker designs eligible for copyright protection? | IP ownership | No -- per US Copyright Office (2023), purely AI-generated images are generally not copyrightable. Speed-to-market is the moat, not IP protection. |
| Q17 | Could AI-generated stickers inadvertently infringe on trademarks? | Legal liability | Yes. Mitigated by trademark blocklist. Monitor for DMCA notices and update blocklist. |
| Q18 | Does the Etsy shop need a business license? | Legal compliance | Depends on jurisdiction. Research local requirements before launch. |
| Q19 | Sales tax collection requirements? | Tax compliance | Etsy collects and remits in most US states (marketplace facilitator). Verify at launch time. |
| Q20 | What is the DMCA takedown response process? | Legal requirement | Defined above in Legal section: delist within 24 hours, review, update blocklist. |

### Known Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Etsy API approval takes > 4 weeks | Medium | Delays launch by weeks | Apply immediately. Prepare self-fulfillment as backup. |
| Sticker Mule does not support single on-demand orders | Medium | Must self-fulfill all orders | Test API before development. Prepare USPS self-fulfillment path. |
| GitHub Actions minutes exceeded | Low | Pipeline stops running | Implement caching, conditional jobs. Monitor usage weekly. Have Railway as contingency. |
| AI-generated stickers infringe trademarks | Medium | DMCA takedowns, Etsy shop suspension | Trademark blocklist + manual monitoring. Respond to DMCA within 24 hours. |
| Etsy changes ToS to prohibit AI-generated products | Low | Entire business model at risk | Monitor Etsy policy updates. Diversify to custom storefront (Phase 2) if needed. |
| Replicate pricing increases | Low | AI costs exceed budget | Pin model version. Have budget cap. Can switch to self-hosted Stable Diffusion if needed. |
| Low conversion rate (< 1%) | Medium | Revenue does not cover costs | Iterate on SEO, titles, images. A/B test in V1.1. Consider Etsy ads. |
| Supabase free tier exhausted | Low | Database unavailable | Monitor storage. Upgrade to Pro ($25/mo) when > 400 MB used. |

---

## What Triggers V1.1

Move to V1.1 (sticker packs + expanded features) when:
- [ ] MVP pipeline is stable (> 95% uptime for 2+ weeks)
- [ ] First 10 organic sales confirmed
- [ ] Positive unit economics verified (actual margins match projections)
- [ ] Etsy API access is stable and rate limits are not a concern

## What Triggers Phase 2

Move to Phase 2 (custom storefront + microservices) when:
- [ ] Revenue consistently exceeds $5,000/mo for 2+ months
- [ ] Hitting Etsy listing limits or free-tier infrastructure limits
- [ ] Etsy fees (6.5%) are eating significantly into margin
- [ ] Need faster trend detection (< 5 min) that GitHub Actions cannot support
- [ ] Customer demand for features Etsy does not support (subscriptions, custom sticker builder)

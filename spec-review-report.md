# MVP Spec Review Report: Sticker Trendz

**Reviewed documents**:
- `/Users/abhishekhodavdekar/git/sticker-trendz/mvp-spec.md`
- `/Users/abhishekhodavdekar/git/sticker-trendz/trending-sticker-architecture.md`

**Reviewer**: Product Manager (Automated Spec Review)
**Date**: 2026-02-06
**Verdict**: The spec is strong on pricing mechanics, data models, and infrastructure choices. However, it has significant gaps in acceptance criteria, error handling, security, legal/compliance, testing, monitoring, and operational runbooks. Addressing these gaps before engineering begins will prevent costly rework.

---

## Executive Summary

The MVP spec is unusually detailed for a side-project/indie product -- the pricing engine, database schemas, GitHub Actions workflows, and fulfillment routing are well thought through. That said, the spec reads more like an **architecture document** than a **product specification**. It describes *what the system does* but is weak on *what happens when things go wrong*, *how we know it is working correctly*, and *what the exact acceptance criteria are* for each capability.

**Critical gaps** (must fix before engineering):
1. No acceptance criteria for any capability
2. No error handling or retry logic defined
3. No Etsy API rate limit / quota management strategy
4. No content moderation appeals or false-positive handling
5. No monitoring, alerting, or operational runbook
6. No testing strategy or validation criteria
7. No legal/IP/copyright risk mitigation for AI-generated images
8. No Etsy OAuth token refresh strategy (tokens expire)

**Total findings**: 47 gaps across 10 categories.

---

## 1. Missing or Incomplete Requirements

### 1.1 Etsy OAuth Token Lifecycle [CRITICAL]
**Gap**: The spec lists `ETSY_ACCESS_TOKEN` as an environment variable but does not address that Etsy OAuth tokens expire. Etsy v3 API uses OAuth 2.0 with access tokens that expire after 3600 seconds (1 hour). The spec has no refresh token strategy, no token storage mechanism, and no handling for token expiration mid-workflow.

**Recommendation**: Add a requirement for an OAuth token refresh module that:
- Stores refresh tokens securely (Supabase or GitHub Secrets cannot be updated at runtime)
- Refreshes the access token before each workflow run
- Handles token refresh failures with alerting
- Consider using Supabase to store and rotate tokens

### 1.2 Etsy API Rate Limits [CRITICAL]
**Gap**: Running trend monitoring every 15 minutes and generating 3-5 stickers per trend means potentially dozens of Etsy API calls per hour (listing creation, image upload, price updates). Etsy's API rate limit is 10,000 calls per day for production apps. The spec does not account for rate limiting.

**Recommendation**: Add rate limit tracking requirements:
- Track daily API call count in Upstash Redis
- Implement request queuing when approaching limits
- Define priority order (new listings > price updates > analytics sync)
- Add rate limit monitoring to daily summary email

### 1.3 Order Fulfillment Trigger Mechanism [HIGH]
**Gap**: The fulfillment flow (line 811-833 of mvp-spec.md) says "Etsy webhook / daily sync picks up order" but does not specify:
- Whether webhooks or polling is used (Etsy's webhook support is limited)
- Polling frequency for order sync
- How to handle orders between sync intervals (customer expects immediate confirmation)
- What happens if fulfillment API (Sticker Mule) is down

**Recommendation**: Specify that `daily-analytics.yml` also handles order sync, or create a separate `order-sync.yml` workflow running every 1-2 hours. Define retry logic for failed fulfillment submissions.

### 1.4 Image Generation Failure Handling [HIGH]
**Gap**: The pipeline assumes image generation always succeeds. Replicate API can fail, return low-quality images, timeout, or return images that do not match the sticker format (e.g., text-heavy, wrong aspect ratio).

**Recommendation**: Add requirements for:
- Retry logic (max 3 retries with exponential backoff)
- Image quality validation (check dimensions, file size, has transparency)
- Fallback behavior: if all 3-5 variations fail, mark trend as `generation_failed` and skip
- Alert if failure rate exceeds 20% in a single run

### 1.5 Duplicate Trend Detection Across Sources [MEDIUM]
**Gap**: The spec says "Check Supabase for duplicates" but the check is only on trends already processed. If the same topic trends on Reddit and Twitter simultaneously, the system could generate duplicate stickers for the same trend from different sources.

**Recommendation**: Add cross-source deduplication:
- Normalize trend topics before duplicate check (lowercase, stem keywords)
- Use keyword overlap scoring (e.g., Jaccard similarity > 0.6 = duplicate)
- Store a canonical trend and link multiple sources to it
- Add a `source` array field or a `trend_sources` join table instead of a single `source TEXT`

### 1.6 Sticker Mule API Integration Details [MEDIUM]
**Gap**: The spec names Sticker Mule as the primary fulfillment provider but provides no details on:
- Sticker Mule API capabilities (do they support automated single-sticker orders via API?)
- Minimum order quantities
- API authentication method
- Turnaround time expectations
- Whether they support pack fulfillment (multiple stickers in one envelope)

**Recommendation**: Validate that Sticker Mule's API supports the MVP use case (single on-demand orders). If not, self-fulfillment via USPS may need to be the primary path for MVP. Document API capabilities and constraints.

### 1.7 International Orders [MEDIUM]
**Gap**: The pricing spec mentions international orders in the shipping_rates table and notes "+$2.00 surcharge (or exclude initially)" for international orders, but does not clearly decide whether international orders are in scope for MVP.

**Recommendation**: Make an explicit decision. Recommended: Exclude international shipping from MVP. Add to Etsy listing settings to restrict to US-only. Revisit for Phase 2.

### 1.8 Customer Communication [LOW]
**Gap**: No requirement for:
- Order confirmation emails (Etsy handles this, but should be noted)
- Shipping notification emails
- Customer support workflow (returns, refunds, complaints)
- Response time SLAs

**Recommendation**: Document that Etsy handles customer-facing communications for MVP. Define a manual process for handling Etsy messages (check daily, respond within 24 hours). Add Etsy message monitoring to the daily analytics sync.

---

## 2. Ambiguous Acceptance Criteria

### 2.1 No Acceptance Criteria Defined [CRITICAL]
**Gap**: The entire spec has zero formal acceptance criteria. There are no "Given/When/Then" statements, no definition of done for any capability, and no way for an engineer to know when a feature is "complete."

**Recommendation**: Add acceptance criteria for every pipeline step. Examples:

**Trend Detection**:
- Given the trend monitor runs, when it finds a topic with an overall score >= 7.0, then it is stored in Supabase with status `discovered` and all score fields populated
- Given a topic has already been processed (exists in `trends` table with status != `discovered`), when the monitor encounters it again, then it is skipped (no duplicate row created)
- Given all social media APIs are unreachable, when the monitor runs, then it logs errors, sends an alert email, and exits with a non-zero exit code

**Image Generation**:
- Given a trend with status `discovered`, when sticker generation runs, then 3-5 PNG images are generated at 1024x1024, each with a transparent background
- Given Replicate returns an error, when generation retries 3 times and all fail, then the trend status is set to `generation_failed` and an alert is sent

**Publishing**:
- Given a sticker with moderation_status `approved`, when the publisher runs, then an Etsy listing is created with title <= 140 characters, exactly 13 tags, price from `pricing_tiers`, and shipping set to $0.00
- Given the Etsy API returns a rate limit error (429), when the publisher encounters it, then it stops publishing, logs remaining items, and retries in the next cycle

### 2.2 Trend Scoring Threshold [MEDIUM]
**Gap**: "Threshold: Only proceed with trends scoring 7+ overall" -- but the scoring is done by GPT-4o-mini with a 1-10 scale. LLM scoring is inherently inconsistent. A score of 7 from one run may not mean the same as 7 from another run.

**Recommendation**: Add calibration requirements:
- Include 2-3 reference examples in the scoring prompt (anchoring)
- Log all scores and periodically review score distribution
- Consider a two-pass approach: initial LLM scoring, then a second pass comparing top candidates
- Define what to do if no trends score 7+ in a cycle (expected behavior, not an error)

### 2.3 "Content Safety" Scoring vs. Moderation [MEDIUM]
**Gap**: Content safety is scored twice: once in GPT-4o-mini trend scoring (Step 1) and again via OpenAI Moderation API on the generated image (Step 4). The relationship between these two checks is ambiguous. Can a trend pass Step 1 safety scoring but fail Step 4 image moderation?

**Recommendation**: Clarify that:
- Step 1 safety scoring is a pre-filter on the *topic* (saves image generation costs)
- Step 4 moderation is validation on the *generated image* (catches visual content issues)
- Both must pass. A trend can pass topic safety but fail image moderation. Define this as expected behavior with a specific status (`moderation_rejected`).

### 2.4 "Sales Override" Definition [LOW]
**Gap**: "If a sticker sells 10+ units at any tier, it stays at current price" -- does this mean 10 units total lifetime, or 10 units since entering the current tier? If a sticker sold 10 units at `just_dropped` price, then moved to `trending`, does the override apply?

**Recommendation**: Clarify as "10+ units sold while at the current pricing tier." Reset the counter when a tier change occurs. Add a `sales_at_current_tier` field to the stickers table, or compute it from the orders table.

---

## 3. Missing Edge Cases and Error States

### 3.1 API Failures and Retry Logic [CRITICAL]
**Gap**: The spec does not define behavior for ANY external API failure:
- Reddit/Twitter/TikTok/Google Trends API down or returning errors
- OpenAI API down or returning errors (scoring, moderation, prompt generation)
- Replicate API down or returning errors
- Etsy API down or returning errors
- Supabase unavailable
- Cloudflare R2 upload failures
- Sticker Mule API failures
- SendGrid failures

**Recommendation**: Add a global error handling section defining:
- Retry policy: 3 retries with exponential backoff (1s, 4s, 16s) for all external APIs
- Circuit breaker: After 5 consecutive failures to the same service, skip for this run and alert
- Partial failure: If 1 of 4 trend sources fails, continue with the other 3 (degrade gracefully)
- GitHub Actions exit codes: non-zero on unrecoverable errors, zero on partial success
- All errors logged to Supabase `error_log` table for debugging

### 3.2 GitHub Actions Free Tier Limits [HIGH]
**Gap**: GitHub Actions free tier provides 2,000 minutes/month. Running every 15 minutes = 96 runs/day. If each run takes 2-5 minutes (Python setup, dependency install, trend detection, image generation, publishing), that is 192-480 minutes/day, or 5,760-14,400 minutes/month. This FAR EXCEEDS the free tier.

**Recommendation**: This is a fundamental feasibility issue. Options:
1. Reduce polling frequency to every 60 minutes (2,880-7,200 min/month -- still over)
2. Separate the trend monitor (lightweight, 1-min run) from the image generator (heavy, 3-5 min run). Only trigger the generator when new qualifying trends are found
3. Cache dependencies aggressively (actions/cache) to reduce per-run time
4. Consider moving to a free-tier compute alternative (Railway, Render cron, or a always-on free-tier VM)
5. Calculate the actual expected monthly minutes and document it

### 3.3 Etsy Listing Limits [HIGH]
**Gap**: Etsy charges $0.20 per listing and listings expire after 4 months. The spec targets 150-500 listings/month. After 3 months, that is 450-1,500 active listings. The spec does not define:
- Maximum concurrent active listings
- Listing renewal strategy (auto-renew or let expire?)
- Budget cap for listing fees ($0.20 x 500 = $100/month in listing fees alone)

**Recommendation**: Define a maximum active listing count (e.g., 300). The archiver should proactively delist underperformers to stay within budget. Add listing fee tracking to the daily analytics.

### 3.4 Trend Volume Spikes [MEDIUM]
**Gap**: During major events (elections, celebrity moments, sports finals), the system could detect dozens of qualifying trends simultaneously. Processing all of them would burn through API budgets and GitHub Actions minutes.

**Recommendation**: Add a per-cycle cap:
- Max 5 new trends processed per 15-minute cycle
- Prioritize by overall score (highest first)
- Queue remaining for next cycle
- Add a daily cap on image generation (e.g., max 50 images/day) to control Replicate costs

### 3.5 Sticker Image Quality [MEDIUM]
**Gap**: No automated quality validation on generated images. Stable Diffusion can produce:
- Distorted text in images
- Artifacts and visual glitches
- Images that are not sticker-shaped (e.g., full scenes instead of die-cut objects)
- Low contrast images that will not print well

**Recommendation**: Add post-generation quality checks:
- Verify image is not mostly blank/white (> 80% white pixels after background removal)
- Verify image is not too complex (file size heuristic -- stickers should be simple)
- Verify aspect ratio after auto-crop is reasonable (not too thin/elongated)
- If quality check fails, regenerate with a modified prompt (max 2 retries)

### 3.6 Empty State: No Trends Found [LOW]
**Gap**: What happens if no trends score 7+ in a cycle? The spec does not define this as a normal condition.

**Recommendation**: Document this as expected behavior. The monitor should log "No qualifying trends found" and exit successfully (exit code 0). No alert needed unless this persists for 24+ hours (may indicate API issues).

### 3.7 Concurrent Workflow Runs [MEDIUM]
**Gap**: GitHub Actions cron triggers are not guaranteed to be precise. Two runs could overlap if one takes longer than 15 minutes. This could cause duplicate trend processing or race conditions on Supabase writes.

**Recommendation**: Add a workflow concurrency lock:
```yaml
concurrency:
  group: trend-monitor
  cancel-in-progress: false
```
This ensures only one instance runs at a time. Late runs queue instead of overlapping.

---

## 4. Gaps in User Flows

### 4.1 No End-to-End User Flow Defined [HIGH]
**Gap**: The spec describes the *system pipeline* but not the *user experience*. There is no documentation of:
- What a customer sees when browsing the Etsy shop
- How listings are organized (sections, categories)
- What the listing page looks like (title, description, images, reviews)
- How pack listings present individual sticker images
- Customer purchase flow through checkout
- Post-purchase experience (order confirmation, shipping updates, delivery)

**Recommendation**: Since Etsy controls the UX, this section can be lighter than a custom storefront. But add:
- Etsy shop structure: define shop sections (e.g., "Trending Now", "Meme Stickers", "Sticker Packs", "Under $5")
- Listing image strategy: primary image = sticker on transparent background, secondary images = lifestyle mockups (sticker on laptop, water bottle)
- Description template with sections (What You Get, Size & Material, Shipping Info)
- Shop "About" page content

### 4.2 Manual Review Flow for Flagged Content [MEDIUM]
**Gap**: Step 4 says "Flag for manual review (email alert) if borderline" but does not define:
- What "borderline" means quantitatively
- What the email alert contains
- How the reviewer approves/rejects (Supabase dashboard? CLI tool? Email reply?)
- SLA for manual review (how long can a flagged sticker wait?)
- What happens to the trend status while awaiting review

**Recommendation**: Define:
- Borderline = moderation score between 0.4 and 0.7 (configurable)
- Email contains: sticker image, trend topic, moderation scores, approve/reject links (deep links to Supabase dashboard or a simple approval API endpoint)
- Review SLA: 24 hours. After 48 hours, auto-reject and alert
- Trend status stays at `generated` with sticker moderation_status = `flagged`

### 4.3 Analytics Consumption Flow [LOW]
**Gap**: The daily analytics sync collects data but the spec does not define how the operator consumes it. Where do they see:
- Current revenue and order count
- Best/worst performing stickers
- Trend scoring accuracy over time
- Cost tracking (AI spend, listing fees)

**Recommendation**: For MVP, define:
- Daily summary email (already mentioned) with key metrics
- A simple Supabase dashboard query or view that the operator can check
- Weekly manual review cadence to assess trend quality and pricing effectiveness

---

## 5. Missing Non-Functional Requirements

### 5.1 No Performance Requirements [HIGH]
**Gap**: The spec lists timing estimates for each pipeline step but does not define performance requirements or SLAs.

**Recommendation**: Define:
- Trend-to-listing SLA: < 45 minutes (end-to-end, 95th percentile)
- Etsy listing creation: < 30 seconds per listing
- Pricing engine daily run: < 15 minutes total
- Image generation: < 2 minutes per sticker (including retries)
- If any step exceeds 2x its expected time, log a warning

### 5.2 No Security Requirements [CRITICAL]
**Gap**: The spec stores API keys as GitHub Secrets (fine for CI) but does not address:
- Supabase Row-Level Security (RLS) policies -- is the Supabase key a service key or anon key?
- Customer data handling in the `orders.customer_data` JSONB field (PII: name, address)
- R2 bucket access controls (are sticker images public? They should be, but intentionally)
- Who has access to the GitHub repo and secrets
- Secret rotation schedule
- Logging -- do logs contain sensitive data?

**Recommendation**: Add a security section:
- Use Supabase service role key (not anon key) stored as GitHub Secret
- Enable RLS on all tables (even with service key, defense in depth)
- R2 bucket: public read for sticker images, no listing/write from public
- Never log API keys, customer PII, or full API responses
- Rotate API keys quarterly
- `customer_data` JSONB: store only what is needed for fulfillment, delete after 90 days

### 5.3 No Data Retention or Privacy Policy [HIGH]
**Gap**: The spec stores customer data (`orders.customer_data` JSONB) but does not address:
- What data is collected and stored
- Retention period
- GDPR/CCPA compliance (even for a small shop, Etsy requires this)
- Right to deletion requests
- Privacy policy content for the Etsy shop

**Recommendation**: Define:
- `customer_data` stores only: name, shipping address, email (as provided by Etsy API)
- Retention: 90 days after order delivery, then purge PII (keep anonymized order record for analytics)
- Add a privacy policy link to the Etsy shop (required by Etsy ToS)
- Define a process for handling deletion requests (manual for MVP)

### 5.4 No Accessibility Requirements [LOW]
**Gap**: Since the storefront is Etsy (which handles its own accessibility), this is lower priority. However, generated listing content (titles, descriptions, alt text) should be considered.

**Recommendation**: Ensure GPT-4o-mini generates:
- Descriptive image alt text for each listing (Etsy supports this)
- Plain-language descriptions (avoid jargon, ALL CAPS, or excessive emoji)

### 5.5 No Disaster Recovery or Backup Strategy [MEDIUM]
**Gap**: Supabase free tier does not include point-in-time recovery. If data is corrupted or accidentally deleted, there is no recovery path.

**Recommendation**: Add:
- Daily database export (pg_dump) stored in R2 or GitHub artifact
- R2 images are durable by default (Cloudflare 11 9s), no additional backup needed
- Document manual recovery procedure

### 5.6 No Observability or Monitoring [HIGH]
**Gap**: The spec mentions a daily summary email but has no monitoring, alerting, or logging strategy for:
- Pipeline health (did the last run succeed?)
- Error rates by service
- API cost tracking
- Workflow run duration trending upward

**Recommendation**: Add:
- GitHub Actions workflow status monitoring (email on failure -- GitHub supports this natively)
- Log all runs to a `pipeline_runs` table in Supabase (start_time, end_time, trends_found, stickers_generated, errors)
- Weekly review of pipeline_runs to spot trends
- Cost tracking: log estimated AI spend per run (tokens used, images generated)

---

## 6. Missing Success Metrics and KPIs

### 6.1 Operational Metrics Missing [HIGH]
**Gap**: The "Success Criteria" section (line 996-1006) defines business milestones but no operational KPIs. There is no way to know if the *system* is working correctly.

**Recommendation**: Add operational metrics:

| Metric | Target | Measurement |
|--------|--------|-------------|
| Pipeline uptime | > 95% of scheduled runs succeed | GitHub Actions success rate |
| Trend detection rate | 5+ new qualifying trends/day | Count of trends with score >= 7 per day |
| Image generation success rate | > 80% of attempts produce usable images | stickers generated / attempts |
| Content moderation false positive rate | < 10% | Manual review of rejected stickers weekly |
| Etsy listing creation success rate | > 95% | Listings created / listings attempted |
| Average trend-to-listing time | < 35 minutes | Timestamp delta: trend.created_at to sticker.published_at |
| Daily AI spend | < $10/day ($300/month) | Logged API costs |

### 6.2 Revenue Metrics Not Tracked in System [MEDIUM]
**Gap**: Revenue targets are stated but the spec does not define how revenue is tracked in the system. The `orders` table tracks individual orders, but there is no:
- Revenue aggregation view or query
- Profit margin tracking per sticker (actual vs. projected)
- Cost of goods sold tracking
- AI generation cost attribution (cost to generate a sticker that never sells = waste)

**Recommendation**: Add:
- A `daily_metrics` table or materialized view aggregating: revenue, orders, COGS, profit, AI spend, listing fees
- Track "cost to acquire a sale" = (total AI spend + listing fees) / total orders
- Track "sticker conversion rate" = stickers with 1+ sales / total stickers published

### 6.3 No Etsy SEO Performance Tracking [MEDIUM]
**Gap**: Etsy SEO is the primary discovery channel but the spec does not define how SEO performance is measured or improved.

**Recommendation**: Add:
- Track Etsy view count per listing (available via Etsy API)
- Track click-through rate (views / impressions, if available)
- Track conversion rate per listing (sales / views)
- Weekly review: identify listings with high views but low sales (pricing issue?) and listings with low views (SEO issue?)
- A/B test title formats by rotating GPT-generated titles

---

## 7. Unclear Scope Boundaries

### 7.1 Storefront Ambiguity Between Specs [HIGH]
**Gap**: The MVP spec (`mvp-spec.md`) uses Etsy as the sole storefront with no custom site. The architecture doc (`trending-sticker-architecture.md`) describes a custom web storefront, mobile app, and admin dashboard. While the architecture doc is clearly a Phase 2-3 vision, the two documents are not cross-referenced and could confuse an engineer about what to build.

**Recommendation**: Add an explicit scope statement to the top of `mvp-spec.md`:

> **Scope**: This spec covers MVP (Phase 1) only. The storefront is Etsy -- there is no custom website, mobile app, or admin dashboard. See `trending-sticker-architecture.md` for the Phase 2-3 vision. Do NOT build anything from the architecture doc for MVP.

### 7.2 Pack Builder Scope [MEDIUM]
**Gap**: The weekly pack builder is included in the MVP spec, but packs add significant complexity (composite thumbnails, pack pricing, pack-specific Etsy listings, pack fulfillment coordination). For an MVP targeting "first sale within 2 weeks," this may be premature.

**Recommendation**: Consider moving the pack builder to a "V1.1" milestone:
- MVP (Week 1-4): Singles only. Validate that the trend-to-Etsy pipeline works end-to-end.
- V1.1 (Week 5-8): Add packs once single sticker sales prove demand.
- This reduces MVP engineering scope by approximately 20-25%.

### 7.3 Pinterest/Instagram/Reddit Marketing [LOW]
**Gap**: The discovery strategy mentions Pinterest, Instagram, and Reddit as manual channels but does not clarify if any tooling or automation for these is in MVP scope.

**Recommendation**: Explicitly state: "MVP discovery is Etsy SEO only. Pinterest/Instagram/Reddit posting is manual and not part of the automated pipeline. No tooling will be built for these channels in MVP."

---

## 8. Missing Technical Considerations

### 8.1 Etsy API v3 Specific Constraints [CRITICAL]
**Gap**: The spec references "Etsy Open API v3" but does not address:
- Etsy requires app review and approval before production API access
- Etsy API has specific required fields for listing creation that are not all listed
- Etsy image upload has specific size/format requirements (max 10 images per listing, specific dimensions)
- Etsy listing requires a `who_made`, `when_made`, and `is_supply` field
- Taxonomy IDs are required for categorization
- Etsy's API does not support all listing fields that the web UI supports

**Recommendation**: Add a technical spike / validation task:
- Create an Etsy developer account and request API access
- Test listing creation in Etsy's sandbox
- Document all required and optional fields
- Confirm image upload requirements
- Estimate time to get production API approval (can take 1-4 weeks)
- This is on the critical path -- if Etsy API access is delayed, the entire MVP is blocked

### 8.2 Replicate API Cold Start [MEDIUM]
**Gap**: Replicate's Stable Diffusion models have cold start times of 10-30 seconds when the model is not loaded. The spec's image generation timing (15-75 seconds) may not account for cold starts on the first image.

**Recommendation**: Document expected cold start behavior. Consider:
- Sending a "warm-up" request at the start of each workflow run
- Using Replicate's "always on" option for the model (adds cost)
- Adjusting timing estimates to include cold start

### 8.3 Reddit JSON API Limitations [MEDIUM]
**Gap**: The spec says Reddit JSON API requires "no key needed." While Reddit does allow appending `.json` to URLs, this is rate-limited to 10 requests/minute for unauthenticated requests and Reddit actively blocks automated scraping.

**Recommendation**: Register for a Reddit API application (free, provides 60 requests/minute for OAuth-authenticated requests). Update the spec to reflect this dependency.

### 8.4 TikTok Data Access [MEDIUM]
**Gap**: The spec lists TikTok with "Unofficial/scraping" as the API method. TikTok actively combats scraping and has no free public API for trend data. This is unreliable and potentially violates TikTok's ToS.

**Recommendation**: For MVP, remove TikTok as a data source. It adds complexity and legal risk with minimal incremental value when Reddit and Google Trends are available. Revisit in Phase 2 when budget supports TikTok's Research API or a third-party data provider.

### 8.5 Twitter/X Free Tier Severely Limited [MEDIUM]
**Gap**: Twitter/X's free API tier allows only 1,500 tweets/month for read access (as of 2025), which is insufficient for trend monitoring. The Basic tier ($100/month) provides 10,000 tweets/month.

**Recommendation**: For MVP on a $75-200/month budget, Twitter is not viable at the free tier. Either:
- Remove Twitter from MVP scope and rely on Reddit + Google Trends
- Budget $100/month for Twitter Basic tier (pushes total cost to $175-300/month)
- Document this decision explicitly

### 8.6 GPT-4o-mini JSON Output Reliability [LOW]
**Gap**: The scoring prompt asks GPT-4o-mini to "Return JSON" but does not use structured output or function calling to guarantee valid JSON. LLMs can return malformed JSON, extra commentary, or markdown code blocks around JSON.

**Recommendation**: Use OpenAI's structured output feature (response_format: { type: "json_object" }) or function calling to guarantee valid JSON. Add JSON parse error handling with retry.

### 8.7 Supabase Free Tier Constraints [MEDIUM]
**Gap**: Supabase free tier limits:
- 500 MB database storage
- 2 GB bandwidth
- 1 GB file storage
- Project pauses after 7 days of inactivity
- 2 projects max

The spec does not estimate storage growth or address these limits.

**Recommendation**: Estimate storage needs:
- Each trend row: ~1 KB
- Each sticker row: ~2 KB
- Each order row: ~1 KB
- At 500 stickers/month: ~1 MB/month database growth
- 500 MB limit gives approximately 2+ years of runway -- this is likely fine
- But the 7-day inactivity pause could be a problem if there is a period with no activity (e.g., vacation). The cron jobs hitting Supabase every 15 minutes prevent this.
- Document that Supabase free tier is viable for MVP but plan migration to Pro ($25/month) if approaching limits

---

## 9. Missing Testing and Validation Criteria

### 9.1 No Testing Strategy [CRITICAL]
**Gap**: The project structure lists test files (`tests/test_trends.py`, `tests/test_stickers.py`, etc.) but the spec defines zero testing requirements. There are no:
- Unit test requirements
- Integration test requirements
- End-to-end test scenarios
- Test data/fixtures
- CI test pipeline

**Recommendation**: Add a testing section:

**Unit Tests (run on every PR)**:
- Trend scorer: given sample trend data, verify scoring logic produces expected tiers
- Pricing engine: given a sticker with known age and sales, verify correct tier and price
- Floor price calculator: given known costs, verify floor price calculation
- Image post-processor: given a sample image, verify resize, crop, and thumbnail generation

**Integration Tests (run daily or on demand)**:
- Trend source connectors: verify Reddit, Google Trends APIs return parseable data
- Replicate image generation: verify a test prompt returns a valid PNG
- Etsy listing creation: verify a test listing can be created in Etsy sandbox
- Supabase read/write: verify CRUD operations on all tables

**End-to-End Test (run weekly)**:
- Full pipeline: inject a fake trend, verify it flows through scoring -> generation -> moderation -> publishing
- Pricing engine: seed test data and verify price adjustments match expectations
- Pack builder: seed 10+ approved stickers and verify pack creation

### 9.2 No Etsy Sandbox Testing Plan [HIGH]
**Gap**: The spec does not mention Etsy's sandbox environment for testing. Publishing to a real Etsy shop during development would create garbage listings.

**Recommendation**: Add:
- All development and testing uses Etsy's sandbox environment
- Define a cutover plan from sandbox to production
- Test with at least 10 listings in sandbox before going live
- Verify pricing, images, tags, and descriptions render correctly

### 9.3 No Rollback Criteria [MEDIUM]
**Gap**: If a deployment introduces a bug (e.g., all stickers get priced at $0, or moderation passes inappropriate content), there is no defined rollback plan.

**Recommendation**: Add rollback criteria:
- If pricing engine sets any price below floor: halt pricing engine, revert via price_history table, alert
- If moderation passes content that is later flagged: ability to bulk-deactivate listings by batch/date
- If image generation produces garbage: ability to bulk-delete stickers created after a timestamp
- All operations should be idempotent where possible

---

## 10. Open Questions That Must Be Addressed

### 10.1 Business/Strategic Questions

| # | Question | Impact | Recommended Answer |
|---|----------|--------|-------------------|
| Q1 | Has Etsy API production access been applied for and approved? | BLOCKER -- cannot ship without it | Apply immediately; takes 1-4 weeks |
| Q2 | Has a Sticker Mule API account been set up and tested? | BLOCKER for fulfillment | Set up and test with a sample order |
| Q3 | Is self-fulfillment (USPS) a viable backup for MVP launch? | De-risks Sticker Mule dependency | Yes -- prepare shipping supplies and USPS account |
| Q4 | What is the Etsy shop name and branding? | Needed for shop setup | Decide before launch |
| Q5 | Is there a budget cap for AI spend per month? | Controls runaway costs | Recommend $150/month hard cap for MVP |
| Q6 | Are sticker packs in MVP or V1.1? | Scoping decision | Recommend V1.1 (see section 7.2) |
| Q7 | Is international shipping in MVP? | Scoping decision | Recommend exclude from MVP |
| Q8 | What happens if a customer receives a defective sticker? | Customer support process | Define refund/reprint policy |

### 10.2 Technical Questions

| # | Question | Impact | Recommended Answer |
|---|----------|--------|-------------------|
| Q9 | How will Etsy OAuth tokens be refreshed? (tokens expire hourly) | BLOCKER -- workflows will fail after 1 hour | Store refresh token in Supabase, refresh before each run |
| Q10 | Can Sticker Mule fulfill single-sticker on-demand orders via API? | Fulfillment viability | Validate with Sticker Mule support |
| Q11 | How will GitHub Actions minutes be managed within the free tier? | Feasibility of 15-min cron | See section 3.2 -- likely need to reduce frequency or optimize |
| Q12 | How will the system handle Etsy's `who_made` and `when_made` required fields for AI-generated products? | Etsy listing compliance | `who_made` = "someone_else", `when_made` = "2020_2025" -- verify Etsy allows AI-generated products |
| Q13 | Does Etsy's ToS allow AI-generated product listings? | LEGAL RISK | As of 2025, Etsy requires disclosure of AI use. Verify current policy and add disclosure to listings. |
| Q14 | What is the Replicate model version to use? | Reproducibility | Pin a specific SDXL model version |
| Q15 | Is there a warm-standby plan if Supabase free tier pauses the project? | Availability risk | Cron jobs prevent this, but document as a known risk |

### 10.3 Legal/Compliance Questions

| # | Question | Impact | Recommended Answer |
|---|----------|--------|-------------------|
| Q16 | Are AI-generated sticker designs eligible for copyright protection? | IP ownership | In the US, purely AI-generated images are generally not copyrightable (per US Copyright Office 2023 guidance). This means competitors could copy designs. |
| Q17 | Could AI-generated stickers inadvertently infringe on existing trademarks or copyrighted characters? | Legal liability | Yes -- trending topics often involve brands, characters, celebrities. Add a trademark keyword blocklist. |
| Q18 | Does the Etsy shop need a business license? | Legal compliance | Depends on jurisdiction. Research local requirements. |
| Q19 | Sales tax collection requirements? | Tax compliance | Etsy collects and remits sales tax in most US states. Verify and document. |
| Q20 | What is the DMCA takedown response process? | Legal requirement | Define: receive notice -> delist within 24 hours -> review -> counter-notice if applicable |

---

## Summary of Findings by Severity

### CRITICAL (Must fix before engineering starts) -- 6 items
1. No acceptance criteria for any capability (Section 2.1)
2. No error handling or retry logic defined (Section 3.1)
3. Etsy OAuth token refresh strategy missing (Section 1.1)
4. Etsy API rate limit management missing (Section 1.2)
5. No security requirements defined (Section 5.2)
6. No testing strategy defined (Section 9.1)

### HIGH (Must fix before launch) -- 11 items
1. GitHub Actions free tier likely insufficient (Section 3.2)
2. Etsy listing limits and budget cap undefined (Section 3.3)
3. Order fulfillment trigger mechanism unclear (Section 1.3)
4. Image generation failure handling missing (Section 1.4)
5. No end-to-end user flow defined (Section 4.1)
6. No performance requirements (Section 5.1)
7. No data retention or privacy policy (Section 5.3)
8. No observability or monitoring (Section 5.6)
9. Operational metrics missing (Section 6.1)
10. Storefront ambiguity between specs (Section 7.1)
11. Etsy API v3 specific constraints not addressed (Section 8.1)

### MEDIUM (Should fix before launch) -- 18 items
1. Duplicate trend detection across sources (Section 1.5)
2. Sticker Mule API integration details missing (Section 1.6)
3. International orders scope undecided (Section 1.7)
4. Trend scoring inconsistency (Section 2.2)
5. Safety scoring vs. moderation ambiguity (Section 2.3)
6. Trend volume spike handling (Section 3.4)
7. Image quality validation (Section 3.5)
8. Concurrent workflow runs (Section 3.7)
9. Manual review flow undefined (Section 4.2)
10. No disaster recovery or backup strategy (Section 5.5)
11. Revenue metrics not tracked in system (Section 6.2)
12. Etsy SEO performance tracking missing (Section 6.3)
13. Pack builder may be premature for MVP (Section 7.2)
14. Replicate cold start timing (Section 8.2)
15. Reddit API authentication needed (Section 8.3)
16. TikTok scraping unreliable and risky (Section 8.4)
17. Twitter/X free tier insufficient (Section 8.5)
18. Supabase free tier constraints (Section 8.7)

### LOW (Nice to have) -- 6 items
1. Customer communication flows (Section 1.8)
2. Sales override definition ambiguity (Section 2.4)
3. Empty state handling (Section 3.6)
4. Analytics consumption flow (Section 4.3)
5. Accessibility requirements (Section 5.4)
6. GPT-4o-mini JSON output reliability (Section 8.6)

---

## Recommended Next Steps

1. **Validate blockers immediately**: Apply for Etsy API access (Q1), test Sticker Mule API (Q2), calculate GitHub Actions minutes budget (Q9)
2. **Add acceptance criteria**: Write Given/When/Then for every pipeline step before engineering starts
3. **Add error handling section**: Define retry, circuit breaker, and alerting policies
4. **Descope for speed**: Remove packs, TikTok, and Twitter from MVP. Focus on Reddit + Google Trends -> Sticker -> Etsy with Sticker Mule fulfillment
5. **Add security and legal sections**: Address Etsy ToS for AI products, trademark blocklist, data retention
6. **Add testing section**: Define unit, integration, and E2E test requirements with Etsy sandbox
7. **Resolve the GitHub Actions minutes feasibility question**: This could force an architecture change

---

*This review was generated by analyzing both spec documents in their entirety. All line references are to `/Users/abhishekhodavdekar/git/sticker-trendz/mvp-spec.md` unless otherwise noted.*

# Sticker Trendz

Automated pipeline that monitors trending topics, generates sticker designs with AI, and publishes them to Etsy.

## Setup

### GitHub Secrets

Add these under **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Description |
|---|---|
| `SUPABASE_URL` | Supabase project URL (e.g. `https://your-project.supabase.co`) |
| `SUPABASE_SERVICE_KEY` | Supabase service role key (not the anon key) |
| `UPSTASH_REDIS_URL` | Upstash Redis REST URL |
| `UPSTASH_REDIS_TOKEN` | Upstash Redis REST token |
| `RESEND_API_KEY` | Resend API key for email alerts |
| `ALERT_EMAIL` | Email address to receive operational alerts |
| `ETSY_API_KEY` | Etsy API key (client_id) |
| `ETSY_API_SECRET` | Etsy API secret (client_secret) |
| `ETSY_SHOP_ID` | Etsy shop ID |
| `CLOUDFLARE_R2_ACCESS_KEY` | Cloudflare R2 access key |
| `CLOUDFLARE_R2_SECRET_KEY` | Cloudflare R2 secret key |
| `CLOUDFLARE_R2_BUCKET` | Cloudflare R2 bucket name |
| `CLOUDFLARE_R2_ENDPOINT` | Cloudflare R2 endpoint URL (e.g. `https://<account-id>.r2.cloudflarestorage.com`) |
| `REPLICATE_API_TOKEN` | Replicate API token for image generation |
| `STICKER_MULE_API_KEY` | Sticker Mule API key for print fulfillment |
| `GEMINI_API_KEY` | Google Gemini API key for LLM scoring and prompt generation — use `OPENAI_API_KEY` instead if you prefer OpenAI |

### GitHub Variables

Add these under **Settings → Secrets and variables → Actions → Variables** (non-sensitive operational caps):

| Variable | Default | Description |
|---|---|---|
| `MAX_TRENDS_PER_CYCLE` | `5` | Max trends processed per 2-hour cycle |
| `MAX_IMAGES_PER_DAY` | `50` | Max images generated per day |
| `MAX_ACTIVE_LISTINGS` | `300` | Max active Etsy listings |
| `REPLICATE_MODEL_VERSION` | *(empty)* | Pinned Replicate model version hash — leave empty to use latest |

### Workflows

| Workflow | Schedule | What it does |
|---|---|---|
| `trend-monitor.yml` | Every 2 hours | Fetches Reddit + Google Trends, scores with LLM, generates sticker images via Replicate, uploads to R2 |
| `daily-pricing.yml` | Daily at 06:00 UTC | Re-prices active Etsy listings based on performance |
| `daily-analytics.yml` | Daily at 08:00 UTC | Syncs Etsy analytics to Supabase, runs DB backup to R2 |

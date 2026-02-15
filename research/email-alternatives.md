# Email Service Alternatives to SendGrid

**Decision date:** 2026-02-14
**Decision:** Switch to **Resend** (see bottom)

## Why we moved away from SendGrid

SendGrid's free plan is only available for the first 2 months (trial). After that, the cheapest paid plan is ~$20/month. For a low-volume alert system (< 100 emails/month), this is not cost-effective.

---

## Free Tier Comparison

| Service | Free Emails | Daily Limit | Permanent Free? | Notes |
|---|---|---|---|---|
| **Resend** | 3,000/mo | 100/day | Yes | Developer-first, modern SDK |
| **Brevo** (fmr. Sendinblue) | ~9,000/mo | 300/day | Yes | Best free volume; EU-based |
| **Mailjet** | 6,000/mo | 200/day | Yes | EU-based, GDPR-friendly |
| **MailerSend** | 3,000/mo | — | Yes | Clean API |
| **Maileroo** | 3,000/mo | — | Yes | Newer service |
| **AWS SES** | Pay-as-you-go | — | Yes | $0.10/1,000 emails; requires AWS |
| **Postmark** | 100/mo | — | Yes | Best deliverability; very limited |
| **SendGrid** | Trial only | — | No | Free for ~2 months then $20+/mo |
| **Mailgun** | Limited trial | — | No | Free tier removed |

---

## Why Resend

1. **Developer-first API** — clean Python SDK (`resend` on PyPI), minimal boilerplate
2. **3,000 emails/month free permanently** — more than enough for operational alerts
3. **100 emails/day** — fits our use case (daily summary + occasional critical alerts)
4. **Simple auth** — single API key, no complex SMTP config
5. **Good deliverability** — DKIM/SPF setup guide is clear

### Usage in this project

We only send ~30–60 emails/month:
- 1 daily summary email/day = ~30/month
- Critical/warning alerts (OAuth failures, rate limits, budget warnings) = ~10–20/month

The 3,000/month free tier has 50–100x headroom.

---

## Migration from SendGrid

- Replace `sendgrid==6.11.0` → `resend==2.x` in `requirements.txt`
- Replace `SENDGRID_API_KEY` → `RESEND_API_KEY` in env vars and GitHub Actions secrets
- Update `src/config.py` `NotificationConfig` field
- Update `src/monitoring/alerter.py` to use `resend.Emails.send()`

### Resend Python SDK usage

```python
import resend

resend.api_key = "re_..."

resend.Emails.send({
    "from": "noreply@sticker-trendz.com",
    "to": ["alerts@example.com"],
    "subject": "Subject here",
    "text": "Plain text body",
})
```

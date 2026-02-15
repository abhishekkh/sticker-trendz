"""
Etsy SEO copy generator for Sticker Trendz.

Uses GPT-4o-mini to generate SEO-optimized listing titles (<=140 chars),
descriptions (following the spec template), and tags (exactly 13 per listing).
All generated text is checked against blocklists before use.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.config import load_config
from src.moderation.blocklist import check_trademark

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 140
REQUIRED_TAG_COUNT = 13

# Evergreen sticker tags always included
EVERGREEN_TAGS = [
    "vinyl sticker",
    "laptop sticker",
    "waterproof decal",
    "water bottle sticker",
]

# Audience targeting tags
AUDIENCE_TAGS = [
    "funny sticker",
    "meme sticker",
    "trendy sticker",
]

DESCRIPTION_TEMPLATE = """{ai_description}

--- What You Get ---
- 1x premium vinyl sticker ({size})
- Waterproof & UV-resistant
- Perfect for laptops, water bottles, notebooks, and more

--- Size & Material ---
- Size: {size} ({dimensions})
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
Questions? Message us! We respond within 24 hours."""

SIZE_MAP = {
    "3in": ("3 inch", "3\" x 3\""),
    "4in": ("4 inch", "4\" x 4\""),
}

SYSTEM_PROMPT = (
    "You are an Etsy SEO expert for a trending sticker shop. "
    "Generate listing copy that ranks well in Etsy search."
)

TITLE_PROMPT_TEMPLATE = """Generate an Etsy listing title for this sticker.

Topic: {topic}
Style: Die-cut vinyl sticker

Requirements:
- Maximum 140 characters
- Format: '{{Trend Topic}} Sticker - {{Style}} Vinyl Decal - Laptop Water Bottle Sticker - Trending {{Category}}'
- Include relevant keywords for Etsy search
- Do NOT include trademark or brand names

Return a JSON object with a "title" field."""

TAGS_PROMPT_TEMPLATE = """Generate exactly 13 Etsy listing tags for this sticker.

Topic: {topic}
Keywords: {keywords}

Requirements:
- 5-7 trend-specific tags related to the topic
- 3-4 evergreen sticker tags (like 'vinyl sticker', 'laptop sticker', 'waterproof decal')
- 2-3 audience/style tags (like 'funny sticker', 'meme sticker')
- Always include 'free shipping' as one tag
- Each tag must be 1-3 words
- No trademark or brand names
- Return exactly 13 tags

Return a JSON object with a "tags" array of exactly 13 strings."""


class SEOGenerator:
    """
    Generates SEO-optimized Etsy listing copy using GPT-4o-mini.
    """

    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        model: Optional[str] = None,
        openai_client: Optional[Any] = None,
    ) -> None:
        self._client = openai_client

        if not self._client:
            cfg = load_config(require_all=False)
            api_key = openai_api_key or cfg.openai.api_key
            self._model = model or cfg.openai.seo_model
            try:
                from openai import OpenAI
                base_url = cfg.openai.base_url or None
                self._client = OpenAI(api_key=api_key, base_url=base_url)
            except Exception as exc:
                logger.error("Failed to initialize OpenAI client: %s", exc)
        else:
            self._model = model or "gemini-2.5-flash"

    def generate_title(self, trend_topic: str, style: str = "vinyl sticker") -> str:
        """
        Generate an SEO-optimized Etsy listing title.

        Args:
            trend_topic: The trend topic for the sticker.
            style: Sticker style descriptor.

        Returns:
            Title string, guaranteed <= 140 characters.
        """
        if self._client:
            try:
                prompt = TITLE_PROMPT_TEMPLATE.format(topic=trend_topic)
                response = self._client.chat.completions.create(
                    model=self._model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.5,
                )
                raw = response.choices[0].message.content or ""
                data = json.loads(raw)
                title = str(data.get("title", ""))

                # Validate
                if title and len(title) <= MAX_TITLE_LENGTH:
                    is_blocked, _ = check_trademark(title)
                    if not is_blocked:
                        return title
                    logger.warning("Generated title contains trademark, using fallback")
            except Exception as exc:
                logger.error("Title generation failed: %s", exc)

        # Fallback template
        title = f"{trend_topic} Sticker - {style.title()} Decal - Laptop Water Bottle Sticker"
        return title[:MAX_TITLE_LENGTH]

    def generate_tags(
        self, trend_topic: str, keywords: Optional[List[str]] = None
    ) -> List[str]:
        """
        Generate exactly 13 Etsy listing tags.

        Mix: 5-7 trend keywords + 3-4 evergreen + 2-3 audience + 'free shipping'

        Args:
            trend_topic: The trend topic.
            keywords: Extracted keywords from the trend.

        Returns:
            List of exactly 13 tag strings.
        """
        tags: List[str] = []

        if self._client:
            try:
                kw_str = ", ".join(keywords[:10]) if keywords else trend_topic
                prompt = TAGS_PROMPT_TEMPLATE.format(
                    topic=trend_topic, keywords=kw_str,
                )
                response = self._client.chat.completions.create(
                    model=self._model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.5,
                )
                raw = response.choices[0].message.content or ""
                data = json.loads(raw)
                ai_tags = data.get("tags", [])
                if isinstance(ai_tags, list):
                    tags = [str(t).lower().strip() for t in ai_tags if t]
            except Exception as exc:
                logger.error("Tag generation failed: %s", exc)

        # Filter out trademarked tags
        tags = [t for t in tags if not check_trademark(t)[0]]

        # Ensure 'free shipping' is present
        if "free shipping" not in tags:
            tags.append("free shipping")

        # Ensure we have enough evergreen tags
        for et in EVERGREEN_TAGS:
            if et not in tags and len(tags) < REQUIRED_TAG_COUNT:
                tags.append(et)

        # Ensure we have audience tags
        for at in AUDIENCE_TAGS:
            if at not in tags and len(tags) < REQUIRED_TAG_COUNT:
                tags.append(at)

        # Fill remaining with trend keywords
        if keywords:
            for kw in keywords:
                if kw.lower() not in tags and len(tags) < REQUIRED_TAG_COUNT:
                    is_blocked, _ = check_trademark(kw)
                    if not is_blocked:
                        tags.append(kw.lower())

        # Pad with generic sticker tags if still short
        padding_tags = [
            "sticker", "decal", "die cut sticker", "cute sticker",
            "trending", "gift idea", "sticker art",
        ]
        for pt in padding_tags:
            if pt not in tags and len(tags) < REQUIRED_TAG_COUNT:
                tags.append(pt)

        # Trim to exactly 13
        tags = tags[:REQUIRED_TAG_COUNT]

        logger.info("Generated %d tags for '%s'", len(tags), trend_topic[:50])
        return tags

    def generate_description(
        self,
        trend_topic: str,
        size: str = "3in",
        ai_description: str = "",
    ) -> str:
        """
        Generate the full listing description following the spec template.

        Args:
            trend_topic: Trend topic for context.
            size: Sticker size ('3in' or '4in').
            ai_description: AI-generated design description.

        Returns:
            Full listing description string.
        """
        if not ai_description and self._client:
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"Write a 1-2 sentence engaging description for a "
                                f"die-cut vinyl sticker inspired by '{trend_topic}'. "
                                f"Do not mention brand names or trademarks."
                            ),
                        },
                    ],
                    temperature=0.7,
                )
                ai_description = response.choices[0].message.content or ""
            except Exception as exc:
                logger.error("Description generation failed: %s", exc)

        if not ai_description:
            ai_description = (
                f"Express your personality with this trendy {trend_topic} inspired sticker! "
                f"Perfect for decorating your laptop, water bottle, or notebook."
            )

        size_label, dimensions = SIZE_MAP.get(size, ("3 inch", '3" x 3"'))

        return DESCRIPTION_TEMPLATE.format(
            ai_description=ai_description,
            size=size_label,
            dimensions=dimensions,
        )

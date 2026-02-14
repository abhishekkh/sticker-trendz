"""
Image prompt generator for Sticker Trendz.

Uses GPT-4o-mini to generate 3 sticker-optimized image prompts per trend.
Each prompt follows the die-cut vinyl sticker template from the spec.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.config import load_config
from src.resilience import retry, RetryExhaustedError

logger = logging.getLogger(__name__)

PROMPTS_PER_TREND = 3

STYLE_DIRECTIVES = (
    "die-cut vinyl sticker design, bold black outlines, vibrant colors, "
    "white background, cartoon illustration style, simple and clean, "
    "suitable for laptop or water bottle sticker. "
    "No text, no words, no letters, no brand names, no logos. "
    "High contrast, fun and trendy aesthetic."
)

SYSTEM_PROMPT = (
    "You are a creative director for a trending sticker business. "
    "Generate unique image prompts for die-cut vinyl stickers that capture "
    "the essence of trending topics. Each prompt must be visually distinct "
    "and suitable for AI image generation."
)

USER_PROMPT_TEMPLATE = """Generate exactly 3 image prompts for die-cut vinyl stickers inspired by this trend.

Trend: {topic}
Context: {context}

Requirements for each prompt:
- Must describe a die-cut vinyl sticker design
- Bold black outlines, vibrant colors, white background
- Cartoon illustration style, simple and clean
- Suitable for laptop or water bottle sticker
- NO text, words, letters, brand names, logos, or recognizable characters
- Each prompt should be a unique visual interpretation
- High contrast, fun and trendy aesthetic

Return a JSON object with a "prompts" array containing exactly 3 prompt strings.
Each prompt should be 1-2 sentences describing the visual design."""


class PromptGenerator:
    """
    Generates sticker image prompts using GPT-4o-mini.

    Produces exactly 3 image prompts per trend, each including the
    required style directives for die-cut vinyl sticker format.
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
            self._model = model or cfg.openai.prompt_model

            try:
                from openai import OpenAI
                base_url = cfg.openai.base_url or None
                self._client = OpenAI(api_key=api_key, base_url=base_url)
            except Exception as exc:
                logger.error("Failed to initialize OpenAI client: %s", exc)
                self._client = None
        else:
            self._model = model or "gemini-2.0-flash"

    def generate_prompts(
        self,
        topic: str,
        context: str = "",
        num_prompts: int = PROMPTS_PER_TREND,
    ) -> List[str]:
        """
        Generate image prompts for a trend topic.

        Args:
            topic: The trend topic to create prompts for.
            context: Additional context about the trend.
            num_prompts: Number of prompts to generate (default 3).

        Returns:
            List of prompt strings (each including style directives).

        Raises:
            RuntimeError: If the OpenAI client is not available.
        """
        if not self._client:
            raise RuntimeError("OpenAI client not available for prompt generation")

        user_prompt = USER_PROMPT_TEMPLATE.format(
            topic=topic,
            context=context or "No additional context.",
        )

        for attempt in range(3):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.8,
                )
                raw = response.choices[0].message.content or ""
                prompts = self._parse_prompts(raw, num_prompts)

                if len(prompts) >= num_prompts:
                    # Append style directives to each prompt
                    full_prompts = [
                        f"A {STYLE_DIRECTIVES} {p}" for p in prompts[:num_prompts]
                    ]
                    logger.info(
                        "Generated %d prompts for trend '%s'",
                        len(full_prompts), topic[:50],
                    )
                    return full_prompts

                logger.warning(
                    "Got %d prompts instead of %d for '%s' (attempt %d), retrying",
                    len(prompts), num_prompts, topic[:50], attempt + 1,
                )

            except Exception as exc:
                logger.error(
                    "Prompt generation failed for '%s' (attempt %d): %s",
                    topic[:50], attempt + 1, exc,
                )

        # Fallback: generate simple template-based prompts
        logger.warning("Using fallback template prompts for '%s'", topic[:50])
        return self._fallback_prompts(topic, num_prompts)

    @staticmethod
    def _parse_prompts(raw_json: str, expected_count: int) -> List[str]:
        """Parse the JSON response and extract prompt strings."""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            return []

        if isinstance(data, dict):
            # Try common keys
            for key in ["prompts", "prompt", "images", "designs"]:
                if key in data and isinstance(data[key], list):
                    return [str(p) for p in data[key] if p]

            # If the dict has numbered keys
            prompts = []
            for i in range(1, expected_count + 1):
                for key in [str(i), f"prompt_{i}", f"prompt{i}"]:
                    if key in data:
                        prompts.append(str(data[key]))
                        break
            if prompts:
                return prompts

        return []

    @staticmethod
    def _fallback_prompts(topic: str, count: int) -> List[str]:
        """Generate simple fallback prompts when the AI call fails."""
        variations = [
            f"A {STYLE_DIRECTIVES} Cute cartoon interpretation of {topic}, centered composition.",
            f"A {STYLE_DIRECTIVES} Fun and playful illustration inspired by {topic}, simple design.",
            f"A {STYLE_DIRECTIVES} Trendy pop-art style illustration related to {topic}, eye-catching design.",
        ]
        return variations[:count]

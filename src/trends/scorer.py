"""
GPT-4o-mini trend scorer for Sticker Trendz.

Scores trends on 4 dimensions (velocity, commercial viability, content
safety, uniqueness) plus an overall composite score. Uses structured
output (response_format=json_object) for reliable JSON parsing.
Only trends scoring 7.0+ overall qualify for sticker generation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.config import load_config
from src.resilience import retry, RetryExhaustedError

logger = logging.getLogger(__name__)

OVERALL_THRESHOLD = 7.0
MAX_JSON_RETRIES = 2

SYSTEM_PROMPT = (
    "You are a trend analyst for a sticker business. "
    "Score this trend on four dimensions."
)

USER_PROMPT_TEMPLATE = """Score this trend for sticker commercial viability.

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
- Score 3-4: "Federal Reserve rate decision" (not stickerable, no youth appeal)"""


@dataclass
class TrendScore:
    """Structured score result for a trend."""

    velocity: int
    commercial: int
    safety: int
    uniqueness: int
    overall: float
    reasoning: str

    def qualifies(self, threshold: float = OVERALL_THRESHOLD) -> bool:
        """Return True if the overall score meets the qualification threshold."""
        return self.overall >= threshold

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a flat dict for database storage."""
        return {
            "score_velocity": self.velocity,
            "score_commercial": self.commercial,
            "score_safety": self.safety,
            "score_uniqueness": self.uniqueness,
            "score_overall": self.overall,
            "reasoning": self.reasoning,
        }


def _validate_score_field(value: Any, field: str, is_float: bool = False) -> Any:
    """Validate and clamp a score field to the expected range."""
    try:
        if is_float:
            v = float(value)
            return max(1.0, min(10.0, v))
        else:
            v = int(value)
            return max(1, min(10, v))
    except (TypeError, ValueError):
        logger.warning("Invalid score field '%s': %s, defaulting to 1", field, value)
        return 1.0 if is_float else 1


def parse_score_response(raw_json: str) -> TrendScore:
    """
    Parse a GPT-4o-mini JSON response into a TrendScore.

    Args:
        raw_json: Raw JSON string from the model.

    Returns:
        TrendScore dataclass.

    Raises:
        ValueError: If the JSON is malformed or missing required fields.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON response: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    required_fields = {"velocity", "commercial", "safety", "uniqueness", "overall", "reasoning"}
    missing = required_fields - set(data.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    return TrendScore(
        velocity=_validate_score_field(data["velocity"], "velocity"),
        commercial=_validate_score_field(data["commercial"], "commercial"),
        safety=_validate_score_field(data["safety"], "safety"),
        uniqueness=_validate_score_field(data["uniqueness"], "uniqueness"),
        overall=_validate_score_field(data["overall"], "overall", is_float=True),
        reasoning=str(data.get("reasoning", "")),
    )


class TrendScorer:
    """
    Scores trends using GPT-4o-mini with structured output.

    Retries on malformed JSON (up to MAX_JSON_RETRIES additional attempts).
    """

    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        model: Optional[str] = None,
        openai_client: Optional[Any] = None,
    ) -> None:
        """
        Args:
            openai_api_key: OpenAI API key. Falls back to config.
            model: Model name. Defaults to gpt-4o-mini.
            openai_client: Pre-built OpenAI client (for testing).
        """
        self._client = openai_client

        if not self._client:
            cfg = load_config(require_all=False)
            api_key = openai_api_key or cfg.openai.api_key
            self._model = model or cfg.openai.scoring_model

            try:
                from openai import OpenAI
                base_url = cfg.openai.base_url or None
                self._client = OpenAI(api_key=api_key, base_url=base_url)
                logger.info("LLM client initialized for scoring")
            except Exception as exc:
                logger.error("Failed to initialize LLM client: %s", exc)
                self._client = None
        else:
            self._model = model or "gemini-2.0-flash"

    def score_trend(
        self,
        topic: str,
        sample_posts: str = "",
        source_list: str = "",
    ) -> Optional[TrendScore]:
        """
        Score a single trend using GPT-4o-mini.

        Args:
            topic: The trend topic string.
            sample_posts: Context about the trend (e.g. post titles).
            source_list: Comma-separated list of sources.

        Returns:
            TrendScore if scoring succeeds, None if all retries fail.
        """
        if not self._client:
            logger.error("OpenAI client not available, cannot score trend")
            return None

        user_prompt = USER_PROMPT_TEMPLATE.format(
            topic=topic,
            sample_posts=sample_posts or "No additional context available.",
            source_list=source_list or "unknown",
        )

        for attempt in range(1, MAX_JSON_RETRIES + 2):  # 1 initial + 2 retries
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                )
                raw_content = response.choices[0].message.content or ""
                score = parse_score_response(raw_content)
                logger.info(
                    "Scored trend '%s': overall=%.1f (%s)",
                    topic[:50], score.overall,
                    "QUALIFIES" if score.qualifies() else "below threshold",
                )
                return score

            except ValueError as exc:
                logger.warning(
                    "JSON parse failed for trend '%s' (attempt %d/%d): %s",
                    topic[:50], attempt, MAX_JSON_RETRIES + 1, exc,
                )
                if attempt > MAX_JSON_RETRIES:
                    logger.error(
                        "All JSON retries exhausted for trend '%s'", topic[:50]
                    )
                    return None

            except Exception as exc:
                logger.error(
                    "OpenAI API error scoring trend '%s' (attempt %d): %s",
                    topic[:50], attempt, exc,
                )
                if attempt > MAX_JSON_RETRIES:
                    return None

        return None

    def score_and_filter(
        self,
        trends: List[Dict[str, Any]],
        threshold: float = OVERALL_THRESHOLD,
    ) -> List[Dict[str, Any]]:
        """
        Score a list of trends and return only those meeting the threshold.

        Each qualifying trend dict gets score fields added directly.

        Args:
            trends: List of trend dicts with 'topic', 'keywords', 'sources'.
            threshold: Minimum overall score to qualify (default 7.0).

        Returns:
            List of qualifying trend dicts with score fields populated.
        """
        qualified: List[Dict[str, Any]] = []

        for trend in trends:
            topic = trend.get("topic", "")
            sample = json.dumps(trend.get("source_data", {}))[:500]
            sources = ", ".join(trend.get("sources", [trend.get("source", "")]))

            score = self.score_trend(topic, sample_posts=sample, source_list=sources)

            if score is None:
                logger.warning("Failed to score trend '%s', skipping", topic[:50])
                continue

            if score.qualifies(threshold):
                trend.update(score.to_dict())
                qualified.append(trend)
            else:
                logger.info(
                    "Trend '%s' below threshold (%.1f < %.1f), filtered out",
                    topic[:50], score.overall, threshold,
                )

        logger.info(
            "Scoring complete: %d/%d trends qualified (threshold=%.1f)",
            len(qualified), len(trends), threshold,
        )
        return qualified

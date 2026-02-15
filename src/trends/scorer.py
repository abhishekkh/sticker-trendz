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
    "Score trends on four dimensions."
)

BATCH_PROMPT_TEMPLATE = """Score each trend below for sticker commercial viability.

{trends_block}

For each trend return a JSON object with these exact fields:
- index (integer): the trend number from the list
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

Return a JSON object with a single key "scores" containing an array of score objects."""


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


def _parse_single_score(data: Dict[str, Any]) -> TrendScore:
    """Parse a single score dict into a TrendScore. Raises ValueError on bad data."""
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


def parse_score_response(raw_json: str) -> TrendScore:
    """
    Parse a single-trend JSON response into a TrendScore.

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

    return _parse_single_score(data)


def parse_batch_response(raw_json: str, expected_count: int) -> Dict[int, TrendScore]:
    """
    Parse a batch JSON response into a dict of index -> TrendScore.

    Args:
        raw_json: Raw JSON string with a "scores" array.
        expected_count: How many trends were sent (for logging).

    Returns:
        Dict mapping 0-based trend index to TrendScore.

    Raises:
        ValueError: If the JSON is malformed or the scores array is missing.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON response: {exc}") from exc

    if not isinstance(data, dict) or "scores" not in data:
        raise ValueError(f"Expected JSON object with 'scores' key, got: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")

    scores_list = data["scores"]
    if not isinstance(scores_list, list):
        raise ValueError(f"'scores' must be an array, got {type(scores_list).__name__}")

    result: Dict[int, TrendScore] = {}
    for item in scores_list:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if idx is None:
            continue
        try:
            result[int(idx) - 1] = _parse_single_score(item)  # convert 1-based to 0-based
        except (ValueError, TypeError) as exc:
            logger.warning("Failed to parse score for index %s: %s", idx, exc)

    if len(result) < expected_count:
        logger.warning(
            "Batch response returned %d scores, expected %d",
            len(result), expected_count,
        )

    return result


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
            self._model = model or "gemini-2.5-flash"

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
        Score a list of trends in a single batch API call and return qualifying ones.

        Sends all trends in one prompt to minimize API quota usage.
        Each qualifying trend dict gets score fields added directly.

        Args:
            trends: List of trend dicts with 'topic', 'keywords', 'sources'.
            threshold: Minimum overall score to qualify (default 7.0).

        Returns:
            List of qualifying trend dicts with score fields populated.
        """
        if not trends:
            return []

        if not self._client:
            logger.error("LLM client not available, cannot score trends")
            return []

        # Build numbered trend block for the prompt
        lines = []
        for i, trend in enumerate(trends, start=1):
            topic = trend.get("topic", "")
            source = trend.get("source", "unknown")
            lines.append(f"{i}. [{source}] {topic}")
        trends_block = "\n".join(lines)

        user_prompt = BATCH_PROMPT_TEMPLATE.format(trends_block=trends_block)

        for attempt in range(1, MAX_JSON_RETRIES + 2):
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
                scores_by_index = parse_batch_response(raw_content, expected_count=len(trends))
                break

            except ValueError as exc:
                logger.warning("Batch JSON parse failed (attempt %d/%d): %s", attempt, MAX_JSON_RETRIES + 1, exc)
                if attempt > MAX_JSON_RETRIES:
                    logger.error("All batch JSON retries exhausted, skipping scoring")
                    return []

            except Exception as exc:
                logger.error("LLM API error during batch scoring (attempt %d): %s", attempt, exc)
                if attempt > MAX_JSON_RETRIES:
                    return []
        else:
            return []

        qualified: List[Dict[str, Any]] = []
        for i, trend in enumerate(trends):
            topic = trend.get("topic", "")
            score = scores_by_index.get(i)

            if score is None:
                logger.warning("No score returned for trend '%s', skipping", topic[:50])
                continue

            logger.info(
                "Scored trend '%s': overall=%.1f (%s)",
                topic[:50], score.overall,
                "QUALIFIES" if score.qualifies(threshold) else "below threshold",
            )

            if score.qualifies(threshold):
                trend.update(score.to_dict())
                qualified.append(trend)

        logger.info(
            "Scoring complete: %d/%d trends qualified (threshold=%.1f)",
            len(qualified), len(trends), threshold,
        )
        return qualified

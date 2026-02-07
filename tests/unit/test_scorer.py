"""Tests for src/trends/scorer.py -- GPT-4o-mini trend scoring module."""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from src.trends.scorer import (
    TrendScore,
    TrendScorer,
    parse_score_response,
    OVERALL_THRESHOLD,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    MAX_JSON_RETRIES,
)


# --- Valid score payloads for reuse ---

VALID_SCORE_QUALIFYING = json.dumps({
    "velocity": 8,
    "commercial": 8,
    "safety": 9,
    "uniqueness": 7,
    "overall": 7.5,
    "reasoning": "Viral and sticker-friendly topic.",
})

VALID_SCORE_BELOW_THRESHOLD = json.dumps({
    "velocity": 5,
    "commercial": 6,
    "safety": 8,
    "uniqueness": 4,
    "overall": 5.5,
    "reasoning": "Moderate appeal, somewhat overdone.",
})

VALID_SCORE_EXACT_7 = json.dumps({
    "velocity": 7,
    "commercial": 7,
    "safety": 7,
    "uniqueness": 7,
    "overall": 7.0,
    "reasoning": "Meets threshold.",
})


class TestParseScoreResponse:
    """Test that valid JSON response is correctly parsed into score object with all fields."""

    def test_valid_json_parsed_into_score_object(self):
        """Valid JSON response is correctly parsed into TrendScore with all fields."""
        raw = VALID_SCORE_QUALIFYING
        score = parse_score_response(raw)

        assert isinstance(score, TrendScore)
        assert score.velocity == 8
        assert score.commercial == 8
        assert score.safety == 9
        assert score.uniqueness == 7
        assert score.overall == 7.5
        assert score.reasoning == "Viral and sticker-friendly topic."

    def test_malformed_json_raises_value_error(self):
        """Malformed JSON raises ValueError (for retry handling)."""
        with pytest.raises(ValueError, match="Malformed JSON"):
            parse_score_response("{ invalid json }")

    def test_non_object_json_raises_value_error(self):
        """Non-object JSON (e.g. array or string) raises ValueError."""
        with pytest.raises(ValueError, match="Expected JSON object"):
            parse_score_response("[1, 2, 3]")

    def test_missing_required_fields_raises_value_error(self):
        """Missing required fields raises ValueError."""
        raw = json.dumps({"velocity": 8, "commercial": 8})  # missing others
        with pytest.raises(ValueError, match="Missing required fields"):
            parse_score_response(raw)

    def test_score_field_validation_integers_clamped_1_to_10(self):
        """Integer score fields are validated and clamped to 1-10."""
        raw = json.dumps({
            "velocity": 0,
            "commercial": 15,
            "safety": 5,
            "uniqueness": -1,
            "overall": 7.0,
            "reasoning": "Test",
        })
        score = parse_score_response(raw)
        assert score.velocity == 1
        assert score.commercial == 10
        assert score.safety == 5
        assert score.uniqueness == 1

    def test_score_field_validation_overall_float_clamped_1_to_10(self):
        """Overall (float) is validated and clamped to 1.0-10.0."""
        raw = json.dumps({
            "velocity": 5,
            "commercial": 5,
            "safety": 5,
            "uniqueness": 5,
            "overall": 0.5,
            "reasoning": "Low",
        })
        score = parse_score_response(raw)
        assert score.overall == 1.0

        raw_high = json.dumps({
            "velocity": 5,
            "commercial": 5,
            "safety": 5,
            "uniqueness": 5,
            "overall": 12.0,
            "reasoning": "High",
        })
        score_high = parse_score_response(raw_high)
        assert score_high.overall == 10.0

    def test_reasoning_empty_string_preserved(self):
        """Reasoning can be empty string and is preserved."""
        raw = json.dumps({
            "velocity": 5,
            "commercial": 5,
            "safety": 5,
            "uniqueness": 5,
            "overall": 7.0,
            "reasoning": "",
        })
        score = parse_score_response(raw)
        assert score.reasoning == ""


class TestTrendScoreQualifies:
    """Test that trends scoring >= 7.0 qualify and < 7.0 are filtered out."""

    def test_overall_7_or_above_qualifies(self):
        """Trends with overall >= 7.0 are returned as qualifying."""
        score = TrendScore(
            velocity=8, commercial=8, safety=9, uniqueness=7,
            overall=7.0, reasoning="Meets threshold.",
        )
        assert score.qualifies() is True
        assert score.qualifies(threshold=7.0) is True

        score_above = TrendScore(
            velocity=8, commercial=8, safety=9, uniqueness=7,
            overall=7.5, reasoning="Above.",
        )
        assert score_above.qualifies() is True

    def test_overall_below_7_filtered_out(self):
        """Trends with overall < 7.0 are filtered out."""
        score = TrendScore(
            velocity=5, commercial=6, safety=8, uniqueness=4,
            overall=6.9, reasoning="Below threshold.",
        )
        assert score.qualifies() is False
        assert score.qualifies(threshold=7.0) is False

    def test_custom_threshold(self):
        """qualifies() respects custom threshold."""
        score = TrendScore(
            velocity=6, commercial=6, safety=6, uniqueness=6,
            overall=6.5, reasoning="Mid.",
        )
        assert score.qualifies(threshold=6.0) is True
        assert score.qualifies(threshold=7.0) is False


class TestTrendScoreToDict:
    """TrendScore.to_dict() for database storage."""

    def test_to_dict_returns_expected_keys(self):
        """to_dict returns flat dict with score_* and reasoning."""
        score = TrendScore(
            velocity=8, commercial=7, safety=9, uniqueness=6,
            overall=7.5, reasoning="Good trend.",
        )
        d = score.to_dict()
        assert d["score_velocity"] == 8
        assert d["score_commercial"] == 7
        assert d["score_safety"] == 9
        assert d["score_uniqueness"] == 6
        assert d["score_overall"] == 7.5
        assert d["reasoning"] == "Good trend."


class TestTrendScorerWithMockedOpenAI:
    """TrendScorer.score_trend using mocked OpenAI client (no real API calls)."""

    def _make_mock_client(self, content: str):
        """Build a mock OpenAI client that returns the given message content."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = content

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        return mock_client

    def test_valid_response_parsed_and_returned(self):
        """Valid JSON from API is parsed and returned as TrendScore."""
        mock_client = self._make_mock_client(VALID_SCORE_QUALIFYING)
        scorer = TrendScorer(openai_client=mock_client)

        result = scorer.score_trend("baby hippo", sample_posts="Cute posts.", source_list="reddit")

        assert result is not None
        assert result.overall == 7.5
        assert result.qualifies() is True

    def test_trend_scoring_7_or_above_returned_as_qualifying(self):
        """Trends scoring >= 7.0 overall are returned as qualifying."""
        mock_client = self._make_mock_client(VALID_SCORE_EXACT_7)
        scorer = TrendScorer(openai_client=mock_client)

        result = scorer.score_trend("trend topic")

        assert result is not None
        assert result.overall == 7.0
        assert result.qualifies() is True

    def test_trend_scoring_below_7_returned_but_qualifies_false(self):
        """Trends scoring < 7.0 are still returned by score_trend; filtering is in score_and_filter."""
        mock_client = self._make_mock_client(VALID_SCORE_BELOW_THRESHOLD)
        scorer = TrendScorer(openai_client=mock_client)

        result = scorer.score_trend("boring topic")

        assert result is not None
        assert result.overall == 5.5
        assert result.qualifies() is False

    def test_malformed_json_triggers_retry_then_succeeds(self):
        """Malformed JSON on first attempt triggers retry; valid JSON on second succeeds."""
        mock_response_fail = MagicMock()
        mock_response_fail.choices = [MagicMock()]
        mock_response_fail.choices[0].message.content = "not valid json"

        mock_response_ok = MagicMock()
        mock_response_ok.choices = [MagicMock()]
        mock_response_ok.choices[0].message.content = VALID_SCORE_QUALIFYING

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            mock_response_fail,
            mock_response_ok,
        ]

        scorer = TrendScorer(openai_client=mock_client)
        result = scorer.score_trend("retry topic")

        assert result is not None
        assert result.overall == 7.5
        assert mock_client.chat.completions.create.call_count == 2

    def test_three_failed_json_parses_returns_none_and_logs_error(self, caplog):
        """After 3 failed JSON parses the trend is skipped and error is logged."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "{ broken }"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        scorer = TrendScorer(openai_client=mock_client)
        with caplog.at_level(logging.ERROR):
            result = scorer.score_trend("bad json topic")

        assert result is None
        assert mock_client.chat.completions.create.call_count == 3  # 1 initial + 2 retries
        assert "All JSON retries exhausted" in caplog.text or "retries exhausted" in caplog.text.lower()

    def test_prompt_includes_system_message_calibration_and_trend_data(self):
        """Prompt includes system message, calibration examples, and trend data."""
        mock_client = self._make_mock_client(VALID_SCORE_QUALIFYING)
        scorer = TrendScorer(openai_client=mock_client)

        scorer.score_trend(
            topic="baby hippo",
            sample_posts="Some post titles.",
            source_list="reddit, google_trends",
        )

        call_kw = mock_client.chat.completions.create.call_args[1]
        messages = call_kw["messages"]

        assert len(messages) >= 2
        system_content = messages[0]["content"]
        user_content = messages[1]["content"]

        assert "trend analyst" in system_content or "Score" in system_content
        assert "baby hippo" in user_content
        assert "Some post titles" in user_content or "Context" in user_content
        assert "reddit" in user_content or "google_trends" in user_content
        assert "Moo Deng" in user_content or "Taylor Swift" in user_content or "Federal Reserve" in user_content
        assert "velocity" in user_content and "commercial" in user_content
        assert "1-10" in user_content or "1.0-10.0" in user_content

    def test_response_format_json_object_passed_to_api(self):
        """API is called with response_format={'type': 'json_object'}."""
        mock_client = self._make_mock_client(VALID_SCORE_QUALIFYING)
        scorer = TrendScorer(openai_client=mock_client)

        scorer.score_trend("topic")

        call_kw = mock_client.chat.completions.create.call_args[1]
        assert call_kw.get("response_format") == {"type": "json_object"}

    def test_no_client_returns_none(self):
        """When OpenAI client is not available, score_trend returns None (no API call)."""
        mock_client = self._make_mock_client(VALID_SCORE_QUALIFYING)
        scorer = TrendScorer(openai_client=mock_client)
        scorer._client = None  # Simulate client init failure
        result = scorer.score_trend("any topic")
        assert result is None
        mock_client.chat.completions.create.assert_not_called()


class TestTrendScorerScoreAndFilter:
    """TrendScorer.score_and_filter: qualifying vs filtered out."""

    def _make_mock_client_returning(self, content: str):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = content
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        return mock_client

    def test_qualifying_trends_included_with_score_fields(self):
        """Trends scoring >= 7.0 are included with score fields populated."""
        mock_client = self._make_mock_client_returning(VALID_SCORE_QUALIFYING)
        scorer = TrendScorer(openai_client=mock_client)

        trends = [
            {"topic": "baby hippo", "keywords": ["hippo"], "sources": ["reddit"], "source_data": {}},
        ]
        result = scorer.score_and_filter(trends, threshold=OVERALL_THRESHOLD)

        assert len(result) == 1
        assert result[0]["topic"] == "baby hippo"
        assert result[0]["score_overall"] == 7.5
        assert result[0]["score_velocity"] == 8
        assert "reasoning" in result[0]

    def test_below_threshold_trends_filtered_out(self):
        """Trends scoring < 7.0 are not included in score_and_filter result."""
        mock_client = self._make_mock_client_returning(VALID_SCORE_BELOW_THRESHOLD)
        scorer = TrendScorer(openai_client=mock_client)

        trends = [
            {"topic": "boring topic", "keywords": ["boring"], "sources": ["reddit"], "source_data": {}},
        ]
        result = scorer.score_and_filter(trends, threshold=OVERALL_THRESHOLD)

        assert len(result) == 0

    def test_mixed_trends_only_qualifying_returned(self):
        """When multiple trends are scored, only those >= threshold are returned."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                content = VALID_SCORE_QUALIFYING
            else:
                content = VALID_SCORE_BELOW_THRESHOLD
            mock_r = MagicMock()
            mock_r.choices = [MagicMock()]
            mock_r.choices[0].message.content = content
            return mock_r

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = side_effect
        scorer = TrendScorer(openai_client=mock_client)

        trends = [
            {"topic": "good", "keywords": [], "sources": ["reddit"], "source_data": {}},
            {"topic": "bad", "keywords": [], "sources": ["reddit"], "source_data": {}},
        ]
        result = scorer.score_and_filter(trends, threshold=7.0)

        assert len(result) == 1
        assert result[0]["topic"] == "good"
        assert result[0]["score_overall"] == 7.5


class TestPromptConstants:
    """Prompt includes all required elements (system message, calibration examples, trend data)."""

    def test_system_prompt_non_empty(self):
        """SYSTEM_PROMPT is non-empty and describes role."""
        assert len(SYSTEM_PROMPT) > 0
        assert "trend" in SYSTEM_PROMPT.lower() or "score" in SYSTEM_PROMPT.lower()

    def test_user_prompt_template_has_placeholders(self):
        """USER_PROMPT_TEMPLATE has topic, sample_posts, source_list placeholders."""
        assert "{topic}" in USER_PROMPT_TEMPLATE
        assert "{sample_posts}" in USER_PROMPT_TEMPLATE
        assert "{source_list}" in USER_PROMPT_TEMPLATE

    def test_user_prompt_includes_calibration_examples(self):
        """User prompt includes calibration examples (Moo Deng, Taylor Swift, Federal Reserve)."""
        assert "Moo Deng" in USER_PROMPT_TEMPLATE or "9-10" in USER_PROMPT_TEMPLATE
        assert "Taylor Swift" in USER_PROMPT_TEMPLATE or "6-7" in USER_PROMPT_TEMPLATE
        assert "Federal Reserve" in USER_PROMPT_TEMPLATE or "3-4" in USER_PROMPT_TEMPLATE

    def test_user_prompt_requests_json_fields(self):
        """User prompt requests velocity, commercial, safety, uniqueness, overall, reasoning."""
        assert "velocity" in USER_PROMPT_TEMPLATE
        assert "commercial" in USER_PROMPT_TEMPLATE
        assert "safety" in USER_PROMPT_TEMPLATE
        assert "uniqueness" in USER_PROMPT_TEMPLATE
        assert "overall" in USER_PROMPT_TEMPLATE
        assert "reasoning" in USER_PROMPT_TEMPLATE

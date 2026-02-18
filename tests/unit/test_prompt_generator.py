"""Tests for src/stickers/prompt_generator.py -- sticker prompt generation module."""

import json
import logging
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from src.stickers.prompt_generator import (
    PromptGenerator,
    PROMPTS_PER_TREND,
    STYLE_DIRECTIVES,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(content: str) -> MagicMock:
    """Build a mock OpenAI client whose first (and only) completion returns *content*."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = content

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


def _valid_prompts_json(prompts: Optional[List[str]] = None) -> str:
    prompts = prompts or [
        "A cute sloth holding a coffee cup, cartoon style.",
        "A kawaii avocado character sunbathing, bold outlines.",
        "A tiny astronaut floating through pastel stars, vibrant colors.",
    ]
    return json.dumps({"prompts": prompts})


# ---------------------------------------------------------------------------
# _parse_prompts
# ---------------------------------------------------------------------------

class TestParsePrompts:
    """PromptGenerator._parse_prompts correctly extracts prompt lists from JSON."""

    def test_valid_prompts_key_returns_list(self):
        raw = json.dumps({"prompts": ["A prompt one.", "Prompt two.", "Prompt three."]})
        result = PromptGenerator._parse_prompts(raw, expected_count=3)
        assert result == ["A prompt one.", "Prompt two.", "Prompt three."]

    def test_alternative_key_prompt_singular(self):
        raw = json.dumps({"prompt": ["One.", "Two.", "Three."]})
        result = PromptGenerator._parse_prompts(raw, expected_count=3)
        assert result == ["One.", "Two.", "Three."]

    def test_alternative_key_images(self):
        raw = json.dumps({"images": ["Img one.", "Img two.", "Img three."]})
        result = PromptGenerator._parse_prompts(raw, expected_count=3)
        assert result == ["Img one.", "Img two.", "Img three."]

    def test_alternative_key_designs(self):
        raw = json.dumps({"designs": ["Des one.", "Des two.", "Des three."]})
        result = PromptGenerator._parse_prompts(raw, expected_count=3)
        assert result == ["Des one.", "Des two.", "Des three."]

    def test_numbered_keys_prompt_1_2_3(self):
        raw = json.dumps({"prompt_1": "First.", "prompt_2": "Second.", "prompt_3": "Third."})
        result = PromptGenerator._parse_prompts(raw, expected_count=3)
        assert len(result) == 3
        assert "First." in result

    def test_bare_numeric_keys(self):
        raw = json.dumps({"1": "First.", "2": "Second.", "3": "Third."})
        result = PromptGenerator._parse_prompts(raw, expected_count=3)
        assert len(result) == 3

    def test_malformed_json_returns_empty_list(self):
        result = PromptGenerator._parse_prompts("{ not valid json }", expected_count=3)
        assert result == []

    def test_json_array_instead_of_object_returns_empty_list(self):
        result = PromptGenerator._parse_prompts('["one", "two"]', expected_count=3)
        assert result == []

    def test_empty_prompts_array_returns_empty_list(self):
        raw = json.dumps({"prompts": []})
        result = PromptGenerator._parse_prompts(raw, expected_count=3)
        assert result == []

    def test_non_string_prompts_are_coerced_to_strings(self):
        raw = json.dumps({"prompts": [123, None, True]})
        result = PromptGenerator._parse_prompts(raw, expected_count=3)
        # None evaluates to falsy so it may be dropped; 123 and True should survive
        assert all(isinstance(p, str) for p in result)

    def test_extra_prompts_beyond_expected_still_returned(self):
        raw = json.dumps({"prompts": ["A.", "B.", "C.", "D.", "E."]})
        result = PromptGenerator._parse_prompts(raw, expected_count=3)
        assert len(result) == 5  # _parse_prompts returns all; slicing happens in generate_prompts


# ---------------------------------------------------------------------------
# _fallback_prompts
# ---------------------------------------------------------------------------

class TestFallbackPrompts:
    """_fallback_prompts returns styled template prompts when the AI call fails."""

    def test_returns_requested_count_of_1(self):
        result = PromptGenerator._fallback_prompts("gaming", count=1)
        assert len(result) == 1

    def test_returns_requested_count_of_3(self):
        result = PromptGenerator._fallback_prompts("gaming", count=3)
        assert len(result) == 3

    def test_all_prompts_include_style_directives(self):
        result = PromptGenerator._fallback_prompts("anime", count=3)
        for prompt in result:
            # STYLE_DIRECTIVES is embedded in every fallback prompt
            assert "die-cut vinyl sticker" in prompt

    def test_topic_appears_in_each_prompt(self):
        result = PromptGenerator._fallback_prompts("cottagecore", count=3)
        for prompt in result:
            assert "cottagecore" in prompt

    def test_topic_wrapped_in_quotes(self):
        """Topic should be quoted (e.g. 'cottagecore') so single words read as concepts."""
        result = PromptGenerator._fallback_prompts("sustainability", count=3)
        for prompt in result:
            assert "'sustainability'" in prompt

    def test_three_prompts_are_distinct(self):
        result = PromptGenerator._fallback_prompts("lofi", count=3)
        assert len(set(result)) == 3

    def test_count_2_returns_only_first_two_variations(self):
        result = PromptGenerator._fallback_prompts("cats", count=2)
        assert len(result) == 2

    def test_audience_or_surface_mentioned(self):
        """At least one fallback prompt mentions the intended audience or surface."""
        result = PromptGenerator._fallback_prompts("vaporwave", count=3)
        combined = " ".join(result).lower()
        assert any(kw in combined for kw in ["gen z", "laptop", "15-30", "youth"])


# ---------------------------------------------------------------------------
# generate_prompts (mocked OpenAI client)
# ---------------------------------------------------------------------------

class TestGeneratePromptsWithMockedClient:
    """generate_prompts orchestrates prompt generation with retries and fallback."""

    def test_valid_response_returns_3_prompts(self):
        mock_client = _make_mock_client(_valid_prompts_json())
        gen = PromptGenerator(openai_client=mock_client)

        result = gen.generate_prompts("aesthetic")

        assert len(result) == 3

    def test_each_prompt_prefixed_with_style_directives(self):
        """STYLE_DIRECTIVES are prepended to every generated prompt."""
        raw_prompts = ["Sloth with coffee.", "Avocado sunbathing.", "Astronaut floating."]
        mock_client = _make_mock_client(_valid_prompts_json(raw_prompts))
        gen = PromptGenerator(openai_client=mock_client)

        result = gen.generate_prompts("wellness")

        for prompt in result:
            assert "die-cut vinyl sticker" in prompt

    def test_style_directives_mention_15_to_30(self):
        """Every returned prompt must carry the audience signal from STYLE_DIRECTIVES."""
        mock_client = _make_mock_client(_valid_prompts_json())
        gen = PromptGenerator(openai_client=mock_client)

        result = gen.generate_prompts("cottagecore")

        for prompt in result:
            assert "15-30" in prompt

    def test_style_directives_mention_laptop_and_water_bottle(self):
        mock_client = _make_mock_client(_valid_prompts_json())
        gen = PromptGenerator(openai_client=mock_client)

        result = gen.generate_prompts("minimalism")

        for prompt in result:
            assert "laptop" in prompt.lower() and "water bottle" in prompt.lower()

    def test_only_num_prompts_returned_when_more_in_response(self):
        extra = _valid_prompts_json(["A.", "B.", "C.", "D.", "E."])
        mock_client = _make_mock_client(extra)
        gen = PromptGenerator(openai_client=mock_client)

        result = gen.generate_prompts("memes", num_prompts=3)

        assert len(result) == 3

    def test_insufficient_count_triggers_retry(self):
        """When only 1 prompt is returned, the generator retries before succeeding."""
        partial_json = json.dumps({"prompts": ["Only one prompt."]})
        full_json = _valid_prompts_json()

        mock_response_partial = MagicMock()
        mock_response_partial.choices = [MagicMock()]
        mock_response_partial.choices[0].message.content = partial_json

        mock_response_full = MagicMock()
        mock_response_full.choices = [MagicMock()]
        mock_response_full.choices[0].message.content = full_json

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            mock_response_partial,
            mock_response_full,
        ]

        gen = PromptGenerator(openai_client=mock_client)
        result = gen.generate_prompts("frogs")

        assert len(result) == 3
        assert mock_client.chat.completions.create.call_count == 2

    def test_all_retries_fail_falls_back_to_template(self, caplog):
        """After 3 failed attempts the generator falls back to template prompts."""
        bad_json = json.dumps({"prompts": []})  # always returns 0 prompts
        mock_client = _make_mock_client(bad_json)
        gen = PromptGenerator(openai_client=mock_client)

        with caplog.at_level(logging.WARNING):
            result = gen.generate_prompts("lofi beats")

        assert len(result) == 3  # fallback still produces 3
        assert mock_client.chat.completions.create.call_count == 3
        assert "fallback" in caplog.text.lower()

    def test_api_exception_falls_back_to_template(self, caplog):
        """Network/API exceptions on all attempts fall back gracefully."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("timeout")
        gen = PromptGenerator(openai_client=mock_client)

        with caplog.at_level(logging.WARNING):
            result = gen.generate_prompts("skater culture")

        assert len(result) == 3
        assert "fallback" in caplog.text.lower()

    def test_no_client_raises_runtime_error(self):
        gen = PromptGenerator(openai_client=MagicMock())
        gen._client = None  # simulate init failure

        with pytest.raises(RuntimeError, match="OpenAI client not available"):
            gen.generate_prompts("vapor wave")

    def test_api_called_with_json_object_response_format(self):
        mock_client = _make_mock_client(_valid_prompts_json())
        gen = PromptGenerator(openai_client=mock_client)

        gen.generate_prompts("dark academia")

        call_kw = mock_client.chat.completions.create.call_args[1]
        assert call_kw.get("response_format") == {"type": "json_object"}

    def test_api_called_with_temperature_0_8(self):
        mock_client = _make_mock_client(_valid_prompts_json())
        gen = PromptGenerator(openai_client=mock_client)

        gen.generate_prompts("goblincore")

        call_kw = mock_client.chat.completions.create.call_args[1]
        assert call_kw.get("temperature") == 0.8

    def test_api_messages_contain_system_and_user_roles(self):
        mock_client = _make_mock_client(_valid_prompts_json())
        gen = PromptGenerator(openai_client=mock_client)

        gen.generate_prompts("Y2K", context="Nostalgia trend.")

        call_kw = mock_client.chat.completions.create.call_args[1]
        messages = call_kw["messages"]
        roles = [m["role"] for m in messages]
        assert "system" in roles
        assert "user" in roles

    def test_topic_appears_in_user_message(self):
        mock_client = _make_mock_client(_valid_prompts_json())
        gen = PromptGenerator(openai_client=mock_client)

        gen.generate_prompts("dark fantasy")

        call_kw = mock_client.chat.completions.create.call_args[1]
        user_msg = next(m["content"] for m in call_kw["messages"] if m["role"] == "user")
        assert "dark fantasy" in user_msg

    def test_context_appears_in_user_message(self):
        mock_client = _make_mock_client(_valid_prompts_json())
        gen = PromptGenerator(openai_client=mock_client)

        gen.generate_prompts("kawaii", context="Japanese cute culture exploding on TikTok.")

        call_kw = mock_client.chat.completions.create.call_args[1]
        user_msg = next(m["content"] for m in call_kw["messages"] if m["role"] == "user")
        assert "Japanese cute culture" in user_msg

    def test_missing_context_uses_placeholder(self):
        """When context is empty, the user message still renders without KeyError."""
        mock_client = _make_mock_client(_valid_prompts_json())
        gen = PromptGenerator(openai_client=mock_client)

        result = gen.generate_prompts("retro")  # no context kwarg

        assert len(result) == 3  # no crash


# ---------------------------------------------------------------------------
# Prompt constant content checks
# ---------------------------------------------------------------------------

class TestPromptConstants:
    """Prompt constants carry all required audience, surface, and expansion signals."""

    def test_style_directives_mentions_15_to_30_audience(self):
        assert "15-30" in STYLE_DIRECTIVES

    def test_style_directives_mentions_laptop(self):
        assert "laptop" in STYLE_DIRECTIVES.lower()

    def test_style_directives_mentions_water_bottle(self):
        assert "water bottle" in STYLE_DIRECTIVES.lower()

    def test_style_directives_mentions_phone_case(self):
        assert "phone" in STYLE_DIRECTIVES.lower()

    def test_style_directives_mentions_size(self):
        assert "inch" in STYLE_DIRECTIVES.lower()

    def test_style_directives_prohibits_text(self):
        assert "no text" in STYLE_DIRECTIVES.lower() or "no words" in STYLE_DIRECTIVES.lower()

    def test_system_prompt_mentions_age_range(self):
        assert "15-30" in SYSTEM_PROMPT

    def test_system_prompt_instructs_expanding_single_word_trends(self):
        assert "single" in SYSTEM_PROMPT.lower() or "expand" in SYSTEM_PROMPT.lower()

    def test_system_prompt_mentions_surfaces(self):
        combined = SYSTEM_PROMPT.lower()
        assert "laptop" in combined or "water bottle" in combined

    def test_user_prompt_template_has_topic_placeholder(self):
        assert "{topic}" in USER_PROMPT_TEMPLATE

    def test_user_prompt_template_has_context_placeholder(self):
        assert "{context}" in USER_PROMPT_TEMPLATE

    def test_user_prompt_template_mentions_audience(self):
        assert "15-30" in USER_PROMPT_TEMPLATE

    def test_user_prompt_template_mentions_surfaces(self):
        combined = USER_PROMPT_TEMPLATE.lower()
        assert "laptop" in combined and "water bottle" in combined

    def test_user_prompt_template_mentions_single_word_expansion(self):
        combined = USER_PROMPT_TEMPLATE.lower()
        assert "single word" in combined or "single-word" in combined

    def test_user_prompt_template_requests_json_prompts_array(self):
        assert '"prompts"' in USER_PROMPT_TEMPLATE or "prompts" in USER_PROMPT_TEMPLATE

    def test_prompts_per_trend_is_3(self):
        assert PROMPTS_PER_TREND == 3

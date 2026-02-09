"""
Integration tests for Replicate SDXL image generation.

Requires REPLICATE_API_TOKEN environment variable.
Costs ~$0.015 per run -- use sparingly.
"""

from __future__ import annotations

import pytest

from tests.conftest import skip_if_no_replicate

pytestmark = [pytest.mark.integration, skip_if_no_replicate]

TEST_PROMPT = "a red circle sticker, white background, vector art, simple"


class TestReplicateGenerationIntegration:
    """Live Replicate API integration tests."""

    def test_client_initializes(self):
        """Replicate client initializes with real token."""
        import replicate
        import os

        client = replicate.Client(api_token=os.getenv("REPLICATE_API_TOKEN"))
        assert client is not None

    def test_generation_returns_bytes(self):
        """Running SDXL with a simple prompt returns image bytes."""
        import replicate
        import os
        import httpx

        client = replicate.Client(api_token=os.getenv("REPLICATE_API_TOKEN"))
        output = client.run(
            "stability-ai/sdxl",
            input={
                "prompt": TEST_PROMPT,
                "width": 1024,
                "height": 1024,
                "num_outputs": 1,
                "output_format": "png",
            },
        )

        assert isinstance(output, list)
        assert len(output) > 0

        # Download the image
        image_url = str(output[0])
        response = httpx.get(image_url, timeout=60)
        response.raise_for_status()
        image_bytes = response.content

        assert len(image_bytes) > 0

    def test_image_is_valid_png(self):
        """Generated image starts with PNG header."""
        import replicate
        import os
        import httpx

        client = replicate.Client(api_token=os.getenv("REPLICATE_API_TOKEN"))
        output = client.run(
            "stability-ai/sdxl",
            input={
                "prompt": TEST_PROMPT,
                "width": 1024,
                "height": 1024,
                "num_outputs": 1,
                "output_format": "png",
            },
        )

        image_url = str(output[0])
        response = httpx.get(image_url, timeout=60)
        image_bytes = response.content

        # PNG magic bytes
        assert image_bytes[:4] == b"\x89PNG"

    def test_image_dimensions(self):
        """Generated image is 1024x1024."""
        import replicate
        import os
        import httpx
        from PIL import Image
        from io import BytesIO

        client = replicate.Client(api_token=os.getenv("REPLICATE_API_TOKEN"))
        output = client.run(
            "stability-ai/sdxl",
            input={
                "prompt": TEST_PROMPT,
                "width": 1024,
                "height": 1024,
                "num_outputs": 1,
                "output_format": "png",
            },
        )

        image_url = str(output[0])
        response = httpx.get(image_url, timeout=60)
        img = Image.open(BytesIO(response.content))
        assert img.size == (1024, 1024)

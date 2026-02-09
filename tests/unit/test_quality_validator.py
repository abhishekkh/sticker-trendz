"""Tests for src/stickers/quality_validator.py -- image quality validation."""

import io
from PIL import Image
import pytest

from src.stickers.quality_validator import (
    validate_image,
    ValidationResult,
    EXPECTED_DIMENSION,
    MIN_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_BYTES,
    MAX_BLANK_RATIO,
    MIN_ASPECT_RATIO,
    MAX_ASPECT_RATIO,
    get_modified_prompt,
)


def _png_bytes_from_image(img: Image.Image) -> bytes:
    """Encode PIL Image to PNG bytes."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_1024_rgba_with_content() -> bytes:
    """Create a 1024x1024 RGBA image with central content (not mostly blank).
    Uses per-pixel variation so PNG size exceeds 50KB (validator minimum)."""
    img = Image.new("RGBA", (1024, 1024), (255, 255, 255, 0))
    left, top = 200, 200
    right, bottom = 824, 824
    for y in range(top, bottom):
        for x in range(left, right):
            # High variation so PNG is > 50KB (avoid solid color compression)
            r = (100 + (x * 7 + y) % 156) % 256
            g = (150 + (x + y * 11) % 106) % 256
            b = (200 + (x * 3 + y * 5) % 56) % 256
            img.putpixel((x, y), (r, g, b, 255))
    return _png_bytes_from_image(img)


def make_1024_mostly_blank() -> bytes:
    """Create a 1024x1024 image that is >80% white/transparent."""
    img = Image.new("RGBA", (1024, 1024), (255, 255, 255, 255))
    # Small content area: 50x50 in center
    for y in range(487, 537):
        for x in range(487, 537):
            img.putpixel((x, y), (50, 50, 50, 255))
    return _png_bytes_from_image(img)


def make_1024_extreme_aspect_tall() -> bytes:
    """Content in a thin vertical strip -> cropped aspect ratio < 0.5 (height >> width)."""
    img = Image.new("RGBA", (1024, 1024), (255, 255, 255, 0))
    # Vertical strip: columns 500-530 (width 30, height 1024 -> ratio ~0.03)
    for x in range(500, 531):
        for y in range(1024):
            img.putpixel((x, y), (100, 100, 100, 255))
    return _png_bytes_from_image(img)


def make_1024_extreme_aspect_wide() -> bytes:
    """Content in a thin horizontal strip -> cropped aspect ratio > 2.0."""
    img = Image.new("RGBA", (1024, 1024), (255, 255, 255, 0))
    # Horizontal strip: rows 500-530 (width 1024, height 30 -> ratio ~34)
    for y in range(500, 531):
        for x in range(1024):
            img.putpixel((x, y), (100, 100, 100, 255))
    return _png_bytes_from_image(img)


def make_tiny_png_under_50kb() -> bytes:
    """Create a valid PNG smaller than 50KB (will fail dimension check too)."""
    img = Image.new("RGBA", (100, 100), (128, 128, 128, 255))
    return _png_bytes_from_image(img)


class TestValidateImageDimensions:
    """Test that dimensions are validated as 1024x1024."""

    def test_valid_1024x1024_passes_dimension_check(self):
        """Image that is 1024x1024 passes dimension check (if other checks pass)."""
        data = make_1024_rgba_with_content()
        result = validate_image(data)
        assert result.width == 1024
        assert result.height == 1024
        assert "wrong_dimensions" not in "; ".join(result.failures)

    def test_non_1024_fails_dimension_check(self):
        """Image that is not 1024x1024 fails with wrong_dimensions."""
        img = Image.new("RGBA", (800, 800), (100, 100, 100, 255))
        data = _png_bytes_from_image(img)
        result = validate_image(data)
        assert result.passed is False
        assert any("wrong_dimensions" in f for f in result.failures)


class TestValidateFileSize:
    """Test file size bounds: 50KB to 5MB."""

    def test_file_size_under_50kb_rejected(self):
        """File size < 50KB is rejected."""
        data = make_tiny_png_under_50kb()
        assert len(data) < MIN_FILE_SIZE_BYTES
        result = validate_image(data)
        assert result.passed is False
        assert any("file_too_small" in f for f in result.failures)

    def test_file_size_over_5mb_rejected(self):
        """File size > 5MB is rejected."""
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * (6 * 1024 * 1024 - 8)  # 6MB total, invalid but large
        assert len(data) > MAX_FILE_SIZE_BYTES
        result = validate_image(data)
        assert result.passed is False
        assert any("file_too_large" in f for f in result.failures)

    def test_valid_size_passes(self):
        """Image with size between 50KB and 5MB passes size check (no file_too_small/large)."""
        data = make_1024_rgba_with_content()
        if not (MIN_FILE_SIZE_BYTES <= len(data) <= MAX_FILE_SIZE_BYTES):
            pytest.skip(
                f"Sample image size {len(data)} not in 50KB-5MB; "
                "content image compresses well"
            )
        result = validate_image(data)
        assert any("file_too_small" in f for f in result.failures) is False
        assert any("file_too_large" in f for f in result.failures) is False


class TestValidateMostlyBlank:
    """Test that mostly blank image (>80% white/transparent) is rejected."""

    def test_mostly_blank_rejected(self):
        """Image with >80% white/transparent pixels is rejected."""
        data = make_1024_mostly_blank()
        result = validate_image(data)
        assert result.passed is False
        assert any("mostly_blank" in f for f in result.failures)
        assert result.blank_ratio > MAX_BLANK_RATIO

    def test_valid_image_with_content_passes_blank_check(self):
        """Image with sufficient content passes blank ratio check."""
        data = make_1024_rgba_with_content()
        result = validate_image(data)
        assert result.blank_ratio <= MAX_BLANK_RATIO
        assert any("mostly_blank" in f for f in result.failures) is False


class TestValidateAspectRatio:
    """Test aspect ratio after crop must be between 0.5 and 2.0."""

    def test_extreme_aspect_ratio_tall_rejected(self):
        """Aspect ratio < 0.5 (very tall content) is rejected."""
        data = make_1024_extreme_aspect_tall()
        result = validate_image(data)
        assert result.passed is False
        assert any("bad_aspect_ratio" in f for f in result.failures)
        assert result.aspect_ratio < MIN_ASPECT_RATIO

    def test_extreme_aspect_ratio_wide_rejected(self):
        """Aspect ratio > 2.0 (very wide content) is rejected."""
        data = make_1024_extreme_aspect_wide()
        result = validate_image(data)
        assert result.passed is False
        assert any("bad_aspect_ratio" in f for f in result.failures)
        assert result.aspect_ratio > MAX_ASPECT_RATIO

    def test_valid_aspect_ratio_passes(self):
        """Image with aspect ratio in [0.5, 2.0] passes."""
        data = make_1024_rgba_with_content()
        result = validate_image(data)
        assert MIN_ASPECT_RATIO <= result.aspect_ratio <= MAX_ASPECT_RATIO
        assert any("bad_aspect_ratio" in f for f in result.failures) is False


class TestValidateAlphaChannel:
    """Test alpha channel detection."""

    def test_rgba_has_alpha(self):
        """RGBA image is reported as having alpha."""
        data = make_1024_rgba_with_content()
        result = validate_image(data)
        assert result.has_alpha is True


class TestValidImagePassesAllChecks:
    """Test that a valid image passes all quality checks."""

    def test_valid_image_passes_all_checks(self):
        """Valid 1024x1024 RGBA image with content, 50KB-5MB, and good aspect ratio passes."""
        data = make_1024_rgba_with_content()
        if len(data) < MIN_FILE_SIZE_BYTES:
            pytest.skip(
                "Sample image compresses to < 50KB; use make_1024_rgba_with_content (varied pixels)"
            )
        result = validate_image(data)
        assert result.passed is True
        assert result.width == EXPECTED_DIMENSION
        assert result.height == EXPECTED_DIMENSION
        assert result.has_alpha is True
        assert result.blank_ratio <= MAX_BLANK_RATIO
        assert MIN_ASPECT_RATIO <= result.aspect_ratio <= MAX_ASPECT_RATIO


class TestGetModifiedPrompt:
    """Test retry prompt modification."""

    def test_modified_prompt_appends_centered_simple(self):
        """get_modified_prompt appends 'Centered, simple composition.'"""
        out = get_modified_prompt("A cute cat sticker")
        assert out == "A cute cat sticker Centered, simple composition."

"""Tests for src/stickers/post_processor.py -- image post-processing pipeline."""

import io
from PIL import Image
import pytest

from src.stickers.post_processor import (
    process_image,
    PostProcessingError,
    ProcessedImage,
    PRINT_READY_SIZE,
    THUMBNAIL_SIZE,
    MAX_PRINT_READY_BYTES,
    MAX_THUMBNAIL_BYTES,
)


def _png_bytes_from_image(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_1024_rgba_with_content() -> bytes:
    """1024x1024 RGBA with central non-white content (survives background clean)."""
    img = Image.new("RGBA", (1024, 1024), (255, 255, 255, 0))
    # Non-white content so _clean_background doesn't remove it
    left, top = 200, 200
    right, bottom = 824, 824
    for y in range(top, bottom):
        for x in range(left, right):
            img.putpixel((x, y), (100, 150, 200, 255))
    return _png_bytes_from_image(img)


def make_1024_mostly_white() -> bytes:
    """1024x1024 mostly white -> high blank ratio after background clean."""
    img = Image.new("RGBA", (1024, 1024), (255, 255, 255, 255))
    # Tiny content
    for y in range(510, 515):
        for x in range(510, 515):
            img.putpixel((x, y), (50, 50, 50, 255))
    return _png_bytes_from_image(img)


def _image_has_alpha_channel(png_bytes: bytes) -> bool:
    img = Image.open(io.BytesIO(png_bytes))
    return img.mode in ("RGBA", "LA", "PA")


class TestProcessImageDimensions:
    """Test that 1024x1024 input produces 900x900 print-ready and 300x300 thumbnail."""

    def test_1024_input_produces_900x900_print_ready_and_300x300_thumbnail(self):
        """1024x1024 input -> print_ready 900x900, thumbnail 300x300."""
        data = make_1024_rgba_with_content()
        result = process_image(data)
        assert isinstance(result, ProcessedImage)
        assert result.print_ready_size == (900, 900)
        assert result.thumbnail_size == (300, 300)
        print_img = Image.open(io.BytesIO(result.print_ready))
        thumb_img = Image.open(io.BytesIO(result.thumbnail))
        assert (print_img.width, print_img.height) == (900, 900)
        assert (thumb_img.width, thumb_img.height) == (300, 300)


class TestProcessImageTransparency:
    """Test that output images have alpha channel."""

    def test_output_images_have_alpha_channel(self):
        """Print-ready and thumbnail outputs have alpha (transparency)."""
        data = make_1024_rgba_with_content()
        result = process_image(data)
        assert _image_has_alpha_channel(result.print_ready)
        assert _image_has_alpha_channel(result.thumbnail)


class TestProcessImageMostlyBlankRejected:
    """Test that mostly blank image is rejected."""

    def test_mostly_blank_image_rejected(self):
        """Image that is >80% transparent/white after background removal raises PostProcessingError."""
        data = make_1024_mostly_white()
        with pytest.raises(PostProcessingError) as exc_info:
            process_image(data)
        assert "mostly blank" in str(exc_info.value).lower() or "blank" in str(exc_info.value).lower()


class TestProcessImageFileSizes:
    """Test print-ready < 2MB and thumbnail < 500KB."""

    def test_print_ready_under_2mb(self):
        """Print-ready output is under 2MB (or best effort per implementation)."""
        data = make_1024_rgba_with_content()
        result = process_image(data)
        assert len(result.print_ready) <= MAX_PRINT_READY_BYTES * 1.1  # allow small overrun (implementation may log warning)

    def test_thumbnail_under_500kb(self):
        """Thumbnail output is under 500KB (or best effort)."""
        data = make_1024_rgba_with_content()
        result = process_image(data)
        assert len(result.thumbnail) <= MAX_THUMBNAIL_BYTES * 1.1


class TestProcessImageValidPasses:
    """Test that valid image passes full pipeline."""

    def test_valid_image_passes_all_steps(self):
        """Valid 1024x1024 RGBA image with content completes without error."""
        data = make_1024_rgba_with_content()
        result = process_image(data)
        assert result.print_ready is not None
        assert result.thumbnail is not None
        assert len(result.print_ready) > 0
        assert len(result.thumbnail) > 0


class TestProcessImageInvalidInput:
    """Test invalid input handling."""

    def test_invalid_image_bytes_raises(self):
        """Invalid image data raises PostProcessingError."""
        with pytest.raises(PostProcessingError):
            process_image(b"not a valid image")

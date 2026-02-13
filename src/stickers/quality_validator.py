"""
Image quality validator for Sticker Trendz.

Validates generated sticker images against quality criteria before
post-processing. Checks dimensions, file size, transparency,
blank detection, and aspect ratio.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PIL import Image
import numpy as np

logger = logging.getLogger(__name__)

# Validation thresholds – image size matches REPLICATE_IMAGE_SIZE env var
def _load_expected_dimension() -> int:
    raw = os.getenv("REPLICATE_IMAGE_SIZE")
    if raw is None:
        return 1024
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "REPLICATE_IMAGE_SIZE has non-integer value '%s', using default 1024", raw,
        )
        return 1024

EXPECTED_DIMENSION = _load_expected_dimension()
MIN_FILE_SIZE_BYTES = 50 * 1024       # 50 KB
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_BLANK_RATIO = 0.80                 # 80% white/transparent
MIN_ASPECT_RATIO = 0.5
MAX_ASPECT_RATIO = 2.0


@dataclass
class ValidationResult:
    """Result of an image quality validation."""

    passed: bool
    failures: List[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    file_size: int = 0
    has_alpha: bool = False
    blank_ratio: float = 0.0
    aspect_ratio: float = 1.0

    def add_failure(self, reason: str) -> None:
        self.failures.append(reason)
        self.passed = False


def validate_image(image_bytes: bytes) -> ValidationResult:
    """
    Run all quality checks on a generated image.

    Checks:
      1. Dimensions are 1024x1024
      2. File size between 50KB and 5MB
      3. Alpha channel presence
      4. Not mostly blank (<80% white/transparent)
      5. Aspect ratio after auto-crop between 0.5 and 2.0

    Args:
        image_bytes: Raw PNG image data.

    Returns:
        ValidationResult with pass/fail status and reasons.
    """
    result = ValidationResult(passed=True)
    result.file_size = len(image_bytes)

    # Check file size
    if result.file_size < MIN_FILE_SIZE_BYTES:
        result.add_failure(
            f"file_too_small: {result.file_size} bytes < {MIN_FILE_SIZE_BYTES} bytes"
        )
    elif result.file_size > MAX_FILE_SIZE_BYTES:
        result.add_failure(
            f"file_too_large: {result.file_size} bytes > {MAX_FILE_SIZE_BYTES} bytes"
        )

    # Open image
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        result.add_failure(f"invalid_image: cannot open image: {exc}")
        return result

    result.width = img.width
    result.height = img.height

    # Check dimensions
    if img.width != EXPECTED_DIMENSION or img.height != EXPECTED_DIMENSION:
        result.add_failure(
            f"wrong_dimensions: {img.width}x{img.height}, expected {EXPECTED_DIMENSION}x{EXPECTED_DIMENSION}"
        )

    # Check alpha channel
    result.has_alpha = img.mode in ("RGBA", "LA", "PA")

    # Check blank ratio
    blank_ratio = _calculate_blank_ratio(img)
    result.blank_ratio = blank_ratio
    if blank_ratio > MAX_BLANK_RATIO:
        result.add_failure(
            f"mostly_blank: {blank_ratio:.1%} white/transparent pixels > {MAX_BLANK_RATIO:.0%} threshold"
        )

    # Check aspect ratio after auto-crop
    aspect_ratio = _calculate_cropped_aspect_ratio(img)
    result.aspect_ratio = aspect_ratio
    if aspect_ratio < MIN_ASPECT_RATIO or aspect_ratio > MAX_ASPECT_RATIO:
        result.add_failure(
            f"bad_aspect_ratio: {aspect_ratio:.2f} outside range [{MIN_ASPECT_RATIO}, {MAX_ASPECT_RATIO}]"
        )

    if result.passed:
        logger.info(
            "Image passed quality validation: %dx%d, %d bytes, blank=%.1f%%, ar=%.2f",
            result.width, result.height, result.file_size,
            result.blank_ratio * 100, result.aspect_ratio,
        )
    else:
        logger.warning(
            "Image failed quality validation: %s",
            "; ".join(result.failures),
        )

    return result


def _calculate_blank_ratio(img: Image.Image) -> float:
    """
    Calculate the ratio of white/transparent pixels in the image.

    A pixel is considered blank if:
      - It is fully transparent (alpha = 0), or
      - It is white (R, G, B all > 250)
    """
    try:
        rgba = img.convert("RGBA")
        arr = np.array(rgba)
        total_pixels = arr.shape[0] * arr.shape[1]

        if total_pixels == 0:
            return 1.0

        # Transparent pixels
        alpha = arr[:, :, 3]
        transparent_count = int(np.sum(alpha < 10))

        # White pixels (non-transparent)
        rgb = arr[:, :, :3]
        white_mask = np.all(rgb > 250, axis=2) & (alpha >= 10)
        white_count = int(np.sum(white_mask))

        blank_count = transparent_count + white_count
        return blank_count / total_pixels

    except Exception as exc:
        logger.warning("Failed to calculate blank ratio: %s", exc)
        return 0.0


def _calculate_cropped_aspect_ratio(img: Image.Image) -> float:
    """
    Calculate the aspect ratio of the content area after auto-crop.

    Uses the bounding box of non-blank content.
    Returns width/height ratio.
    """
    try:
        rgba = img.convert("RGBA")
        arr = np.array(rgba)

        # Find non-blank pixels (not white AND not transparent)
        alpha = arr[:, :, 3]
        rgb = arr[:, :, :3]
        content_mask = (alpha >= 10) & ~np.all(rgb > 250, axis=2)

        if not np.any(content_mask):
            return 1.0

        rows = np.any(content_mask, axis=1)
        cols = np.any(content_mask, axis=0)

        row_indices = np.where(rows)[0]
        col_indices = np.where(cols)[0]

        if len(row_indices) == 0 or len(col_indices) == 0:
            return 1.0

        height = row_indices[-1] - row_indices[0] + 1
        width = col_indices[-1] - col_indices[0] + 1

        if height == 0:
            return 1.0

        return width / height

    except Exception as exc:
        logger.warning("Failed to calculate aspect ratio: %s", exc)
        return 1.0


def get_modified_prompt(original_prompt: str) -> str:
    """
    Modify a prompt for retry after quality validation failure.

    Appends "centered, simple composition" per the spec.

    Args:
        original_prompt: The original image generation prompt.

    Returns:
        Modified prompt string.
    """
    return f"{original_prompt} Centered, simple composition."

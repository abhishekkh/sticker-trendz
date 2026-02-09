"""
Image post-processor for Sticker Trendz.

Pillow-based pipeline that prepares generated sticker images for
Etsy listing: background removal, auto-crop, resize to print-ready
(900x900) and thumbnail (300x300), and file size optimization.
Also supports lifestyle mockup compositing.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from PIL import Image, ImageFilter
import numpy as np

logger = logging.getLogger(__name__)

PRINT_READY_SIZE = (900, 900)
THUMBNAIL_SIZE = (300, 300)
PRINT_READY_DPI = (300, 300)
MAX_PRINT_READY_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_THUMBNAIL_BYTES = 500 * 1024          # 500 KB
MAX_BLANK_RATIO = 0.80

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOCKUP_DIR = os.path.join(_PROJECT_ROOT, "assets", "mockups")


@dataclass
class ProcessedImage:
    """Result of post-processing a sticker image."""

    print_ready: bytes
    thumbnail: bytes
    print_ready_size: Tuple[int, int]
    thumbnail_size: Tuple[int, int]


class PostProcessingError(Exception):
    """Raised when post-processing fails."""


def process_image(image_bytes: bytes) -> ProcessedImage:
    """
    Run the full post-processing pipeline on a generated image.

    Steps:
      1. Clean/make background transparent
      2. Auto-crop to content bounds
      3. Validate aspect ratio
      4. Resize to 900x900 print-ready
      5. Generate 300x300 thumbnail
      6. Optimize file sizes

    Args:
        image_bytes: Raw PNG image data (typically 1024x1024).

    Returns:
        ProcessedImage with print_ready and thumbnail bytes.

    Raises:
        PostProcessingError: If the image is mostly blank or invalid.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except Exception as exc:
        raise PostProcessingError(f"Cannot open image: {exc}") from exc

    # Step 1: Clean background (make near-white pixels transparent)
    img = _clean_background(img)

    # Check if mostly blank after background cleanup
    blank_ratio = _calculate_blank_ratio(img)
    if blank_ratio > MAX_BLANK_RATIO:
        raise PostProcessingError(
            f"Image is mostly blank after background removal "
            f"({blank_ratio:.1%} transparent/white)"
        )

    # Step 2: Auto-crop to content bounds
    img = _auto_crop(img)

    # Step 3: Resize to print-ready dimensions
    print_img = _resize_with_padding(img, PRINT_READY_SIZE)

    # Step 4: Generate thumbnail
    thumb_img = _resize_with_padding(img, THUMBNAIL_SIZE)

    # Step 5: Optimize and export
    print_bytes = _optimize_png(print_img, MAX_PRINT_READY_BYTES)
    thumb_bytes = _optimize_png(thumb_img, MAX_THUMBNAIL_BYTES)

    logger.info(
        "Post-processing complete: print=%d bytes, thumb=%d bytes",
        len(print_bytes), len(thumb_bytes),
    )

    return ProcessedImage(
        print_ready=print_bytes,
        thumbnail=thumb_bytes,
        print_ready_size=PRINT_READY_SIZE,
        thumbnail_size=THUMBNAIL_SIZE,
    )


def _clean_background(img: Image.Image) -> Image.Image:
    """
    Make near-white and near-transparent pixels fully transparent.

    This cleans up the background from AI-generated images.
    """
    arr = np.array(img)

    # Make near-white pixels transparent (RGB all > 245)
    rgb = arr[:, :, :3]
    alpha = arr[:, :, 3]
    white_mask = np.all(rgb > 245, axis=2)
    arr[white_mask, 3] = 0

    # Make already near-transparent pixels fully transparent
    arr[alpha < 20, 3] = 0

    return Image.fromarray(arr, "RGBA")


def _auto_crop(img: Image.Image) -> Image.Image:
    """
    Crop the image to the bounding box of non-transparent content.

    Adds a small padding margin (5% of max dimension).
    """
    arr = np.array(img)
    alpha = arr[:, :, 3]

    # Find content pixels (alpha > 10)
    content_mask = alpha > 10
    if not np.any(content_mask):
        return img  # No content to crop

    rows = np.any(content_mask, axis=1)
    cols = np.any(content_mask, axis=0)
    row_min, row_max = np.where(rows)[0][[0, -1]]
    col_min, col_max = np.where(cols)[0][[0, -1]]

    # Add 5% padding
    padding = max(int(max(row_max - row_min, col_max - col_min) * 0.05), 5)
    row_min = max(0, row_min - padding)
    row_max = min(img.height - 1, row_max + padding)
    col_min = max(0, col_min - padding)
    col_max = min(img.width - 1, col_max + padding)

    cropped = img.crop((col_min, row_min, col_max + 1, row_max + 1))
    logger.debug(
        "Auto-cropped: %dx%d -> %dx%d",
        img.width, img.height, cropped.width, cropped.height,
    )
    return cropped


def _resize_with_padding(img: Image.Image, target_size: Tuple[int, int]) -> Image.Image:
    """
    Resize image to fit within target_size while maintaining aspect ratio,
    then center on a transparent canvas of exact target dimensions.
    """
    # Calculate scale to fit
    scale_w = target_size[0] / img.width
    scale_h = target_size[1] / img.height
    scale = min(scale_w, scale_h)

    new_w = max(1, int(img.width * scale))
    new_h = max(1, int(img.height * scale))

    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Center on transparent canvas
    canvas = Image.new("RGBA", target_size, (0, 0, 0, 0))
    offset_x = (target_size[0] - new_w) // 2
    offset_y = (target_size[1] - new_h) // 2
    canvas.paste(resized, (offset_x, offset_y), resized)

    return canvas


def _optimize_png(img: Image.Image, max_bytes: int) -> bytes:
    """
    Export image as optimized PNG, reducing quality if needed to stay
    under the size limit.
    """
    buf = io.BytesIO()
    pnginfo = img.info.copy() if hasattr(img, "info") else {}
    img.save(buf, format="PNG", optimize=True, dpi=PRINT_READY_DPI)
    data = buf.getvalue()

    if len(data) <= max_bytes:
        return data

    # Try quantizing to reduce size
    try:
        quantized = img.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        quantized = quantized.convert("RGBA")
        buf2 = io.BytesIO()
        quantized.save(buf2, format="PNG", optimize=True, dpi=PRINT_READY_DPI)
        data2 = buf2.getvalue()
        if len(data2) <= max_bytes:
            return data2
    except Exception:
        pass

    # Return best effort even if over limit
    logger.warning(
        "PNG optimization: %d bytes exceeds target %d bytes",
        len(data), max_bytes,
    )
    return data


def _calculate_blank_ratio(img: Image.Image) -> float:
    """Calculate ratio of transparent/white pixels."""
    arr = np.array(img.convert("RGBA"))
    total = arr.shape[0] * arr.shape[1]
    if total == 0:
        return 1.0
    alpha = arr[:, :, 3]
    transparent = int(np.sum(alpha < 10))
    rgb = arr[:, :, :3]
    white_mask = np.all(rgb > 250, axis=2) & (alpha >= 10)
    white = int(np.sum(white_mask))
    return (transparent + white) / total


# ------------------------------------------------------------------
# Mockup generation (STR-023)
# ------------------------------------------------------------------

def generate_mockup(
    sticker_image: bytes,
    template_path: str,
    position: Tuple[int, int] = (0, 0),
    size: Optional[Tuple[int, int]] = None,
    angle: float = 0.0,
) -> bytes:
    """
    Composite a sticker image onto a mockup template.

    Args:
        sticker_image: PNG bytes of the sticker (with transparency).
        template_path: Path to the template image file.
        position: (x, y) position to place the sticker center.
        size: Target size (width, height) for the sticker on the template.
        angle: Rotation angle in degrees.

    Returns:
        PNG bytes of the composited mockup.
    """
    try:
        template = Image.open(template_path).convert("RGBA")
        sticker = Image.open(io.BytesIO(sticker_image)).convert("RGBA")
    except Exception as exc:
        raise PostProcessingError(f"Cannot open images for mockup: {exc}") from exc

    # Resize sticker
    if size:
        sticker = sticker.resize(size, Image.Resampling.LANCZOS)

    # Rotate if needed
    if angle != 0:
        sticker = sticker.rotate(-angle, expand=True, resample=Image.Resampling.BICUBIC)

    # Calculate paste position (center sticker at the position)
    paste_x = position[0] - sticker.width // 2
    paste_y = position[1] - sticker.height // 2

    # Composite
    template.paste(sticker, (paste_x, paste_y), sticker)

    buf = io.BytesIO()
    template.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# Default mockup positions
LAPTOP_MOCKUP = {
    "template": os.path.join(MOCKUP_DIR, "laptop_template.png"),
    "position": (512, 340),
    "size": (200, 200),
    "angle": 0,
}

BOTTLE_MOCKUP = {
    "template": os.path.join(MOCKUP_DIR, "bottle_template.png"),
    "position": (256, 400),
    "size": (150, 150),
    "angle": -5,
}

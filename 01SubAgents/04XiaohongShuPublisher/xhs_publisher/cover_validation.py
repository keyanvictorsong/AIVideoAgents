"""Conservative comparison of a complete 3:4 cover with its visible preview.

This checks the same artwork after resizing/compression. It does not align,
crop, or recognize artwork, and cannot recover text lost in tiny thumbnails.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

SIZE = (96, 128)
LIMITS = {
    "rgb_mae": 0.045,
    "worst_tile_mae": 0.14,
    "worst_four_tiles_mae": 0.075,
    "gradient_mae": 0.025,
}


def _rgb(source: Path | BytesIO) -> Image.Image:
    with Image.open(source) as original:
        picture = ImageOps.exif_transpose(original).convert("RGBA")
        background = Image.new("RGBA", picture.size, "white")
        return Image.alpha_composite(background, picture).convert("RGB")


def _normalized(picture: Image.Image) -> Image.Image:
    return picture.resize(SIZE, Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(0.6))


def compare_cover(reference_path: str | Path, preview_png_bytes: bytes) -> dict:
    """Return a fail-closed match decision and inspectable normalized errors.

    ``metric`` is the largest error divided by its threshold; at most 1 passes.
    Thresholds tolerate ordinary thumbnail scaling and JPEG compression while
    local tiles retain sensitivity to moved/missing titles. Human review is
    still required, especially when the original lettering is very small.
    """
    details: dict = {"method": "rgb_tiles_gradient_v1", "normalized_size": list(SIZE),
                     "thresholds": dict(LIMITS)}

    def rejected(reason: str) -> dict:
        details["reason"] = reason
        return {"matches": False, "metric": None, "details": details}

    try:
        reference = _rgb(Path(reference_path))
        preview = _rgb(BytesIO(preview_png_bytes))
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        return rejected("unreadable_image")
    details.update(reference_size=list(reference.size), preview_size=list(preview.size))
    if abs(reference.width / reference.height / 0.75 - 1) > 0.01:
        return rejected("reference_is_not_3_to_4")
    aspect_error = abs((preview.width / preview.height) / (reference.width / reference.height) - 1)
    details["aspect_error"] = round(aspect_error, 6)
    if aspect_error > 0.03:
        return rejected("preview_aspect_mismatch")
    if min(preview.size) < 48:
        return rejected("preview_too_small")

    reference, preview = _normalized(reference), _normalized(preview)
    channel_errors = [abs(a - b) / 255
                      for a, b in zip(reference.tobytes(), preview.tobytes())]
    errors = [sum(channel_errors[start:start + 3]) / 3
              for start in range(0, len(channel_errors), 3)]
    width, height = SIZE
    tile_errors = []
    for top in range(0, height, 16):
        for start in range(0, width, 12):
            values = [errors[y * width + x]
                      for y in range(top, top + 16) for x in range(start, start + 12)]
            tile_errors.append(sum(values) / len(values))
    tile_errors.sort(reverse=True)

    gray_left = reference.convert("L").tobytes()
    gray_right = preview.convert("L").tobytes()
    gradients = []
    for y in range(height - 1):
        for x in range(width - 1):
            index = y * width + x
            for offset in (1, width):
                gradients.append(abs((gray_left[index + offset] - gray_left[index])
                                     - (gray_right[index + offset] - gray_right[index])) / 255)
    measured = {
        "rgb_mae": sum(errors) / len(errors),
        "worst_tile_mae": tile_errors[0],
        "worst_four_tiles_mae": sum(tile_errors[:4]) / 4,
        "gradient_mae": sum(gradients) / len(gradients),
    }
    metric = max(measured[key] / limit for key, limit in LIMITS.items())
    details.update({key: round(value, 6) for key, value in measured.items()})
    details["reason"] = "matched" if metric <= 1 else "different_content"
    return {"matches": metric <= 1, "metric": round(metric, 6), "details": details}

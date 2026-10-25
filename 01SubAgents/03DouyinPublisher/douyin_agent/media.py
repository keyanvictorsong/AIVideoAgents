"""Extract real video frames and prepare user-selected cover layouts.

Only image measurements are used for frame selection. This module does not infer
what is happening in the video, transcribe speech, or verify text with OCR.
"""
from __future__ import annotations

import io
import json
import math
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


class MediaError(RuntimeError):
    """A video, FFmpeg, or font could not be used."""


FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)


def _run(arguments: list[str], *, timeout: int = 90) -> bytes:
    try:
        result = subprocess.run(arguments, capture_output=True, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise MediaError(f"缺少 {arguments[0]}，请先安装 FFmpeg。") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{arguments[0]} 处理超时。") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace")[-1800:]
        raise MediaError(f"{arguments[0]} 处理失败：{detail}") from exc
    return result.stdout


def probe_video(video: Path) -> dict[str, Any]:
    """Return stream metadata; reject missing/unreadable/non-video inputs."""
    video = Path(video).expanduser().resolve()
    if not video.is_file():
        raise MediaError(f"视频文件不存在：{video}")
    try:
        metadata = json.loads(_run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video),
        ]))
        stream = next(s for s in metadata.get("streams", []) if s.get("codec_type") == "video")
        duration = float(stream.get("duration") or metadata.get("format", {}).get("duration", 0))
    except (ValueError, TypeError, StopIteration) as exc:
        raise MediaError("无法读取视频时长或画面信息。") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("视频时长必须大于 0。")
    try:
        rate_numerator, rate_denominator = str(stream.get("avg_frame_rate", "30/1")).split("/")
        fps = float(rate_numerator) / float(rate_denominator)
    except (ValueError, ZeroDivisionError):
        fps = 30.
    if not math.isfinite(fps) or fps <= 0:
        fps = 30.
    return {
        "duration": duration,
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "hdr": stream.get("color_transfer") in {"arib-std-b67", "smpte2084"},
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"), "fps": fps,
        "stream_index": int(stream["index"]),
    }


def _extract_frame(video: Path, timestamp: float, metadata: dict, max_width: int | None = None) -> Image.Image:
    filters: list[str] = []
    # FFmpeg applies display rotation and YCbCr->RGB conversion. Keep 16 bits for
    # HLG/PQ and tone-map in NumPy: stock macOS/Homebrew FFmpeg may lack zscale.
    if max_width:
        filters.append(f"scale='min({int(max_width)},iw)':-1")
    filters.append("format=rgb48be" if metadata["hdr"] else "format=rgb24")
    data = _run([
        "ffmpeg", "-v", "error", "-threads", "2", "-filter_threads", "1", "-ss", f"{timestamp:.6f}",
        "-i", str(video), "-map", f"0:{metadata['stream_index']}", "-frames:v", "1", "-an",
        "-vf", ",".join(filters), "-f", "image2pipe", "-vcodec", "png", "pipe:1",
    ])
    try:
        if metadata["hdr"]:
            decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if decoded is None:
                raise ValueError("empty HDR frame")
            rgb = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB).astype(np.float32) / np.iinfo(decoded.dtype).max
            return _tone_map_hdr(rgb, metadata["color_transfer"], metadata.get("color_primaries"))
        return Image.open(io.BytesIO(data)).convert("RGB")
    except (OSError, ValueError) as exc:
        raise MediaError(f"无法在 {timestamp:.2f} 秒提取视频画面。") from exc


def _tone_map_hdr(encoded: np.ndarray, transfer: str, primaries: str | None) -> Image.Image:
    """Decode BT.2100 HLG/PQ, compress luminance, then encode an SDR sRGB JPEG.

    HLG uses a 1000-nit reference display and 1.2 system gamma. Modified Reinhard
    has a 1000-nit white point and 100-nit SDR reference. This is an automatic
    display transform, not a claim to reproduce a particular editor's grade.
    """
    encoded = np.clip(encoded, 0., 1.)
    if transfer == "arib-std-b67":
        a, b, c = .17883277, .28466892, .55991073
        scene = np.where(encoded <= .5, encoded * encoded / 3.,
                         (np.exp((encoded - c) / a) + b) / 12.)
        luminance = np.maximum(np.sum(scene * np.array([.2627, .6780, .0593]), axis=2, keepdims=True), 0.)
        linear = 10. * scene * np.power(luminance, .2)
    else:
        m1, m2 = 2610. / 16384., 2523. / 32.
        c1, c2, c3 = 3424. / 4096., 2413. / 128., 2392. / 128.
        power = np.power(encoded, 1. / m2)
        linear = 100. * np.power(np.maximum(power - c1, 0.) / np.maximum(c2 - c3 * power, 1.e-8), 1. / m1)
    if primaries == "bt2020":
        matrix = np.array([[1.660491, -.587641, -.072850],
                           [-.124550, 1.132900, -.008349],
                           [-.018151, -.100579, 1.118730]], dtype=np.float32)
        linear = linear @ matrix.T
    linear = np.maximum(linear, 0.)
    luminance = np.sum(linear * np.array([.2126, .7152, .0722]), axis=2, keepdims=True)
    mapped_luminance = luminance * (1. + luminance / 100.) / (1. + luminance)
    mapped = linear * mapped_luminance / np.maximum(luminance, 1.e-8)
    mapped = np.clip(mapped, 0., 1.)
    srgb = np.where(mapped <= .0031308, mapped * 12.92, 1.055 * np.power(mapped, 1. / 2.4) - .055)
    return Image.fromarray(np.round(np.clip(srgb, 0., 1.) * 255.).astype(np.uint8))


def _resolve_font(font_path: str | None) -> str:
    choices = [str(Path(font_path).expanduser())] if font_path else list(FONT_CANDIDATES)
    for choice in choices:
        if Path(choice).is_file():
            try:
                ImageFont.truetype(choice, 40)
                return str(Path(choice).resolve())
            except OSError:
                pass
    if font_path:
        raise MediaError(f"无法加载指定字体：{font_path}")
    raise MediaError("未找到中文字体，请用 --font 指定 .ttf / .otf / .ttc 字体。")


def _detect_existing_title(image: Image.Image) -> dict[str, Any]:
    """Do not infer lettering from an original user's color or layout style.

    Text preservation is a caller choice. No OCR or style classifier is supplied.
    """
    return {"detected": False, "bbox": None,
            "reason": "通用版不根据封面颜色推断标题；请显式选择 preserve 或 add。"}


def _subtitle_likelihood(image: Image.Image) -> dict[str, Any]:
    """Find a lower line of small bright, dark-bordered glyph-like components.

    This is deliberately a layout/contrast heuristic, not OCR. A single white
    shirt, rectangle or curtain is not enough: several compact components must
    share a baseline and span a substantial part of the frame.
    """
    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    white = (rgb.min(axis=2) >= 175) & (hsv[:, :, 1] < 85)
    yellow = (hsv[:, :, 0] >= 16) & (hsv[:, :, 0] <= 41) & (hsv[:, :, 1] > 135) & (hsv[:, :, 2] > 170)
    mask = ((white | yellow) * 255).astype(np.uint8)
    mask[:round(height * .55)] = 0
    mask[round(height * .92):] = 0
    mask[:, :round(width * .04)] = 0
    mask[:, round(width * .96):] = 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    components = []
    ring_width = max(1, round(width * .003))
    kernel = np.ones((ring_width * 2 + 1, ring_width * 2 + 1), np.uint8)
    for label in range(1, count):
        x, y, w, h, area = (int(value) for value in stats[label])
        if not (height * .007 <= h <= height * .055 and width * .002 <= w <= width * .08
                and max(4, width * height * .000008) <= area <= width * height * .0025):
            continue
        x0, y0 = max(0, x - ring_width), max(0, y - ring_width)
        x1, y1 = min(width, x + w + ring_width), min(height, y + h + ring_width)
        local = (labels[y0:y1, x0:x1] == label).astype(np.uint8)
        ring = (cv2.dilate(local, kernel) > 0) & (local == 0)
        dark_border = np.count_nonzero((gray[y0:y1, x0:x1] < 85) & ring) / max(1, np.count_nonzero(ring))
        if dark_border >= .30:
            components.append([x, y, x + w, y + h, dark_border])
    best = {"likelihood": 0., "components": 0, "bbox": None,
            "reason": "未检测到下半部明显的浅色描边字幕行。"}
    for anchor in components:
        # English letters and Chinese characters can differ in height while
        # keeping the same lower baseline; tolerate descenders and punctuation.
        line = [component for component in components
                if abs(component[3] - anchor[3]) <= max(height * .009, (anchor[3] - anchor[1]) * .35)]
        if len(line) < 3:
            continue
        bbox = [min(c[0] for c in line), min(c[1] for c in line), max(c[2] for c in line), max(c[3] for c in line)]
        span = (bbox[2] - bbox[0]) / width
        if span < .13 or (bbox[3] - bbox[1]) > height * .07:
            continue
        # Both character count and line width must support the decision.
        # Short Chinese subtitle lines still contain only a few components.
        # Their smaller extent produces a moderate rather than full penalty.
        likelihood = min(1., len(line) / 6.) * min(1., span / .30)
        if likelihood > best["likelihood"]:
            best = {"likelihood": round(likelihood, 3), "components": len(line), "bbox": bbox,
                    "reason": "下半部检测到多枚沿同一基线排列的浅色深边组件，疑似烧录字幕；未使用 OCR。"}
    return best


def _analyze_frame(image: Image.Image) -> dict[str, Any]:
    rgb = np.asarray(image)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    # Clipping and mean luminance are inexpensive, reproducible exposure checks.
    clipped = float(np.mean((gray < 12) | (gray > 245)))
    mean = float(gray.mean())
    exposure = max(0., 1. - abs(mean - 125.) / 125.) * (1. - clipped * .65)
    faces: list[list[int]] = []
    visible_eyes = 0
    cascade_dir = Path(cv2.data.haarcascades)
    face_model = cv2.CascadeClassifier(str(cascade_dir / "haarcascade_frontalface_default.xml"))
    eye_model = cv2.CascadeClassifier(str(cascade_dir / "haarcascade_eye_tree_eyeglasses.xml"))
    if not face_model.empty():
        equalized = cv2.equalizeHist(gray)
        detections = face_model.detectMultiScale(equalized, scaleFactor=1.1, minNeighbors=5,
                                                 minSize=(max(32, image.width // 16),) * 2)
        for x, y, w, h in detections:
            faces.append([int(x), int(y), int(x + w), int(y + h)])
            if not eye_model.empty():
                eyes = eye_model.detectMultiScale(equalized[y:y + int(h * .62), x:x + w],
                                                  scaleFactor=1.08, minNeighbors=4,
                                                  minSize=(max(9, w // 10),) * 2)
                visible_eyes += min(2, len(eyes))
    face_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in faces) / (image.width * image.height)
    # Eye detection is only a small bonus: glasses and head pose can fool Haar
    # cascades, so lack of detected eyes is not proof that somebody blinked.
    subtitles = _subtitle_likelihood(image)
    subtitle_penalty = 16. * subtitles["likelihood"]
    score = (math.log1p(sharpness) * 7. + exposure * 23. + min(face_area, .25) * 45.
             + (5. if faces else 0.) + min(visible_eyes, 2) * 2. - subtitle_penalty)
    return {
        "score": round(score, 3), "sharpness": round(sharpness, 3),
        "exposure": round(exposure, 3), "clipped_fraction": round(clipped, 3),
        "faces": faces, "visible_eyes": int(visible_eyes),
        "subtitle_likelihood": subtitles["likelihood"], "subtitle_detection": subtitles,
        "subtitle_penalty": round(subtitle_penalty, 3),
        "scoring_reason": "清晰度、曝光、人脸和可见眼睛加分；下半部疑似烧录字幕按置信度扣除最多 16 分（未使用 OCR）。",
        "title_detection": _detect_existing_title(image),
        "analysis_size": list(image.size),
    }


def _sample_times(duration: float, samples: int, frame_interval: float = 1 / 30) -> list[float]:
    # Include early frames because many videos show the title only at the start.
    end = max(0., duration - max(min(.15, duration / 10), frame_interval * 1.05))
    if samples == 1:
        return [min(duration * .15, end)]
    values = list(np.linspace(min(.4, duration * .03, end), end, samples))
    if duration > 8 and samples >= 4:
        values[1] = min(2., end)
        values[2] = min(4., end)
    return sorted(set(round(float(value), 5) for value in values))


def _scale_boxes(boxes: list[list[int]], before: tuple[int, int], after: tuple[int, int]) -> list[list[int]]:
    return [[round(box[0] * after[0] / before[0]), round(box[1] * after[1] / before[1]),
             round(box[2] * after[0] / before[0]), round(box[3] * after[1] / before[1])]
            for box in boxes]


def _fit_protected(image: Image.Image, size: tuple[int, int], faces: list[list[int]],
                   title_box: list[int] | None, *, preserve_frame: bool = False) -> tuple[Image.Image, list[list[int]], str]:
    """Crop only if protected faces and existing lettering fit; otherwise pad."""
    width, height = image.size
    target_ratio = size[0] / size[1]
    crop_width = min(width, round(height * target_ratio))
    crop_height = min(height, round(width / target_ratio))
    # Keep some space around the detected face and text, not just their center.
    protected = []
    for x1, y1, x2, y2 in faces:
        pad_x, pad_y = (x2 - x1) * .16, (y2 - y1) * .26
        protected.append([max(0, x1 - pad_x), max(0, y1 - pad_y),
                          min(width, x2 + pad_x), min(height, y2 + pad_y)])
    if title_box:
        protected.append([max(0, title_box[0] - width * .03), max(0, title_box[1] - height * .02),
                          min(width, title_box[2] + width * .03), min(height, title_box[3] + height * .02)])
    # A portrait in a landscape cover is usually better as a whole frame than a
    # narrow facial crop, even when a detector misses the face.
    should_pad = preserve_frame or (width / height < .9 and target_ratio > 1.1)
    x, y = (width - crop_width) / 2., (height - crop_height) / 2.
    if protected:
        union = [min(b[0] for b in protected), min(b[1] for b in protected),
                 max(b[2] for b in protected), max(b[3] for b in protected)]
        should_pad |= union[2] - union[0] > crop_width or union[3] - union[1] > crop_height
        if not should_pad:
            if title_box:
                y = title_box[1] - crop_height * .10
            x = min(max(x, union[2] - crop_width, 0), union[0], width - crop_width)
            y = min(max(y, union[3] - crop_height, 0), union[1], height - crop_height)
    if should_pad:
        background = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
        background = ImageEnhance.Brightness(background.filter(ImageFilter.GaussianBlur(size[0] * .035))).enhance(.65)
        foreground = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
        offset = ((size[0] - foreground.width) // 2, (size[1] - foreground.height) // 2)
        background.paste(foreground, offset)
        scale = foreground.width / width
        transformed = [[round(b[0] * scale + offset[0]), round(b[1] * scale + offset[1]),
                        round(b[2] * scale + offset[0]), round(b[3] * scale + offset[1])] for b in faces]
        return background, transformed, "完整画面配模糊背景，保留人物和原有标题。"
    x, y = round(x), round(y)
    crop = image.crop((x, y, x + crop_width, y + crop_height)).resize(size, Image.Resampling.LANCZOS)
    transformed = [[round((b[0] - x) * size[0] / crop_width), round((b[1] - y) * size[1] / crop_height),
                    round((b[2] - x) * size[0] / crop_width), round((b[3] - y) * size[1] / crop_height)] for b in faces]
    return crop, transformed, "根据人物和标题位置裁切。"


def _wrap_text(title: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    # Keep a natural title break after a colon when both parts fit.
    for colon in ("：", ":"):
        if colon in title:
            prefix, suffix = title.split(colon, 1)
            proposed = [prefix + colon, suffix]
            if suffix and all(font.getlength(part) <= max_width for part in proposed):
                return proposed
    lines: list[str] = []
    current = ""
    for character in title:
        if character == "\n":
            lines.append(current)
            current = ""
        elif current and font.getlength(current + character) > max_width:
            lines.append(current)
            current = character
        else:
            current += character
    if current:
        lines.append(current)
    return lines or [title]


def _intersection(first: tuple[int, int, int, int], second: list[int]) -> int:
    return max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(0, min(first[3], second[3]) - max(first[1], second[1]))


def _draw_title(image: Image.Image, title: str, font_path: str, faces: list[list[int]]) -> tuple[Image.Image, dict]:
    width, height = image.size
    max_width = round(width * .82)
    font_size = max(20, round(min(width * .087, height * .085)))
    while True:
        font = ImageFont.truetype(font_path, font_size)
        lines = _wrap_text(title, font, max_width)
        ascent, descent = font.getmetrics()
        line_height = round((ascent + descent) * .98)
        block_height = line_height * len(lines)
        if block_height <= height * .32 or font_size <= 16:
            break
        font_size -= 2
    # Neutral high-contrast lettering is only applied when explicitly requested.
    stroke = max(2, round(font_size * .075))
    outer = stroke + max(2, round(font_size * .04))
    extrusion = max(2, round(font_size * .075))
    starts = [round(height * .11), round(height * .04), round(height * .64), round(height * .79 - block_height)]
    max_line_width = math.ceil(max(font.getlength(line) for line in lines))
    def overlap(start: int) -> float:
        box = ((width - max_line_width) // 2 - outer, start - outer,
               (width + max_line_width) // 2 + outer, start + block_height + outer + extrusion)
        return sum(_intersection(box, face) for face in faces)
    y = min(starts, key=lambda start: (overlap(start), abs(start - height * .11)))
    layer = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(layer)
    for index, line in enumerate(lines):
        line_width = font.getlength(line)
        x, baseline = (width - line_width) / 2, y + index * line_height
        # anchor=lt makes placement independent of the font's ascender offset.
        draw.text((x, baseline + extrusion), line, font=font, anchor="lt", fill="#111111",
                  stroke_width=outer + 2, stroke_fill="#ffffff")
        draw.text((x, baseline + extrusion), line, font=font, anchor="lt", fill="#111111",
                  stroke_width=stroke + 1, stroke_fill="#111111")
        draw.text((x, baseline), line, font=font, anchor="lt", fill="#ffffff",
                  stroke_width=stroke, stroke_fill="#111111")
    result = Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")
    return result, {"lines": lines, "font_size": font_size, "y": y, "face_overlap_pixels": int(overlap(y))}


def _contact_sheet(frames: list[Image.Image], candidates: list[dict], selected: int,
                   output: Path, font_path: str) -> None:
    cols = min(4, len(frames))
    cell_width, cell_height = 260, 315
    sheet = Image.new("RGB", (cols * cell_width, math.ceil(len(frames) / cols) * cell_height), "#17191d")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(font_path, 17)
    for index, (frame, candidate) in enumerate(zip(frames, candidates)):
        x, y = (index % cols) * cell_width, (index // cols) * cell_height
        thumb = ImageOps.contain(frame, (cell_width - 16, cell_height - 48))
        sheet.paste(thumb, (x + (cell_width - thumb.width) // 2, y + 7))
        label = f"{candidate['timestamp']:.2f}s  score {candidate['score']:.1f}"
        draw.text((x + 9, y + cell_height - 32), label, font=font, fill="#ffe12c" if index == selected else "white")
        if index == selected:
            draw.rectangle((x + 1, y + 1, x + cell_width - 2, y + cell_height - 2), outline="#ffe12c", width=4)
    sheet.save(output, quality=91)


def prepare_covers(video: Path, title: str, output_dir: Path, *, font_path: str | None = None,
                   frame_time: float | None = None, samples: int = 16, title_mode: str = "preserve") -> dict[str, Any]:
    """Create native, 3:4 and 4:3 covers plus a candidate contact sheet.

    ``title_mode`` is ``preserve`` (never draw new text), or ``add``
    (draw the supplied title). ``auto`` is a compatibility alias of ``preserve``.
    A manual ``frame_time`` bypasses candidate selection. Returned paths are
    absolute and every value is JSON serializable. No video pixels are generated.
    """
    video = Path(video).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    title = str(title).strip()
    if not title:
        raise MediaError("封面标题不能为空。")
    if title_mode not in {"auto", "add", "preserve"}:
        raise MediaError("title_mode 必须是 auto、add 或 preserve。")
    if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 64:
        raise MediaError("samples 必须是 1 到 64 的整数。")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise MediaError("缺少 FFmpeg / ffprobe，请先安装 FFmpeg。")
    metadata = probe_video(video)
    if frame_time is not None:
        if not math.isfinite(frame_time) or not 0 <= frame_time < metadata["duration"]:
            raise MediaError(f"frame_time 必须在 0 到 {metadata['duration']:.2f} 秒之间（不含结尾）。")
        times = [min(float(frame_time), max(0., metadata["duration"] - 1.05 / metadata["fps"]))]
    else:
        times = _sample_times(metadata["duration"], samples, 1 / metadata["fps"])
    font = _resolve_font(font_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    def sample(timestamp: float) -> tuple[Image.Image, dict]:
        frame = _extract_frame(video, timestamp, metadata, 640)
        stats = _analyze_frame(frame)
        stats["timestamp"] = timestamp
        return frame, stats
    with ThreadPoolExecutor(max_workers=min(3, len(times))) as executor:
        sampled = list(executor.map(sample, times))
    frames, candidates = [s[0] for s in sampled], [s[1] for s in sampled]
    selected = max(range(len(candidates)), key=lambda index: candidates[index]["score"])
    best = candidates[selected]
    image = _extract_frame(video, best["timestamp"], metadata)
    faces = _scale_boxes(best["faces"], tuple(best["analysis_size"]), image.size)
    detection = _detect_existing_title(image)
    preserve = title_mode in {"preserve", "auto"}
    title_box = detection["bbox"] if preserve else None
    raw_frame = output_dir / "selected-frame.jpg"
    image.save(raw_frame, quality=96)
    layouts = {}
    files = {}
    for kind, size in (("native", image.size), ("vertical", (1080, 1440)), ("horizontal", (1440, 1080))):
        if kind == "native":
            cover, cover_faces, layout = image.copy(), faces, "视频原始画幅。"
        else:
            cover, cover_faces, layout = _fit_protected(image, size, faces, title_box,
                                                       preserve_frame=preserve and title_box is None)
        title_info = None
        if not preserve:
            cover, title_info = _draw_title(cover, title, font, cover_faces)
        target = output_dir / f"cover-{kind}.jpg"
        cover.save(target, quality=95, subsampling=0)
        files[kind] = str(target)
        layouts[kind] = {"size": list(size), "layout": layout, "title": title_info}
    contact_sheet = output_dir / "frame-candidates.jpg"
    _contact_sheet(frames, candidates, selected, contact_sheet, font)
    warnings = ["帧选择只比较清晰度、曝光及轻量人脸/眼睛检测，不判断动作含义或表情；请检查候选图。"]
    if preserve and detection["detected"]:
        warnings.append("原画面疑似已含标题，已保留；未进行 OCR，无法自动确认它与本次标题完全一致。")
    elif preserve:
        warnings.append("已按 preserve 保留原画面，本次没有叠加标题。")
    else:
        warnings.append("已按 add 叠加黑白标题；请检查字体、换行和实际可读性。")
    if any(layout["title"] and layout["title"]["face_overlap_pixels"] for layout in layouts.values()):
        warnings.append("至少一张封面标题与检测到的人脸区域重叠，请预览或手动选择其他帧。")
    if frame_time is not None and times[0] != frame_time:
        warnings.append(f"指定时间位于最后一帧之后，已改取 {times[0]:.3f} 秒附近的可解码画面。")
    reason = (f"手动选择 {best['timestamp']:.2f} 秒画面。" if frame_time is not None else
              f"从 {len(candidates)} 个时间点中，按清晰度、曝光、人脸与眼睛检测加权，并对疑似烧录字幕扣分，选择 {best['timestamp']:.2f} 秒画面。")
    return {
        "video": str(video), "title": title, **files,
        "raw_frame": str(raw_frame), "contact_sheet": str(contact_sheet),
        "timestamp": best["timestamp"], "duration": metadata["duration"],
        "font": font, "font_family": ImageFont.truetype(font, 40).getname()[0],
        "selection_reason": reason, "title_mode": title_mode,
        "title_preserved": preserve, "title_detection": detection,
        "hdr_tone_mapped": metadata["hdr"],
        "hdr_transform": "HLG/PQ → BT.709/sRGB，1000-nit 参考显示与自动亮度映射" if metadata["hdr"] else None,
        "layouts": layouts,
        "warnings": warnings, "candidates": candidates,
    }

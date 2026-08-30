from __future__ import annotations

from pathlib import Path
from typing import Any


def rect_pixels(rect: dict[str, float], width: int, height: int) -> tuple[int, int, int, int]:
    left = max(0, min(width - 1, round(rect["x"] * width)))
    top = max(0, min(height - 1, round(rect["y"] * height)))
    right = max(left + 1, min(width, round((rect["x"] + rect["w"]) * width)))
    bottom = max(top + 1, min(height, round((rect["y"] + rect["h"]) * height)))
    return left, top, right, bottom


def panel_generation_size(rect: dict[str, float], width: int, height: int) -> tuple[int, int]:
    target_width = max(1, round(rect["w"] * width))
    target_height = max(1, round(rect["h"] * height))
    scale = max(1.0, 256.0 / min(target_width, target_height))
    generated_width = max(256, min(4096, round(target_width * scale / 64) * 64))
    generated_height = max(256, min(4096, round(target_height * scale / 64) * 64))
    return generated_width, generated_height


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        Path("C:/Windows/Fonts/malgunbd.ttf" if bold else "C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(str(path), max(8, int(size)))
        except Exception:
            continue
    return ImageFont.load_default()


def _text_length(draw, text: str, font) -> float:
    box = draw.textbbox((0, 0), text or " ", font=font)
    return float(box[2] - box[0])


def _wrap_text(draw, text: str, font, max_width: int) -> str:
    output: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        words = paragraph.split(" ") if " " in paragraph else list(paragraph)
        separator = " " if " " in paragraph else ""
        line = ""
        for word in words:
            candidate = f"{line}{separator if line else ''}{word}"
            if line and _text_length(draw, candidate, font) > max_width:
                output.append(line)
                line = word
            else:
                line = candidate
        output.append(line)
    return "\n".join(output)


def _fit_text(draw, text: str, start_size: int, max_width: int, max_height: int, *, bold=False):
    size = max(8, int(start_size))
    while size >= 8:
        font = _font(size, bold=bold)
        wrapped = _wrap_text(draw, text, font, max_width)
        box = draw.multiline_textbbox((0, 0), wrapped or " ", font=font, spacing=max(1, size // 5))
        if box[2] - box[0] <= max_width and box[3] - box[1] <= max_height:
            return font, wrapped
        size -= 1
    font = _font(8, bold=bold)
    return font, _wrap_text(draw, text, font, max_width)


def _rotated_layer(layer, degrees: float, center: tuple[float, float]):
    if abs(float(degrees or 0)) < 0.01:
        return layer
    return layer.rotate(float(degrees), resample=2, center=center, expand=False)


def font_pixels(font_scale: float, short_side: int, *, base_ratio: float) -> int:
    """Support both old normalized sizes and the current 1.0-based multiplier."""
    scale = float(font_scale)
    ratio = scale if scale <= 0.2 else base_ratio * scale
    return max(8, round(ratio * int(short_side)))


def _draw_bubble(canvas, bubble: dict[str, Any]) -> None:
    from PIL import Image, ImageDraw

    width, height = canvas.size
    left, top, right, bottom = rect_pixels(bubble["rect"], width, height)
    raw_tail = bubble.get("tail")
    tail = (
        (round(raw_tail["x"] * width), round(raw_tail["y"] * height))
        if isinstance(raw_tail, dict) else ((left + right) // 2, bottom)
    )
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    outline_width = max(2, round(min(width, height) * 0.003))
    fill = (255, 255, 255, 238)
    outline = (20, 20, 24, 255)
    base_x = min(right - outline_width, max(left + outline_width, tail[0]))
    draw.polygon(
        [(base_x - outline_width * 3, bottom - outline_width),
         (base_x + outline_width * 3, bottom - outline_width), tail],
        fill=fill,
        outline=outline,
    )
    style = str(bubble.get("style") or "round").lower()
    if style in {"round", "oval", "thought"}:
        draw.ellipse((left, top, right, bottom), fill=fill, outline=outline, width=outline_width)
    else:
        radius = max(6, min(right - left, bottom - top) // 8)
        draw.rounded_rectangle(
            (left, top, right, bottom), radius=radius, fill=fill, outline=outline, width=outline_width
        )
    padding = max(5, outline_width * 3)
    start_size = font_pixels(bubble["font_scale"], min(width, height), base_ratio=0.035)
    font, wrapped = _fit_text(
        draw,
        bubble.get("text", ""),
        start_size,
        max(1, right - left - padding * 2),
        max(1, bottom - top - padding * 2),
    )
    draw.multiline_text(
        ((left + right) / 2, (top + bottom) / 2),
        wrapped,
        font=font,
        fill=(15, 15, 18, 255),
        anchor="mm",
        align="center",
        spacing=max(1, getattr(font, "size", 10) // 5),
    )
    layer = _rotated_layer(layer, bubble.get("rotation", 0), ((left + right) / 2, (top + bottom) / 2))
    canvas.alpha_composite(layer)


def _parse_color(raw: Any) -> tuple[int, int, int, int]:
    text = str(raw or "#111111").strip().lstrip("#")
    if len(text) == 6:
        try:
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), 255
        except ValueError:
            pass
    return 20, 20, 24, 255


def _draw_sound_effect(canvas, effect: dict[str, Any]) -> None:
    from PIL import Image, ImageDraw

    width, height = canvas.size
    anchor = (
        round(effect["anchor"]["x"] * width),
        round(effect["anchor"]["y"] * height),
    )
    max_width = max(1, round(float(effect["max_width"]) * width))
    start_size = font_pixels(effect["font_scale"], min(width, height), base_ratio=0.07)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font, wrapped = _fit_text(draw, effect.get("text", ""), start_size, max_width, height, bold=True)
    stroke = max(1, round(getattr(font, "size", 12) * 0.09))
    draw.multiline_text(
        anchor,
        wrapped,
        font=font,
        fill=_parse_color(effect.get("color")),
        stroke_width=stroke,
        stroke_fill=(255, 255, 255, 245),
        anchor="mm",
        align="center",
    )
    layer = _rotated_layer(layer, effect.get("rotation", 0), anchor)
    canvas.alpha_composite(layer)


def compose_page(
    width: int,
    height: int,
    page: dict[str, Any],
    panel_images: dict[str, Any],
    *,
    text_mode: str,
):
    from PIL import Image, ImageDraw, ImageOps

    canvas = Image.new("RGBA", (int(width), int(height)), (18, 18, 20, 255))
    draw = ImageDraw.Draw(canvas)
    border = max(2, round(min(width, height) * 0.005))
    panels = page.get("_render_panels") or page.get("panels") or []
    for panel in panels:
        image = panel_images.get(panel["id"])
        if image is None:
            raise ValueError(f"panel image missing: {panel['id']}")
        left, top, right, bottom = rect_pixels(panel["rect"], width, height)
        fitted = ImageOps.fit(image.convert("RGB"), (right - left, bottom - top), method=Image.Resampling.LANCZOS)
        canvas.paste(fitted.convert("RGBA"), (left, top))
        draw.rectangle((left, top, right - 1, bottom - 1), outline=(8, 8, 10, 255), width=border)

    if str(text_mode).lower() == "overlay":
        for bubble in page.get("bubbles") or []:
            _draw_bubble(canvas, bubble)
        for effect in page.get("sound_effects") or []:
            _draw_sound_effect(canvas, effect)
    return canvas.convert("RGB")

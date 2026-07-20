from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from PIL import ImageFont


def _is_emoji_codepoint(char: str) -> bool:
    value = ord(char)
    return (
        0x1F000 <= value <= 0x1FAFF
        or 0x2600 <= value <= 0x27BF
        or 0x2300 <= value <= 0x23FF
        or 0x1F1E6 <= value <= 0x1F1FF
        or value in {0x200D, 0x20E3, 0xFE0E, 0xFE0F}
    )


def author_rich_spans(
    author: str,
    *,
    font_size: float = 4.8,
    emoji_scale: float = 1.0,
) -> list[dict[str, Any]]:
    """Cover the full string using JianYing's UTF-16 text-range offsets."""
    if not author:
        return []
    spans: list[dict[str, Any]] = []
    start = 0
    utf16_cursor = len(author[0].encode("utf-16-le")) // 2
    emoji = _is_emoji_codepoint(author[0])
    for index, char in enumerate(author[1:], 1):
        current = _is_emoji_codepoint(char)
        if current == emoji:
            utf16_cursor += len(char.encode("utf-16-le")) // 2
            continue
        spans.append({
            "start": start,
            "end": utf16_cursor,
            "size": round(font_size * emoji_scale if emoji else font_size, 3),
            "emoji": emoji,
        })
        start = utf16_cursor
        emoji = current
        utf16_cursor += len(char.encode("utf-16-le")) // 2
    spans.append({
        "start": start,
        "end": utf16_cursor,
        "size": round(font_size * emoji_scale if emoji else font_size, 3),
        "emoji": emoji,
    })
    return spans


def author_text_style(author: str, base_size: float = 4.8) -> dict[str, Any]:
    """Return a fixed-size, single-line style for a creator identifier."""
    visual_units = sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in author
    )
    return {
        "font_size": round(base_size, 3),
        "auto_wrapping": False,
        "max_line_width": 1.0,
        "visual_units": visual_units,
    }


def author_center_transform_x(
    author: str,
    *,
    font_path: str = "",
    font_size: float = 4.8,
    canvas_width: int = 1080,
    right_edge_transform_x: float = 0.92,
) -> float:
    """Convert a fixed right edge into JianYing's center-based transform X."""
    pixel_font_size = max(1, round(font_size * 10.0))
    try:
        path = Path(font_path).expanduser()
        if not font_path or not path.exists():
            raise OSError("font unavailable")
        font = ImageFont.truetype(str(path), pixel_font_size)
        left, _, right, _ = font.getbbox(author)
        text_width = float(max(0, right - left))
    except OSError:
        visual_units = sum(
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            for char in author
        )
        text_width = visual_units * pixel_font_size * 0.55
    transform_x = right_edge_transform_x - text_width / max(1, canvas_width)
    return round(max(-1.0, min(1.0, transform_x)), 6)

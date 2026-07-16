from __future__ import annotations

import re


def validate_cover_title(title: str, min_chars: int = 6, max_chars: int = 16) -> list[str]:
    compact = re.sub(r"[\s\n，。！？,.!?]", "", title)
    errors: list[str] = []
    if not min_chars <= len(compact) <= max_chars:
        errors.append(f"cover title must contain {min_chars}-{max_chars} characters")
    if title.count("\n") > 1:
        errors.append("cover title must use at most two lines")
    banned = ("全网最强", "不看后悔", "精彩内容", "此生必去")
    if any(term in title for term in banned):
        errors.append("cover title contains prohibited generic or exaggerated copy")
    return errors


def split_title(title: str, max_lines: int = 2) -> list[str]:
    text = title.strip().replace("\n", "")
    if max_lines == 1 or len(text) <= 9:
        return [text]
    midpoint = len(text) // 2
    return [text[:midpoint], text[midpoint:]]


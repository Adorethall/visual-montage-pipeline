from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path

import httpx


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _json_content(text: str) -> dict:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        value = "\n".join(lines)
    return json.loads(value)


def review_video_candidates(video: Path, prompt: str, timeout: float = 300) -> dict:
    base = os.environ["LLM_API_BASE"].rstrip("/")
    key = os.environ["LLM_API_KEY"]
    response = httpx.post(
        f"{base}/chat/completions",
        json={
            "model": os.getenv("GEMMA_MODEL", "google/gemma-4-12B-it"),
            "messages": [
                {"role": "system", "content": "Validate visual montage candidates. Return valid JSON only."},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "video_url", "video_url": {"url": _data_url(video)}},
                ]},
            ],
            "temperature": 0.1,
            "max_tokens": 6000,
        },
        headers={"Authorization": f"Bearer {key}"}, timeout=timeout,
    )
    response.raise_for_status()
    return _json_content(response.json()["choices"][0]["message"]["content"])


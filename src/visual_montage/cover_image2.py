from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image


_RINGS_SUBMIT_LOCK = threading.Lock()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image2_prompt(
    *,
    title: str,
    category: str,
    language: str,
    style: str,
    reserve_logo_safe_zone: bool,
) -> str:
    logo_lock = """
RESERVED BRAND SAFE ZONE
- The top-left logo area is an INVISIBLE exclusion zone for a local post-processing logo overlay; it is not a design element.
- Keep the image pixels in this area natural and unchanged. Do not create a blank panel or a visible reserved area.
- Never render, imply, or mark its boundaries: no dashed or solid box, rectangle, frame, outline, guide, bounding box, placeholder, safe-area indicator, selection marquee, crop marks, UI controls, or editing handles.
- Do not place headline, decoration, sparkle, line, sticker, glow, or high-contrast graphic inside approximately x=4%-39% and y=4%-13% of the canvas.
- Do not generate, imitate, redraw, or add any logo, brand mark, watermark, app name, icon, or extra text anywhere.
""" if reserve_logo_safe_zone else ""
    return f"""Edit the supplied image into a polished, visually rich 9:16 {language} Xiaohongshu / RedNote cover for the {category} category.

CANVAS
- Keep the final canvas strictly 9:16, targeting 1080x1920.

LOCKED SOURCE CONTENT — HIGHEST PRIORITY
- Treat the supplied image as a locked background beneath a separate graphic-overlay layer.
- Preserve every physical entity and all source pixels as faithfully as possible. Do not modify, redraw, regenerate, replace, retouch, beautify, reshape, recolor, move, crop, extend, remove, or relight any person, facial identity, facial feature, skin, makeup, hair, body, hand, pose, clothing, product, packaging, label, food, object, action, background element, texture, reflection, shadow, highlight, color, lighting, or framing.
- Do not change product shape, proportions, color, packaging text, branding, or visible details. Do not invent or remove people, products, objects, ingredients, accessories, physical details, claims, logos, watermarks, or UI elements.
- Only add non-destructive typography and flat graphic overlays above the original image. If visual richness conflicts with preservation of the source image, preserve the source image.
{logo_lock}

EXACT TITLE PACKAGING
- Add exactly this {language} headline, with identical spelling and capitalization: "{title}"
- Create the complete cover typography directly in the image. The headline may wrap when useful and must remain clearly readable at mobile-thumbnail size.
- Choose the typography style, font personality, hierarchy, scale, layout, palette, graphic accents, and decorative language yourself according to this specific image and its subject.
- Do not follow a predetermined hand-drawn, brush, sticker, serif, sans-serif, or editorial style. Make an image-specific art-direction decision.
- You may add one or two extremely short {language} supporting phrases or labels only when they genuinely improve the composition and remain grounded in visible content. Do not add unsupported claims.
- Do not cover any face, eyes, hands, product, packaging text, logo safe zone, creator ID, or important action or costume detail.

VISUAL DIRECTION
- {style}
- Make the result layered, expressive, energetic, and professionally art-directed rather than sparse or minimal. Use a clear primary headline, supporting visual hierarchy, balanced negative space, and multiple coordinated design details.
- Keep the composition rich but organized, premium, and readable. Avoid a cover consisting of only one plain text line.

NEGATIVE CONSTRAINTS
- Do not add Chinese text unless it already exists in the locked source image. Do not add misspelled words, duplicate letters, extra logos, new watermarks, QR codes, UI chrome, design guides, selection borders, dashed frames, placeholder boxes, or edit-mode artifacts.
- Do not alter existing source text, logos, creator IDs, or physical entities.

Return one finished cover image only."""


def _valid_cover(path: Path) -> tuple[bool, str]:
    if not path.is_file() or path.stat().st_size == 0:
        return False, "Image2 did not produce an output image"
    try:
        with Image.open(path) as image:
            width, height = image.size
            image.verify()
    except Exception as exc:
        return False, f"Image2 output is unreadable: {exc}"
    ratio = width / height
    if abs(ratio - 9 / 16) > 0.035:
        return False, f"Image2 output has unexpected aspect ratio: {width}x{height}"
    return True, ""


def generate_image2_cover(
    *,
    source_image: Path,
    output_image: Path,
    title: str,
    category: str,
    language: str,
    config: dict[str, Any],
    cache_dir: Path,
    reserve_logo_safe_zone: bool = False,
) -> dict[str, Any]:
    task_key = str(config.get("task_key") or "gpt-image2")
    request_timeout_seconds = float(config.get("request_timeout_seconds", 120))
    style = str(
        config.get("style_direction")
        or "Trendy RedNote editorial cover with clean, high-impact typography."
    )
    prompt = _image2_prompt(
        title=title,
        category=category,
        language=language,
        style=style,
        reserve_logo_safe_zone=reserve_logo_safe_zone,
    )
    payload = {
        "prompt": prompt,
        "images": [str(source_image.resolve())],
        "image_size": None,
        "size": str(config.get("size") or "1080x1920"),
        "quality": str(config.get("quality") or "high"),
        "output_format": "png",
        "background": "opaque",
        "request_timeout_seconds": request_timeout_seconds,
    }
    identity = hashlib.sha256(
        json.dumps(
            {
                "source": _sha256_file(source_image),
                "task_key": task_key,
                "payload": payload,
                "cache_version": 5,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    cache_path = cache_dir / f"{identity}.png"
    output_image.parent.mkdir(parents=True, exist_ok=True)
    payload_path = output_image.with_name("image2-payload.json")
    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    valid_cache, _ = _valid_cover(cache_path)
    if valid_cache:
        shutil.copy2(cache_path, output_image)
        return {
            "ok": True,
            "provider": "gpt-image2",
            "task_key": task_key,
            "task_id": None,
            "cache_hit": True,
            "title_embedded": True,
            "logo_embedded": False,
            "prompt_path": str(payload_path),
            "output_path": str(output_image),
        }
    rings = shutil.which("rings")
    if not rings:
        raise RuntimeError("Image2 cover is enabled but the rings CLI is not available")
    retry_delays = [
        float(value)
        for value in config.get(
            "submission_retry_delays_seconds",
            [15, 30, 60, 120],
        )
    ]
    # Rings performs local state/skill checks before submission. Serializing this
    # step avoids both its local lock and a thundering herd when the provider's
    # account-level concurrency counter has not released a previous task yet.
    with _RINGS_SUBMIT_LOCK:
        for attempt in range(len(retry_delays) + 1):
            run = subprocess.run(
                [rings, "task", "run", task_key, "--input-file", str(payload_path), "--bg"],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            combined = f"{run.stdout}\n{run.stderr}"
            if run.returncode == 0:
                break
            concurrency_limited = "concurrency limit exceeded" in combined.lower()
            if not concurrency_limited or attempt >= len(retry_delays):
                raise RuntimeError(
                    f"Image2 submission failed with exit {run.returncode}: "
                    f"{combined.strip() or 'no Rings CLI output'}"
                )
            delay = retry_delays[attempt]
            print(
                f"Image2 provider concurrency is full; retrying submission "
                f"in {delay:g}s ({attempt + 1}/{len(retry_delays)})",
                flush=True,
            )
            time.sleep(delay)
    match = re.search(r"^TASK_ID=(\S+)$", combined, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Unable to read Image2 task id: {combined.strip()}")
    task_id = match.group(1)
    try:
        watch = subprocess.run(
            [rings, "task", "watch", task_id, "--save-to", str(output_image)],
            check=False,
            capture_output=True,
            text=True,
            timeout=request_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output_image.unlink(missing_ok=True)
        raise RuntimeError(
            f"Image2 task {task_id} produced no image within "
            f"{request_timeout_seconds:g}s; using fallback cover"
        ) from exc
    if watch.returncode != 0:
        detail = (watch.stderr or watch.stdout).strip()
        status = subprocess.run(
            [rings, "task", "get", task_id, "--output", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if status.returncode == 0:
            try:
                envelope = json.loads(status.stdout)
                detail = str(
                    envelope.get("error_message")
                    or envelope.get("status")
                    or detail
                )
            except json.JSONDecodeError:
                detail = status.stdout.strip() or detail
        raise RuntimeError(f"Image2 task {task_id} failed: {detail}")
    valid, error = _valid_cover(output_image)
    if not valid:
        raise RuntimeError(error)
    cache_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(output_image, cache_path)
    return {
        "ok": True,
        "provider": "gpt-image2",
        "task_key": task_key,
        "task_id": task_id,
        "cache_hit": False,
        "title_embedded": True,
        "logo_embedded": False,
        "prompt_path": str(payload_path),
        "output_path": str(output_image),
    }

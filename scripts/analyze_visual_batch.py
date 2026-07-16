#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from visual_montage.io import load_manifest, load_yaml, write_json
from visual_montage.candidate_registry import CandidateRegistry
from visual_montage.storage import get_storage


def render_prompt(
    template: str,
    profile: dict,
    video_id: str,
    duration: float,
    marlin_recall: list[dict] | None = None,
) -> str:
    review = profile["gemma_review"]
    events = review["events"]
    duration_rule = review["candidate_duration"]
    count_rule = review["candidate_count"]
    event_catalog = "\n".join(
        f"- {event_id}: {event['label']}; keywords: {', '.join(event.get('keywords') or [])}"
        for event_id, event in events.items()
    )
    replacements = {
        "event_catalog": event_catalog,
        "positive_visual_keywords": "\n".join(
            f"- {value}" for value in review["positive_visual_keywords"]
        ),
        "negative_visual_keywords": "\n".join(
            f"- {value}" for value in review["negative_visual_keywords"]
        ),
        "video_id": video_id,
        "duration": f"{duration:.3f}",
        "candidate_count_min": str(count_rule["minimum"]),
        "candidate_count_max": str(count_rule["maximum"]),
        "candidate_duration_min": str(duration_rule["minimum"]),
        "candidate_duration_max": str(duration_rule["maximum"]),
        "marlin_recall": (
            json.dumps(marlin_recall, ensure_ascii=False, indent=2)
            if marlin_recall
            else "Marlin was not used for this video. Review the complete video directly."
        ),
    }
    output = template
    for key, value in replacements.items():
        output = output.replace("{{" + key + "}}", value)
    return output


def should_use_marlin(profile: dict, duration: float) -> bool:
    config = profile.get("marlin_recall") or {}
    return bool(config.get("enabled")) and duration >= float(
        config.get("minimum_video_duration_seconds", 30.0)
    )


def load_marlin_queries(project_root: Path, profile: dict) -> list[dict]:
    config = profile["marlin_recall"]
    query_file = project_root / config["query_path"]
    groups = (load_yaml(query_file).get("query_groups") or [])[
        : int(config.get("max_query_groups", 4))
    ]
    variant = config.get("query_variant", "normal")
    output = []
    for group in groups:
        query = group.get(variant) or group.get("normal") or group.get("broad")
        if query:
            output.append({"query_group": group["id"], "query": query})
    return output


def call_marlin(
    proxy: Path,
    material,
    profile: dict,
    project_root: Path,
) -> dict:
    from worker_stubs.marlin import MarlinFindInput, marlin_find_stub

    config = profile["marlin_recall"]
    digest = hashlib.sha1(str(material.path).encode("utf-8")).hexdigest()[:12]
    key = (
        f"{str(config.get('upload_prefix', 'visual-montage/marlin-inputs')).strip('/')}/"
        f"{material.video_id}-{digest}.mp4"
    )
    uploaded = get_storage().upload_for_worker(
        proxy,
        key,
        int(config.get("presigned_url_expires_seconds", 86400)),
    )
    results = []
    for query in load_marlin_queries(project_root, profile):
        result = marlin_find_stub.run(
            input=MarlinFindInput(
                video_url=uploaded["public_url"],
                event=query["query"],
                temperature=0.1,
            )
        )
        results.append(
            {
                **query,
                "ok": result.ok,
                "scene": result.scene,
                "events": result.events,
                "span": result.span,
                "raw": result.raw,
                "status": result.status,
            }
        )
    return {"uploaded": uploaded, "queries": results}


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def compress(path: Path, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path), "-vf", "fps=3,scale=480:-2", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "34",
            "-movflags", "+faststart", str(output),
        ],
        check=True,
    )


def extract_candidate_frame(
    video: Path,
    peak_time: float,
    output: Path,
    width: int,
    height: int,
) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{peak_time:.3f}", "-i", str(video),
            "-frames:v", "1",
            "-vf",
            (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            ),
            str(output),
        ],
        check=True,
    )


def generate_contact_sheet(
    candidates: list[dict],
    output: Path,
    profile: dict,
) -> dict:
    config = profile.get("contact_sheet") or {}
    if not config.get("enabled", False):
        return {"enabled": False, "generated": False}
    if not candidates:
        return {"enabled": True, "generated": False, "reason": "no_candidates"}

    columns = max(1, int(config.get("columns", 4)))
    thumb_width = int(config.get("thumbnail_width", 320))
    thumb_height = int(config.get("thumbnail_height", 480))
    label_height = int(config.get("label_height", 86))
    rows = (len(candidates) + columns - 1) // columns
    background = str(config.get("background_color", "#151515"))
    primary = str(config.get("label_color", "#F5F5F5"))
    secondary = str(config.get("secondary_label_color", "#B8B8B8"))
    canvas = Image.new(
        "RGB",
        (columns * thumb_width, rows * (thumb_height + label_height)),
        background,
    )
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=18)
    small_font = ImageFont.load_default(size=15)
    failures = []

    with tempfile.TemporaryDirectory(prefix="visual-montage-contact-sheet-") as tmp:
        temp_dir = Path(tmp)
        for index, candidate in enumerate(candidates):
            row, column = divmod(index, columns)
            x = column * thumb_width
            y = row * (thumb_height + label_height)
            frame = temp_dir / f"{index:04d}.jpg"
            try:
                extract_candidate_frame(
                    Path(candidate["video_path"]),
                    float(candidate["peak_time"]),
                    frame,
                    thumb_width,
                    thumb_height,
                )
                with Image.open(frame) as image:
                    canvas.paste(image.convert("RGB"), (x, y))
            except Exception as exc:
                failures.append(
                    {"candidate_id": candidate["candidate_id"], "error": str(exc)}
                )
                draw.rectangle(
                    (x, y, x + thumb_width, y + thumb_height),
                    fill="#303030",
                )
                draw.text((x + 12, y + 12), "FRAME EXTRACTION FAILED", fill=primary, font=font)

            confidence = float(candidate.get("confidence") or 0.0)
            video_id = str(candidate["video_id"])
            short_video_id = video_id if len(video_id) <= 12 else video_id[-12:]
            line_1 = f"{index + 1:02d}  {candidate['event']}"
            line_2 = (
                f"src=...{short_video_id}  t={float(candidate['peak_time']):.2f}s  "
                f"conf={confidence:.2f}"
            )
            draw.text((x + 10, y + thumb_height + 8), line_1, fill=primary, font=font)
            draw.text(
                (x + 10, y + thumb_height + 42),
                line_2,
                fill=secondary,
                font=small_font,
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(
        output,
        "JPEG",
        quality=int(config.get("jpeg_quality", 88)),
        optimize=True,
    )
    return {
        "enabled": True,
        "generated": True,
        "path": str(output),
        "candidate_count": len(candidates),
        "frame_failure_count": len(failures),
        "frame_failures": failures,
        "columns": columns,
        "rows": rows,
        "width": canvas.width,
        "height": canvas.height,
    }


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def parse_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines)
    return json.loads(text)


def call_gemma(
    video: Path,
    prompt_text: str,
    timeout: float,
) -> tuple[dict, dict]:
    base = os.environ["LLM_API_BASE"].rstrip("/")
    key = os.environ["LLM_API_KEY"]
    model = os.getenv("GEMMA_MODEL", "google/gemma-4-12B-it")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return valid JSON only. Follow the supplied category review specification exactly.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "video_url", "video_url": {"url": data_url(video)}},
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": 5000,
    }
    response = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    envelope = response.json()
    return parse_json(envelope["choices"][0]["message"]["content"]), envelope


def normalize(raw: dict, material, duration: float, profile: dict) -> list[dict]:
    review = profile["gemma_review"]
    allowed_events = set(review["events"])
    default_event = review["default_event"]
    output = []
    for index, item in enumerate(raw.get("candidates") or [], 1):
        try:
            start = max(0.0, float(item["start"]))
            end = min(duration, float(item["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start or end - start < 0.4:
            continue
        peak = float(item.get("peak_time") or ((start + end) / 2))
        scores = {
            key: max(0.0, min(1.0, float(item.get(key) or 0.0)))
            for key in ("aesthetic", "payoff", "action_intensity", "subject_visibility")
        }
        output.append(
            {
                "candidate_id": f"{material.video_id}_gemma_{index:02d}",
                "video_id": material.video_id,
                "video_path": material.path,
                "event": (
                    str(item.get("event"))
                    if str(item.get("event")) in allowed_events
                    else default_event
                ),
                "source_window": {"start": round(start, 3), "end": round(end, 3)},
                "preferred_trim": {"start": round(start, 3), "end": round(end, 3)},
                "peak_time": round(max(start, min(end, peak)), 3),
                "description": str(item.get("description") or ""),
                "scores": {
                    **scores,
                    "category_event_value": 1.0,
                    "sharpness": 0.7,
                    "composition": 0.7,
                    "context_independence": 0.85,
                },
                "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
                "risks": list(item.get("risks") or []),
                "penalties": {},
                "roles": ["visual_montage"],
            }
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--category", default="beauty")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    args = parser.parse_args()

    load_dotenv(args.env_file)
    project_root = Path(__file__).resolve().parents[1]
    profile_path = args.profile or project_root / "profiles" / "categories" / f"{args.category}.yaml"
    profile = load_yaml(profile_path)
    if profile.get("category_id") != args.category:
        raise SystemExit(f"Profile category_id does not match --category: {profile_path}")
    review = profile.get("gemma_review")
    if not review:
        raise SystemExit(f"Profile has no gemma_review configuration: {profile_path}")
    prompt_path = args.prompt_file or project_root / review["prompt_path"]
    prompt_template = prompt_path.read_text(encoding="utf-8")
    selected = [
        item for item in load_manifest(args.manifest)
        if item.enabled and item.category == args.category
    ][: args.limit]
    if not selected:
        raise SystemExit("No matching materials")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = args.output_dir / "raw"
    proxy_dir = args.output_dir / "proxies"
    raw_dir.mkdir(exist_ok=True)
    proxy_dir.mkdir(exist_ok=True)
    combined = []
    failures = []
    routing_summary = {"marlin": 0, "gemma_only": 0, "marlin_fallback": 0}
    history_config = ((profile.get("batch_generation") or {}).get("history") or {})
    registry_path = Path(
        history_config.get("registry_path", "data/catalog/candidate-registry.sqlite")
    )
    if not registry_path.is_absolute():
        registry_path = project_root / registry_path

    for position, material in enumerate(selected, 1):
        started = time.monotonic()
        try:
            duration = probe_duration(Path(material.path))
            proxy = proxy_dir / f"{material.video_id}.mp4"
            compress(Path(material.path), proxy)
            marlin = None
            route = "gemma_only"
            if should_use_marlin(profile, duration):
                try:
                    marlin = call_marlin(proxy, material, profile, project_root)
                    route = "marlin_then_gemma"
                    routing_summary["marlin"] += 1
                except Exception as exc:
                    if not profile["marlin_recall"].get("fail_open", True):
                        raise
                    marlin = {"error": str(exc), "queries": []}
                    route = "marlin_failed_gemma_fallback"
                    routing_summary["marlin_fallback"] += 1
            else:
                routing_summary["gemma_only"] += 1
            prompt_text = render_prompt(
                prompt_template,
                profile,
                material.video_id,
                duration,
                (marlin or {}).get("queries"),
            )
            analysis, envelope = call_gemma(proxy, prompt_text, args.timeout)
            candidates = normalize(analysis, material, duration, profile)
            if history_config.get("enabled", False):
                with CandidateRegistry(registry_path) as registry:
                    candidates = registry.register(candidates, args.category)
            combined.extend(candidates)
            write_json(
                raw_dir / f"{material.video_id}.json",
                {
                    "material": material.model_dump(),
                    "duration_seconds": duration,
                    "analysis": analysis,
                    "candidates": candidates,
                    "profile_path": str(profile_path),
                    "prompt_path": str(prompt_path),
                    "analysis_route": route,
                    "marlin": marlin,
                    "usage": envelope.get("usage"),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                },
            )
            print(
                f"[{position}/{len(selected)}] {material.video_id}: "
                f"{len(candidates)} candidates ({route})",
                flush=True,
            )
        except Exception as exc:
            failures.append({"video_id": material.video_id, "path": material.path, "error": str(exc)})
            print(f"[{position}/{len(selected)}] {material.video_id}: FAILED {exc}", flush=True)

    contact_sheet = generate_contact_sheet(
        combined,
        args.output_dir / "contact-sheet.jpg",
        profile,
    )
    write_json(
        args.output_dir / "candidate-pool.json",
        {
            "schema_version": "1.0",
            "category": args.category,
            "video_count": len(selected),
            "candidate_count": len(combined),
            "candidates": combined,
            "failures": failures,
            "routing_summary": routing_summary,
            "contact_sheet": contact_sheet,
            "candidate_registry": {
                "enabled": bool(history_config.get("enabled", False)),
                "path": str(registry_path),
            },
        },
    )
    print(
        json.dumps(
            {
                "videos": len(selected),
                "candidates": len(combined),
                "failures": len(failures),
                "routing": routing_summary,
                "contact_sheet": contact_sheet,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

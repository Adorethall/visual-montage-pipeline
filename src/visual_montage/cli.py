from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .batch import batch_compose
from .candidate_registry import CandidateRegistry
from .cover import validate_cover_title
from .io import load_campaign, load_manifest, load_yaml
from .models import ensure_local_materials
from .packaging import fixed_package_items, validate_package
from .pipeline import analyze_music, build_cover_metadata, compose_from_candidates


def validate_config(root: Path) -> list[str]:
    errors: list[str] = []
    for relative in ("config/default.yaml", "config/models.yaml", "profiles/categories/beauty.yaml", "profiles/categories/food.yaml"):
        path = root / relative
        if not path.is_file():
            errors.append(f"missing {relative}")
            continue
        try:
            load_yaml(path)
        except Exception as exc:
            errors.append(f"invalid {relative}: {exc}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="visual-montage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-config")
    validate = sub.add_parser("validate-input")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--campaign", type=Path, required=True)
    validate.add_argument("--cover-title", default="")
    music = sub.add_parser("analyze-music")
    music.add_argument("--music", type=Path, required=True)
    music.add_argument("--output", type=Path, required=True)
    compose = sub.add_parser("compose")
    compose.add_argument("--candidate-pool", type=Path, required=True)
    compose.add_argument("--profile", type=Path, required=True)
    compose.add_argument("--campaign", type=Path, required=True)
    compose.add_argument("--music-analysis", type=Path, required=True)
    compose.add_argument("--output", type=Path, required=True)
    batch = sub.add_parser("batch-compose")
    batch.add_argument("--candidate-pool", type=Path, required=True)
    batch.add_argument("--profile", type=Path, required=True)
    batch.add_argument("--campaign", type=Path, required=True)
    batch.add_argument("--music-analysis", type=Path, required=True)
    batch.add_argument("--registry", type=Path, default=Path("data/catalog/candidate-registry.sqlite"))
    batch.add_argument("--output-dir", type=Path, required=True)
    batch.add_argument("--run-id", required=True)
    batch.add_argument("--count", type=int, required=True)
    finalize = sub.add_parser("candidate-finalize")
    finalize.add_argument("--registry", type=Path, default=Path("data/catalog/candidate-registry.sqlite"))
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--state", choices=("committed", "released"), required=True)
    cover = sub.add_parser("cover-metadata")
    cover.add_argument("--title", required=True)
    cover.add_argument("--frame", type=Path, required=True)
    cover.add_argument("--video-id", required=True)
    cover.add_argument("--timestamp", type=float, required=True)
    cover.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == "validate-config":
        errors = validate_config(root)
    elif args.command == "validate-input":
        errors = []
        materials = load_manifest(args.manifest)
        try:
            ensure_local_materials(materials)
        except Exception as exc:
            errors.append(str(exc))
        campaign = load_campaign(args.campaign)
        package = fixed_package_items(campaign)
        errors.extend(validate_package(campaign, package))
        if args.cover_title:
            errors.extend(validate_cover_title(args.cover_title))
    elif args.command == "analyze-music":
        result = analyze_music(args.music, args.output)
        print(json.dumps({"ok": True, "output": str(args.output), "bpm": result["bpm"]}, ensure_ascii=False, indent=2))
        return 0
    elif args.command == "compose":
        result = compose_from_candidates(
            candidate_pool=args.candidate_pool, profile_path=args.profile,
            campaign_path=args.campaign, music_analysis_path=args.music_analysis, output=args.output,
        )
        print(json.dumps({"ok": result["validation"]["passed"], "output": str(args.output), "validation": result["validation"]}, ensure_ascii=False, indent=2))
        return 0 if result["validation"]["passed"] else 1
    elif args.command == "batch-compose":
        if args.count < 1:
            parser.error("--count must be at least 1")
        result = batch_compose(
            candidate_pool=args.candidate_pool,
            profile_path=args.profile,
            campaign_path=args.campaign,
            music_analysis_path=args.music_analysis,
            registry_path=args.registry,
            output_dir=args.output_dir,
            run_id=args.run_id,
            count=args.count,
        )
        ok = all(item["validation"]["passed"] for item in result["plans"])
        print(json.dumps({"ok": ok, **result}, ensure_ascii=False, indent=2))
        return 0 if ok else 2
    elif args.command == "candidate-finalize":
        with CandidateRegistry(args.registry) as registry:
            changed = registry.finalize_run(args.run_id, args.state)
        print(json.dumps({
            "ok": True,
            "run_id": args.run_id,
            "state": args.state,
            "candidate_count": changed,
            "registry": str(args.registry),
        }, ensure_ascii=False, indent=2))
        return 0
    else:
        result = build_cover_metadata(
            title=args.title, frame_path=args.frame, video_id=args.video_id,
            timestamp=args.timestamp, output=args.output,
        )
        print(json.dumps({"ok": result["validation"]["passed"], "output": str(args.output), "validation": result["validation"]}, ensure_ascii=False, indent=2))
        return 0 if result["validation"]["passed"] else 1
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())

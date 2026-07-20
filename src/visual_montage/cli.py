from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .batch import batch_compose
from .batch_runner import run_batch
from .candidate_registry import CandidateRegistry
from .cover import validate_cover_title
from .io import load_campaign, load_manifest, load_yaml
from .models import VisualCandidate, ensure_local_materials
from .packaging import fixed_package_items, validate_package
from .pipeline import analyze_music, build_cover_metadata, compose_from_candidates
from .voiceover import generate_voiceover


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
    register = sub.add_parser("candidate-register")
    register.add_argument("--candidate-pool", type=Path, required=True)
    register.add_argument("--category", required=True)
    register.add_argument("--registry", type=Path, default=Path("data/catalog/candidate-registry.sqlite"))
    batch_run = sub.add_parser("batch-run")
    batch_run.add_argument("--manifest", type=Path, required=True)
    batch_run.add_argument("--category", required=True)
    batch_run.add_argument("--limit", type=int, required=True)
    batch_run.add_argument("--count", type=int, required=True)
    batch_run.add_argument("--campaign", type=Path, required=True)
    batch_run.add_argument("--profile", type=Path)
    batch_run.add_argument("--music-analysis", type=Path)
    batch_run.add_argument("--voiceover-audio", type=Path)
    batch_run.add_argument(
        "--voiceover-mode",
        choices=("cached", "regenerate"),
        default="cached",
    )
    batch_run.add_argument("--asset-library", type=Path, default=Path("data/assets/asset-library.yaml"))
    batch_run.add_argument("--registry", type=Path, default=Path("data/catalog/candidate-registry.sqlite"))
    batch_run.add_argument("--run-id", required=True)
    batch_run.add_argument("--env-file", type=Path, default=Path(".env"))
    batch_run.add_argument(
        "--drafts-root",
        type=Path,
        default=Path("/Users/linying/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"),
    )
    batch_run.add_argument(
        "--media-root",
        type=Path,
        default=Path("/Users/linying/Movies/JianyingPro/RednoteMedia"),
    )
    batch_run.add_argument("--force-analysis", action="store_true")
    batch_run.add_argument("--force-audio", action="store_true")
    batch_run.add_argument("--cache-only", action="store_true")
    generate_voice = sub.add_parser("generate-voiceover")
    generate_voice.add_argument("--campaign", type=Path, required=True)
    generate_voice.add_argument("--output", type=Path, required=True)
    generate_voice.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("data/cache/voiceover"),
    )
    generate_voice.add_argument("--force", action="store_true")
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
    elif args.command == "candidate-register":
        raw = json.loads(args.candidate_pool.read_text(encoding="utf-8"))
        candidates = [
            VisualCandidate.model_validate(item)
            for item in raw.get("candidates", raw)
        ]
        with CandidateRegistry(args.registry) as registry:
            registered = registry.register(candidates, args.category)
            history = registry.history(args.category)
        print(json.dumps({
            "ok": True,
            "category": args.category,
            "input_candidate_count": len(candidates),
            "registered_candidate_count": len(registered),
            "category_registry_count": len(history),
            "registry": str(args.registry),
        }, ensure_ascii=False, indent=2))
        return 0
    elif args.command == "batch-run":
        if args.count < 1 or args.limit < 1:
            parser.error("--count and --limit must be at least 1")
        if args.force_analysis and args.cache_only:
            parser.error("--force-analysis and --cache-only cannot be used together")
        profile_path = args.profile or Path("profiles/categories") / f"{args.category}.yaml"
        result = run_batch(
            project_root=root.resolve(),
            manifest=args.manifest.resolve(),
            category=args.category,
            limit=args.limit,
            count=args.count,
            campaign_path=args.campaign.resolve(),
            profile_path=profile_path.resolve(),
            music_analysis_path=(
                args.music_analysis.resolve() if args.music_analysis else None
            ),
            voiceover_audio=(
                args.voiceover_audio.resolve() if args.voiceover_audio else None
            ),
            asset_library=args.asset_library.resolve(),
            registry_path=args.registry.resolve(),
            run_id=args.run_id,
            env_file=args.env_file.resolve(),
            drafts_root=args.drafts_root.resolve(),
            media_root=args.media_root.resolve(),
            force_analysis=args.force_analysis,
            force_audio=args.force_audio,
            cache_only=args.cache_only,
            voiceover_mode=args.voiceover_mode,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    elif args.command == "generate-voiceover":
        result = generate_voiceover(
            campaign_path=args.campaign.resolve(),
            output=args.output.resolve(),
            cache_dir=args.cache_dir.resolve(),
            force=args.force,
        )
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
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

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import yaml


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
FIELDNAMES = [
    "video_id",
    "path",
    "category",
    "note_id",
    "author",
    "enabled",
    "parent_category",
    "source_category",
    "market",
    "language",
]


def parse_material_name(path: Path) -> dict[str, str]:
    try:
        head, market, language, note_id = path.stem.rsplit("_", 3)
        category_block, author_name = head.split("_@", 1)
        parent_category, source_category = category_block.split("-", 1)
    except ValueError as exc:
        raise ValueError(
            "expected filename: 一级品类-二级品类_@作者_市场_语言_note_id.mp4"
        ) from exc
    if not note_id:
        raise ValueError("missing note_id")
    return {
        "note_id": note_id.strip(),
        "author": f"@{author_name.strip()}",
        "parent_category": parent_category.strip(),
        "source_category": source_category.strip(),
        "market": market.strip(),
        "language": language.strip(),
    }


def build_manifest(source_dir: Path, output: Path, category_map_path: Path) -> dict:
    category_map = yaml.safe_load(category_map_path.read_text(encoding="utf-8")) or {}
    rows: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            metadata = parse_material_name(path)
            category = str(
                category_map.get(metadata["source_category"])
                or metadata["source_category"]
            )
            rows.append(
                {
                    "video_id": f"{category}_{metadata['note_id']}",
                    "path": str(path.resolve()),
                    "category": category,
                    "note_id": metadata["note_id"],
                    "author": metadata["author"],
                    "enabled": "true",
                    "parent_category": metadata["parent_category"],
                    "source_category": metadata["source_category"],
                    "market": metadata["market"],
                    "language": metadata["language"],
                }
            )
        except ValueError as exc:
            failures.append({"path": str(path), "error": str(exc)})

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return {
        "output": str(output.resolve()),
        "videos": len(rows),
        "categories": counts,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a weekly mixed-category manifest")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--category-map",
        type=Path,
        default=Path("data/inputs/category-map.yaml"),
    )
    args = parser.parse_args()
    if not args.source_dir.is_dir():
        parser.error(f"source directory does not exist: {args.source_dir}")
    if not args.category_map.is_file():
        parser.error(f"category map does not exist: {args.category_map}")
    result = build_manifest(args.source_dir.resolve(), args.output, args.category_map)
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False).strip())
    return 0 if not result["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

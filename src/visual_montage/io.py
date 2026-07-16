from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import yaml

from .models import Campaign, Material


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_manifest(path: Path) -> list[Material]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [Material.model_validate(row) for row in csv.DictReader(handle)]


def load_campaign(path: Path) -> Campaign:
    raw = load_yaml(path)
    product = raw.get("product_demo") or {}
    brand = raw.get("brand") or {}
    copy = raw.get("copy") or {}
    voice = raw.get("voiceover") or {}
    return Campaign(
        campaign_id=raw["campaign_id"], category=raw["category"],
        duration_seconds=raw.get("duration_seconds", 20), language=raw.get("language", "zh-CN"),
        aspect_ratio=raw.get("aspect_ratio", "9:16"),
        product_openpage_asset_id=product["openpage_asset_id"],
        product_recording_asset_id=product["recording_asset_id"],
        endcard_asset_id=brand["endcard_asset_id"],
        logo_asset_id=brand.get("logo_asset_id", ""),
        animated_logo_asset_id=brand.get("animated_logo_asset_id", ""),
        cover_logo_asset_id=brand.get("cover_logo_asset_id", brand.get("logo_asset_id", "")),
        hook_copy=copy.get("hook", ""), brand_message=copy["brand_message"], cta=copy["cta"],
        voiceover_text=voice["text"],
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

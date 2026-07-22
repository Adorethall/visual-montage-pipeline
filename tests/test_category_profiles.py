from pathlib import Path

from visual_montage.io import load_yaml
from visual_montage.io import load_campaign


ROOT = Path(__file__).resolve().parents[1]


def test_new_category_profiles_have_renderable_dependencies() -> None:
    for category in (
        "overall_makeup",
        "cosmetics_recommendation",
        "short_drama",
        "anime",
    ):
        profile = load_yaml(ROOT / "profiles" / "categories" / f"{category}.yaml")
        assert profile["category_id"] == category
        assert profile["preferred_events"]
        assert profile["gemma_review"]["events"]
        assert (ROOT / profile["gemma_review"]["prompt_path"]).is_file()
        queries = load_yaml(ROOT / profile["marlin_recall"]["query_path"])
        assert queries["query_groups"]


def test_drama_campaigns_match_registered_assets() -> None:
    library = load_yaml(ROOT / "data" / "assets" / "asset-library.yaml")
    registered = {
        item["asset_id"]
        for group in ("product_openpages", "product_recordings", "brand_assets", "fonts")
        for item in library[group]
    }
    for category in ("short_drama", "anime"):
        path = ROOT / "data" / "inputs" / "campaigns" / f"{category}_20s.yaml"
        raw = load_yaml(path)
        campaign = load_campaign(path)
        assert campaign.category == category
        assert raw["product_demo"]["recording_asset_id"] in registered
        assert raw["product_demo"]["openpage_asset_id"] in registered
        assert raw["brand"]["endcard_asset_id"] in registered
        assert len([part for part in raw["voiceover"]["text"].split(".") if part.strip()]) >= 2

from __future__ import annotations

from .models import Campaign, TimelineItem


DEFAULT_STRUCTURE = {
    "hook": (0.0, 0.8),
    "montage_1": (0.8, 7.2),
    "product_openpage": (7.2, 8.7),
    "product_recording": (8.7, 12.8),
    "montage_2": (12.8, 17.5),
    "endcard": (17.5, 20.0),
}


def fixed_package_items(campaign: Campaign) -> list[TimelineItem]:
    return [
        TimelineItem(timeline_id="pkg_openpage", role="product_openpage", source_type="product_openpage",
                     start=7.2, end=8.7, asset_id=campaign.product_openpage_asset_id),
        TimelineItem(timeline_id="pkg_recording", role="product_recording", source_type="product_recording",
                     start=8.7, end=12.8, asset_id=campaign.product_recording_asset_id),
        TimelineItem(timeline_id="pkg_endcard", role="endcard", source_type="endcard",
                     start=17.5, end=20.0, asset_id=campaign.endcard_asset_id),
    ]


def validate_package(campaign: Campaign, items: list[TimelineItem]) -> list[str]:
    errors: list[str] = []
    roles = {item.role: item for item in items}
    for required in ("product_openpage", "product_recording", "endcard"):
        if required not in roles:
            errors.append(f"missing {required}")
    if "product_openpage" in roles and "product_recording" in roles:
        if abs(roles["product_openpage"].end - roles["product_recording"].start) > 0.001:
            errors.append("product openpage must immediately precede product recording")
    if not campaign.voiceover_text.strip():
        errors.append("continuous two-sentence product voiceover is required")
    if len([x for x in campaign.voiceover_text.replace("！", "。").replace("？", "。").split("。") if x.strip()]) < 2:
        errors.append("voiceover must contain at least two sentences")
    return errors


import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_weekly_manifest.py"
SPEC = importlib.util.spec_from_file_location("build_weekly_manifest", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
parse_material_name = MODULE.parse_material_name


def test_parse_weekly_material_name_uses_second_level_category() -> None:
    parsed = parse_material_name(
        Path(
            "彩妆测试-化妆品推荐_@羽儿🦋（护肤小生活版）_"
            "北美,欧洲_英语_6a124e2c0000000036018ce6.mp4"
        )
    )
    assert parsed["parent_category"] == "彩妆测试"
    assert parsed["source_category"] == "化妆品推荐"
    assert parsed["author"] == "@羽儿🦋（护肤小生活版）"
    assert parsed["note_id"] == "6a124e2c0000000036018ce6"

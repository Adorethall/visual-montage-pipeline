from PIL import Image, ImageDraw

from visual_montage.batch_runner import (
    _author_from_video_path,
    _jianying_draft_name,
    _logo_aligned_transform_y,
    _select_cover_candidate,
)
from visual_montage.author_overlay import (
    author_center_transform_x,
    author_rich_spans,
    author_text_style,
)


def test_extracts_author_from_manifest_filename() -> None:
    path = (
        "/materials/美食展示-美食展示_@大喜的vlog_新马泰_英语_"
        "685a993f000000001c036a75.mp4"
    )
    assert _author_from_video_path(path) == "@大喜的vlog"


def test_extracts_author_from_star_delimited_filename() -> None:
    path = (
        "/materials/美食展示-美食展示*@大喜的vlog*新马泰_英语_"
        "685a993f000000001c036a75.mp4"
    )
    assert _author_from_video_path(path) == "@大喜的vlog"


def test_preserves_underscores_inside_author() -> None:
    path = (
        "/materials/两性-情感故事_@Joey_yyyyy_新马泰_英语_"
        "68915ffb0000000023038b19.mp4"
    )
    assert _author_from_video_path(path) == "@Joey_yyyyy"


def test_missing_author_is_empty() -> None:
    assert _author_from_video_path("/materials/plain-video.mp4") == ""


def test_jianying_draft_name_uses_first_timeline_source() -> None:
    plan = {
        "campaign": {"category": "anime", "language": "en-US"},
        "selected_candidates": [
            {
                "video_id": "anime_69bb9627000000002301cf17",
                "video_path": (
                    "/materials/Drama测试-二次元_@BigMommmm_北美,欧洲_英语_"
                    "69bb9627000000002301cf17.mp4"
                ),
            },
            {
                "video_id": "anime_697df60e000000001a035622",
                "video_path": "/materials/ignored.mp4",
            },
        ],
    }
    assert _jianying_draft_name(plan, 1) == (
        "Drama测试-二次元-69bb9627000000002301cf17-英语-001"
    )


def test_jianying_draft_name_supports_star_author_delimiter() -> None:
    plan = {
        "campaign": {"category": "beauty", "language": "zh-CN"},
        "selected_candidates": [
            {
                "video_id": "beauty_685a993f000000001c036a75",
                "video_path": (
                    "/materials/彩妆测试-整体妆容*@作者*北美_英语_"
                    "685a993f000000001c036a75.mp4"
                ),
            }
        ],
    }
    assert _jianying_draft_name(plan, 12) == (
        "彩妆测试-整体妆容-685a993f000000001c036a75-中文（简体）-012"
    )


def test_cover_candidate_always_uses_first_timeline_highlight() -> None:
    selected = [
        {
            "video_id": "high_score_application",
            "event": "application",
            "scores": {"aesthetic": 1.0, "subject_visibility": 1.0, "payoff": 1.0},
            "final_score": 1.0,
        },
        {
            "video_id": "preferred_reveal",
            "event": "final_look_reveal",
            "scores": {"aesthetic": 0.85, "subject_visibility": 0.9, "payoff": 0.9},
            "final_score": 0.9,
        },
    ]
    chosen = _select_cover_candidate(selected, {"cover_events": ["final_look_reveal"]})
    assert chosen["video_id"] == "high_score_application"


def test_author_height_aligns_with_visible_logo_center(tmp_path) -> None:
    logo = Image.new("RGBA", (720, 1280), (0, 0, 0, 0))
    ImageDraw.Draw(logo).rectangle((41, 79, 297, 120), fill=(255, 255, 255, 255))
    path = tmp_path / "logo.png"
    logo.save(path)
    assert _logo_aligned_transform_y(path) == 0.84375


def test_short_author_keeps_default_size() -> None:
    style = author_text_style("@大喜的vlog")
    assert style["font_size"] == 4.8
    assert style["auto_wrapping"] is False


def test_long_author_keeps_size_and_moves_center_left() -> None:
    short = author_text_style("@short")
    style = author_text_style("@VeryLongCreatorNameWithTravelStories")
    assert style["font_size"] == short["font_size"] == 4.8
    assert style["auto_wrapping"] is False
    short_x = author_center_transform_x("@short")
    long_x = author_center_transform_x("@VeryLongCreatorNameWithTravelStories")
    assert long_x < short_x


def test_emoji_gets_its_own_smaller_span() -> None:
    spans = author_rich_spans("@小媛奶糖🍬")
    assert spans == [
        {"start": 0, "end": 5, "size": 4.8, "emoji": False},
        {"start": 5, "end": 7, "size": 4.8, "emoji": True},
    ]


def test_emoji_inside_author_uses_two_utf16_units() -> None:
    spans = author_rich_spans("@🥥啵啵")
    assert spans == [
        {"start": 0, "end": 1, "size": 4.8, "emoji": False},
        {"start": 1, "end": 3, "size": 4.8, "emoji": True},
        {"start": 3, "end": 5, "size": 4.8, "emoji": False},
    ]

from PIL import Image, ImageDraw

from visual_montage.batch_runner import (
    _author_from_video_path,
    _logo_aligned_transform_y,
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

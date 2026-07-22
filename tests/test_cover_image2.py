from pathlib import Path
import subprocess

from PIL import Image

from visual_montage.cover_image2 import generate_image2_cover
from visual_montage.batch_runner import _branded_cover_preview


def test_generate_image2_cover_uses_profile_title_and_caches(tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output = tmp_path / "cover" / "cover-image2.png"
    cache_dir = tmp_path / "cache"
    Image.new("RGB", (720, 1280), "#557799").save(source)
    calls = []

    monkeypatch.setattr("visual_montage.cover_image2.shutil.which", lambda _: "/usr/bin/rings")

    def fake_run(command, **kwargs):
        calls.append(command)
        if "run" in command:
            return subprocess.CompletedProcess(command, 0, "TASK_ID=tpub_test\n", "")
        Image.new("RGB", (1080, 1920), "#446688").save(output)
        return subprocess.CompletedProcess(command, 0, "completed\n", "")

    monkeypatch.setattr("visual_montage.cover_image2.subprocess.run", fake_run)
    first = generate_image2_cover(
        source_image=source,
        output_image=output,
        title="Travel Inspo You Need To Save",
        category="travel",
        language="en-US",
        config={"enabled": True},
        cache_dir=cache_dir,
        reserve_logo_safe_zone=True,
    )
    assert first["ok"] is True
    assert first["title_embedded"] is True
    assert first["logo_embedded"] is False
    assert first["task_id"] == "tpub_test"
    assert len(calls) == 2
    payload = (output.parent / "image2-payload.json").read_text(encoding="utf-8")
    assert 'Travel Inspo You Need To Save' in payload
    assert 'Leave the top-left area completely empty' in payload
    assert 'below the logo safe zone' in payload
    assert '"request_timeout_seconds": 120.0' in payload

    output.unlink()
    second = generate_image2_cover(
        source_image=source,
        output_image=output,
        title="Travel Inspo You Need To Save",
        category="travel",
        language="en-US",
        config={"enabled": True},
        cache_dir=cache_dir,
        reserve_logo_safe_zone=True,
    )
    assert second["cache_hit"] is True
    assert output.is_file()
    assert len(calls) == 2


def test_branded_cover_preview_preserves_full_canvas_logo_coordinates(tmp_path):
    frame = tmp_path / "frame.png"
    logo = tmp_path / "logo.png"
    output = tmp_path / "output.png"
    Image.new("RGBA", (100, 160), (20, 30, 40, 255)).save(frame)
    overlay = Image.new("RGBA", (100, 160), (0, 0, 0, 0))
    overlay.putpixel((8, 12), (255, 0, 0, 255))
    overlay.save(logo)

    _branded_cover_preview(frame, logo, output)

    with Image.open(output) as rendered:
        assert rendered.convert("RGBA").getpixel((8, 12)) == (255, 0, 0, 255)


def test_branded_cover_preview_can_shift_logo_down(tmp_path):
    frame = tmp_path / "frame.png"
    logo = tmp_path / "logo.png"
    output = tmp_path / "output.png"
    Image.new("RGBA", (100, 160), (20, 30, 40, 255)).save(frame)
    overlay = Image.new("RGBA", (100, 160), (0, 0, 0, 0))
    overlay.putpixel((8, 12), (255, 0, 0, 255))
    overlay.save(logo)

    _branded_cover_preview(frame, logo, output, vertical_offset_ratio=0.025)

    with Image.open(output) as rendered:
        rgba = rendered.convert("RGBA")
        assert rgba.getpixel((8, 12)) == (20, 30, 40, 255)
        assert rgba.getpixel((8, 16)) == (255, 0, 0, 255)

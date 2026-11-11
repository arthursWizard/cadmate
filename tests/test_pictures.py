"""SVG to PNG, the step every picture cadmate makes goes through. resvg-py is a dependency, so this runs on each OS in CI
(Windows included) with nothing else installed."""
import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

import pytest

from cadmate import protoboard as pb
from cadmate import protoboard_render as pr
from cadmate import kicad_live
from cadmate.kicad_live import LiveError, _svg_to_png

# Sized in mm like kicad-cli's SVGs, with a viewBox that doesn't start at 0.
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="-10 -5 20 10">'
       '<rect x="-4" y="-2" width="8" height="4" fill="#ff0000"/></svg>')


def png_info(path: Path) -> tuple[int, int, tuple[int, ...]]:
    """Width, height and the top-left pixel. The first pixel of the first row reads the same under every PNG filter."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    width, height, depth, color = ihdr
    assert depth == 8 and color in (2, 6)
    raw = zlib.decompress(idat)
    return width, height, tuple(raw[1:1 + (4 if color == 6 else 3)])


def test_svg_to_png_width_and_white_background(tmp_path):
    svg = tmp_path / "part.svg"
    svg.write_text(SVG, encoding="utf-8")
    png = tmp_path / "part.png"
    _svg_to_png(svg, png, 400)
    width, height, corner = png_info(png)
    assert width == 400
    assert abs(height / width - 0.5) < 0.05  # the margin _pixel_size adds keeps the aspect
    assert corner[:3] == (255, 255, 255)


def test_svg_to_png_path_with_spaces_and_accents(tmp_path):
    folder = tmp_path / "mes cartes é"
    folder.mkdir()
    svg = folder / "carte ü.svg"
    svg.write_text(SVG, encoding="utf-8")
    _svg_to_png(svg, folder / "carte ü.png", 200)
    assert png_info(folder / "carte ü.png")[0] == 200


def test_svg_to_png_bad_svg_is_a_live_error(tmp_path):
    svg = tmp_path / "broken.svg"
    svg.write_text("<svg this is not xml", encoding="utf-8")
    with pytest.raises(LiveError):
        _svg_to_png(svg, tmp_path / "broken.png", 200)


def test_protoboard_render_makes_a_png(tmp_path):
    example = Path(__file__).parent.parent / "examples" / "protoboard" / "555-blinker.json"
    r = pb.route(json.loads(example.read_text(encoding="utf-8")))
    out = pr.render(r["design"], tmp_path / "blinker", width=900)
    assert "png" in out, out.get("png_error")
    width, _, corner = png_info(Path(out["png"]))
    assert width == 900 and corner[:3] == (255, 255, 255)


def test_protoboard_text_is_drawn(tmp_path):
    """resvg finds fonts by exact family name; when it finds none, text is dropped without an error. A render with the
    system's fonts must differ from one with no fonts at all, or every label went missing."""
    import resvg_py

    example = Path(__file__).parent.parent / "examples" / "protoboard" / "555-blinker.json"
    r = pb.route(json.loads(example.read_text(encoding="utf-8")))
    out = pr.render(r["design"], tmp_path / "blinker", width=600)
    no_text = tmp_path / "no-text.png"
    sized = tmp_path / "sized.svg"  # the same size the PNG was drawn at
    sized.write_text(Path(out["svg"]).read_text(encoding="utf-8"), encoding="utf-8")
    kicad_live._pixel_size(sized, 600)
    no_text.write_bytes(bytes(resvg_py.svg_to_bytes(svg_path=str(sized), width=600, background="white",
                                                    skip_system_fonts=True)))
    assert Path(out["png"]).read_bytes() != no_text.read_bytes()


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("fc-match"), reason="fontconfig on Linux only")
def test_sans_family_from_fontconfig():
    assert kicad_live._sans_family()


def test_fallback_without_resvg(tmp_path, monkeypatch):
    """With resvg-py missing, rsvg-convert is used when there is one, else a LiveError says what to install."""
    monkeypatch.setitem(sys.modules, "resvg_py", None)  # import resvg_py now raises ImportError
    monkeypatch.setattr(kicad_live.platform, "system", lambda: "Linux")
    svg = tmp_path / "part.svg"
    svg.write_text(SVG, encoding="utf-8")
    calls = []
    monkeypatch.setattr(kicad_live.shutil, "which", lambda name: "/usr/bin/rsvg-convert" if name == "rsvg-convert" else None)
    monkeypatch.setattr(kicad_live.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    _svg_to_png(svg, tmp_path / "part.png", 300)
    assert calls and calls[0][0] == "rsvg-convert" and "300" in calls[0]
    monkeypatch.setattr(kicad_live.shutil, "which", lambda name: None)
    with pytest.raises(LiveError, match="rsvg-convert"):
        _svg_to_png(svg, tmp_path / "part.png", 300)

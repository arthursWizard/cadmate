"""Every text read and write in cadmate names its encoding. Without one, Python uses the locale's (cp1252 on most
Windows machines), so a µ or Ω in a net name breaks parsing there, and writes turn \\n into \\r\\n."""

import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "cadmate"
CALL = re.compile(r"\.(read_text|write_text)\(")


def _calls(text: str):
    """Each read_text/write_text call with its arguments, brackets balanced, so calls split over lines count."""
    for m in CALL.finditer(text):
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        yield m.group(1), text[m.end():i - 1], text.count("\n", 0, m.start()) + 1


def test_every_text_read_and_write_names_its_encoding():
    missing = [f"{f.relative_to(SRC).as_posix()}:{line} {kind}" for f in sorted(SRC.rglob("*.py"))
               if "freecad_scripts" not in f.parts  # run inside FreeCAD, which is UTF-8 on every platform
               for kind, args, line in _calls(f.read_text(encoding="utf-8"))
               if not re.search(r"\bencoding\s*=", re.sub(r"\.(read_text|write_text)\([^()]*\)", "", args))]
    assert not missing, "add encoding=\"utf-8\" (and newline=\"\\n\" when writing a KiCad file): " + ", ".join(missing)

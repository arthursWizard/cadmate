"""Everything cadmate ties to a KiCad or FreeCAD version, in one place.

When a new KiCad or FreeCAD release changes something, this is the file to adapt:

- ``MIN_VERSIONS`` and ``TESTED_VERSIONS``: the oldest release cadmate supports, and the newest one it was checked
  against. ``environment`` reports an install outside that range, so the user knows before something breaks.
- The ``*_FORMAT`` numbers: the file format versions cadmate writes. KiCad reads files from older versions, so these
  move only when cadmate needs a feature of a newer format (and then ``MIN_VERSIONS`` moves with them).
- ``KICAD_CLI``: the kicad-cli arguments cadmate uses. If a release renames a flag, change it here; if two versions
  must both work, branch on the version from ``apps.kicad_info()`` in ``kicad_cli_args``.

Reading doesn't depend on any of this: files are parsed as generic s-expressions, and the parts cadmate reads have
been stable since KiCad 6 (footprints since KiCad 5).
"""

from __future__ import annotations

import re

MIN_VERSIONS = {"kicad": (10, 0), "freecad": (1, 1)}
TESTED_VERSIONS = {"kicad": (10, 0), "freecad": (1, 1)}

FOOTPRINT_FORMAT = "20260206"  # (footprint ...) as KiCad 10 writes it
SYMBOL_FORMAT = "20251024"  # (kicad_symbol_lib ...) as KiCad 10 writes it
LIB_TABLE_FORMAT = 7  # sym-lib-table and fp-lib-table
DRU_FORMAT = 1  # the board's custom rules, .kicad_dru

# Fixed arguments per kicad-cli command, before the per-call ones (--output, the file, --layers...).
KICAD_CLI = {
    "version": ["version"],
    "pcb_drc": ["pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "--refill-zones"],
    "pcb_drc_parity": ["--schematic-parity"],
    "sch_erc": ["sch", "erc", "--format", "json", "--severity-all", "--units", "mm"],
    "pcb_svg": ["pcb", "export", "svg", "--mode-single", "--fit-page-to-board", "--exclude-drawing-sheet"],
    "fp_svg": ["fp", "export", "svg", "--sketch-pads-on-fab-layers"],
    "sym_svg": ["sym", "export", "svg"],
    "pcb_gerbers": ["pcb", "export", "gerbers", "--subtract-soldermask"],
    "pcb_drill": ["pcb", "export", "drill", "--format", "excellon", "--excellon-separate-th", "--generate-map",
                  "--map-format", "gerberx2"],
    "pcb_pos": ["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both"],
}


def kicad_cli_args(command: str) -> list[str]:
    """The fixed kicad-cli arguments for a command, as a new list the caller can extend."""
    return list(KICAD_CLI[command])


def parse(version: str | None) -> tuple[int, int] | None:
    """(major, minor) from a version string such as "10.0.4" or "1.1.0 (Git)"; None when there is none."""
    m = re.search(r"(\d+)\.(\d+)", version or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def status(app: str, version: str | None) -> str:
    """'unknown', 'too_old', 'supported', or 'newer_than_tested' for an installed version of 'kicad' or 'freecad'."""
    v = parse(version)
    if v is None:
        return "unknown"
    if v < MIN_VERSIONS[app]:
        return "too_old"
    if v > TESTED_VERSIONS[app]:  # a nightly (10.99) counts as newer too
        return "newer_than_tested"
    return "supported"


def dotted(v: tuple[int, ...]) -> str:
    return ".".join(map(str, v))

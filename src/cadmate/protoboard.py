"""Protoboard (perfboard) layouts: through-hole parts on a 2.54 mm hole grid, checked and routed.

A design is a dict (or a JSON file holding one):

    {
      "board": {"type": "perfboard", "cols": 24, "rows": 16, "pads": "single"},
      "parts": [
        {"ref": "U1", "part": "dip", "params": {"pins": 8}, "at": "E6", "rotation": 0, "value": "NE555"},
        {"ref": "R1", "part": "resistor_axial", "params": {"span": 4}, "at": "B3", "value": "1k"},
        {"ref": "X1", "part": {"pins": {"1": [0, 0], "2": [2, 0]}, "body": {"rect": [-1, -1.5, 6.1, 1.5]}}, "at": "K2"}
      ],
      "nets": {"VCC": ["U1.8", "U1.4", "R1.1"], "GND": ["U1.1", "C1.2"]},
      "wires": [{"net": "GND", "side": "bottom", "path": ["D3", "D9", "F9"]},
                {"net": "VCC", "side": "jumper", "path": ["A2", "A12"]},
                {"net": "OUT", "side": "top", "path": ["H4", "H8"]}]
    }

Holes are named by row letter and column number, "A1" top left seen from the component side. "at" is the hole of
pin 1; rotation turns the part clockwise (seen from the top) about that hole, in steps of 90 degrees.

Perfboard here means a board of separate pads. "pads" says where they are: "single" (bottom only, the default),
"double" (both sides, plated through: a hole's two pads are joined) or "double-unplated" (both sides, not joined: where
a net changes side away from a lead, push a bit of bare wire through the hole and solder it both sides, a via).
Parts go on top. A hole belongs to one net on both sides. Connections are:
- bottom: solder bridges and bare tinned wire running pad to pad along rows and columns. They may run under a part's
  body (the pads underneath are free on the solder side), but two nets never share a pad, so they never cross.
- top (double-sided boards): the same on the component-side pads, but never under a part's body, where the iron
  can't reach.
- jumper: insulated wire on the component side, straight along a row or column. Each end goes down its own free hole
  and is soldered to that pad underneath. A jumper can't lie over a part's leads or body, a top track or another
  jumper, so the picture stays copyable.
"""

from __future__ import annotations

import heapq
import json
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

PITCH = 2.54  # mm
PADS = ("single", "double", "double-unplated")
LEAD_MARGIN = 0.4  # mm: a hole whose centre is this close to a body is covered by it


class DesignError(ValueError):
    pass


# --- holes ------------------------------------------------------------------------------------------


def row_name(r: int) -> str:
    s = ""
    r += 1
    while r:
        r, k = divmod(r - 1, 26)
        s = chr(65 + k) + s
    return s


def hole_name(h: tuple[int, int]) -> str:
    return f"{row_name(h[1])}{h[0] + 1}"


def parse_hole(name) -> tuple[int, int]:
    if isinstance(name, (list, tuple)) and len(name) == 2:
        return int(name[0]), int(name[1])
    m = re.fullmatch(r"\s*([A-Za-z]+)\s*(\d+)\s*", str(name))
    if not m:
        raise DesignError(f"'{name}' is not a hole name like 'C5' (row letter, column number)")
    r = 0
    for ch in m.group(1).upper():
        r = r * 26 + ord(ch) - 64
    return int(m.group(2)) - 1, r - 1


# --- parts library ------------------------------------------------------------------------------------
# Each part: pins as hole offsets from pin 1 (x right, y down, rotation 0), the body as shapes in mm relative to pin 1,
# marks (polarity) and notes. Shapes: {"rect": [x0, y0, x1, y1]}, {"circle": [cx, cy, r]}, {"halfdisk": [cx, cy, r]}
# (flat side towards -y).


@dataclass
class PartDef:
    name: str
    pins: dict[str, tuple[int, int]]
    body: list[dict]
    marks: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    kind: str = "generic"


def _resistor(span: int = 4, length: float = 6.3, dia: float = 2.5) -> PartDef:
    if span < 2:
        if span == 1:
            return PartDef(f"resistor_axial span 1 (standing)", {"1": (0, 0), "2": (1, 0)},
                           [{"circle": [0, 0, dia / 2 + 0.2]}], kind="resistor",
                           notes=["standing: body over pin 1, lead 2 bent back down"])
        raise DesignError("resistor span must be at least 1 hole")
    L = span * PITCH
    if L < length + 1.5:
        raise DesignError(f"a span of {span} holes ({L:.2f} mm) is too short for a {length} mm body; use {math.ceil((length + 1.5) / PITCH)} or stand it (span 1)")
    c = L / 2
    return PartDef(f"resistor_axial span {span}", {"1": (0, 0), "2": (span, 0)},
                   [{"rect": [c - length / 2, -dia / 2, c + length / 2, dia / 2]}], kind="resistor")


def _diode(package: str = "DO-35", span: int | None = None) -> PartDef:
    length, dia, default = {"DO-35": (3.8, 1.9, 3), "DO-41": (5.2, 2.7, 4), "DO-201": (9.5, 5.3, 6)}.get(package, (None, None, None))
    if length is None:
        raise DesignError(f"diode package '{package}' unknown; use DO-35, DO-41 or DO-201")
    span = span or default
    L = span * PITCH
    if L < length + 1.5:
        raise DesignError(f"a span of {span} holes is too short for {package}")
    c = L / 2
    x0 = c - length / 2
    return PartDef(f"diode_axial {package} span {span}", {"1": (0, 0), "2": (span, 0)},
                   [{"rect": [x0, -dia / 2, c + length / 2, dia / 2]}],
                   marks=[{"band": [x0 + 0.3, -dia / 2, x0 + 0.9, dia / 2]}], kind="diode",
                   notes=["pin 1 is the cathode (band end)"])


def _cap_ceramic(pitch: int = 1, width: float = 5.0, depth: float = 3.0) -> PartDef:
    c = pitch * PITCH / 2
    return PartDef(f"cap_ceramic pitch {pitch}", {"1": (0, 0), "2": (pitch, 0)},
                   [{"rect": [c - width / 2, -depth / 2, c + width / 2, depth / 2]}], kind="capacitor")


_ELECTROLYTIC = {5: (2.0, 1), 6.3: (2.5, 1), 8: (3.5, 1), 10: (5.0, 2), 12.5: (5.0, 2)}


def _cap_electrolytic(diameter: float = 6.3) -> PartDef:
    diameter = float(diameter)
    key = next((k for k in _ELECTROLYTIC if abs(k - diameter) < 0.05), None)
    if key is None:
        raise DesignError(f"electrolytic diameter {diameter} unknown; use one of {sorted(_ELECTROLYTIC)}")
    real, holes = _ELECTROLYTIC[key]
    notes = ["pin 1 is +"]
    if abs(real - holes * PITCH) > 0.2:
        notes.append(f"lead pitch is {real} mm: bend the leads to {holes * PITCH:.2f} mm")
    c = holes * PITCH / 2
    return PartDef(f"cap_electrolytic D{key}", {"1": (0, 0), "2": (holes, 0)}, [{"circle": [c, 0, key / 2]}],
                   marks=[{"plus": [c - key / 2 * 0.55, -key / 2 * 0.55]}], kind="electrolytic", notes=notes)


def _led(size: float = 5) -> PartDef:
    size = float(size)
    if size not in (3, 5):
        raise DesignError("LED size must be 3 or 5 (mm)")
    r = 1.95 if size == 3 else 2.9
    return PartDef(f"led {int(size)}mm", {"1": (0, 0), "2": (1, 0)}, [{"circle": [PITCH / 2, 0, r]}],
                   marks=[{"flat": [PITCH / 2, 0, r]}], kind="led",
                   notes=["pin 1 is the cathode: short lead, flat side of the rim"])


def _dip(pins: int = 8, width: int = 3) -> PartDef:
    if pins < 4 or pins % 2:
        raise DesignError("DIP pin count must be even and at least 4")
    if width not in (3, 4, 6):
        raise DesignError("DIP row spacing is 3 holes (0.3 in), 4 or 6 (0.6 in)")
    n = pins // 2
    # Rotation 0, seen from the top with the notch at the left: pin 1 bottom left, pins 1..n along the bottom row,
    # n+1..2n back along the top row (counter-clockwise, as on every DIP).
    p = {str(i + 1): (i, 0) for i in range(n)} | {str(n + i + 1): (n - 1 - i, -width) for i in range(n)}
    m = 0.635
    body = [{"rect": [-1.0, -width * PITCH + m, (n - 1) * PITCH + 1.0, -m]}]
    marks = [{"notch": [-1.0, -width * PITCH / 2, 0.9]}, {"dot": [0.2, -m - 0.9, 0.35]}]
    return PartDef(f"dip-{pins} {'0.6in' if width == 6 else f'{width * 0.1:.1f}in'}", p, body, marks, kind="ic",
                   notes=["rotation 0: notch to the left, pin 1 bottom left, pins count counter-clockwise from the top"])


def _header(pins: int = 4, rows: int = 1) -> PartDef:
    if rows not in (1, 2) or pins < 1 or (rows == 2 and pins % 2):
        raise DesignError("header: rows 1 or 2, and an even pin count for 2 rows")
    if rows == 1:
        p = {str(i + 1): (i, 0) for i in range(pins)}
        body = [{"rect": [-1.27, -1.27, (pins - 1) * PITCH + 1.27, 1.27]}]
    else:
        p = {}
        for k in range(pins // 2):
            p[str(2 * k + 1)] = (k, 0)
            p[str(2 * k + 2)] = (k, 1)
        body = [{"rect": [-1.27, -1.27, (pins // 2 - 1) * PITCH + 1.27, PITCH + 1.27]}]
    return PartDef(f"header {rows}x{pins // rows}", p, body, kind="header")


def _to92() -> PartDef:
    return PartDef("to92", {"1": (0, 0), "2": (1, 0), "3": (2, 0)}, [{"halfdisk": [PITCH, -0.6, 2.4]}], kind="transistor",
                   notes=["leads are 1.27 mm apart: spread them to 2.54 mm", "rotation 0: flat face up (towards row A)"])


def _to220() -> PartDef:
    return PartDef("to220 vertical", {"1": (0, 0), "2": (1, 0), "3": (2, 0)},
                   [{"rect": [PITCH - 5.1, -3.3, PITCH + 5.1, 1.3]}], marks=[{"tab": [PITCH - 5.1, -3.3, PITCH + 5.1, -2.0]}],
                   kind="regulator", notes=["rotation 0: metal tab up (towards row A)"])


def _screw_terminal(pins: int = 2) -> PartDef:
    if pins < 2:
        raise DesignError("screw terminal needs at least 2 pins")
    return PartDef(f"screw_terminal 5.08mm x{pins}", {str(i + 1): (2 * i, 0) for i in range(pins)},
                   [{"rect": [-2.54, -3.6, (pins - 1) * 5.08 + 2.54, 4.0]}], kind="connector",
                   notes=["rotation 0: wire entries face down (towards the last row)"])


def _tact() -> PartDef:
    return PartDef("tact_switch 6x6", {"1": (0, 0), "2": (3, 0), "3": (0, 2), "4": (3, 2)},
                   [{"rect": [0.81, -0.46, 6.81, 5.54]}], marks=[{"circle_mark": [3.81, 2.54, 1.7]}], kind="switch",
                   notes=["leads are 6.5 x 4.5 mm, not on the grid: bend them out to 7.62 x 5.08 mm",
                          "which pins are joined inside differs by maker: check with a meter (usually 1-3 and 2-4 across the 4.5 mm side)"])


def _trimpot() -> PartDef:
    return PartDef("trimpot 3296W", {"1": (0, 0), "2": (1, 0), "3": (2, 0)},
                   [{"rect": [PITCH - 4.8, -2.4, PITCH + 4.8, 2.4]}], marks=[{"circle_mark": [PITCH + 3.4, -1.0, 0.8]}],
                   kind="pot", notes=["pin 2 is the wiper"])


def _crystal() -> PartDef:
    return PartDef("crystal HC49/US", {"1": (0, 0), "2": (2, 0)}, [{"rect": [PITCH - 5.5, -2.3, PITCH + 5.5, 2.3]}],
                   kind="crystal", notes=["lead pitch 4.88 mm sits in 5.08 mm holes"])


LIBRARY = {
    "resistor_axial": (_resistor, "Axial resistor lying flat. params: span (holes between leads, default 4 for 1/4 W; 1 = standing)"),
    "diode_axial": (_diode, "Axial diode lying flat, pin 1 = cathode. params: package DO-35 (1N4148) / DO-41 (1N400x) / DO-201, span"),
    "cap_ceramic": (_cap_ceramic, "Ceramic or film capacitor. params: pitch (1 or 2 holes), width, depth (mm)"),
    "cap_electrolytic": (_cap_electrolytic, "Radial electrolytic, pin 1 = +. params: diameter 5, 6.3, 8, 10, 12.5"),
    "led": (_led, "Round LED, pin 1 = cathode. params: size 3 or 5"),
    "dip": (_dip, "DIP IC or socket. params: pins (even), width 3 (0.3 in) or 6 (0.6 in)"),
    "header": (_header, "Pin header or socket strip, 2.54 mm. params: pins, rows 1 or 2"),
    "to92": (_to92, "TO-92 transistor or regulator, leads spread to 2.54 mm"),
    "to220": (_to220, "TO-220 standing, tab up at rotation 0"),
    "screw_terminal": (_screw_terminal, "5.08 mm screw terminal block. params: pins"),
    "tact_switch": (_tact, "6x6 mm tactile switch (leads bent onto the grid)"),
    "trimpot": (_trimpot, "Multiturn trimmer 3296W, inline leads"),
    "crystal_hc49": (_crystal, "HC49/US crystal"),
}


def list_parts() -> dict:
    out = {}
    for name, (fn, doc) in LIBRARY.items():
        d = fn()
        out[name] = {"description": doc, "default": d.name, "pins": {k: list(v) for k, v in d.pins.items()},
                     "covers": [list(h) for h in sorted(covered_offsets(d))], "notes": d.notes}
    out["custom"] = {"description": "Give 'part' as a dict instead of a name: {'pins': {'1': [0, 0], ...} (hole offsets from pin 1), "
                                    "'body': {'rect': [x0, y0, x1, y1]} or {'circle': [cx, cy, r]} or a list of those, in mm from pin 1, "
                                    "optional 'covers': [[dx, dy], ...] to set the covered holes by hand, 'notes': [...]}"}
    return out


def _custom(spec: dict) -> PartDef:
    pins = spec.get("pins")
    if not isinstance(pins, dict) or not pins:
        raise DesignError("a custom part needs 'pins': {'1': [dx, dy], ...} in holes from pin 1")
    body = spec.get("body") or []
    if isinstance(body, dict):
        body = [body]
    d = PartDef(spec.get("name", "custom"), {str(k): (int(v[0]), int(v[1])) for k, v in pins.items()}, body,
                notes=list(spec.get("notes", [])), kind=spec.get("kind", "generic"))
    if "covers" in spec:
        d.marks.append({"covers": [tuple(map(int, c)) for c in spec["covers"]]})
    return d


def part_def(part, params: dict | None = None) -> PartDef:
    if isinstance(part, dict):
        return _custom(part)
    if part not in LIBRARY:
        raise DesignError(f"part '{part}' is not in the library; known: {', '.join(LIBRARY)}, or give a custom part as a dict")
    try:
        return LIBRARY[part][0](**(params or {}))
    except TypeError as e:
        raise DesignError(f"{part}: bad params {params}: {e}") from e


def _inside(shape: dict, x: float, y: float, grow: float) -> bool:
    if "rect" in shape:
        x0, y0, x1, y1 = shape["rect"]
        return min(x0, x1) - grow <= x <= max(x0, x1) + grow and min(y0, y1) - grow <= y <= max(y0, y1) + grow
    if "circle" in shape:
        cx, cy, r = shape["circle"]
        return math.hypot(x - cx, y - cy) <= r + grow
    if "halfdisk" in shape:
        cx, cy, r = shape["halfdisk"]
        return y >= cy - grow and math.hypot(x - cx, y - cy) <= r + grow
    return False


def covered_offsets(d: PartDef) -> set[tuple[int, int]]:
    """Hole offsets (from pin 1, rotation 0) under the body, not counting the part's own pins."""
    for m in d.marks:
        if "covers" in m:
            return set(m["covers"]) - set(d.pins.values())
    xs, ys = [], []
    for s in d.body:
        for k in ("rect", "circle", "halfdisk"):
            if k in s:
                v = s[k]
                if k == "rect":
                    xs += [v[0], v[2]]
                    ys += [v[1], v[3]]
                else:
                    xs += [v[0] - v[2], v[0] + v[2]]
                    ys += [v[1] - v[2], v[1] + v[2]]
    if not xs:
        return set()
    out = set()
    for i in range(math.floor(min(xs) / PITCH) - 1, math.ceil(max(xs) / PITCH) + 2):
        for j in range(math.floor(min(ys) / PITCH) - 1, math.ceil(max(ys) / PITCH) + 2):
            if any(_inside(s, i * PITCH, j * PITCH, LEAD_MARGIN) for s in d.body):
                out.add((i, j))
    return out - set(d.pins.values())


def rotate(off: tuple[float, float], rot: int) -> tuple[float, float]:
    x, y = off
    for _ in range((rot // 90) % 4):
        x, y = -y, x  # clockwise on screen (y down)
    return x, y


# --- the design ---------------------------------------------------------------------------------------


@dataclass
class Placed:
    ref: str
    d: PartDef
    at: tuple[int, int]
    rotation: int
    value: str = ""

    def hole(self, off) -> tuple[int, int]:
        x, y = rotate(off, self.rotation)
        return self.at[0] + round(x), self.at[1] + round(y)

    def pin_holes(self) -> dict[str, tuple[int, int]]:
        return {p: self.hole(o) for p, o in self.d.pins.items()}

    def covered(self) -> set[tuple[int, int]]:
        return {self.hole(o) for o in covered_offsets(self.d)}

    def to_board(self, x: float, y: float) -> tuple[float, float]:
        """mm offset from pin 1 (rotation 0) to board hole units (floats)."""
        rx, ry = rotate((x / PITCH, y / PITCH), self.rotation)
        return self.at[0] + rx, self.at[1] + ry


@dataclass
class Wire:
    net: str
    side: str  # "bottom", "top" (solder track on the component side) or "jumper"
    path: list[tuple[int, int]]
    locked: bool = True

    def to_dict(self) -> dict:
        return {"net": self.net, "side": self.side, "path": [hole_name(h) for h in self.path]}


@dataclass
class Design:
    cols: int
    rows: int
    parts: dict[str, Placed]
    nets: dict[str, list[tuple[str, str]]]
    wires: list[Wire]
    title: str = ""
    board_type: str = "perfboard"
    raw: dict = field(default_factory=dict)
    pads: str = "single"

    @property
    def double(self) -> bool:
        return self.pads != "single"

    def on_board(self, h) -> bool:
        return 0 <= h[0] < self.cols and 0 <= h[1] < self.rows

    def pin_hole(self, ref: str, pin: str) -> tuple[int, int]:
        return self.parts[ref].pin_holes()[pin]

    def pin_owner(self) -> dict[tuple[int, int], tuple[str, str]]:
        out = {}
        for p in self.parts.values():
            for pin, h in p.pin_holes().items():
                out.setdefault(h, (p.ref, pin))
        return out

    def net_of_pin(self) -> dict[tuple[str, str], str]:
        return {rp: n for n, rps in self.nets.items() for rp in rps}

    def to_dict(self) -> dict:
        out = dict(self.raw)
        out["wires"] = [w.to_dict() for w in self.wires]
        return out


def load(design) -> Design:
    if isinstance(design, (str, Path)):
        p = Path(design)
        if not p.exists():
            raise DesignError(f"{p} does not exist")
        design = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(design, dict):
        raise DesignError("a design is a dict with board, parts, nets and optionally wires")
    raw = json.loads(json.dumps(design))
    b = design.get("board") or {}
    btype = b.get("type", "perfboard")
    if btype != "perfboard":
        raise DesignError(f"board type '{btype}' isn't supported yet; only 'perfboard' (separate pads). Stripboard comes later.")
    try:
        cols, rows = int(b["cols"]), int(b["rows"])
    except (KeyError, TypeError, ValueError):
        raise DesignError("board needs cols and rows (number of holes across and down)") from None
    if not (2 <= cols <= 200 and 2 <= rows <= 200):
        raise DesignError("board cols and rows must be between 2 and 200")
    pads = str(b.get("pads", "single"))
    if pads not in PADS:
        raise DesignError(f"board pads must be one of {', '.join(PADS)}, not '{pads}'")
    parts: dict[str, Placed] = {}
    for item in design.get("parts", []):
        ref = str(item.get("ref", "")).strip()
        if not ref:
            raise DesignError(f"part without a ref: {item}")
        if ref in parts:
            raise DesignError(f"ref {ref} is used twice")
        rot = int(item.get("rotation", 0))
        if rot % 90:
            raise DesignError(f"{ref}: rotation must be a multiple of 90")
        try:
            d = part_def(item.get("part"), item.get("params"))
        except DesignError as e:
            raise DesignError(f"{ref}: {e}") from None
        parts[ref] = Placed(ref, d, parse_hole(item.get("at", "A1")), rot % 360, str(item.get("value", "")))
    nets: dict[str, list[tuple[str, str]]] = {}
    for name, pins in (design.get("nets") or {}).items():
        lst = []
        for s in pins:
            if "." not in str(s):
                raise DesignError(f"net {name}: '{s}' should be REF.PIN, like U1.3")
            ref, pin = str(s).rsplit(".", 1)
            lst.append((ref, pin))
        nets[str(name)] = lst
    wires = []
    for w in design.get("wires", []) or []:
        side = w.get("side", "bottom")
        if side not in ("bottom", "top", "jumper"):
            raise DesignError(f"wire side must be 'bottom', 'top' or 'jumper', not '{side}'")
        path = [parse_hole(h) for h in w.get("path", [])]
        wires.append(Wire(str(w.get("net", "")), side, path))
    return Design(cols, rows, parts, nets, wires, str(design.get("title", "")), btype, raw, pads)


# --- geometry of wires --------------------------------------------------------------------------------


def _run(a, b) -> list[tuple[int, int]]:
    """Holes from a to b along a row or column, both ends included."""
    if a[0] != b[0] and a[1] != b[1]:
        raise DesignError(f"{hole_name(a)} to {hole_name(b)} is not along one row or column")
    dx = (b[0] > a[0]) - (b[0] < a[0])
    dy = (b[1] > a[1]) - (b[1] < a[1])
    n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
    return [(a[0] + dx * i, a[1] + dy * i) for i in range(n + 1)]


def wire_holes(w: Wire) -> list[tuple[int, int]]:
    """Bottom and top tracks: every pad the wire is soldered to. Jumper: the two end holes."""
    if w.side == "jumper":
        return [w.path[0], w.path[-1]]
    out = [w.path[0]]
    for a, b in zip(w.path, w.path[1:]):
        out += _run(a, b)[1:]
    return out


def wire_top_cover(w: Wire) -> list[tuple[int, int]]:
    """Holes a wire takes on the component side: a jumper's whole run, ends included, or a top track's pads."""
    if w.side == "jumper":
        return _run(w.path[0], w.path[-1])
    return wire_holes(w) if w.side == "top" else []


def vias(d: Design) -> list[tuple[str, tuple[int, int]]]:
    """Holes where a net goes from a top track to a bottom track with no lead or jumper through the hole. On a
    plated board the plating joins them; on an unplated one each needs a bit of wire through, soldered both sides."""
    if not d.double:
        return []
    pins = set(d.pin_owner())
    ends = {h for w in d.wires if w.side == "jumper" for h in (w.path[0], w.path[-1])}
    top: dict = {}
    bottom: dict = {}
    for w in d.wires:
        if w.side in ("top", "bottom"):
            try:
                for h in wire_holes(w):
                    (top if w.side == "top" else bottom).setdefault(h, w.net)
            except DesignError:
                continue
    return sorted(((n, h) for h, n in top.items() if bottom.get(h) == n and h not in pins and h not in ends),
                  key=lambda x: (x[1][1], x[1][0]))


# --- check --------------------------------------------------------------------------------------------


def _issue(level, code, msg, holes=()):
    return {"level": level, "code": code, "message": msg, "holes": [hole_name(h) for h in holes]}


def check(design) -> dict:
    """Placement and wiring check. Errors make the board wrong or unbuildable; warnings are worth a look."""
    d = design if isinstance(design, Design) else load(design)
    issues = []
    pin_at: dict[tuple[int, int], list[str]] = {}
    cover_at: dict[tuple[int, int], list[str]] = {}
    for p in d.parts.values():
        for pin, h in p.pin_holes().items():
            pin_at.setdefault(h, []).append(f"{p.ref}.{pin}")
            if not d.on_board(h):
                issues.append(_issue("error", "pin_off_board", f"{p.ref} pin {pin} is off the board", [h]))
        for h in p.covered():
            cover_at.setdefault(h, []).append(p.ref)
            if not d.on_board(h):
                issues.append(_issue("warning", "body_off_board", f"{p.ref}'s body hangs over the board edge", [h]))
        for n in p.d.notes:
            if any(w in n for w in ("bend", "spread", "not on the grid", "meter")):
                issues.append(_issue("info", "lead_note", f"{p.ref}: {n}"))
    for h, who in pin_at.items():
        if len(who) > 1:
            issues.append(_issue("error", "hole_shared", f"{' and '.join(who)} are in the same hole", [h]))
        refs_cov = [r for r in cover_at.get(h, []) if r not in {w.split('.')[0] for w in who}]
        if refs_cov:
            issues.append(_issue("error", "pin_under_body", f"{who[0]} goes into a hole under {', '.join(refs_cov)}'s body", [h]))
    # body against body (shared covered holes)
    seen = set()
    for h, refs in cover_at.items():
        if len(refs) > 1:
            key = tuple(sorted(refs))
            if key not in seen:
                seen.add(key)
                issues.append(_issue("error", "bodies_overlap", f"{' and '.join(key)} overlap", [h]))
    # nets
    pin_net: dict[tuple[str, str], str] = {}
    for net, rps in d.nets.items():
        for ref, pin in rps:
            if ref not in d.parts:
                issues.append(_issue("error", "unknown_part", f"net {net}: no part {ref}"))
                continue
            if pin not in d.parts[ref].d.pins:
                issues.append(_issue("error", "unknown_pin", f"net {net}: {ref} has no pin {pin} (pins: {', '.join(d.parts[ref].d.pins)})"))
                continue
            if (ref, pin) in pin_net and pin_net[(ref, pin)] != net:
                issues.append(_issue("error", "pin_in_two_nets", f"{ref}.{pin} is in {pin_net[(ref, pin)]} and {net}"))
            pin_net[(ref, pin)] = net
    unused = [f"{p.ref}.{pin}" for p in d.parts.values() for pin in p.d.pins if (p.ref, pin) not in pin_net]
    if unused:
        issues.append(_issue("info", "unconnected_pins", f"pins in no net: {', '.join(unused)}"))
    conn = connectivity(d)
    issues += conn["issues"]
    errors = [i for i in issues if i["level"] == "error"]
    return {"ok": not errors, "errors": len(errors), "issues": issues, "open": conn["open"], "nets_done": conn["done"]}


def _sides(d: Design, w: Wire) -> tuple[str, ...]:
    """Which pad layers a wire's holes touch: 'b' bottom, 't' top. On single-sided and plated boards a hole is one
    node, called 'b'."""
    if d.pads != "double-unplated":
        return ("b",)
    return {"bottom": ("b",), "top": ("t",), "jumper": ("b", "t")}[w.side]


def _node(d: Design, h, side: str):
    return (h, side if d.pads == "double-unplated" else "b")


def connectivity(d: Design) -> dict:
    """Which nets the wires join completely, which pins are still apart, and wiring faults (shorts, bad jumpers).
    On an unplated double-sided board a hole's top and bottom pads are separate nodes, joined by a lead, a jumper end,
    or a via wire wherever one net has copper on both sides of the hole."""
    issues = []
    owner: dict = {}  # node -> net
    pin_holes = d.pin_owner()
    net_of = d.net_of_pin()
    for h, (ref, pin) in pin_holes.items():
        for s in ("b", "t"):
            owner[_node(d, h, s)] = net_of.get((ref, pin), f"(unconnected {ref}.{pin})")
    covered = {h: p.ref for p in d.parts.values() for h in p.covered()}
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        parent[find(a)] = find(b)

    for h in pin_holes:
        join(_node(d, h, "b"), _node(d, h, "t"))
    top_used: dict[tuple[int, int], tuple] = {}  # hole -> (tag, net, kind) on the component side
    for i, w in enumerate(d.wires):
        tag = f"{w.side} wire {i + 1} ({w.net})"
        if len(w.path) < 2:
            issues.append(_issue("error", "bad_wire", f"{tag} needs at least two holes"))
            continue
        try:
            holes = wire_holes(w)
            cover = wire_top_cover(w)
        except DesignError as e:
            issues.append(_issue("error", "bad_wire", f"{tag}: {e}"))
            continue
        if w.side == "jumper" and len(w.path) != 2:
            issues.append(_issue("error", "bad_wire", f"{tag}: a jumper is one straight wire, two holes"))
            continue
        if w.net not in d.nets:
            issues.append(_issue("warning", "wire_unknown_net", f"{tag}: no net called {w.net}"))
        off = [h for h in holes + cover if not d.on_board(h)]
        if off:
            issues.append(_issue("error", "wire_off_board", f"{tag} leaves the board", off[:1]))
            continue
        for h in holes:
            for s in _sides(d, w):
                o = owner.get(_node(d, h, s))
                if o is not None and o != w.net:
                    where = {"b": "bottom pad", "t": "top pad"}[s] if d.pads == "double-unplated" else "pad"
                    issues.append(_issue("error", "short", f"{tag} touches {o} at {hole_name(h)} ({where})", [h]))
                owner.setdefault(_node(d, h, s), w.net)
        if w.side == "jumper":
            for h in (w.path[0], w.path[-1]):
                join(_node(d, h, "b"), _node(d, h, "t"))
                if h in pin_holes:
                    issues.append(_issue("error", "jumper_in_pin_hole", f"{tag} ends in {'.'.join(pin_holes[h])}'s hole", [h]))
                if h in covered:
                    issues.append(_issue("error", "jumper_under_body", f"{tag} ends under {covered[h]}'s body", [h]))
            for h in cover[1:-1]:
                if h in pin_holes:
                    issues.append(_issue("error", "jumper_over_pin", f"{tag} lies over {'.'.join(pin_holes[h])}", [h]))
                elif h in covered:
                    issues.append(_issue("error", "jumper_over_body", f"{tag} lies over {covered[h]}'s body", [h]))
        if w.side == "top":
            if not d.double:
                issues.append(_issue("error", "top_track_single_sided", f"{tag}: this board has pads on the bottom only "
                                      "(set board pads to 'double' or 'double-unplated' if it has them on top too)"))
            for h in holes:
                if h in covered:
                    issues.append(_issue("error", "top_track_under_body", f"{tag} runs under {covered[h]}'s body, where it can't be soldered", [h]))
        ends = (w.path[0], w.path[-1]) if w.side == "jumper" else ()
        for h in cover:
            prev = top_used.get(h)
            if prev:
                ptag, pnet, pkind = prev
                both_tracks = pkind == "top" and w.side == "top"
                end_on_track = (w.side == "jumper" and pkind == "top" and h in ends) or \
                               (w.side == "top" and pkind == "jumper-end")
                if pnet != w.net or not (both_tracks or end_on_track):
                    code = "jumpers_cross" if w.side == "jumper" and pkind.startswith("jumper") else "top_crossing"
                    issues.append(_issue("error", code, f"{tag} crosses {ptag}", [h]))
            kind = ("jumper-end" if h in ends else "jumper") if w.side == "jumper" else "top"
            if not prev or kind == "jumper":
                top_used[h] = (tag, w.net, kind)
        s0 = _sides(d, w)[0] if w.side != "jumper" else "b"
        for a, b in zip(holes, holes[1:]):
            if w.side == "jumper":
                join(_node(d, a, "b"), _node(d, b, "b"))
            else:
                join(_node(d, a, s0), _node(d, b, s0))
    # via wires: one net on both pads of a hole
    if d.pads == "double-unplated":
        for (h, s), n in list(owner.items()):
            if s == "b" and owner.get((h, "t")) == n:
                join((h, "b"), (h, "t"))
    done, open_ = [], []
    for net, rps in d.nets.items():
        valid = [rp for rp in rps if rp[0] in d.parts and rp[1] in d.parts[rp[0]].d.pins]
        if len(valid) < 2:
            continue
        groups: dict = {}
        for r, p in valid:
            groups.setdefault(find(_node(d, d.pin_hole(r, p), "b")), []).append(f"{r}.{p}")
        if len(groups) == 1:
            done.append(net)
        else:
            open_.append({"net": net, "groups": list(groups.values())})
    return {"issues": issues, "open": open_, "done": done}


# --- router -------------------------------------------------------------------------------------------
# Holes are the routing resources. A pad belongs to at most one net. On single-sided and plated boards a hole is one
# pad; on an unplated double-sided board its top and bottom pads are separate, so a top track can pass over a hole
# whose bottom pad another net uses, which is what makes the second side worth having. On the component side a hole
# carries at most one thing: a lead, a body, a jumper, or one net's top track. The router negotiates (PathFinder
# style): every net is routed with a price on holes other nets want, the price of contested holes goes up each round,
# until no hole is shared. If it doesn't settle, the result is made legal net by net and anything left is reported
# open. A plain ordered router runs too, with rip-up by reordering, and the better of the two is kept.

JUMPER_COST = 6.0
JUMPER_PER_HOLE = 0.6
TURN_COST = 0.6
TOP_STEP = 1.3  # a top track costs a little more than the bottom: soldering among the parts is fiddlier
VIA_COST = 4.0  # unplated board: a bit of wire through the hole, soldered both sides
MAX_JUMPER = 12
NEGOTIATE_ROUNDS = 30
NEGOTIATE_PATIENCE = 8  # rounds without fewer shared holes before giving up


@dataclass
class _Grid:
    d: Design
    owner: dict  # (hole, side) node -> net owning that pad: pins and wires (see _node)
    pins: set
    covered: set
    top_owner: dict  # hole -> (net, "jumper" | "top") on the component side

    def free(self, h, side, net) -> bool:
        return self.d.on_board(h) and self.owner.get(_node(self.d, h, side), net) == net


def _base_grid(d: Design, wires: list[Wire]) -> _Grid:
    net_of = d.net_of_pin()
    owner, pins = {}, set()
    for h, (ref, pin) in d.pin_owner().items():
        for s in ("b", "t"):
            owner[_node(d, h, s)] = net_of.get((ref, pin), f"(unconnected {ref}.{pin})")
        pins.add(h)
    covered = {h for p in d.parts.values() for h in p.covered()}
    g = _Grid(d, owner, pins, covered, {})
    for w in wires:
        _commit(g, w)
    return g


def _commit(g: _Grid, w: Wire) -> None:
    for h in wire_holes(w):
        for s in _sides(g.d, w):
            g.owner.setdefault(_node(g.d, h, s), w.net)
    kind = "jumper" if w.side == "jumper" else "top"
    for h in wire_top_cover(w):
        if h not in g.top_owner or kind == "jumper":
            g.top_owner[h] = (w.net, kind)


_DIRS = [(1, 0), (0, 1), (-1, 0), (0, -1)]


class _Costs:
    """Extra price of using a hole, None where it can't be used: its bottom pad (pad), its top pad (pad_t; the same
    as pad unless the board is unplated), its top side under a jumper (jump), its top side for a top track (track)."""

    def __init__(self, pad, pad_t, jump, track):
        self.pad, self.pad_t, self.jump, self.track = pad, pad_t, jump, track


def _strict_costs(g: _Grid, net: str, allow_tracks: bool) -> _Costs:
    d = g.d

    def pad(h):
        return 0.0 if g.free(h, "b", net) else None

    def pad_t(h):
        return 0.0 if g.free(h, "t", net) else None

    def jump(h):
        return None if h in g.pins or h in g.covered or h in g.top_owner or not d.on_board(h) else 0.0

    def track(h):
        if not allow_tracks or h in g.covered or not d.on_board(h):
            return None
        o = g.top_owner.get(h)
        return 0.0 if o is None or o == (net, "top") else None

    return _Costs(pad, pad_t, jump, track)


def _search(d: Design, sources: set, targets: set, allow_jumpers: bool, c: _Costs, pins: set):
    """Cheapest path from any source hole to any target hole. Returns [(hole, how)] with how in
    {'start', 'bottom', 'top', 'jumper'}, or None. State: hole, direction, side (0 bottom, 1 top, 2 off a jumper)."""
    dist, prev, heap, tie = {}, {}, [], 0
    for s in sources:
        dist[(s, -1, -1)] = 0.0
        heap.append((0.0, tie, s, -1, -1))
        tie += 1
    heapq.heapify(heap)
    tl = list(targets)

    def est(h):
        return min(abs(h[0] - x) + abs(h[1] - y) for x, y in tl) if len(tl) < 40 else 0.0

    def push(key, cost, how, frm):
        nonlocal tie
        if cost < dist.get(key, 1e18):
            dist[key] = cost
            prev[key] = (frm, how)
            tie += 1
            heapq.heappush(heap, (cost + est(key[0]), tie) + key)

    while heap:
        f, _, cur, dr, ly = heapq.heappop(heap)
        state = (cur, dr, ly)
        cost = dist.get(state)
        if cost is None or f - est(cur) > cost + 1e-9:
            continue
        if cur in targets and cur not in sources:
            path, key = [], state
            while key in prev:
                pkey, how = prev[key]
                path.append((key[0], how))
                key = pkey
            path.append((key[0], "start"))
            return path[::-1]
        # changing side here: free through a lead or a jumper end (ly 2), a via wire otherwise
        change = 0.0 if cur in pins or ly == 2 else VIA_COST
        bcur = c.pad(cur)
        tcur = c.track(cur)
        if tcur is not None and c.pad_t(cur) is None:
            tcur = None
        jcur = c.jump(cur) if allow_jumpers and ly != 2 and bcur is not None and c.pad_t(cur) is not None else None
        for k, (dx, dy) in enumerate(_DIRS):
            n = (cur[0] + dx, cur[1] + dy)
            if bcur is not None:
                pc = c.pad(n)
                if pc is not None:
                    turn = TURN_COST if ly == 0 and dr != k else 0.0
                    push((n, k, 0), cost + 1.0 + pc + turn + (change + bcur if ly == 1 else 0.0), "bottom", state)
            if tcur is not None:
                pt, tn = c.pad_t(n), c.track(n)
                if pt is not None and tn is not None:
                    turn = TURN_COST if ly == 1 and dr != k else 0.0
                    sw = change if ly in (0, -1) else 0.0
                    push((n, k, 1), cost + TOP_STEP + pt + tn + turn + sw, "top", state)
            if jcur is None:
                continue
            running = 0.0
            for L in range(2, MAX_JUMPER + 1):
                mid = (cur[0] + dx * (L - 1), cur[1] + dy * (L - 1))
                tm = c.jump(mid)
                if tm is None:
                    break
                running += tm
                m = (cur[0] + dx * L, cur[1] + dy * L)
                if not d.on_board(m):
                    break
                pc, pt, te = c.pad(m), c.pad_t(m), c.jump(m)
                if pc is None or pt is None or te is None:
                    continue
                push((m, k, 2), cost + JUMPER_COST + JUMPER_PER_HOLE * L + jcur + running + pc + pt + te, "jumper", state)
    return None


def _path_wires(net: str, path) -> list[Wire]:
    out, run, kind = [], [path[0][0]], None

    def flush():
        if kind and len(run) > 1:
            out.append(Wire(net, kind, list(run), locked=False))

    for h, how in path[1:]:
        if how == "jumper":
            flush()
            out.append(Wire(net, "jumper", [run[-1], h], locked=False))
            run, kind = [h], None
        elif kind is None or how == kind:
            run.append(h)
            kind = how
        else:
            flush()
            run, kind = [run[-1], h], how
    flush()
    return out


def _route_tree(d: Design, net: str, pins: list, existing: list[Wire], allow_jumpers: bool, c: _Costs, on_wires,
                pin_holes: set):
    """Join a net's pins (and the copper it already has) into one tree, nearest piece first.
    Returns (new wires, number of joins that failed)."""
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for w in existing:
        if w.net == net:
            hs = wire_holes(w)
            for a, b in zip(hs, hs[1:]):
                parent[find(a)] = find(b)
    groups: dict = {}
    for h in pins:
        groups.setdefault(find(h), set()).add(h)
    for w in existing:
        if w.net == net:
            for h in wire_holes(w):
                if find(h) in groups:
                    groups[find(h)].add(h)
    comps = sorted(groups.values(), key=lambda s: min(s))
    new, failed = [], 0
    if len(comps) < 2:
        return new, 0
    tree = comps.pop(0)
    while comps:
        path = _search(d, tree, set().union(*comps), allow_jumpers, c, pin_holes)
        if path is None:
            failed += 1  # this piece can't reach the rest; carry on joining the others among themselves
            tree = comps.pop(0)
            continue
        ws = _path_wires(net, path)
        on_wires(ws)
        new += ws
        tree |= {h for h, _ in path}
        hit = next(cc for cc in comps if path[-1][0] in cc)
        comps.remove(hit)
        tree |= hit
    return new, failed


def _hpwl(holes) -> int:
    xs = [h[0] for h in holes]
    ys = [h[1] for h in holes]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def _score(added: list[Wire], failed: dict) -> tuple:
    cost = sum(len(wire_holes(w)) * (TOP_STEP if w.side == "top" else 1.0) for w in added if w.side != "jumper") + \
        sum(JUMPER_COST for w in added if w.side == "jumper")
    return (sum(failed.values()), sum(1 for w in added if w.side == "jumper"), cost)


@dataclass
class _Job:
    d: Design
    locked: list
    nets: dict
    allow_jumpers: bool
    allow_tracks: bool


def _fits(g: _Grid, w: Wire) -> bool:
    if any(not g.free(h, s, w.net) for h in wire_holes(w) for s in _sides(g.d, w)):
        return False
    for h in wire_top_cover(w):
        o = g.top_owner.get(h)
        if w.side == "jumper" and (o is not None or h in g.pins or h in g.covered):
            return False
        if w.side == "top" and (h in g.covered or (o is not None and o != (w.net, "top"))):
            return False
    return True


def _strict_pass(job: _Job, order: list, keep: dict | None = None):
    """Route nets one after the other on a grid where everything routed so far is fixed. keep: routes (net -> wires)
    to take as they are when they still fit."""
    g = _base_grid(job.d, job.locked)
    added, failed = [], {}
    for net in order:
        if keep and net in keep:
            ws = keep[net]
            if all(_fits(g, w) for w in ws):
                for w in ws:
                    _commit(g, w)
                added += ws
                continue
        c = _strict_costs(g, net, job.allow_tracks)
        ws, f = _route_tree(job.d, net, job.nets[net], job.locked, job.allow_jumpers, c,
                            lambda w_: [_commit(g, x) for x in w_], g.pins)
        added += ws
        if f:
            failed[net] = f
    return added, failed


def _negotiate(job: _Job, order: list):
    d = job.d
    g = _base_grid(d, job.locked)
    pad_use: dict = {}  # node -> {net: count}
    top_use: dict = {}  # hole -> {(net, kind): count}
    hist_pad: dict = {}
    hist_top: dict = {}
    routes: dict = {n: [] for n in job.nets}
    pres = 0.5
    best_over, stale = None, 0

    def use(ws, net, k):
        for w in ws:
            for h in set(wire_holes(w)):
                for s in _sides(d, w):
                    cnt = pad_use.setdefault(_node(d, h, s), {})
                    cnt[net] = cnt.get(net, 0) + k
            kind = "jumper" if w.side == "jumper" else "top"
            for h in set(wire_top_cover(w)):
                cnt = top_use.setdefault(h, {})
                cnt[(net, kind)] = cnt.get((net, kind), 0) + k

    def top_conflict(cnt) -> bool:
        live = {key: v for key, v in cnt.items() if v > 0}
        if len({n for n, _ in live}) > 1:
            return True
        return any(kind == "jumper" for _, kind in live) and sum(live.values()) > 1

    for _ in range(NEGOTIATE_ROUNDS):
        failed = {}
        for net in order:
            use(routes[net], net, -1)

            def pad(h, net=net, side="b"):
                if not g.free(h, side, net):
                    return None
                node = _node(d, h, side)
                others = sum(1 for n, v in pad_use.get(node, {}).items() if n != net and v > 0)
                return hist_pad.get(node, 0.0) + pres * others * 4

            def pad_t(h, net=net):
                return pad(h, net, "t")

            def jump(h):
                if h in g.pins or h in g.covered or h in g.top_owner or not d.on_board(h):
                    return None
                return hist_top.get(h, 0.0) + pres * sum(v for v in top_use.get(h, {}).values() if v > 0) * 4

            def track(h, net=net):
                if not job.allow_tracks or h in g.covered or not d.on_board(h):
                    return None
                o = g.top_owner.get(h)
                if o is not None and o != (net, "top"):
                    return None
                others = sum(v for key, v in top_use.get(h, {}).items() if key != (net, "top") and v > 0)
                return hist_top.get(h, 0.0) + pres * others * 4

            ws, f = _route_tree(d, net, job.nets[net], job.locked, job.allow_jumpers, _Costs(pad, pad_t, jump, track),
                                lambda w_, net=net: use(w_, net, 1), g.pins)
            routes[net] = ws
            if f:
                failed[net] = f
        over_pad = [h for h, cnt in pad_use.items() if sum(1 for v in cnt.values() if v > 0) > 1]
        over_top = [h for h, cnt in top_use.items() if top_conflict(cnt)]
        if not over_pad and not over_top:
            break
        n_over = len(over_pad) + len(over_top)
        if best_over is None or n_over < best_over:
            best_over, stale = n_over, 0
        else:
            stale += 1
            if stale >= NEGOTIATE_PATIENCE:
                break
        for h in over_pad:
            hist_pad[h] = hist_pad.get(h, 0.0) + 1.0
        for h in over_top:
            hist_top[h] = hist_top.get(h, 0.0) + 1.0
        pres *= 1.5
    # make it legal: keep each net's negotiated route where it still fits, reroute the rest around what's kept
    return _strict_pass(job, order, keep=routes)


def _repair(job: _Job, added: list, failed: dict, order: list, rounds: int = 8):
    """Rip-up and reroute for what's still open: route an open net as if other nets' copper could be crossed at a
    price, rip up the nets in its way, route it first, then put the others back around it."""
    best = (_score(added, failed), added, failed)
    tried = set()
    for _ in range(rounds):
        _, added, failed = best
        todo = [n for n in failed if n not in tried]
        if not todo:
            break
        x = todo[0]
        tried.add(x)
        routes: dict = {}
        for w in added:
            routes.setdefault(w.net, []).append(w)
        g = _base_grid(job.d, job.locked + [w for w in added if w.net != x])
        d = job.d
        hard = _base_grid(d, job.locked)  # pins and your wires can't be ripped up

        def pad(h, side="b"):
            if not d.on_board(h):
                return None
            node = _node(d, h, side)
            if hard.owner.get(node, x) != x:
                return None
            o = g.owner.get(node, x)
            return 0.0 if o == x else 15.0

        def pad_t(h):
            return pad(h, "t")

        def jump(h):
            if h in g.pins or h in g.covered or h in hard.top_owner or not d.on_board(h):
                return None
            return 15.0 if h in g.top_owner else 0.0

        def track(h):
            if not job.allow_tracks or h in g.covered or not d.on_board(h) or h in hard.top_owner:
                return None
            o = g.top_owner.get(h)
            return 0.0 if o is None or o == (x, "top") else 15.0

        ws, f = _route_tree(d, x, job.nets[x], job.locked, job.allow_jumpers, _Costs(pad, pad_t, jump, track),
                            lambda w_: None, g.pins)
        if f:
            continue
        blockers = set()
        for w in ws:
            for h in wire_holes(w):
                for sd in _sides(d, w):
                    o = g.owner.get(_node(d, h, sd))
                    if o and o != x and o in job.nets:
                        blockers.add(o)
            for h in wire_top_cover(w):
                o = g.top_owner.get(h)
                if o and o[0] != x and o[0] in job.nets:
                    blockers.add(o[0])
        if not blockers:
            continue
        new_order = [x] + [n for n in order if n in blockers] + [n for n in order if n != x and n not in blockers]
        keep = {n: r for n, r in routes.items() if n not in blockers and n != x}
        a2, f2 = _strict_pass(job, new_order, keep=keep)
        if _score(a2, f2) < best[0]:
            best = (_score(a2, f2), a2, f2)
            tried.clear()
            tried.add(x)
    return best[1], best[2]


def route(design, passes: int = 12, allow_jumpers: bool = True, allow_top_tracks: bool = True, seed: int = 1,
          keep_wires: bool = True) -> dict:
    """Route every net that isn't joined yet. Wires already in the design are kept (they're treated as yours).
    Top tracks are only routed on unplated double-sided boards: on a plated board a hole's two pads are one node, so
    the top side adds no room, only somewhere harder to solder. Returns the routed design (with all wires), what's
    still open, and counts."""
    d = design if isinstance(design, Design) else load(design)
    pre = check(d)
    fatal = [i for i in pre["issues"] if i["level"] == "error"]
    if fatal:
        return {"ok": False, "error": "fix the placement and wiring errors first", "issues": fatal}
    locked = list(d.wires) if keep_wires else []
    nets = {}
    for net, rps in d.nets.items():
        hs = [d.pin_hole(r, p) for r, p in rps]
        if len(hs) >= 2:
            nets[net] = hs
    job = _Job(d, locked, nets, allow_jumpers, allow_top_tracks and d.pads == "double-unplated")
    order = sorted(nets, key=lambda n: (_hpwl(nets[n]), len(nets[n])))
    added, failed = _negotiate(job, order)
    best = (_score(added, failed), added, failed)
    rng = random.Random(seed)
    k = 0
    while best[0][0] and k < passes:  # still open: try plain ordered passes, failed nets first
        rest = [n for n in order if n not in failed]
        rest.sort(key=lambda n: _hpwl(nets[n]) + rng.random() * 4)
        order = sorted(failed, key=lambda n: -failed[n]) + rest
        added, failed = _strict_pass(job, order)
        if _score(added, failed) < best[0]:
            best = (_score(added, failed), added, failed)
        k += 1
    if best[2]:
        added, failed = _repair(job, best[1], best[2], order)
        if _score(added, failed) < best[0]:
            best = (_score(added, failed), added, failed)
    d.wires = locked + _merge(best[1])
    after = check(d)
    return summary(d, after) | {"design": d.to_dict(), "nets_total": len(nets)}


def summary(d: Design, chk: dict | None = None) -> dict:
    chk = chk or check(d)
    out = {
        "ok": not chk["open"] and chk["ok"],
        "open": chk["open"],
        "issues": [i for i in chk["issues"] if i["level"] != "info"],
        "jumpers": sum(1 for w in d.wires if w.side == "jumper"),
        "bottom_runs": sum(1 for w in d.wires if w.side == "bottom"),
        "nets_done": len(chk["nets_done"]),
    }
    if d.double:
        out["top_runs"] = sum(1 for w in d.wires if w.side == "top")
        v = vias(d)
        if d.pads == "double-unplated":
            out["via_wires"] = [{"net": n, "hole": hole_name(h)} for n, h in v]
        else:
            out["side_changes"] = len(v)
    return out


def _merge(wires: list[Wire]) -> list[Wire]:
    """Track paths reduced to their corners, so the build list reads 'C3 to C9 to F9'."""
    out = []
    for w in wires:
        if w.side == "jumper":
            out.append(w)
            continue
        pts = [w.path[0]]
        for h in w.path[1:]:
            if len(pts) >= 2 and ((pts[-2][0] == pts[-1][0] == h[0]) or (pts[-2][1] == pts[-1][1] == h[1])):
                pts[-1] = h
            else:
                pts.append(h)
        out.append(Wire(w.net, w.side, pts, locked=False))
    return out


def save(design: dict, path) -> Path:
    p = Path(path)
    p.write_text(json.dumps(design, indent=2) + "\n", encoding="utf-8")
    return p

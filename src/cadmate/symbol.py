"""Read KiCad symbols (.kicad_sym) and put symbols into a library file.

Reads every symbol library format from KiCad 6 onward, and the one-file-per-symbol
folders (.kicad_symdir) KiCad's own library uses since KiCad 10. Derived symbols
("extends") are resolved, so a symbol reads the same whether it is drawn itself
or borrows its parent's drawing. Coordinates are mm in KiCad's symbol frame:
Y points up, and a pin's position is its connection point (the outer end).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr

GRID = 2.54  # 100 mil, KiCad's pin grid
FORMAT_VERSION = "20251024"  # KiCad 10, the oldest version cadmate supports

PIN_TYPES = (
    "input", "output", "bidirectional", "tri_state", "passive", "free", "unspecified",
    "power_in", "power_out", "open_collector", "open_emitter", "no_connect",
)
PIN_SHAPES = (
    "line", "inverted", "clock", "inverted_clock", "input_low", "clock_low", "output_low",
    "edge_clock_high", "non_logic",
)
# Pin rotation in the file -> the side of the body the pin sits on.
SIDES = {0: "left", 180: "right", 90: "bottom", 270: "top"}


def stack_number(numbers: list[str]) -> str:
    """KiCad 10's native pin stack: one pin standing for several pads, numbered [1,5,8-10]."""
    def key(n: str):
        m = re.fullmatch(r"([A-Za-z]*)(\d+)", n)
        return (m.group(1), int(m.group(2))) if m else (n, -1)

    parts: list[str] = []
    run: list[tuple[str, int]] = []

    def flush():
        if len(run) >= 3:
            parts.append(f"{run[0][0]}{run[0][1]}-{run[-1][0]}{run[-1][1]}")
        else:
            parts.extend(f"{p}{n}" for p, n in run)
        run.clear()

    for n in sorted(numbers, key=key):
        prefix, num = key(n)
        if num < 0:
            flush()
            parts.append(n)
        elif run and run[-1][0] == prefix and run[-1][1] + 1 == num:
            run.append((prefix, num))
        else:
            flush()
            run.append((prefix, num))
    flush()
    return "[" + ",".join(parts) + "]"


def expand_number(number: str) -> list[str]:
    """The pad numbers a pin stands for: itself, or every number in a native stack like [1,5,8-10]."""
    m = re.fullmatch(r"\[(.*)\]", number.strip())
    if not m:
        return [number]
    out = []
    for part in m.group(1).split(","):
        part = part.strip()
        r = re.fullmatch(r"([A-Za-z]*)(\d+)-\1?(\d+)", part)
        if r:
            out += [f"{r.group(1)}{i}" for i in range(int(r.group(2)), int(r.group(3)) + 1)]
        elif part:
            out.append(part)
    return out


@dataclass
class Pin:
    number: str
    name: str
    etype: str
    shape: str = "line"
    x: float = 0.0
    y: float = 0.0
    rotation: int = 0
    length: float = GRID
    hidden: bool = False
    unit: int = 1  # 0: common to all units
    style: int = 1  # body style (De Morgan); 0: common to all
    alternates: list[tuple[str, str, str]] = field(default_factory=list)  # (name, type, shape)

    @property
    def side(self) -> str:
        return SIDES.get(self.rotation % 360, "other")

    @property
    def numbers(self) -> list[str]:
        return expand_number(self.number)

    @property
    def inner(self) -> tuple[float, float]:
        """Where the pin meets the body."""
        dx, dy = {0: (1, 0), 180: (-1, 0), 90: (0, 1), 270: (0, -1)}.get(self.rotation % 360, (0, 0))
        return (self.x + dx * self.length, self.y + dy * self.length)

    def to_dict(self) -> dict:
        d = {"number": self.number, "name": self.name, "type": self.etype, "shape": self.shape, "x": self.x, "y": self.y,
             "side": self.side, "length": self.length, "hidden": self.hidden, "unit": self.unit}
        if self.alternates:
            d["alternates"] = [{"name": n, "type": t, "shape": s} for n, t, s in self.alternates]
        return d


@dataclass
class Rect:
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    width: float = 0.254
    fill: str = "background"
    unit: int = 0
    style: int = 0

    @property
    def center(self) -> tuple[float, float]:
        return ((self.xmin + self.xmax) / 2, (self.ymin + self.ymax) / 2)


@dataclass
class Polyline:
    pts: list[tuple[float, float]]
    width: float = 0.254
    fill: str = "background"
    unit: int = 0
    style: int = 0


@dataclass
class Property:
    value: str
    x: float = 0.0
    y: float = 0.0
    hidden: bool = False
    justify: str = ""
    private: bool = False  # KiCad's private fields; the library uses them for KLC exceptions


@dataclass
class Symbol:
    name: str
    properties: dict[str, Property] = field(default_factory=dict)
    pins: list[Pin] = field(default_factory=list)
    rects: list[Rect] = field(default_factory=list)
    polylines: list[Polyline] = field(default_factory=list)
    other_graphics: int = 0  # circles, arcs, text
    pin_names_offset: float = 0.508
    hide_pin_names: bool = False
    hide_pin_numbers: bool = False
    extends: str | None = None
    power: bool = False
    units_locked: bool = False
    format_version: str | None = None

    def prop(self, key: str) -> str | None:
        p = self.properties.get(key)
        return p.value if p else None

    @property
    def unit_count(self) -> int:
        units = {p.unit for p in self.pins} | {r.unit for r in self.rects} | {g.unit for g in self.polylines}
        return max([u for u in units if u > 0], default=1)

    def pins_of(self, unit: int) -> list[Pin]:
        return [p for p in self.pins if p.unit in (unit, 0)]

    def body(self, unit: int) -> Rect | None:
        """The unit's outline: its one rectangle, or the one nearest the origin."""
        rects = [r for r in self.rects if r.unit in (unit, 0)]
        return min(rects, key=lambda r: r.center[0] ** 2 + r.center[1] ** 2) if rects else None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "extends": self.extends,
            "properties": {k: v.value for k, v in self.properties.items()},
            "units": self.unit_count,
            "pins": [p.to_dict() for p in self.pins],
            "bodies": [vars(r) for r in self.rects],
            "pin_names_offset": self.pin_names_offset,
            "format_version": self.format_version,
        }


# --- reading ----------------------------------------------------------------------------------


def _hidden(node: sexpr.Node) -> bool:
    """KiCad 8 writes a bare hide atom; KiCad 9 and later write (hide yes)."""
    if "hide" in node.atoms():
        return True
    h = node.child("hide")
    return h is not None and h.atoms()[:1] != ["no"]


def _property(node: sexpr.Node) -> tuple[str, Property]:
    vals = [a for a in node.items[1:] if isinstance(a, str) and a != "private"]
    key, value = vals[0], vals[1] if len(vals) > 1 else ""
    at = node.child("at")
    xy = at.floats() if at else [0.0, 0.0]
    eff = node.child("effects")
    hidden = _hidden(node) or (eff is not None and _hidden(eff))
    justify = ""
    if eff is not None and eff.child("justify") is not None:
        justify = " ".join(eff.child("justify").atoms())
    return key, Property(value, xy[0], xy[1], hidden, justify, "private" in node.items[1:2])


def _unit_style(sub_name: str) -> tuple[int, int]:
    m = re.search(r"_(\d+)_(\d+)$", sub_name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _pin(node: sexpr.Node, unit: int, style: int) -> Pin:
    atoms = node.atoms()
    at = node.child("at").floats()
    length = node.child("length")
    name = node.child("name")
    number = node.child("number")
    alternates = []
    for alt in node.children("alternate"):
        a = alt.atoms()
        alternates.append((a[0], a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else "line"))
    return Pin(
        number=number.atoms()[0] if number and number.atoms() else "",
        name=name.atoms()[0] if name and name.atoms() else "",
        etype=atoms[0] if atoms else "unspecified",
        shape=atoms[1] if len(atoms) > 1 else "line",
        x=at[0], y=at[1], rotation=int(round(at[2])) % 360 if len(at) > 2 else 0,
        length=length.floats()[0] if length else 0.0,
        hidden=_hidden(node), unit=unit, style=style, alternates=alternates,
    )


def _symbol(node: sexpr.Node, version: str | None) -> Symbol:
    sym = Symbol(name=node.atoms()[0], format_version=version)
    ext = node.child("extends")
    sym.extends = ext.atoms()[0] if ext else None
    sym.power = node.child("power") is not None
    pn = node.child("pin_names")
    if pn is not None:
        off = pn.child("offset")
        if off is not None:
            sym.pin_names_offset = off.floats()[0]
        sym.hide_pin_names = _hidden(pn)
    pnum = node.child("pin_numbers")
    sym.hide_pin_numbers = pnum is not None and _hidden(pnum)
    for p in node.children("property"):
        key, prop = _property(p)
        sym.properties[key] = prop
    if "ki_description" in sym.properties and "Description" not in sym.properties:  # KiCad 6 and 7
        sym.properties["Description"] = sym.properties.pop("ki_description")
    sym.units_locked = "ki_locked" in sym.properties or node.child("unit_locked") is not None
    for sub in node.children("symbol"):
        unit, style = _unit_style(sub.atoms()[0])
        for item in sub.items:
            if not isinstance(item, sexpr.Node):
                continue
            if item.head == "pin":
                sym.pins.append(_pin(item, unit, style))
            elif item.head == "rectangle":
                (x1, y1), (x2, y2) = item.child("start").floats()[:2], item.child("end").floats()[:2]
                stroke = item.child("stroke")
                width = stroke.child("width").floats()[0] if stroke and stroke.child("width") else 0.0
                fill = item.child("fill")
                ftype = fill.child("type").atoms()[0] if fill and fill.child("type") else "none"
                sym.rects.append(Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), width, ftype, unit, style))
            elif item.head == "polyline":
                pts = [tuple(xy.floats()[:2]) for xy in item.child("pts").children("xy")] if item.child("pts") else []
                stroke = item.child("stroke")
                width = stroke.child("width").floats()[0] if stroke and stroke.child("width") else 0.0
                fill = item.child("fill")
                ftype = fill.child("type").atoms()[0] if fill and fill.child("type") else "none"
                sym.polylines.append(Polyline(pts, width, ftype, unit, style))
            elif item.head in ("circle", "arc", "text", "bezier", "text_box"):
                sym.other_graphics += 1
    return sym


def parse_library(text: str) -> list[Symbol]:
    """Every symbol in a .kicad_sym file, with derived symbols resolved against the others in it."""
    root = sexpr.parse(text)
    if root.head != "kicad_symbol_lib":
        raise sexpr.SexprError(f"not a KiCad symbol library (root is {root.head!r})")
    v = root.child("version")
    version = v.atoms()[0] if v else None
    symbols = [_symbol(n, version) for n in root.children("symbol")]
    by_name = {s.name: s for s in symbols}
    return [_resolve(s, by_name.get) for s in symbols]


def _resolve(sym: Symbol, lookup) -> Symbol:
    """Give a derived symbol its parent's pins and drawing, keeping its own fields."""
    seen = {sym.name}
    parent_name = sym.extends
    while parent_name:
        parent = lookup(parent_name)
        if parent is None or parent_name in seen:
            break
        seen.add(parent_name)
        if not sym.pins and not sym.rects:
            sym.pins, sym.rects, sym.other_graphics = list(parent.pins), list(parent.rects), parent.other_graphics
            sym.polylines = list(parent.polylines)
            sym.pin_names_offset, sym.hide_pin_names, sym.hide_pin_numbers = parent.pin_names_offset, parent.hide_pin_names, parent.hide_pin_numbers
            sym.units_locked = sym.units_locked or parent.units_locked
        for k, p in parent.properties.items():
            sym.properties.setdefault(k, p)
        parent_name = parent.extends
    return sym


def read(path: str | Path, name: str | None = None) -> Symbol:
    """Read one symbol from a .kicad_sym library (by name, or the only one), or from a .kicad_symdir file."""
    path = Path(path).expanduser()
    if path.is_dir():
        if name is None:
            raise ValueError(f"{path} is a symbol folder; give the symbol name")
        path = path / f"{name}.kicad_sym"
    symbols = parse_library(path.read_text(encoding="utf-8"))
    if path.parent.suffix == ".kicad_symdir":
        # One symbol per file: a parent lives in its own file next to it.
        def lookup(n: str) -> Symbol | None:
            f = path.parent / f"{n}.kicad_sym"
            return parse_library(f.read_text(encoding="utf-8"))[0] if f.exists() else None

        symbols = [_resolve(s, lookup) if s.extends and not s.pins else s for s in symbols]
    if name is None:
        if len(symbols) != 1:
            raise ValueError(f"{path.name} holds {len(symbols)} symbols; give the symbol name: {[s.name for s in symbols][:20]}")
        return symbols[0]
    for s in symbols:
        if s.name == name:
            return s
    raise ValueError(f"no symbol {name!r} in {path.name}; it has {[s.name for s in symbols][:20]}")


# --- writing into a library ------------------------------------------------------------------------


EMPTY_LIBRARY = f'(kicad_symbol_lib\n\t(version {FORMAT_VERSION})\n\t(generator "cadmate")\n\t(generator_version "0.1")\n)\n'


def parse_symbol(symbol_text: str) -> Symbol:
    """Read a lone symbol block, as the generator writes it."""
    return parse_library(EMPTY_LIBRARY.replace("\n)", "\n" + symbol_text + ")"))[0]


def put_in_library(library_text: str | None, symbol_text: str, name: str, replace: bool = False) -> str:
    """Add a symbol block to a library's text, or replace the one with the same name.

    Everything else in the file is kept byte for byte, so symbols KiCad wrote stay as KiCad wrote them.
    """
    text = library_text if library_text and library_text.strip() else EMPTY_LIBRARY
    root = sexpr.parse(text)
    if root.head != "kicad_symbol_lib":
        raise ValueError("not a KiCad symbol library")
    for node in root.children("symbol"):
        if node.atoms()[:1] == [name]:
            if not replace:
                raise FileExistsError(f"the library already has a symbol {name!r}")
            start = text.rfind("\n", 0, node.start) + 1
            return text[:start] + symbol_text.rstrip("\n") + text[node.end:]
    close = root.end - 1  # the library's closing ")"
    before = text[:close].rstrip()
    return before + "\n" + symbol_text.rstrip("\n") + "\n" + text[close:]

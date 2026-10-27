"""Read KiCad footprints (.kicad_mod) and link 3D models into them.

Works on the footprint format from KiCad 6 onward ("footprint" root) and on
the older "module" root, so it does not depend on the installed KiCad version.
Coordinates are in mm, in KiCad's footprint frame (Y points down).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import sexpr

COURTYARD_LAYERS = {"F.CrtYd", "B.CrtYd"}


@dataclass
class Pad:
    number: str
    kind: str  # smd, thru_hole, np_thru_hole, connect
    shape: str
    x: float
    y: float
    rotation: float
    width: float
    height: float
    drill: float | None
    layers: list[str] = field(default_factory=list)
    drill_y: float | None = None  # set for a slot: KiCad's (drill oval x y)
    offset: tuple[float, float] = (0.0, 0.0)  # copper relative to the hole, in the pad's own frame: KiCad's (drill .. (offset x y))
    castellated: bool = False  # KiCad's (property pad_prop_castellated): a hole cut in half by the board edge

    @property
    def half_size(self) -> tuple[float, float]:
        return _rotated_half_size(self.width, self.height, self.rotation)

    @property
    def copper_center(self) -> tuple[float, float]:
        """Centre of the pad's copper; differs from (x, y), the hole, when the pad is offset."""
        ox, oy = self.offset
        if not (ox or oy):
            return self.x, self.y
        r = math.radians(-self.rotation)  # KiCad rotates pads clockwise on screen (Y down)
        return self.x + ox * math.cos(r) - oy * math.sin(r), self.y + ox * math.sin(r) + oy * math.cos(r)

    @property
    def box(self) -> "BBox":
        hw, hh = self.half_size
        cx, cy = self.copper_center
        return BBox(cx - hw, cy - hh, cx + hw, cy + hh)


@dataclass
class Model:
    path: str
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    rotate: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class Graphic:
    """A drawn shape: kind is line, rect, poly, circle or arc; pts are its defining points."""

    kind: str
    layer: str
    pts: list[tuple[float, float]]
    width: float
    filled: bool = False


@dataclass
class BBox:
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def height(self) -> float:
        return self.ymax - self.ymin

    @property
    def center(self) -> tuple[float, float]:
        return ((self.xmin + self.xmax) / 2, (self.ymin + self.ymax) / 2)


@dataclass
class Footprint:
    name: str
    format_version: str | None
    smd: bool
    pads: list[Pad]
    courtyard: BBox | None
    models: list[Model] = field(default_factory=list)
    graphics: list[Graphic] = field(default_factory=list)
    texts: list[tuple[str, str, str]] = field(default_factory=list)  # (field or "user", text, layer)

    def on_layer(self, *layers: str) -> list[Graphic]:
        return [g for g in self.graphics if g.layer in layers]

    @property
    def pin1(self) -> Pad | None:
        for pad in self.pads:
            if pad.number in ("1", "A1"):
                return pad
        return None

    @property
    def pad_extents(self) -> BBox | None:
        if not self.pads:
            return None
        boxes = [p.box for p in self.pads]
        return BBox(min(b.xmin for b in boxes), min(b.ymin for b in boxes), max(b.xmax for b in boxes), max(b.ymax for b in boxes))

    def signal_pads(self) -> list[Pad]:
        """Numbered, distinct pins: skips mounting holes and repeats of the same number (split thermal pads)."""
        seen, out = set(), []
        for p in self.pads:
            if p.kind == "np_thru_hole" or not p.number or p.number in seen:
                continue
            seen.add(p.number)
            out.append(p)
        return out

    def pitch(self) -> float | None:
        """Smallest centre distance between two numbered pads of similar size (ignores exposed pads)."""
        pads = self.signal_pads()
        best = None
        for i, a in enumerate(pads):
            for b in pads[i + 1 :]:
                if abs(a.width * a.height - b.width * b.height) > 0.25 * max(a.width * a.height, 1e-9):
                    continue
                d = math.hypot(a.x - b.x, a.y - b.y)
                if d > 1e-6 and (best is None or d < best):
                    best = d
        return round(best, 4) if best is not None else None

    def to_dict(self) -> dict:
        d = asdict(self)
        del d["graphics"], d["texts"]
        d["pin1"] = asdict(self.pin1) if self.pin1 else None
        d["pad_extents"] = asdict(self.pad_extents) if self.pad_extents else None
        d["pitch"] = self.pitch()
        d["signal_pad_count"] = len(self.signal_pads())
        return d


def _rotated_half_size(w: float, h: float, rot: float) -> tuple[float, float]:
    r = math.radians(rot)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    return (w * c + h * s) / 2, (w * s + h * c) / 2


def _xyz(node: sexpr.Node | None, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if node is None:
        return default
    inner = node.child("xyz")
    vals = inner.floats() if inner else []
    return (vals[0], vals[1], vals[2]) if len(vals) == 3 else default


def parse(text: str) -> Footprint:
    root = sexpr.parse(text)
    if root.head not in ("footprint", "module"):
        raise ValueError(f"not a footprint file (root is {root.head!r})")
    atoms = root.atoms()
    name = atoms[0] if atoms else ""
    version = root.child("version")
    attr = root.child("attr")
    smd = bool(attr and "smd" in attr.atoms())

    pads = []
    for p in root.children("pad"):
        a = p.atoms()
        at = (p.child("at").floats() if p.child("at") else []) + [0.0, 0.0, 0.0]
        size = (p.child("size").floats() if p.child("size") else []) + [0.0, 0.0]
        drill_node = p.child("drill")
        drill_vals = drill_node.floats() if drill_node else []
        pads.append(
            Pad(
                number=a[0] if a else "",
                kind=a[1] if len(a) > 1 else "",
                shape=a[2] if len(a) > 2 else "",
                x=at[0],
                y=at[1],
                rotation=at[2],
                width=size[0],
                height=size[1],
                drill=drill_vals[0] if drill_vals else None,
                layers=p.child("layers").atoms() if p.child("layers") else [],
                drill_y=drill_vals[1] if len(drill_vals) > 1 and drill_node and "oval" in drill_node.atoms() else None,
                offset=_offset(drill_node),
                castellated=bool(p.child("property") and "pad_prop_castellated" in p.child("property").atoms()),
            )
        )
    if attr is None:
        smd = bool(pads) and all(p.kind == "smd" for p in pads)

    return Footprint(
        name=name,
        format_version=version.atoms()[0] if version and version.atoms() else None,
        smd=smd,
        pads=pads,
        courtyard=_courtyard(root),
        models=[_model(m) for m in root.children("model")],
        graphics=_graphics(root),
        texts=_texts(root),
    )


_GRAPHIC_KINDS = {"fp_line": "line", "fp_rect": "rect", "fp_poly": "poly", "fp_circle": "circle", "fp_arc": "arc"}


def _offset(drill: sexpr.Node | None) -> tuple[float, float]:
    node = drill.child("offset") if drill else None
    v = node.floats() if node else []
    return (v[0], v[1]) if len(v) >= 2 else (0.0, 0.0)


def _graphics(root: sexpr.Node) -> list[Graphic]:
    out = []
    for item in root.items:
        if not isinstance(item, sexpr.Node) or item.head not in _GRAPHIC_KINDS:
            continue
        layer = item.child("layer")
        pts = []
        for key in ("start", "center", "mid", "end"):
            n = item.child(key)
            if n and len(n.floats()) >= 2:
                pts.append(tuple(n.floats()[:2]))
        if item.child("pts"):
            pts += [tuple(xy.floats()[:2]) for xy in item.child("pts").children("xy")]
        stroke = item.child("stroke")
        width_node = (stroke.child("width") if stroke else None) or item.child("width")
        fill = item.child("fill")
        out.append(
            Graphic(
                kind=_GRAPHIC_KINDS[item.head],
                layer=layer.atoms()[0] if layer and layer.atoms() else "",
                pts=pts,
                width=width_node.floats()[0] if width_node and width_node.floats() else 0.0,
                filled=bool(fill and fill.atoms() and fill.atoms()[0] in ("solid", "yes")),
            )
        )
    return out


def _texts(root: sexpr.Node) -> list[tuple[str, str, str]]:
    out = []
    for item in root.children("property") + root.children("fp_text"):
        a = item.atoms()
        layer = item.child("layer")
        if len(a) >= 2:
            name = {"reference": "Reference", "value": "Value"}.get(a[0], a[0])
            out.append((name, a[1], layer.atoms()[0] if layer and layer.atoms() else ""))
    return out


def _model(m: sexpr.Node) -> Model:
    a = m.atoms()
    # KiCad 5 and older used (at (xyz ..)) in inches; KiCad 6+ uses (offset (xyz ..)) in mm.
    offset = _xyz(m.child("offset"), (0.0, 0.0, 0.0))
    if m.child("offset") is None and m.child("at") is not None:
        offset = tuple(v * 25.4 for v in _xyz(m.child("at"), (0.0, 0.0, 0.0)))
    return Model(
        path=a[0] if a else "",
        offset=offset,
        scale=_xyz(m.child("scale"), (1.0, 1.0, 1.0)),
        rotate=_xyz(m.child("rotate"), (0.0, 0.0, 0.0)),
    )


def _courtyard(root: sexpr.Node) -> BBox | None:
    xs: list[float] = []
    ys: list[float] = []
    for item in root.items:
        if not isinstance(item, sexpr.Node) or item.head not in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
            continue
        layer = item.child("layer")
        if not layer or not (set(layer.atoms()) & COURTYARD_LAYERS):
            continue
        for key in ("start", "end", "mid", "center"):
            n = item.child(key)
            if n and len(n.floats()) >= 2:
                xs.append(n.floats()[0])
                ys.append(n.floats()[1])
        if item.head == "fp_circle" and item.child("center") and item.child("end"):
            cx, cy = item.child("center").floats()[:2]
            ex, ey = item.child("end").floats()[:2]
            r = math.hypot(ex - cx, ey - cy)
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
        pts = item.child("pts")
        if pts:
            for xy in pts.children("xy"):
                v = xy.floats()
                if len(v) >= 2:
                    xs.append(v[0])
                    ys.append(v[1])
    if not xs:
        return None
    return BBox(min(xs), min(ys), max(xs), max(ys))


def read(path: str | Path) -> Footprint:
    return parse(Path(path).read_text(encoding="utf-8"))


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0"


def model_block(model: Model, indent: str = "\t") -> str:
    def xyz(v):
        return f"(xyz {_fmt(v[0])} {_fmt(v[1])} {_fmt(v[2])})"

    i2 = indent * 2
    return (
        f"{indent}(model {sexpr.quote(model.path)}\n"
        f"{i2}(offset\n{i2}{indent}{xyz(model.offset)}\n{i2})\n"
        f"{i2}(scale\n{i2}{indent}{xyz(model.scale)}\n{i2})\n"
        f"{i2}(rotate\n{i2}{indent}{xyz(model.rotate)}\n{i2})\n"
        f"{indent})"
    )


def link_model(text: str, model: Model, replace: bool = True) -> str:
    """Return the footprint text with model linked.

    With replace=True every existing (model ...) block is removed first, so the
    footprint ends up with exactly this one model. Everything else in the file
    is left byte for byte.
    """
    root = sexpr.parse(text)
    if root.head not in ("footprint", "module"):
        raise ValueError("not a footprint file")
    existing = root.children("model")
    indent = "\t" if "\n\t" in text else "  "
    block = model_block(model, indent)

    if replace and existing:
        # Put the new block where the first old one was; drop the others with their leading whitespace.
        out = text
        for m in sorted(existing[1:], key=lambda n: n.start, reverse=True):
            s = m.start
            while s > 0 and out[s - 1] in " \t\r\n":
                s -= 1
            out = out[:s] + out[m.end :]
        first = existing[0]
        line_start = out.rfind("\n", 0, first.start) + 1
        return out[:line_start] + block + out[first.end :]

    close = root.end - 1
    before = text[:close].rstrip()
    return before + "\n" + block + "\n" + text[close:]

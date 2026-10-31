"""A routed board read from KiCad's .kicad_pcb text, for layout review.

Reads the copper that matters to a reviewer: footprints with their pads in board
coordinates, tracks, vias, zone fills, the stackup and the net classes from the
project file. The same text comes from a file on disk or from the open PCB
editor (kipy's Board.get_as_string), so the review works on both.

Units are mm in KiCad's board frame (Y down). KiCad stores a footprint's pads
relative to the footprint, already mirrored when it sits on the back, with the
pad's own angle in board terms.
"""

from __future__ import annotations

import bisect
import fnmatch
import heapq
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr

Point = tuple[float, float]


# --- the board ---------------------------------------------------------------------------------------


@dataclass
class Pad:
    ref: str
    number: str
    net: str | None
    x: float
    y: float
    w: float
    h: float
    angle: float
    shape: str
    layers: list[str]
    kind: str  # smd, thru_hole, np_thru_hole, connect
    pintype: str | None = None
    pinfunction: str | None = None
    extra: list[list[Point]] = field(default_factory=list)  # a custom pad's polygons, board coordinates

    @property
    def name(self) -> str:
        if not self.number and self.kind == "np_thru_hole":
            return f"{self.ref} hole"
        fn = f" ({self.pinfunction})" if self.pinfunction else ""
        return f"{self.ref} pad {self.number}{fn}"

    def contains(self, p: Point, tol: float = 0.01) -> bool:
        if self.extra and any(point_in_polygon(p, poly) for poly in self.extra):
            return True
        dx, dy = p[0] - self.x, p[1] - self.y
        if self.shape == "circle":
            return math.hypot(dx, dy) <= max(self.w, self.h) / 2 + tol
        a = math.radians(self.angle)
        lx, ly = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
        return abs(lx) <= self.w / 2 + tol and abs(ly) <= self.h / 2 + tol

    def distance(self, p: Point) -> float:
        """From p to the pad's copper edge (0 inside); rounded corners count as square."""
        if self.extra:
            d = min(0.0 if point_in_polygon(p, poly) else min(point_seg(p, a, b)[0] for a, b in zip(poly, poly[1:] + poly[:1]))
                    for poly in self.extra)
            if d == 0.0:
                return 0.0
            return min(d, self._anchor_distance(p))
        return self._anchor_distance(p)

    def _anchor_distance(self, p: Point) -> float:
        dx, dy = p[0] - self.x, p[1] - self.y
        if self.shape == "circle":
            return max(0.0, math.hypot(dx, dy) - max(self.w, self.h) / 2)
        a = math.radians(self.angle)
        lx, ly = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
        ox, oy = max(0.0, abs(lx) - self.w / 2), max(0.0, abs(ly) - self.h / 2)
        return math.hypot(ox, oy)

    def edge_points(self, grow: float = 0.05) -> list[Point]:
        """The centre and eight points just outside the pad's outline, in board coordinates."""
        a = math.radians(self.angle)
        out = [(self.x, self.y)]
        hw, hh = self.w / 2 + grow, self.h / 2 + grow
        for lx, ly in ((hw, 0), (-hw, 0), (0, hh), (0, -hh), (hw, hh), (-hw, hh), (hw, -hh), (-hw, -hh)):
            if self.shape == "circle" and lx and ly:
                lx, ly = lx * 0.7071, ly * 0.7071
            out.append((self.x + lx * math.cos(a) + ly * math.sin(a), self.y - lx * math.sin(a) + ly * math.cos(a)))
        return out

    def on(self, layer: str) -> bool:
        return layer in self.layers


@dataclass
class Part:
    ref: str
    value: str
    lib: str
    x: float
    y: float
    rotation: float
    side: str  # front, back
    pads: list[Pad]
    courtyard: list[Point]  # convex hull in board coordinates; empty when the footprint has none
    smd: bool
    through_hole: bool
    dnp: bool = False
    description: str = ""

    @property
    def copper_pads(self) -> list[Pad]:
        return [p for p in self.pads if any(l.endswith(".Cu") for l in p.layers)]


@dataclass
class Track:
    start: Point
    end: Point
    width: float
    layer: str
    net: str | None
    mid: Point | None = None
    locked: bool = False

    @property
    def length(self) -> float:
        if self.mid is None:
            return dist(self.start, self.end)
        return dist(self.start, self.mid) + dist(self.mid, self.end)

    def pieces(self) -> list[tuple[Point, Point]]:
        return [(self.start, self.end)] if self.mid is None else [(self.start, self.mid), (self.mid, self.end)]


@dataclass
class Via:
    x: float
    y: float
    size: float
    drill: float
    layers: list[str]  # the two end layers
    net: str | None


@dataclass
class Fill:
    """One filled area of a zone on one layer. KiCad stores holes as cuts into the outline, so even-odd is exact."""

    net: str | None
    layer: str
    pts: list[Point]
    box: tuple[float, float, float, float]
    _rows: list[list[float]] | None = None

    STEP = 0.05  # mm between scan lines: contains() is exact across X, within this in Y

    def _raster(self) -> list[list[float]]:
        x0, y0, x1, y1 = self.box
        n = int((y1 - y0) / self.STEP) + 1
        rows: list[list[float]] = [[] for _ in range(n)]
        pts = self.pts
        for i in range(len(pts)):
            (ax, ay), (bx, by) = pts[i - 1], pts[i]
            if ay == by:
                continue
            lo, hi = (ay, by) if ay < by else (by, ay)
            r0 = max(0, math.ceil((lo - y0) / self.STEP - 0.5))
            r1 = min(n - 1, math.floor((hi - y0) / self.STEP - 0.5))
            for r in range(r0, r1 + 1):
                y = y0 + (r + 0.5) * self.STEP
                if lo <= y < hi:
                    rows[r].append(ax + (y - ay) * (bx - ax) / (by - ay))
        for r in rows:
            r.sort()
        return rows

    def contains(self, p: Point) -> bool:
        x0, y0, x1, y1 = self.box
        if not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1):
            return False
        if self._rows is None:
            self._rows = self._raster()
        r = min(len(self._rows) - 1, max(0, int((p[1] - y0) / self.STEP)))
        return bisect.bisect_right(self._rows[r], p[0]) % 2 == 1


@dataclass
class NetClass:
    name: str
    track_width: float | None
    clearance: float | None
    via_diameter: float | None
    via_drill: float | None
    diff_pair_width: float | None = None
    diff_pair_gap: float | None = None


@dataclass
class Board:
    name: str
    parts: list[Part]
    tracks: list[Track]
    vias: list[Via]
    fills: list[Fill]
    zone_nets: dict[str, list[str]]  # net -> layers it has a zone on
    copper_layers: list[str]  # top to bottom
    copper_thickness: dict[str, float]  # mm, per layer
    thickness: float
    nets: list[str]
    net_classes: dict[str, NetClass] = field(default_factory=dict)
    net_class_of: dict[str, str] = field(default_factory=dict)
    rules: dict = field(default_factory=dict)  # the project's design rules, KiCad's keys
    edges: list[tuple[Point, Point]] = field(default_factory=list)
    mask_openings: list[tuple[str, list[Point]]] = field(default_factory=list)  # (copper layer it exposes, hull), from graphics
    keepouts: list[tuple[str, list[Point]]] = field(default_factory=list)  # (copper layer, outline): rule areas, no tracks or vias
    power_symbol_nets: set[str] = field(default_factory=set)  # from the schematic's power symbols
    sources: list[str] = field(default_factory=list)  # files read besides the board

    @property
    def pads(self) -> list[Pad]:
        return [p for part in self.parts for p in part.copper_pads]

    def part(self, ref: str) -> Part | None:
        return next((p for p in self.parts if p.ref == ref), None)

    def net_class(self, net: str) -> NetClass | None:
        return self.net_classes.get(self.net_class_of.get(net, "Default")) or self.net_classes.get("Default")

    def adjacent(self, layer: str) -> list[str]:
        """The copper layers right above and below this one."""
        if layer not in self.copper_layers:
            return []
        i = self.copper_layers.index(layer)
        return [self.copper_layers[j] for j in (i - 1, i + 1) if 0 <= j < len(self.copper_layers)]

    def is_outer(self, layer: str) -> bool:
        return layer in (self.copper_layers[0], self.copper_layers[-1]) if self.copper_layers else True

    def bbox(self) -> tuple[float, float, float, float] | None:
        pts = [p for e in self.edges for p in e]
        if not pts:
            return None
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)


def _xy(node: sexpr.Node | None) -> Point | None:
    if node is None:
        return None
    f = node.floats()
    return (f[0], f[1]) if len(f) >= 2 else None


def _net(node: sexpr.Node, names: dict[str, str]) -> str | None:
    n = node.child("net")
    if n is None or not n.atoms():
        nn = node.child("net_name")
        return nn.atoms()[0] if nn is not None and nn.atoms() and nn.atoms()[0] else None
    a = n.atoms()
    name = a[1] if len(a) >= 2 else names.get(a[0], a[0])
    return name or None


def _layers(node: sexpr.Node, copper: list[str]) -> list[str]:
    out = []
    for key in ("layer", "layers"):
        c = node.child(key)
        if c is not None:
            out += c.atoms()
    expanded = []
    for l in out:
        if l in ("*.Cu", "F&B.Cu"):
            expanded += copper if l == "*.Cu" else [copper[0], copper[-1]] if copper else []
        else:
            expanded.append(l)
    return expanded


def _prop(node: sexpr.Node, key: str) -> str:
    for p in node.children("property"):
        a = p.atoms()
        if len(a) >= 2 and a[0] == key:
            return a[1]
    for t in node.children("fp_text"):  # KiCad 7 and older
        a = t.atoms()
        if len(a) >= 2 and a[0] == key.lower():
            return a[1]
    return ""


def _rotate(lx: float, ly: float, deg: float) -> Point:
    """KiCad's rotation: positive turns counter-clockwise on screen, with Y pointing down."""
    a = math.radians(deg)
    return lx * math.cos(a) + ly * math.sin(a), -lx * math.sin(a) + ly * math.cos(a)


def _shape_points(node: sexpr.Node) -> list[Point]:
    """Points on a graphic item's outline, enough for a hull or an edge list."""
    h = node.head
    pts = []
    if h in ("fp_line", "gr_line"):
        pts = [_xy(node.child("start")), _xy(node.child("end"))]
    elif h in ("fp_rect", "gr_rect"):
        s, e = _xy(node.child("start")), _xy(node.child("end"))
        if s and e:
            pts = [s, (e[0], s[1]), e, (s[0], e[1])]
    elif h in ("fp_circle", "gr_circle"):
        c, e = _xy(node.child("center")), _xy(node.child("end"))
        if c and e:
            r = dist(c, e)
            pts = [(c[0] + r * math.cos(k * math.pi / 8), c[1] + r * math.sin(k * math.pi / 8)) for k in range(16)]
    elif h in ("fp_arc", "gr_arc"):
        pts = [_xy(node.child("start")), _xy(node.child("mid")), _xy(node.child("end"))]
    elif h in ("fp_poly", "gr_poly"):
        p = node.child("pts")
        pts = [_xy(x) for x in p.children("xy")] if p is not None else []
    return [p for p in pts if p]


def _edge_segments(node: sexpr.Node) -> list[tuple[Point, Point]]:
    pts = _shape_points(node)
    if len(pts) < 2:
        return []
    closed = node.head in ("fp_rect", "gr_rect", "fp_circle", "gr_circle", "fp_poly", "gr_poly")
    segs = list(zip(pts, pts[1:]))
    if closed:
        segs.append((pts[-1], pts[0]))
    return segs


def _copper_order(root: sexpr.Node) -> list[str]:
    lays = root.child("layers")
    names = []
    if lays is not None:
        for l in lays.items[1:]:
            if isinstance(l, sexpr.Node) and len(l.items) >= 2 and isinstance(l.items[1], str) and l.items[1].endswith(".Cu"):
                names.append(l.items[1])

    def key(n: str) -> tuple[int, int]:
        if n == "F.Cu":
            return (0, 0)
        if n == "B.Cu":
            return (2, 0)
        m = re.match(r"In(\d+)\.Cu", n)
        return (1, int(m.group(1)) if m else 0)

    return sorted(names, key=key) or ["F.Cu", "B.Cu"]


def parse(text: str, name: str = "board") -> Board:
    root = sexpr.parse(text)
    copper = _copper_order(root)
    names = {}
    for n in root.children("net"):
        a = n.atoms()
        if len(a) >= 2:
            names[a[0]] = a[1]
    thickness = 1.6
    gen = root.child("general")
    if gen is not None and gen.child("thickness") is not None:
        thickness = (gen.child("thickness").floats() or [1.6])[0]
    cu_t = {l: 0.035 for l in copper}
    setup = root.child("setup")
    stack = setup.child("stackup") if setup is not None else None
    if stack is not None:
        for l in stack.children("layer"):
            a = l.atoms()
            t = l.child("thickness")
            if a and a[0] in cu_t and t is not None and t.floats():
                cu_t[a[0]] = t.floats()[0]

    parts, tracks, vias, fills, edges, masks, keepouts = [], [], [], [], [], [], []
    zone_nets: dict[str, list[str]] = {}
    for node in root.items:
        if not isinstance(node, sexpr.Node):
            continue
        h = node.head
        if h == "footprint":
            parts.append(_part(node, names, copper, edges, masks))
        elif h in ("segment", "arc"):
            s, e = _xy(node.child("start")), _xy(node.child("end"))
            if s and e:
                lay = _layers(node, copper)
                tracks.append(Track(s, e, (node.child("width").floats() or [0])[0] if node.child("width") else 0.0,
                                    lay[0] if lay else "?", _net(node, names), _xy(node.child("mid")) if h == "arc" else None,
                                    "locked" in node.atoms() or node.child("locked") is not None))
        elif h == "via":
            at = _xy(node.child("at"))
            if at:
                lay = _layers(node, copper)
                drill = node.child("drill")
                vias.append(Via(at[0], at[1], (node.child("size").floats() or [0])[0] if node.child("size") else 0.0,
                                (drill.floats() or [0])[0] if drill is not None else 0.0, lay or [copper[0], copper[-1]],
                                _net(node, names)))
        elif h == "zone":
            ko = node.child("keepout")
            if ko is not None:
                rules_ = {c.head: (c.atoms() or [""])[0] for c in ko.items if isinstance(c, sexpr.Node)}
                poly = node.child("polygon")
                pts_node = poly.child("pts") if poly is not None else None
                pts = [p for p in (_xy(x) for x in pts_node.children("xy")) if p] if pts_node is not None else []
                if len(pts) >= 3 and "not_allowed" in (rules_.get("tracks"), rules_.get("vias")):
                    keepouts += [(l, pts) for l in _layers(node, copper) if l in copper]
                continue
            net = _net(node, names)
            if net:
                zone_nets.setdefault(net, [])
                zone_nets[net] += [l for l in _layers(node, copper) if l.endswith(".Cu") and l not in zone_nets[net]]
            for fp in node.children("filled_polygon"):
                lay = _layers(fp, copper)
                pts_node = fp.child("pts")
                pts = [p for p in (_xy(x) for x in pts_node.children("xy")) if p] if pts_node is not None else []
                if len(pts) >= 3 and lay:
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    fills.append(Fill(net, lay[0], pts, (min(xs), min(ys), max(xs), max(ys))))
        elif h and h.startswith("gr_") and "Edge.Cuts" in _layers(node, copper):
            edges += _edge_segments(node)
        elif h and h.startswith("gr_"):
            _mask_opening(node, _layers(node, copper), copper, _shape_points(node), masks)
    nets = sorted({v for v in names.values() if v} | {t.net for t in tracks if t.net} | {p.net for pt in parts for p in pt.pads if p.net})
    return Board(name, parts, tracks, vias, fills, zone_nets, copper, cu_t, thickness, nets, edges=edges, mask_openings=masks,
                 keepouts=keepouts)


def _mask_opening(node: sexpr.Node, lays: list[str], copper: list[str], pts: list[Point], masks: list) -> None:
    """A filled shape drawn on a mask layer (a logo, a test area) bares the copper under it: anything routed there is exposed."""
    if not copper or node.head[3:] not in ("rect", "circle", "poly") or len(pts) < 3:
        return
    for mask, cu in (("F.Mask", copper[0]), ("B.Mask", copper[-1])):
        if mask in lays:
            masks.append((cu, convex_hull(pts)))


def _part(node: sexpr.Node, names: dict[str, str], copper: list[str], edges: list, masks: list | None = None) -> Part:
    at = node.child("at").floats() if node.child("at") is not None else [0, 0]
    fx, fy = at[0], at[1]
    frot = at[2] if len(at) > 2 else 0.0
    ref = _prop(node, "Reference")
    side = "back" if _layers(node, copper)[:1] == ["B.Cu"] else "front"
    pads = []
    for p in node.children("pad"):
        a = p.atoms()
        pat = p.child("at").floats() if p.child("at") is not None else [0, 0]
        dx, dy = _rotate(pat[0], pat[1], frot)
        size = p.child("size").floats() if p.child("size") is not None else [0, 0]
        pf, pt = p.child("pinfunction"), p.child("pintype")
        pang = pat[2] if len(pat) > 2 else 0.0  # the file has the pad's own orientation, left out when it is 0
        extra = []
        prim = p.child("primitives")
        for g in prim.children("gr_poly") if prim is not None else []:
            pts = _shape_points(g)
            if len(pts) >= 3:
                extra.append([(fx + dx + r[0], fy + dy + r[1]) for r in (_rotate(x, y, pang) for x, y in pts)])
        pads.append(Pad(ref, a[0] if a else "", _net(p, names), fx + dx, fy + dy, size[0], size[1] if len(size) > 1 else size[0],
                        pang, a[2] if len(a) > 2 else "rect", _layers(p, copper), a[1] if len(a) > 1 else "",
                        pt.atoms()[0] if pt is not None and pt.atoms() else None, pf.atoms()[0] if pf is not None and pf.atoms() else None,
                        extra))
    court = []
    for g in node.items:
        if isinstance(g, sexpr.Node) and g.head and g.head.startswith("fp_"):
            lays = _layers(g, copper)
            pts = [(fx + d[0], fy + d[1]) for d in (_rotate(x, y, frot) for x, y in _shape_points(g))]
            if any(l.endswith("CrtYd") for l in lays):
                court += pts
            if "Edge.Cuts" in lays:
                edges += _edge_segments_abs(g, fx, fy, frot)
            if masks is not None:
                _mask_opening(g, lays, copper, pts, masks)
    attr = node.child("attr")
    attrs = attr.atoms() if attr is not None else []
    return Part(ref, _prop(node, "Value"), (node.atoms() or [""])[0], fx, fy, frot, side, pads, convex_hull(court),
                "smd" in attrs, "through_hole" in attrs, "dnp" in attrs or node.child("dnp") is not None, _prop(node, "Description"))


def _edge_segments_abs(g: sexpr.Node, fx: float, fy: float, frot: float) -> list[tuple[Point, Point]]:
    out = []
    for a, b in _edge_segments(g):
        ra, rb = _rotate(*a, frot), _rotate(*b, frot)
        out.append(((fx + ra[0], fy + ra[1]), (fx + rb[0], fy + rb[1])))
    return out


# --- the project around the board ----------------------------------------------------------------------


def _pattern_match(pattern: str, net: str) -> bool:
    if fnmatch.fnmatchcase(net, pattern):
        return True
    try:
        return re.fullmatch(pattern, net) is not None
    except re.error:
        return False


def load_project(board: Board, project_file: Path | None) -> None:
    """Net classes and design rules from the .kicad_pro next to the board."""
    if project_file is None or not project_file.exists():
        return
    try:
        pro = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    board.sources.append(str(project_file))
    ns = pro.get("net_settings", {})
    for c in ns.get("classes", []):
        board.net_classes[c.get("name", "?")] = NetClass(c.get("name", "?"), c.get("track_width"), c.get("clearance"),
                                                         c.get("via_diameter"), c.get("via_drill"), c.get("diff_pair_width"),
                                                         c.get("diff_pair_gap"))
    default = board.net_classes.get("Default")
    for c in board.net_classes.values():  # KiCad 8+ leaves values a class doesn't set to the Default class
        if default and c is not default:
            for f in ("track_width", "clearance", "via_diameter", "via_drill", "diff_pair_width", "diff_pair_gap"):
                if getattr(c, f) is None:
                    setattr(c, f, getattr(default, f))
    assigned = ns.get("netclass_assignments") or {}
    patterns = ns.get("netclass_patterns") or []
    for net in board.nets:
        cls = assigned.get(net)
        if isinstance(cls, list):
            cls = cls[0] if cls else None
        if cls is None:
            for p in patterns:
                if _pattern_match(p.get("pattern", ""), net):
                    cls = p.get("netclass")
                    break
        if cls:
            board.net_class_of[net] = cls
    board.rules = (pro.get("board", {}).get("design_settings", {}) or {}).get("rules", {}) or {}


def schematic_files(folder: Path, stem: str | None = None) -> list[Path]:
    """The board's schematic: <stem>.kicad_sch and the sheets it includes, followed down the hierarchy. Without a
    root sheet of that name, every sheet in the folder (a folder may hold other boards' schematics too)."""
    root = folder / f"{stem}.kicad_sch" if stem else None
    if root is None or not root.exists():
        return sorted(folder.glob("*.kicad_sch"))
    seen, todo = [], [root]
    while todo:
        f = todo.pop()
        if f in seen or not f.exists():
            continue
        seen.append(f)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        todo += [f.parent / m for m in re.findall(r'\(property\s+"Sheetfile"\s+"([^"]+)"', text)]
    return seen


def load_schematic(board: Board, folder: Path, stem: str | None = None) -> None:
    """Net names of the schematic's power symbols (power:GND, power:+3V3 ...), from the board's schematic sheets."""
    for sch in schematic_files(folder, stem):
        try:
            root = sexpr.parse(sch.read_text(encoding="utf-8", errors="replace"))
        except (OSError, sexpr.SexprError):
            continue
        board.sources.append(str(sch))
        power_libs = set()
        libs = root.child("lib_symbols")
        if libs is not None:
            for s in libs.children("symbol"):
                if s.child("power") is not None and s.atoms():
                    power_libs.add(s.atoms()[0])
        for s in root.children("symbol"):
            lib = s.child("lib_id")
            lib_id = lib.atoms()[0] if lib is not None and lib.atoms() else ""
            if lib_id in power_libs or lib_id.startswith("power:"):
                v = _prop(s, "Value")
                if v:
                    board.power_symbol_nets.add(v)


# --- geometry ----------------------------------------------------------------------------------------


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def point_seg(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """(distance from p to segment ab, position along ab from a in mm)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return dist(p, a), 0.0
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    q = (a[0] + t * dx, a[1] + t * dy)
    return dist(p, q), t * math.sqrt(L2)


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def seg_seg(a: Point, b: Point, c: Point, d: Point) -> float:
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    d3, d4 = _cross(a, b, c), _cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(point_seg(a, c, d)[0], point_seg(b, c, d)[0], point_seg(c, a, b)[0], point_seg(d, a, b)[0])


def point_in_polygon(p: Point, poly: list[Point]) -> bool:
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def convex_hull(pts: list[Point]) -> list[Point]:
    pts = sorted(set((round(x, 4), round(y, 4)) for x, y in pts))
    if len(pts) < 3:
        return pts
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def polygon_gap(a: list[Point], b: list[Point]) -> float:
    """Distance between two convex polygons; 0 when they overlap."""
    if not a or not b:
        return math.inf
    if any(point_in_polygon(p, b) for p in a) or any(point_in_polygon(p, a) for p in b):
        return 0.0
    ea, eb = list(zip(a, a[1:] + a[:1])), list(zip(b, b[1:] + b[:1]))
    return min(seg_seg(p, q, r, s) for p, q in ea for r, s in eb)


def polygon_area(pts: list[Point]) -> float:
    return abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))) / 2


# --- copper connectivity -----------------------------------------------------------------------------


class NetGraph:
    """The copper of one net as a graph: pads, vias and track ends joined by tracks, and by pours.

    Path lengths along tracks are exact; a path through a pour is counted as the straight line between the
    points where it enters and leaves, which is how current spreads in a solid plane without slots.
    """

    ZONE_PAIRWISE_LIMIT = 300

    def __init__(self, board: Board, net: str):
        self.net = net
        self.nodes: list[tuple[str, object, str | None]] = []  # (kind, item, layer)
        self.edges: dict[int, list[tuple[int, float, str]]] = {}
        self.pad_node: dict[int, int] = {}
        pads = [p for p in board.pads if p.net == net]
        tracks = [t for t in board.tracks if t.net == net]
        vias = [v for v in board.vias if v.net == net]
        fills = [f for f in board.fills if f.net == net]
        for p in pads:
            self.pad_node[id(p)] = self._add("pad", p, None)
        via_nodes = [self._add("via", v, None) for v in vias]
        ends: list[tuple[int, Point, str, Track]] = []
        for t in tracks:
            a, b = self._add("end", t.start, t.layer), self._add("end", t.end, t.layer)
            self._link(a, b, t.length, "track")
            ends += [(a, t.start, t.layer, t), (b, t.end, t.layer, t)]
            for pad in pads:  # a track passing over a pad joins it, as KiCad's connectivity has it
                if pad.on(t.layer) and not pad.contains(t.start, tol=t.width / 2) and not pad.contains(t.end, tol=t.width / 2):
                    along = self._touch(t, pad)
                    if along is not None:
                        self._link(a, self.pad_node[id(pad)], along, "pad")
                        self._link(b, self.pad_node[id(pad)], t.length - along, "pad")
        for i, (n, p, lay, t) in enumerate(ends):
            for m, q, lay2, t2 in ends[i + 1:]:
                if lay == lay2 and dist(p, q) <= max(0.01, min(t.width, t2.width) / 2):
                    self._link(n, m, dist(p, q), "joint")
            for t2 in tracks:
                if t2 is t or t2.layer != lay:
                    continue
                for s, e in t2.pieces():
                    d, along = point_seg(p, s, e)
                    if d <= t2.width / 2 and 0.01 < along < dist(s, e) - 0.01:  # a T joint into the middle of t2
                        sn = self._find_end(t2.start, lay)
                        en = self._find_end(t2.end, lay)
                        if sn is not None and en is not None:
                            self._link(n, sn, along, "track")
                            self._link(n, en, t2.length - along, "track")
            for pad in pads:
                if pad.on(lay) and pad.contains(p, tol=t.width / 2):
                    self._link(n, self.pad_node[id(pad)], dist(p, (pad.x, pad.y)), "pad")
            for vn, v in zip(via_nodes, vias):
                if dist(p, (v.x, v.y)) <= v.size / 2 + t.width / 2:
                    self._link(n, vn, dist(p, (v.x, v.y)), "via")
        for vn, v in zip(via_nodes, vias):
            for pad in pads:
                if pad.contains((v.x, v.y), tol=v.size / 2):
                    self._link(vn, self.pad_node[id(pad)], dist((v.x, v.y), (pad.x, pad.y)), "via-in-pad")
        for f in fills:
            members: list[tuple[int, Point]] = []
            for pad in pads:
                if pad.on(f.layer) and any(f.contains(q) for q in pad.edge_points()):
                    members.append((self.pad_node[id(pad)], (pad.x, pad.y)))
            for vn, v in zip(via_nodes, vias):
                ring = [(v.x + (v.size / 2 + 0.05) * math.cos(k * math.pi / 4), v.y + (v.size / 2 + 0.05) * math.sin(k * math.pi / 4)) for k in range(8)]
                if any(f.contains(q) for q in [(v.x, v.y)] + ring):
                    members.append((vn, (v.x, v.y)))
            for n, p, lay, t in ends:
                if lay == f.layer and f.contains(p):
                    members.append((n, p))
            if len(members) <= self.ZONE_PAIRWISE_LIMIT:
                for i, (n, p) in enumerate(members):
                    for m, q in members[i + 1:]:
                        self._link(n, m, dist(p, q), "pour")
            else:
                hub = self._add("pour", f, f.layer)
                for n, p in members:
                    self._link(n, hub, 0.0, "pour")

    @staticmethod
    def _touch(t: Track, pad: Pad) -> float | None:
        """How far along t its copper first touches the pad, or None."""
        r = math.hypot(pad.w, pad.h) / 2 + max((dist((pad.x, pad.y), q) for poly in pad.extra for q in poly), default=0)
        if point_seg((pad.x, pad.y), t.start, t.end)[0] > r + t.width / 2 or t.mid is not None:
            return None
        n = max(2, int(t.length / 0.02))
        for k in range(n + 1):
            q = (t.start[0] + (t.end[0] - t.start[0]) * k / n, t.start[1] + (t.end[1] - t.start[1]) * k / n)
            if pad.distance(q) <= t.width / 2 + 1e-6:
                return t.length * k / n
        return None

    def _add(self, kind: str, item, layer: str | None) -> int:
        self.nodes.append((kind, item, layer))
        self.edges[len(self.nodes) - 1] = []
        return len(self.nodes) - 1

    def _link(self, a: int, b: int, w: float, how: str) -> None:
        self.edges[a].append((b, w, how))
        self.edges[b].append((a, w, how))

    def _find_end(self, p: Point, layer: str) -> int | None:
        for i, (kind, item, lay) in enumerate(self.nodes):
            if kind == "end" and lay == layer and item == p:
                return i
        return None

    def path(self, a: Pad, b: Pad) -> dict | None:
        """Shortest copper path between two pads: length (mm), vias passed, and whether it goes through a pour."""
        src, dst = self.pad_node.get(id(a)), self.pad_node.get(id(b))
        if src is None or dst is None:
            return None
        best = {src: 0.0}
        prev: dict[int, tuple[int, str]] = {}
        heap = [(0.0, src)]
        while heap:
            d, n = heapq.heappop(heap)
            if n == dst:
                break
            if d > best.get(n, math.inf):
                continue
            for m, w, how in self.edges[n]:
                nd = d + w
                if nd < best.get(m, math.inf) - 1e-9:
                    best[m] = nd
                    prev[m] = (n, how)
                    heapq.heappush(heap, (nd, m))
        if dst not in best:
            return None
        vias, pour, n = 0, False, dst
        while n != src:
            n, how = prev[n]
            if self.nodes[n][0] == "via":
                vias += 1
            pour = pour or how == "pour"
        return {"length": round(best[dst], 2), "vias": vias, "through_pour": pour}

    def islands(self) -> list[list[Pad]]:
        """Groups of this net's pads joined by copper. More than one group: the net isn't fully routed."""
        seen: set[int] = set()
        out = []
        for pid, start in self.pad_node.items():
            if start in seen:
                continue
            stack, group = [start], []
            seen.add(start)
            while stack:
                n = stack.pop()
                if self.nodes[n][0] == "pad":
                    group.append(self.nodes[n][1])
                for m, _, _ in self.edges[n]:
                    if m not in seen:
                        seen.add(m)
                        stack.append(m)
            out.append(group)
        return out

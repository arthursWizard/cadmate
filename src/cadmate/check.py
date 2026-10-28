"""Checks: a 3D model's placement against its footprint, and footprints themselves.

check_footprint reviews any footprint, generated or not, against the rules the
KiCad library follows (KLC) and the ones that make a board fail: pads that
touch, pads outside the courtyard, silkscreen on copper.

The model's bounding box (from FreeCAD, in the model's own frame) is moved by
the footprint's model offset and compared with the pads and courtyard. KiCad's
footprint Y axis points down while the 3D Y axis points up, so pad Y values
are flipped before comparing. Rotations other than steps of 90 degrees are not
handled here yet (they would need the rotated shape, not its box). Where the pads
look the same turned 180 degrees, the box cannot show a turned model, so the
model's pin 1 mark is checked against pad 1.
"""

from __future__ import annotations

import math
import statistics

from .footprint import BBox, Footprint, Model, Pad

CENTER_TOLERANCE = 0.25  # mm
Z_TOLERANCE = 0.05  # mm
PAD_COVER_TOLERANCE = 0.5  # mm
OVERSIZE_WARNING = 0.5  # mm
MARK_SIDE_TOLERANCE = 0.05  # mm; a pin 1 mark or pad this close to the pad centre is on neither side


def _rotate90(v: tuple[float, float, float], axis: int, quarter_turns: int) -> tuple[float, float, float]:
    """Rotate v by quarter_turns * 90 degrees counter-clockwise about axis (0=X, 1=Y, 2=Z)."""
    i, j = [(1, 2), (2, 0), (0, 1)][axis]
    v = list(v)
    for _ in range(quarter_turns % 4):
        v[i], v[j] = -v[j], v[i]
    return tuple(v)


def _apply_placement(bbox: dict, model: Model) -> dict:
    """Place the model's bounding box as KiCad does: scale, rotate X, then Y, then Z, then offset.

    KiCad stores the rotation angles negated (a file value of -90 about X turns
    a Y-up model so its +Y points to +Z), so each angle is applied as -angle.
    """
    sx, sy, sz = model.scale
    corners = [
        (x * sx, y * sy, z * sz)
        for x in (bbox["xmin"], bbox["xmax"])
        for y in (bbox["ymin"], bbox["ymax"])
        for z in (bbox["zmin"], bbox["zmax"])
    ]
    for axis in (0, 1, 2):
        turns = round(-model.rotate[axis] / 90)
        corners = [_rotate90(c, axis, turns) for c in corners]
    ox, oy, oz = model.offset
    return {
        "xmin": min(c[0] for c in corners) + ox, "xmax": max(c[0] for c in corners) + ox,
        "ymin": min(c[1] for c in corners) + oy, "ymax": max(c[1] for c in corners) + oy,
        "zmin": min(c[2] for c in corners) + oz, "zmax": max(c[2] for c in corners) + oz,
    }


def placement_matrix(model: Model) -> list[float]:
    """KiCad's model placement as a 4x4 matrix, row by row: scale, rotate X, Y, Z (each by -angle), then offset."""
    m = [[model.scale[0], 0.0, 0.0], [0.0, model.scale[1], 0.0], [0.0, 0.0, model.scale[2]]]
    for axis in (0, 1, 2):
        a = math.radians(-model.rotate[axis])
        c, s = math.cos(a), math.sin(a)
        i, j = [(1, 2), (2, 0), (0, 1)][axis]
        r = [[1.0 if p == q else 0.0 for q in range(3)] for p in range(3)]
        r[i][i], r[i][j], r[j][i], r[j][j] = c, -s, s, c
        m = [[sum(r[p][k] * m[k][q] for k in range(3)) for q in range(3)] for p in range(3)]
    rows = [[round(v, 12) for v in m[p]] + [model.offset[p]] for p in range(3)]
    return [v for row in rows for v in row] + [0.0, 0.0, 0.0, 1.0]


def _covers_holes(footprint: Footprint, b: dict, pad_box: dict, errors: list[str], warnings: list[str], what: str) -> None:
    """The model should reach every pad centre (hole, castellation, lead); pad edges may stick out."""
    out = max(b["xmin"] - pad_box["xmax"], pad_box["xmin"] - b["xmax"], b["ymin"] - pad_box["ymax"], pad_box["ymin"] - b["ymax"])
    centres = [(p.x, -p.y) for p in footprint.pads if p.drill] or [(p.x, -p.y) for p in footprint.pads if p.number]
    uncovered = max(max(b["xmin"] - x, x - b["xmax"], b["ymin"] - y, y - b["ymax"]) for x, y in centres)
    if out > 0:
        errors.append("model does not overlap the pads at all; check the offset")
    elif uncovered > PAD_COVER_TOLERANCE:
        warnings.append(f"{what} lie {uncovered:.2f} mm beyond the model; check the offset")


def _symmetric_pads(footprint: Footprint, cx: float, cy: float) -> bool:
    """Every pad has a twin mirrored through the pad centre (packages do; modules and connectors often don't)."""
    pts = [(p.x, -p.y) for p in footprint.signal_pads()]
    return all(any(abs(2 * cx - x - u) < 0.05 and abs(2 * cy - y - v) < 0.05 for u, v in pts) for x, y in pts)


def _check_pin1_mark(footprint: Footprint, marks: list[dict], pad_box: dict, errors: list[str], warnings: list[str],
                     measured: dict) -> None:
    """On pads that look the same turned 180 degrees (or 90 for square parts), the box cannot tell the model's
    orientation; its pin 1 mark can. The mark must sit on pin 1's side of the pad centre, along each axis where
    pin 1 is off centre."""
    pin1 = footprint.pin1
    if pin1 is None or len(footprint.signal_pads()) < 4:
        return
    cx, cy = (pad_box["xmin"] + pad_box["xmax"]) / 2, (pad_box["ymin"] + pad_box["ymax"]) / 2
    if not _symmetric_pads(footprint, cx, cy):
        return  # the box and the pads already pin the orientation down

    def side(v: float) -> int:
        return 0 if abs(v) < MARK_SIDE_TOLERANCE else (1 if v > 0 else -1)

    want = (side(pin1.x - cx), side(-pin1.y - cy))
    if want == (0, 0):
        return
    if not marks:
        warnings.append("no round pin 1 mark found on top of the model, so its orientation on these pads (which look the same "
                        "turned 180 degrees) is not checked; add a dimple at pin 1 or check the 3D view")
        return
    found = []
    for m in marks:
        got = (side(m["x"] - cx), side(m["y"] - cy))
        axes = [i for i in (0, 1) if want[i] and got[i]]
        if axes and all(got[i] == want[i] for i in axes):
            measured["pin1_mark"] = (round(m["x"], 4), round(-m["y"], 4))
            return
        if axes:
            found.append((m, axes, got))
    if not found:
        warnings.append("the model's pin 1 mark lies on the pad centre line, so its orientation is not checked")
        return
    m, axes, got = found[0]
    where = f"the model's pin 1 mark is at ({m['x']:.2f}, {-m['y']:.2f}) and pad 1 at ({pin1.x:.2f}, {pin1.y:.2f})"
    if len(axes) == 2 and all(got[i] == -want[i] for i in axes):
        errors.append(f"{where}, opposite corners: the model is turned 180 degrees about Z; add 180 to its Z rotation")
    else:
        flipped = " and ".join("XY"[i] for i in axes if got[i] != want[i])
        errors.append(f"{where}, on the other side along {flipped}: the model is turned 90 degrees or mirrored")


def check_model(footprint: Footprint, bbox: dict, model: Model | None = None, expected: dict | None = None, cutout: bool = False,
                by_solid: bool = False, marks: list[dict] | None = None) -> dict:
    """Compare a model bounding box with the footprint.

    bbox: xmin..zmax of the model as read from its STEP file.
    model: the placement from the footprint (offset/scale/rotate); identity if None.
    expected: optional body size from the datasheet, keys width/length/height (mm). As in the footprint
    families, width is along X and length along Y of the footprint as drawn (pin 1 at the top left).
    cutout: the board has a cutout under the part, so an SMD model may reach below the board
    (a module with parts on its underside, soldered flat).
    by_solid: leave out the courtyard and below-the-board checks, because review.review_footprint makes
    them solid by solid (the overall box cannot tell a lead from a part under a module).
    marks: the model's round pin 1 marks, placed (x, y in the 3D frame), as inspect_model.py finds them; None when
    nobody looked. On pads that look the same turned round, the mark is what shows the model's orientation.
    """
    model = model or Model(path="")
    if any(abs(r - 90 * round(r / 90)) > 1e-6 for r in model.rotate):
        return {"ok": False, "errors": ["only rotations in steps of 90 degrees are checked so far"], "warnings": [], "measured": {}}
    b = _apply_placement(bbox, model)
    errors: list[str] = []
    warnings: list[str] = []

    pads = footprint.pad_extents
    # Footprint frame -> 3D frame: y_3d = -y_fp
    pad_box = None if pads is None else {"xmin": pads.xmin, "xmax": pads.xmax, "ymin": -pads.ymax, "ymax": -pads.ymin}
    measured = {"model_bbox_placed": b, "pad_extents_3d": pad_box}

    if footprint.smd and pad_box:
        mcx, mcy = (b["xmin"] + b["xmax"]) / 2, (b["ymin"] + b["ymax"]) / 2
        pcx, pcy = (pad_box["xmin"] + pad_box["xmax"]) / 2, (pad_box["ymin"] + pad_box["ymax"]) / 2
        measured["center_offset"] = (round(mcx - pcx, 4), round(mcy - pcy, 4))
        if not _symmetric_pads(footprint, pcx, pcy):
            # Pads not centred on the body (a module or connector with pads on some sides only): the
            # model's centre says nothing, so check that it reaches every pad instead.
            _covers_holes(footprint, b, pad_box, errors, warnings, "pads")
        elif abs(mcx - pcx) > CENTER_TOLERANCE or abs(mcy - pcy) > CENTER_TOLERANCE:
            errors.append(f"model centre is ({mcx - pcx:+.2f}, {mcy - pcy:+.2f}) mm from the pad centre")
        if b["zmin"] < -Z_TOLERANCE and not by_solid:
            msg = f"SMD model goes {-b['zmin']:.2f} mm below the board surface"
            if cutout:
                warnings.append(msg + "; the board needs a cutout there")
            else:
                errors.append(msg + " (pass cutout if the board has a cutout under it)")
        if b["zmin"] > Z_TOLERANCE:
            warnings.append(f"model floats {b['zmin']:.2f} mm above the board; fine only if the leads are separate solids that touch the pads")
    if not any(p.kind == "smd" for p in footprint.pads):  # mixed parts (USB-C) are centred in KiCad's library
        pin1 = footprint.pin1
        if pin1 is not None and (abs(pin1.x) > 1e-3 or abs(pin1.y) > 1e-3) and not by_solid:  # check_footprint says it then
            warnings.append("through-hole footprint whose pin 1 is not at the origin (KiCad library convention puts it there)")
        if b["zmin"] > Z_TOLERANCE:
            warnings.append("through-hole model has no part below the board; leads may be missing")
        if -b["zmin"] > b["zmax"]:
            warnings.append(f"model is mostly below the board (z {b['zmin']:.2f}..{b['zmax']:.2f}); it may be upside down, check the rotation")
        if pad_box:
            _covers_holes(footprint, b, pad_box, errors, warnings, "holes")

    if marks is not None and pad_box:
        _check_pin1_mark(footprint, marks, pad_box, errors, warnings, measured)

    court = footprint.courtyard
    if court is not None and not by_solid:
        c3 = {"xmin": court.xmin, "xmax": court.xmax, "ymin": -court.ymax, "ymax": -court.ymin}
        over = max(c3["xmin"] - b["xmin"], b["xmax"] - c3["xmax"], c3["ymin"] - b["ymin"], b["ymax"] - c3["ymax"])
        measured["courtyard_3d"] = c3
        if over > 0.01:
            warnings.append(f"model extends {over:.2f} mm outside the courtyard")

    if expected:
        size = {"width": b["xmax"] - b["xmin"], "length": b["ymax"] - b["ymin"], "height": b["zmax"] - max(b["zmin"], 0.0)}
        measured["size"] = {k: round(v, 4) for k, v in size.items()}
        if "width" in expected and "length" in expected:
            ew, el = float(expected["width"]), float(expected["length"])
            if abs(ew - el) > 0.1 and abs(size["width"] - el) + abs(size["length"] - ew) < abs(size["width"] - ew) + abs(size["length"] - el):
                errors.append(f"model is {size['width']:.2f} mm along X and {size['length']:.2f} mm along Y, which fits the datasheet "
                              f"width {ew} and length {el} turned 90 degrees: width is along X and length along Y of the footprint; "
                              "swap them if you mixed them up, or rotate the model")
        for key in ("length", "width", "height"):
            if key in expected:
                # Overall model size includes leads, so it may only be larger than the body, by a bounded amount.
                diff = size[key] - float(expected[key])
                if diff < -0.05:
                    errors.append(f"model {key} {size[key]:.2f} mm is smaller than the datasheet {expected[key]} mm")
                elif key != "height" and diff > OVERSIZE_WARNING:
                    warnings.append(f"model {key} {size[key]:.2f} mm is {diff:.2f} mm over the datasheet {expected[key]} mm; fine only for parts that overhang (connectors, leads)")
                elif key == "height" and diff > 0.1:
                    errors.append(f"model height {size[key]:.2f} mm exceeds the datasheet {expected[key]} mm")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "measured": measured}


EP_PASTE_MIN_SIDE = 1.5  # mm; pads at least this wide and this big get a split-paste warning
EP_PASTE_MIN_AREA = 4.0  # mm^2
MIN_ANNULAR_RING = 0.15  # mm of copper around a plated hole; IPC-7251 nominal pads give 0.25 or more
MIN_PAD_GAP = 0.15  # mm of copper between pads; below this many fabs cannot keep a solder mask web
SILK_PAD_CLEARANCE = 0.2  # KLC
COURTYARD_CLEARANCE = 0.25  # KLC default; connectors and large parts often use 0.5
COURTYARD_LINE = 0.05  # KLC
GRID = 0.01  # KLC courtyard grid


Point = tuple[float, float]
Segment = tuple[Point, Point]


def _edges(pts: list[Point], closed: bool) -> list[Segment]:
    pairs = list(zip(pts, pts[1:]))
    if closed and len(pts) > 2:
        pairs.append((pts[-1], pts[0]))
    return pairs


def _graphic_edges(g) -> list[Segment]:
    if g.kind == "line" and len(g.pts) >= 2:
        return [(g.pts[0], g.pts[1])]
    if g.kind == "rect" and len(g.pts) >= 2:
        (x0, y0), (x1, y1) = g.pts[0], g.pts[1]
        return _edges([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], True)
    if g.kind == "poly":
        return _edges(g.pts, True)
    if g.kind == "arc" and len(g.pts) >= 3:
        return _edges(g.pts[:3], False)  # start, mid, end: close enough for clearances
    if g.kind == "circle" and len(g.pts) >= 2:
        (cx, cy), (ex, ey) = g.pts[0], g.pts[1]
        r = math.hypot(ex - cx, ey - cy)
        ring = [(cx + r * math.cos(a * math.pi / 16), cy + r * math.sin(a * math.pi / 16)) for a in range(32)]
        return _edges(ring, True)
    return []


def _point_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _point_box(p: Point, b: BBox) -> float:
    dx = max(b.xmin - p[0], 0.0, p[0] - b.xmax)
    dy = max(b.ymin - p[1], 0.0, p[1] - b.ymax)
    return math.hypot(dx, dy)


def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

    return orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0


def _segment_box(a: Point, b: Point, box: BBox) -> float:
    """Distance between a segment and a box; 0 if they touch or overlap."""
    if _point_box(a, box) == 0 or _point_box(b, box) == 0:
        return 0.0
    corners = [(box.xmin, box.ymin), (box.xmax, box.ymin), (box.xmax, box.ymax), (box.xmin, box.ymax)]
    if any(_segments_cross(a, b, c, d) for c, d in _edges(corners, True)):
        return 0.0
    return min([_point_box(a, box), _point_box(b, box)] + [_point_segment(c, a, b) for c in corners])


def _inside(p: Point, edges: list[Segment]) -> bool:
    """Even-odd test; points on an edge count as inside."""
    if any(_point_segment(p, a, b) < 1e-6 for a, b in edges):
        return True
    hits = 0
    for (x0, y0), (x1, y1) in edges:
        if (y0 > p[1]) != (y1 > p[1]):
            x = x0 + (p[1] - y0) * (x1 - x0) / (y1 - y0)
            if x > p[0]:
                hits += 1
    return hits % 2 == 1


def _box_gap(a: BBox, b: BBox) -> float:
    """Gap between two boxes; negative when they overlap."""
    dx = max(a.xmin - b.xmax, b.xmin - a.xmax)
    dy = max(a.ymin - b.ymax, b.ymin - a.ymax)
    if dx < 0 and dy < 0:
        return max(dx, dy)
    return math.hypot(max(dx, 0.0), max(dy, 0.0))


def _seg_dist(p, a, b) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _separated(a: list, b: list) -> bool:
    """Separating-axis test for two convex polygons."""
    for poly in (a, b):
        for i in range(len(poly)):
            (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % len(poly)]
            nx, ny = y1 - y2, x2 - x1
            if nx == ny == 0:
                continue
            pa = [nx * x + ny * y for x, y in a]
            pb = [nx * x + ny * y for x, y in b]
            if max(pa) < min(pb) or max(pb) < min(pa):
                return True
    return False


def pad_gap(a: Pad, b: Pad) -> float:
    """Copper to copper between two pads, by their real shapes (round and oval pads, rotation); negative when they overlap."""
    box = _box_gap(a.box, b.box)
    if box > 0.5:  # far apart: the boxes say enough
        return box
    pa, pb = a.outline(), b.outline()
    if not _separated(pa, pb):
        return box if box < 0 else 0.0
    edges = lambda poly: [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]
    return min(min(_seg_dist(p, *e) for p in pa for e in edges(pb)), min(_seg_dist(p, *e) for p in pb for e in edges(pa)))


def _copper(p: Pad) -> bool:
    if p.kind == "np_thru_hole" and p.drill is not None and p.width <= p.drill + 1e-6 and p.height <= (p.drill_y or p.drill) + 1e-6:
        return False  # a bare hole: KiCad lists *.Cu on it but there is no copper round it
    return any(layer.endswith(".Cu") for layer in p.layers) or (not p.layers and p.kind != "np_thru_hole")


def _front(p: Pad) -> bool:
    return any(layer in ("F.Cu", "*.Cu") for layer in p.layers) or not p.layers


def check_footprint(fp: Footprint, courtyard_clearance: float = COURTYARD_CLEARANCE) -> dict:
    """Review a footprint. Errors would make a bad board; warnings break library conventions or need a look."""
    errors: list[str] = []
    warnings: list[str] = []
    measured: dict = {}
    if not fp.pads:
        return {"ok": False, "errors": ["footprint has no pads"], "warnings": [], "measured": {}}

    thin_rings: dict[float, list[str]] = {}
    for p in fp.pads:
        label = f"pad {p.number or '(unnumbered)'}"
        if p.width <= 0 or p.height <= 0:
            errors.append(f"{label} has no size")
        if p.kind == "thru_hole" and p.drill is not None:
            hole = f"{p.drill}x{p.drill_y}" if p.drill_y else f"{p.drill}"
            ring = min((p.width - p.drill) / 2 - abs(p.offset[0]), (p.height - (p.drill_y or p.drill)) / 2 - abs(p.offset[1]))
            measured.setdefault("min_annular_ring", ring)
            measured["min_annular_ring"] = round(min(measured["min_annular_ring"], ring), 4)
            if ring <= 1e-9:
                errors.append(f"{label}: drill {hole} mm leaves no copper ring on a {p.width}x{p.height} mm pad")
            elif ring < MIN_ANNULAR_RING - 1e-6:
                thin_rings.setdefault(round(ring, 3), []).append(p.number or "(unnumbered)")
        if p.drill_y is not None and p.shape not in ("oval", "roundrect", "rect"):
            warnings.append(f"{label}: slotted hole in a {p.shape} pad; use an oval pad")
        if p.kind == "smd" and p.layers and not any(layer.endswith(".Cu") for layer in p.layers) and p.number:
            warnings.append(f"{label} has a number but no copper layer")
    for ring, numbers in sorted(thin_rings.items()):
        which = f"pad {numbers[0]}" if len(numbers) == 1 else f"pads {', '.join(numbers)}"
        warnings.append(f"{which}: copper ring {ring:.3f} mm around the hole; most fabs want at least {MIN_ANNULAR_RING} mm. "
                        "Keep it if the datasheet's land pattern draws it so, and check the fab's minimum")

    # A big pad with one full paste opening (an exposed pad as drawn) floats the part and voids; IPC-7093 wants it split.
    paste_only = [p for p in fp.pads if "F.Paste" in p.layers and not _copper(p)]
    for p in fp.pads:
        if p.kind == "smd" and "F.Paste" in p.layers and _copper(p) and min(p.width, p.height) >= EP_PASTE_MIN_SIDE \
                and p.width * p.height >= EP_PASTE_MIN_AREA and not any(_box_gap(p.box, q.box) < 0 for q in paste_only):
            warnings.append(f"pad {p.number or '(unnumbered)'} ({p.width}x{p.height} mm) has one paste opening over all of it; "
                            "an exposed or thermal pad needs its paste split to about 50-80% (IPC-7093), or the part floats "
                            "and voids. generate_footprint's custom family does that with paste_coverage on the pad. "
                            "Fine as is for a mounting tab or a big terminal")

    # Copper between differently numbered pads.
    copper = [p for p in fp.pads if _copper(p)]
    worst, close = None, []
    for i, a in enumerate(copper):
        for b in copper[i + 1 :]:
            if a.number and a.number == b.number:
                continue
            gap = pad_gap(a, b)
            if worst is None or gap < worst:
                worst = gap
            if gap < -1e-6:
                errors.append(f"pads {a.number or '?'} and {b.number or '?'} overlap")
            elif gap < MIN_PAD_GAP - 1e-6:
                close.append((gap, a.number or "?", b.number or "?"))
    if worst is not None:
        measured["min_pad_gap"] = round(worst, 4)
    if close:
        close.sort()
        shown = ", ".join(f"{a} and {b} ({g:.3f} mm)" for g, a, b in close[:8])
        more = f" and {len(close) - 8} more pairs" if len(close) > 8 else ""
        warnings.append(f"{len(close)} pad pair{'s are' if len(close) > 1 else ' is'} closer than {MIN_PAD_GAP} mm, where many fabs "
                        f"drop the solder mask between them: {shown}{more}")

    numbers = [p.number for p in fp.pads if p.number]
    if fp.pin1 is None:
        warnings.append("no pad numbered 1 or A1")
    ints = sorted({int(n) for n in numbers if n.isdigit()})
    if ints and len(ints) == len(set(numbers)) and ints != list(range(1, len(ints) + 1)):
        missing = sorted(set(range(1, ints[-1] + 1)) - set(ints))
        warnings.append(f"pad numbers skip {missing[:10]}; fine only if the datasheet leaves those pins out")

    # Courtyard.
    court = [e for g in fp.on_layer("F.CrtYd") for e in _graphic_edges(g)]
    if not court:
        errors.append("no front courtyard (F.CrtYd)")
    else:
        outside = [p.number or "?" for p in fp.pads if _front(p) and not all(_inside(c, court) for c in _box_corners(p.box))]
        if outside:
            errors.append(f"pads outside the courtyard: {outside[:10]}")
        else:
            gap = min(min(_segment_box(a, b, p.box) for a, b in court) for p in fp.pads if _front(p))
            measured["courtyard_to_pad"] = round(gap, 4)
            if gap < courtyard_clearance - 0.005:
                warnings.append(f"courtyard is {gap:.3f} mm from the nearest pad; expected at least {courtyard_clearance} mm")
        fab_pts = [pt for g in fp.on_layer("F.Fab") for e in _graphic_edges(g) for pt in e]
        if fab_pts and not all(_inside(pt, court) for pt in fab_pts):
            warnings.append("the fabrication outline (body) reaches outside the courtyard")
        off_grid = {v for g in fp.on_layer("F.CrtYd") for pt in g.pts for v in pt if abs(v / GRID - round(v / GRID)) > 1e-6}
        if off_grid:
            warnings.append(f"courtyard points off the {GRID} mm grid: {sorted(off_grid)[:5]}")
        widths = {g.width for g in fp.on_layer("F.CrtYd")}
        if widths - {COURTYARD_LINE}:
            warnings.append(f"courtyard line width {sorted(widths)} mm; KiCad's library uses {COURTYARD_LINE} mm")

    # Silkscreen against copper on the same side.
    for side, cu in (("F.SilkS", "F.Cu"), ("B.SilkS", "B.Cu")):
        pads = [p for p in fp.pads if cu in p.layers or "*.Cu" in p.layers or (not p.layers and cu == "F.Cu")]
        close = None
        for g in fp.on_layer(side):
            for a, b in _graphic_edges(g):
                for p in pads:
                    d = _segment_box(a, b, p.box) - g.width / 2
                    if close is None or d < close[0]:
                        close = (d, p.number)
        if close is None:
            continue
        measured[f"{side}_to_pad"] = round(close[0], 4)
        if close[0] < -1e-6:
            errors.append(f"{side} silkscreen lies on pad {close[1]}")
        elif close[0] < SILK_PAD_CLEARANCE - 0.005:
            warnings.append(f"{side} silkscreen is {close[0]:.3f} mm from pad {close[1]}; KiCad's library keeps {SILK_PAD_CLEARANCE} mm")

    if not fp.on_layer("F.Fab", "B.Fab"):
        warnings.append("no fabrication outline (F.Fab) showing the body")
    names = {t[0] for t in fp.texts}
    for field_name in ("Reference", "Value"):
        if field_name not in names:
            warnings.append(f"no {field_name} field")
    if not any(t[1] == "${REFERENCE}" and t[2].endswith("Fab") for t in fp.texts):
        warnings.append("no ${REFERENCE} text on the fabrication layer")

    # Origin conventions.
    ext = fp.pad_extents
    if fp.smd and ext is not None:
        cx, cy = ext.center
        if math.hypot(cx, cy) > 0.01:
            warnings.append(f"SMD footprint origin is ({-cx:+.3f}, {-cy:+.3f}) mm from the pad centre; KiCad's library puts it at the centre")
    if not any(p.kind == "smd" for p in fp.pads) and fp.pin1 is not None and (abs(fp.pin1.x) > 1e-3 or abs(fp.pin1.y) > 1e-3):
        warnings.append("through-hole footprint whose pin 1 is not at the origin (KiCad library convention puts it there)")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "measured": measured}


def _box_corners(b: BBox) -> list[Point]:
    return [(b.xmin, b.ymin), (b.xmax, b.ymin), (b.xmax, b.ymax), (b.xmin, b.ymax)]


def compare_footprints(a: Footprint, b: Footprint, tolerance: float = 0.005, align: bool = False) -> dict:
    """Compare two footprints pad by pad (a is the candidate, b the reference).

    With align, a footprint whose origin sits elsewhere (pin 1 against the pads' centre, say) is first moved by
    the median offset of the pads both number alike, and that offset is reported as origin_offset.
    """

    def by_number(fp: Footprint) -> dict[str, list[Pad]]:
        out: dict[str, list[Pad]] = {}
        for p in fp.pads:
            if p.number:
                out.setdefault(p.number, []).append(p)
        for v in out.values():
            v.sort(key=lambda p: (p.x, p.y))
        return out

    pa, pb = by_number(a), by_number(b)
    ox = oy = 0.0
    if align:
        single = [n for n in set(pa) & set(pb) if len(pa[n]) == len(pb[n]) == 1]
        if single:
            ox = statistics.median(pa[n][0].x - pb[n][0].x for n in single)
            oy = statistics.median(pa[n][0].y - pb[n][0].y for n in single)
            if math.hypot(ox, oy) <= tolerance:
                ox = oy = 0.0
    if ox or oy:
        pa = {n: sorted(v, key=lambda p: (p.x - ox, p.y - oy)) for n, v in pa.items()}
    only_a = sorted(set(pa) - set(pb), key=_natural)
    only_b = sorted(set(pb) - set(pa), key=_natural)
    diffs = []
    worst = 0.0
    for n in sorted(set(pa) & set(pb), key=_natural):
        if len(pa[n]) != len(pb[n]):
            diffs.append({"pad": n, "issue": f"{len(pa[n])} pads in the candidate, {len(pb[n])} in the reference"})
            continue
        for x, y in zip(pa[n], pb[n]):
            (xw, xh), (yw, yh) = x.half_size, y.half_size
            d = {"dx": x.x - ox - y.x, "dy": x.y - oy - y.y, "dwidth": 2 * (xw - yw), "dheight": 2 * (xh - yh)}
            m = max(abs(v) for v in d.values())
            worst = max(worst, m)
            extra = {}
            if x.kind != y.kind:
                extra["kind"] = f"{x.kind} vs {y.kind}"
            if x.shape != y.shape:
                extra["shape"] = f"{x.shape} vs {y.shape}"
            if x.offset != y.offset:
                extra["offset"] = f"{x.offset} vs {y.offset}"
            if (x.drill, x.drill_y) != (y.drill, y.drill_y):
                extra["drill"] = f"{_hole(x)} vs {_hole(y)}"
            if m > tolerance or extra:
                diffs.append({"pad": n} | {k: round(v, 4) for k, v in d.items()} | extra)
    result = {
        "same_pads": not only_a and not only_b and not diffs,
        "max_pad_difference": round(worst, 4),
        "only_in_candidate": only_a,
        "only_in_reference": only_b,
        "pad_differences": diffs[:50],
    }
    if ox or oy:
        result["origin_offset"] = [round(ox, 4), round(oy, 4)]
        result["note"] = (f"the origins differ: the reference's origin is at ({-ox:g}, {-oy:g}) in the candidate's frame. "
                          "Pads and boxes are compared with the footprints aligned")
    for key, box_a, box_b in (("courtyard", a.courtyard, b.courtyard), ("pad_extents", a.pad_extents, b.pad_extents)):
        if box_a and box_b:
            shift = {"xmin": ox, "ymin": oy, "xmax": ox, "ymax": oy}
            result[f"{key}_difference"] = {k: round(getattr(box_a, k) - shift[k] - getattr(box_b, k), 4) for k in ("xmin", "ymin", "xmax", "ymax")}
    return result


def _hole(p: Pad) -> str:
    if p.drill is None:
        return "none"
    return f"{p.drill}x{p.drill_y}" if p.drill_y else f"{p.drill}"


def _natural(s: str):
    return (0, int(s), "") if s.isdigit() else (1, 0, s)

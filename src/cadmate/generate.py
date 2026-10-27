"""Generate KiCad footprints from datasheet dimensions.

The agent reads the datasheet; everything from there on is deterministic:
pads from IPC-7351B (see ipc.py) or from the datasheet's own land pattern,
then courtyard, fabrication outline, silkscreen and text per the KiCad
library conventions (KLC). Coordinates are mm in KiCad's footprint frame
(Y points down). The file is written in the KiCad 8 format, which KiCad 8
and every later version opens.

Families:
- soic:   gull-wing leads on two sides (SOIC, SOP, SSOP, TSSOP, MSOP)
- qfn:    no-lead pads on four sides, or two for DFN, with an optional exposed pad
- custom: pads placed as the datasheet's recommended land pattern gives them
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import check, footprint, ipc, packages, sexpr
from .footprint import BBox, Model, Pad

FAMILIES: dict[str, dict] = {
    "soic": {
        "description": "Gull-wing leads on two sides: SOIC, SOP, SSOP, TSSOP, MSOP. Pin 1 top left, pins counted down the left side then up the right.",
        "required": ["pins", "pitch", "body_width", "body_length", "overall_width", "lead_width", "lead_length"],
        "optional": ["package"],
        "notes": "body_width is across the leads (JEDEC E1), body_length along them (D), overall_width tip to tip (E), lead_length the flat foot (L), lead_width b.",
    },
    "qfn": {
        "description": "No-lead packages: QFN, DFN, with an optional exposed pad. Pins counted anticlockwise from the top of the left side.",
        "required": ["pitch", "body_width", "body_length", "lead_width", "lead_length"],
        "optional": ["pins", "pins_x", "pins_y", "ep_width", "ep_length", "paste_coverage", "heel_reduction", "package"],
        "notes": "pins_x pads on each of the top and bottom edges, pins_y on each of the left and right edges (pins_x 0 for DFN). Square parts may give pins alone. lead_length is L, lead_width b. paste_coverage of the exposed pad defaults to 0.65.",
    },
    "custom": {
        "description": "Any part whose datasheet gives a recommended land pattern: modules, connectors, odd packages. Pads are placed as given.",
        "required": ["body_width", "body_length"],
        "optional": ["pads", "pad_rows", "body_center", "origin"],
        "notes": (
            "pads: [{number, x, y, width, height, shape?, kind?, drill?, rotation?}], kind smd (default), thru_hole or np_thru_hole. "
            "Through-hole pads may give the lead instead of drill and size: lead_diameter for round leads, or lead_width and "
            "lead_thickness for flat ones (add slot: true for a slotted hole, long axis along X; rotate 90 for Y). Holes and pads "
            "then follow IPC-7251 for the density; give leads as [min, max] where the datasheet has a range. "
            "pad_rows: [{first, count, x, y, dx, dy, width, height, ...}] numbers pads first, first+1, ... stepping by (dx, dy). "
            "Coordinates in KiCad's frame, Y down. origin: auto (SMD centre of the pads, through-hole pin 1), center, pin1 or as_given."
        ),
    },
}

# Only the soic and qfn values need tolerances; the rest are plain numbers.
TOLERANCED = {"body_width", "body_length", "overall_width", "lead_width", "lead_length"}

SILK_WIDTH = 0.12
FAB_WIDTH = 0.1
COURTYARD_WIDTH = 0.05
SILK_OFFSET = 0.11  # silk outline outside the nominal body
SILK_PAD_CLEARANCE = 0.2
COURTYARD_GRID = 0.01
EP_PAD_CLEARANCE = 0.2  # copper gap between the exposed pad and the signal pads
MAX_RADIUS = 0.25  # roundrect corner radius cap


@dataclass
class Design:
    name: str
    descr: str
    tags: str
    pads: list[Pad]
    body: BBox
    pin1_corner: tuple[int, int] = (-1, -1)  # which body corner gets the fab chamfer
    courtyard_clearance: float = 0.25
    courtyard_rects: list[BBox] = field(default_factory=list)  # union is the courtyard; defaults to body and pads
    report: dict = field(default_factory=dict)
    model: Model | None = None


def generate(
    family: str,
    params: dict,
    name: str | None = None,
    density: str = "nominal",
    courtyard_clearance: float | None = None,
    datasheet: str | None = None,
    model_path: str | None = None,
) -> dict:
    """Build a footprint. Returns {"ok", "name", "text", "report", "errors", "warnings"}; text is set only when ok."""
    if family not in FAMILIES:
        return _fail([f"unknown family {family!r}; known: {sorted(FAMILIES)}"])
    if density not in ipc.DENSITIES:
        return _fail([f"density must be one of {ipc.DENSITIES}"])
    spec = FAMILIES[family]
    missing = [k for k in spec["required"] if params.get(k) is None]
    if missing:
        return _fail([f"missing {k}" for k in missing])
    warnings: list[str] = []
    unknown = sorted(set(params) - set(spec["required"]) - set(spec["optional"]) - {"height", "standoff"})
    if unknown:
        warnings.append(f"ignored unknown parameters: {unknown}")

    try:
        if family == "soic":
            design = _soic(params, density)
        elif family == "qfn":
            design = _qfn(params, density)
        else:
            design = _custom(params, density)
    except (ValueError, TypeError, KeyError) as e:
        return _fail([str(e)], warnings)

    if name:
        design.name = name
    if courtyard_clearance is not None:
        design.courtyard_clearance = courtyard_clearance
    elif family != "custom":
        design.courtyard_clearance = ipc.COURTYARD[density]
    if datasheet:
        design.descr = f"{design.descr}, {datasheet}"
    if model_path:
        design.model = Model(path=model_path)
    warnings += design.report.pop("warnings", [])

    text = to_kicad_mod(design)
    fp = footprint.parse(text)  # never hand out a file we cannot read back
    review = check.check_footprint(fp, courtyard_clearance=design.courtyard_clearance)
    errors = review["errors"]
    warnings += review["warnings"]
    return {
        "ok": not errors,
        "name": design.name,
        "text": text if not errors else None,
        "report": design.report,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "pads": len(fp.pads),
            "signal_pads": len(fp.signal_pads()),
            "pitch": fp.pitch(),
            "pad_extents": vars(fp.pad_extents) if fp.pad_extents else None,
            "courtyard": vars(fp.courtyard) if fp.courtyard else None,
        },
    }


def _fail(errors: list[str], warnings: list[str] | None = None) -> dict:
    return {"ok": False, "name": None, "text": None, "report": {}, "errors": errors, "warnings": warnings or []}


def _dims(params: dict, keys) -> dict[str, ipc.Dim]:
    out = {}
    for k in keys:
        if params.get(k) is None:
            continue
        try:
            out[k] = ipc.dim(params[k])
        except ValueError as e:
            raise ValueError(f"{k}: {e}") from None
        if out[k].min <= 0:
            raise ValueError(f"{k} must be positive")
    return out


def _nominals(params: dict) -> dict[str, float]:
    out = {}
    for k, v in params.items():
        try:
            out[k] = ipc.dim(v).nom
        except (ValueError, TypeError):
            pass
    return out


def _geometry_check(family: str, params: dict) -> None:
    errors = packages.geometry_errors(family, _nominals(params))
    if errors:
        raise ValueError("; ".join(errors))


def _f(v: float) -> str:
    return footprint._fmt(v)


def _rratio(w: float, h: float) -> float:
    return round(min(0.25, MAX_RADIUS / min(w, h)), 4)


def _smd(number: str, x: float, y: float, w: float, h: float, shape: str = "roundrect", layers=None) -> Pad:
    return Pad(number, "smd", shape, round(x, 6), round(y, 6), 0.0, round(w, 6), round(h, 6), None, layers or ["F.Cu", "F.Paste", "F.Mask"])


def _soic(params: dict, density: str) -> Design:
    _geometry_check("soic", params)
    d = _dims(params, TOLERANCED)
    pins, pitch = int(ipc.dim(params["pins"]).nom), ipc.dim(params["pitch"]).nom
    if pins < 2 or pins % 2:
        raise ValueError("soic needs an even pin count of at least 2")
    table = "gullwing_fine" if pitch <= ipc.FINE_PITCH + 1e-9 else "gullwing"
    fillets = ipc.FILLETS[table][density]
    land = ipc.land(d["overall_width"], d["lead_length"], d["lead_width"], fillets)
    if land.g <= 0:
        raise ValueError("pads meet in the middle; check overall_width and lead_length")
    per_side = pins // 2
    pads = []
    for i in range(per_side):
        y = (i - (per_side - 1) / 2) * pitch
        pads.append(_smd(str(i + 1), -land.center, y, land.pad_length, land.x))
    for i in range(per_side):
        y = ((per_side - 1) / 2 - i) * pitch
        pads.append(_smd(str(per_side + i + 1), land.center, y, land.pad_length, land.x))
    bw, bl = d["body_width"].nom, d["body_length"].nom
    pkg = params.get("package") or "SOIC"
    name = f"{pkg}-{pins}_{_f(bw)}x{_f(bl)}mm_P{_f(pitch)}mm"
    return Design(
        name=name,
        descr=f"{pkg}, {pins} Pin, body {_f(bw)}x{_f(bl)} mm, pitch {_f(pitch)} mm, IPC-7351B {density} density",
        tags=f"{pkg} SO",
        pads=pads,
        body=BBox(-bw / 2, -bl / 2, bw / 2, bl / 2),
        report={"ipc_table": table, "fillets_toe_heel_side": fillets, "Z": land.z, "G": land.g, "X": land.x,
                "pad_size": [land.pad_length, land.x], "pad_center_x": land.center},
    )


def _qfn(params: dict, density: str) -> Design:
    _geometry_check("qfn", params)
    d = _dims(params, TOLERANCED)
    pitch = ipc.dim(params["pitch"]).nom
    pins_x, pins_y = _qfn_counts(params)
    fillets = ipc.FILLETS["no_lead"][density]
    heel = float(params.get("heel_reduction") or 0.0)
    ep_w = ipc.dim(params["ep_width"]).nom if params.get("ep_width") is not None else None
    ep_l = ipc.dim(params["ep_length"]).nom if params.get("ep_length") is not None else None
    if (ep_w is None) != (ep_l is None):
        raise ValueError("give both ep_width and ep_length, or neither")
    warnings: list[str] = []
    report: dict = {"ipc_table": "no_lead", "fillets_toe_heel_side": fillets}

    def side(span: ipc.Dim, ep: float | None, label: str) -> ipc.Land:
        land = ipc.land(span, d["lead_length"], d["lead_width"], fillets, heel)
        if ep is not None and (land.g - ep) / 2 < EP_PAD_CLEARANCE - 1e-9:
            g = ipc.ceil_to(ep + 2 * EP_PAD_CLEARANCE, ipc.ROUND_BASE)
            warnings.append(f"{label} pads shortened at the heel to keep {EP_PAD_CLEARANCE} mm from the exposed pad (G {land.g} -> {g})")
            land = ipc.Land(land.z, g, land.x)
        if land.z - land.g <= 0:
            raise ValueError(f"no room for the {label} pads between the exposed pad and the body edge")
        report[f"{label}_Z_G_X"] = [land.z, land.g, land.x]
        return land

    pads: list[Pad] = []
    n = 0
    lr = side(d["body_width"], ep_w, "left/right") if pins_y else None
    tb = side(d["body_length"], ep_l, "top/bottom") if pins_x else None
    # Anticlockwise seen from the top, starting at the top of the left side (Y points down).
    if lr:
        for i in range(pins_y):
            n += 1
            pads.append(_smd(str(n), -lr.center, (i - (pins_y - 1) / 2) * pitch, lr.pad_length, lr.x))
    if tb:
        for i in range(pins_x):
            n += 1
            pads.append(_smd(str(n), (i - (pins_x - 1) / 2) * pitch, tb.center, tb.x, tb.pad_length))
    if lr:
        for i in range(pins_y):
            n += 1
            pads.append(_smd(str(n), lr.center, ((pins_y - 1) / 2 - i) * pitch, lr.pad_length, lr.x))
    if tb:
        for i in range(pins_x):
            n += 1
            pads.append(_smd(str(n), ((pins_x - 1) / 2 - i) * pitch, -tb.center, tb.x, tb.pad_length))
    if ep_w is not None:
        n += 1
        pads.append(_smd(str(n), 0, 0, ep_w, ep_l, "rect", ["F.Cu", "F.Mask"]))
        pads += _paste_grid(ep_w, ep_l, float(params.get("paste_coverage") or 0.65))
        report["ep_paste"] = f"{len(pads) - n} paste openings, {round(100 * float(params.get('paste_coverage') or 0.65))}% coverage"

    bw, bl = d["body_width"].nom, d["body_length"].nom
    signal = 2 * (pins_x + pins_y)
    pkg = params.get("package") or ("DFN" if pins_x == 0 or pins_y == 0 else "QFN")
    ep_tag = "-1EP" if ep_w is not None else ""
    ep_name = f"_EP{_f(ep_w)}x{_f(ep_l)}mm" if ep_w is not None else ""
    report["warnings"] = warnings
    return Design(
        name=f"{pkg}-{signal}{ep_tag}_{_f(bw)}x{_f(bl)}mm_P{_f(pitch)}mm{ep_name}",
        descr=f"{pkg}, {signal} Pin, body {_f(bw)}x{_f(bl)} mm, pitch {_f(pitch)} mm, IPC-7351B {density} density",
        tags=f"{pkg} NoLead",
        pads=pads,
        body=BBox(-bw / 2, -bl / 2, bw / 2, bl / 2),
        report=report,
    )


def _qfn_counts(params: dict) -> tuple[int, int]:
    def count(k):
        return int(ipc.dim(params[k]).nom) if params.get(k) is not None else None

    pins, px, py = count("pins"), count("pins_x"), count("pins_y")
    if px is None and py is None:
        if pins is None or pins % 4:
            raise ValueError("give pins_x and pins_y, or a pin count divisible by 4 for a square part")
        px = py = pins // 4
    elif px is None or py is None:
        if pins is None:
            raise ValueError("give both pins_x and pins_y, or one of them with pins")
        known = px if px is not None else py
        other, rem = divmod(pins - 2 * known, 2)
        if rem or other < 0:
            raise ValueError(f"{pins} pins do not split into sides with {known} pins on two of them")
        px, py = (known, other) if px is not None else (other, known)
    if pins is not None and pins != 2 * (px + py):
        raise ValueError(f"pins {pins} does not match 2 * (pins_x {px} + pins_y {py})")
    if px + py == 0:
        raise ValueError("no pins")
    return px, py


def _paste_grid(w: float, h: float, coverage: float) -> list[Pad]:
    """Split the exposed pad's paste into a grid of openings, so the part does not float on one big blob."""
    if not 0.2 <= coverage <= 1.0:
        raise ValueError("paste_coverage must be between 0.2 and 1")
    nx, ny = max(1, math.ceil(w / 1.25 - 1e-9)), max(1, math.ceil(h / 1.25 - 1e-9))
    k = math.sqrt(coverage)
    pw, ph = round(w / nx * k, 2), round(h / ny * k, 2)
    out = []
    for i in range(nx):
        for j in range(ny):
            x = (i - (nx - 1) / 2) * w / nx
            y = (j - (ny - 1) / 2) * h / ny
            out.append(Pad("", "smd", "roundrect", round(x, 6), round(y, 6), 0.0, pw, ph, None, ["F.Paste"]))
    return out


def _custom(params: dict, density: str = "nominal") -> Design:
    pads: list[Pad] = []
    sizing: dict[str, dict] = {}  # lead -> computed hole and pad, for the report
    for row in params.get("pad_rows") or []:
        for k in ("first", "count", "x", "y"):
            if row.get(k) is None:
                raise ValueError(f"pad_rows entry needs {k}")
        for i in range(int(row["count"])):
            spec = {k: v for k, v in row.items() if k not in ("first", "count", "dx", "dy")}
            spec |= {"number": str(int(row["first"]) + i), "x": row["x"] + i * row.get("dx", 0), "y": row["y"] + i * row.get("dy", 0)}
            pads.append(_custom_pad(spec, density, sizing))
    for spec in params.get("pads") or []:
        pads.append(_custom_pad(spec, density, sizing))
    if not pads:
        raise ValueError("custom needs pads or pad_rows")

    bw, bl = float(params["body_width"]), float(params["body_length"])
    ext = _extents(pads)
    bc = params.get("body_center")
    bx, by = (float(bc[0]), float(bc[1])) if bc else ext.center
    body = BBox(bx - bw / 2, by - bl / 2, bx + bw / 2, by + bl / 2)

    tht = any(p.kind != "smd" for p in pads)
    origin = params.get("origin") or "auto"
    pin1 = next((p for p in pads if p.number in ("1", "A1")), None)
    if origin == "auto":
        origin = "pin1" if tht else "center"
    if origin == "center":
        shift = ext.center
    elif origin == "pin1":
        if pin1 is None:
            raise ValueError("origin pin1 needs a pad numbered 1 or A1")
        shift = (pin1.x, pin1.y)
    elif origin == "as_given":
        shift = (0.0, 0.0)
    else:
        raise ValueError("origin must be auto, center, pin1 or as_given")
    sx, sy = shift
    for p in pads:
        p.x, p.y = round(p.x - sx, 6), round(p.y - sy, 6)
    body = BBox(body.xmin - sx, body.ymin - sy, body.xmax - sx, body.ymax - sy)

    corner = (-1, -1)
    if pin1 is not None:
        cx, cy = body.center
        corner = (-1 if pin1.x <= cx else 1, -1 if pin1.y <= cy else 1)
    report = {"origin": origin, "shifted_by": [round(-sx, 6), round(-sy, 6)], "through_hole": tht}
    if sizing:
        report["ipc7251_holes"] = list(sizing.values())
    return Design(name="Custom", descr="Custom land pattern from the datasheet", tags="", pads=pads, body=body,
                  pin1_corner=corner, report=report, courtyard_rects=[])


def _custom_pad(spec: dict, density: str = "nominal", sizing: dict | None = None) -> Pad:
    for k in ("number", "x", "y"):
        if spec.get(k) is None:
            raise ValueError(f"pad needs {k}: {spec}")
    leads = {k: spec[k] for k in ("lead_diameter", "lead_width", "lead_thickness") if spec.get(k) is not None}
    kind = spec.get("kind", "thru_hole" if spec.get("drill") or leads else "smd")
    if kind not in ("smd", "thru_hole", "np_thru_hole"):
        raise ValueError(f"pad kind must be smd, thru_hole or np_thru_hole, got {kind!r}")
    number = "" if kind == "np_thru_hole" else str(spec["number"])
    drill = drill_y = None
    w, h = spec.get("width"), spec.get("height")
    if kind == "thru_hole" and leads and spec.get("drill") is None:
        try:
            hole = ipc.tht_hole(density, slot=bool(spec.get("slot")), **{k: ipc.dim(v) for k, v in leads.items()})
        except ValueError as e:
            raise ValueError(f"pad {number}: {e}") from None
        drill, drill_y = hole.drill, hole.drill_y
        if w is None and h is None:
            w, h = hole.pad_w, hole.pad_h
        if sizing is not None:
            key = repr(sorted(leads.items())) + repr(bool(spec.get("slot")))
            sizing.setdefault(key, {"lead": leads, "drill": [drill, drill_y] if drill_y else drill, "pad": [w, h], "pads": []})["pads"].append(number)
    elif kind != "smd":
        if spec.get("drill") is None:
            raise ValueError(f"through-hole pad {number!r} needs drill, or the lead size to work it out from")
        d = spec["drill"]
        drill, drill_y = (float(d[0]), float(d[1])) if isinstance(d, (list, tuple)) else (float(d), None)
        if w is None and h is None and kind == "np_thru_hole":
            w, h = drill, drill_y or drill
    if w is None or h is None:
        raise ValueError(f"pad {number!r} needs width and height")
    w, h = float(w), float(h)
    if kind == "smd":
        shape, layers = spec.get("shape", "roundrect"), ["F.Cu", "F.Paste", "F.Mask"]
    else:
        default = "circle" if abs(w - h) < 1e-9 else "oval"
        if number in ("1", "A1") and kind == "thru_hole":
            default = "rect" if abs(w - h) < 1e-9 else "roundrect"
        shape, layers = spec.get("shape", default), ["*.Cu", "*.Mask"]
    if shape not in ("rect", "roundrect", "circle", "oval"):
        raise ValueError(f"pad shape must be rect, roundrect, circle or oval, got {shape!r}")
    return Pad(number, kind, shape, float(spec["x"]), float(spec["y"]), float(spec.get("rotation", 0.0)), w, h, drill, layers, drill_y)


def _extents(pads: list[Pad]) -> BBox:
    boxes = [p.box for p in pads]
    return BBox(min(b.xmin for b in boxes), min(b.ymin for b in boxes), max(b.xmax for b in boxes), max(b.ymax for b in boxes))


# --- courtyard, silkscreen, fabrication ---------------------------------------------------------


def _grow(b: BBox, by: float) -> BBox:
    """Grow a box and snap it outward to the courtyard grid."""
    return BBox(-ipc.ceil_to(-(b.xmin - by), COURTYARD_GRID), -ipc.ceil_to(-(b.ymin - by), COURTYARD_GRID),
                ipc.ceil_to(b.xmax + by, COURTYARD_GRID), ipc.ceil_to(b.ymax + by, COURTYARD_GRID))


def courtyard_outline(design: Design) -> list[list[tuple[float, float]]]:
    """Closed loops tracing the courtyard: body and each pad group grown by the clearance, as one rectilinear outline."""
    c = design.courtyard_clearance
    if design.courtyard_rects:
        rects = [_grow(r, c) for r in design.courtyard_rects]
    elif any(p.kind != "smd" for p in design.pads) or len(design.pads) < 2:
        rects = [_grow(_union_box([design.body, _extents(design.pads)]), c)]
    else:
        rects = [_grow(design.body, c)] + [_grow(g, c) for g in _pad_groups(design.pads)]
    return _union_outline(rects)


def _union_box(boxes: list[BBox]) -> BBox:
    return BBox(min(b.xmin for b in boxes), min(b.ymin for b in boxes), max(b.xmax for b in boxes), max(b.ymax for b in boxes))


def _pad_groups(pads: list[Pad]) -> list[BBox]:
    """Boxes around the pads on each side of the centre (left, right, top, bottom rows) and the centre pads."""
    groups: dict[str, list[BBox]] = {}
    for p in pads:
        if not p.number:
            continue
        b = p.box
        if b.xmax < 0 and abs(p.x) >= abs(p.y):
            key = "left"
        elif b.xmin > 0 and abs(p.x) >= abs(p.y):
            key = "right"
        elif b.ymax < 0:
            key = "top"
        elif b.ymin > 0:
            key = "bottom"
        else:
            key = "centre"
        groups.setdefault(key, []).append(b)
    return [_union_box(v) for v in groups.values()]


def _union_outline(rects: list[BBox]) -> list[list[tuple[float, float]]]:
    """Outline of a union of axis-aligned rectangles, as closed loops of corner points."""
    xs = sorted({v for r in rects for v in (r.xmin, r.xmax)})
    ys = sorted({v for r in rects for v in (r.ymin, r.ymax)})

    def filled(i: int, j: int) -> bool:
        if not (0 <= i < len(xs) - 1 and 0 <= j < len(ys) - 1):
            return False
        cx, cy = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
        return any(r.xmin < cx < r.xmax and r.ymin < cy < r.ymax for r in rects)

    # Directed edges with the filled cell on the same side, so they chain into loops.
    nxt: dict[tuple[int, int], tuple[int, int]] = {}
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            if not filled(i, j):
                continue
            if not filled(i, j - 1):
                nxt[(i, j)] = (i + 1, j)
            if not filled(i + 1, j):
                nxt[(i + 1, j)] = (i + 1, j + 1)
            if not filled(i, j + 1):
                nxt[(i + 1, j + 1)] = (i, j + 1)
            if not filled(i - 1, j):
                nxt[(i, j + 1)] = (i, j)
    loops = []
    while nxt:
        start = next(iter(nxt))
        loop, cur = [start], nxt.pop(start)
        while cur != start:
            loop.append(cur)
            cur = nxt.pop(cur)
        # Drop points in the middle of a straight run.
        pts = [(xs[i], ys[j]) for i, j in loop]
        keep = []
        for k, p in enumerate(pts):
            a, b = pts[k - 1], pts[(k + 1) % len(pts)]
            if not ((a[0] == p[0] == b[0]) or (a[1] == p[1] == b[1])):
                keep.append(p)
        loops.append(keep)
    return loops


def _clip_segment(a: tuple[float, float], b: tuple[float, float], keepouts: list[BBox]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Cut the parts of an axis-aligned segment that run through any keep-out box."""
    horizontal = abs(a[1] - b[1]) < 1e-9
    fixed = a[1] if horizontal else a[0]
    lo, hi = sorted((a[0], b[0]) if horizontal else (a[1], b[1]))
    pieces = [(lo, hi)]
    for k in keepouts:
        k_lo, k_hi = (k.xmin, k.xmax) if horizontal else (k.ymin, k.ymax)
        f_lo, f_hi = (k.ymin, k.ymax) if horizontal else (k.xmin, k.xmax)
        if not f_lo < fixed < f_hi:
            continue
        out = []
        for s, e in pieces:
            if e <= k_lo or s >= k_hi:
                out.append((s, e))
                continue
            if s < k_lo:
                out.append((s, k_lo))
            if e > k_hi:
                out.append((k_hi, e))
        pieces = out
    pieces = [(s, e) for s, e in pieces if e - s > 0.05]
    if horizontal:
        return [((s, fixed), (e, fixed)) for s, e in pieces]
    return [((fixed, s), (fixed, e)) for s, e in pieces]


def _silk(design: Design) -> tuple[list, list]:
    """Silk outline segments and the pin 1 arrow polygon, kept clear of the pads."""
    keep = SILK_PAD_CLEARANCE + SILK_WIDTH / 2
    keepouts = [BBox(b.xmin - keep, b.ymin - keep, b.xmax + keep, b.ymax + keep) for b in (p.box for p in design.pads if any("Cu" in layer for layer in p.layers))]
    o = SILK_OFFSET
    b = BBox(design.body.xmin - o, design.body.ymin - o, design.body.xmax + o, design.body.ymax + o)
    corners = [(b.xmin, b.ymin), (b.xmax, b.ymin), (b.xmax, b.ymax), (b.xmin, b.ymax)]
    segments = []
    for k in range(4):
        segments += _clip_segment(corners[k], corners[(k + 1) % 4], keepouts)

    arrow = []
    pin1 = next((p for p in design.pads if p.number in ("1", "A1")), None)
    if pin1 is not None:
        box = pin1.box
        ext = _extents(design.pads)
        dx, dy = pin1.x - ext.center[0], pin1.y - ext.center[1]
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            dx = -1.0
        gap, length, half = keep + 0.01, 0.33, 0.24
        if abs(dx) >= abs(dy):  # arrow beside pad 1, pointing at it along X
            s = -1 if dx < 0 else 1
            tip = (box.xmin - gap, pin1.y) if s < 0 else (box.xmax + gap, pin1.y)
            arrow = [tip, (tip[0] + s * length, tip[1] - half), (tip[0] + s * length, tip[1] + half)]
        else:
            s = -1 if dy < 0 else 1
            tip = (pin1.x, box.ymin - gap) if s < 0 else (pin1.x, box.ymax + gap)
            arrow = [tip, (tip[0] - half, tip[1] + s * length), (tip[0] + half, tip[1] + s * length)]
    return segments, arrow


def _fab_outline(design: Design) -> list[tuple[float, float]]:
    b = design.body
    ch = min(1.0, min(b.width, b.height) / 4)
    sx, sy = design.pin1_corner
    corners = [(b.xmin, b.ymin), (b.xmax, b.ymin), (b.xmax, b.ymax), (b.xmin, b.ymax)]
    pts = []
    for x, y in corners:
        if (x == (b.xmin if sx < 0 else b.xmax)) and (y == (b.ymin if sy < 0 else b.ymax)):
            # Chamfer the pin 1 corner; keep the outline going round in the same direction.
            on_x = (x - sx * ch, y)
            on_y = (x, y - sy * ch)
            first, second = (on_y, on_x) if (sx < 0) == (sy < 0) else (on_x, on_y)
            pts += [first, second]
        else:
            pts.append((x, y))
    return pts


# --- writer ---------------------------------------------------------------------------------


def _xy(p) -> str:
    return f"{_f(p[0])} {_f(p[1])}"


def _stroke(width: float) -> str:
    return f"(stroke (width {_f(width)}) (type solid))"


def to_kicad_mod(design: Design) -> str:
    out: list[str] = []
    add = out.append
    court = courtyard_outline(design)
    cy_min = min(p[1] for loop in court for p in loop)
    cy_max = max(p[1] for loop in court for p in loop)
    smd = all(p.kind == "smd" for p in design.pads)

    add(f"(footprint {sexpr.quote(design.name)}")
    add("\t(version 20240108)")
    add('\t(generator "cadmate")')
    add('\t(generator_version "0.1")')
    add('\t(layer "F.Cu")')
    add(f"\t(descr {sexpr.quote(design.descr)})")
    if design.tags:
        add(f"\t(tags {sexpr.quote(design.tags)})")
    add(f'\t(property "Reference" "REF**" (at 0 {_f(round(cy_min - 0.7, 4))} 0) (layer "F.SilkS")\n\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t)')
    add(f'\t(property "Value" {sexpr.quote(design.name)} (at 0 {_f(round(cy_max + 0.7, 4))} 0) (layer "F.Fab")\n\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t)')
    add(f"\t(attr {'smd' if smd else 'through_hole'})")

    for loop in court:
        for k, a in enumerate(loop):
            b = loop[(k + 1) % len(loop)]
            add(f'\t(fp_line (start {_xy(a)}) (end {_xy(b)}) {_stroke(COURTYARD_WIDTH)} (layer "F.CrtYd"))')
    pts = " ".join(f"(xy {_xy(p)})" for p in _fab_outline(design))
    add(f'\t(fp_poly (pts {pts}) {_stroke(FAB_WIDTH)} (fill none) (layer "F.Fab"))')
    segments, arrow = _silk(design)
    for a, b in segments:
        add(f'\t(fp_line (start {_xy(a)}) (end {_xy(b)}) {_stroke(SILK_WIDTH)} (layer "F.SilkS"))')
    if arrow:
        pts = " ".join(f"(xy {_xy(p)})" for p in arrow)
        add(f'\t(fp_poly (pts {pts}) {_stroke(SILK_WIDTH)} (fill solid) (layer "F.SilkS"))')
    size = min(1.0, max(0.5, round(min(design.body.width, design.body.height) / 4, 2)))
    cx, cy = design.body.center
    add(f'\t(fp_text user "${{REFERENCE}}" (at {_xy((round(cx, 4), round(cy, 4)))} 0) (layer "F.Fab")\n\t\t(effects (font (size {_f(size)} {_f(size)}) (thickness {_f(round(size * 0.15, 3))})))\n\t)')

    for p in design.pads:
        at = _xy((p.x, p.y)) + (f" {_f(p.rotation)}" if p.rotation else "")
        line = f"\t(pad {sexpr.quote(p.number)} {p.kind} {p.shape} (at {at}) (size {_f(p.width)} {_f(p.height)})"
        if p.drill is not None and p.drill_y is not None:
            line += f" (drill oval {_f(p.drill)} {_f(p.drill_y)})"
        elif p.drill is not None:
            line += f" (drill {_f(p.drill)})"
        line += " (layers " + " ".join(sexpr.quote(layer) for layer in p.layers) + ")"
        if p.shape == "roundrect":
            line += f" (roundrect_rratio {_f(_rratio(p.width, p.height))})"
        add(line + ")")

    if design.model:
        add(footprint.model_block(design.model))
    add(")")
    return "\n".join(out) + "\n"

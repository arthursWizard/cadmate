"""Review an existing footprint against its 3D model, the datasheet and the symbol.

check.check_footprint covers what the footprint file alone shows (courtyard, clearances,
annular rings). review_footprint adds what needs a second source, so it also works on
footprints cadmate did not make:

- pad numbering: numbers that run backwards along a row (two pads swapped), and with the
  datasheet's pin positions, every pad number against the pin the datasheet puts there
- the 3D model, solid by solid: bodies outside the courtyard, parts that go into the board
  (a module with parts on its underside, soldered flat, needs a cutout or a standoff),
  leads that miss their hole or are too big for it
- the datasheet's lead sizes: holes too small for the lead
- castellated modules: on the host board's footprint the copper must reach past the
  module's edge, where the solder fillet forms; on the module's own board the edge must
  cut each castellated hole

Solids come from FreeCAD (model.inspect), already placed as KiCad places the model, in the
3D frame (Y up, Z = 0 the top of the board). Everything here works in KiCad's footprint
frame (mm, Y down), so the coordinates in messages are the ones the footprint editor shows.
"""

from __future__ import annotations

import collections
import math
import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import apps, check, footprint, ipc, model, symbol_check
from .footprint import BBox, Footprint, Model, Pad
from .symbol import Symbol

Z_TOLERANCE = check.Z_TOLERANCE
BOARD_THICKNESS = 1.6  # mm, the usual host board
POSITION_TOLERANCE = 0.1  # mm between a datasheet pin position and a pad: datasheets round to 0.05 or 0.1
CASTELLATED_OUTSIDE = 0.5  # mm of host copper past a castellated module's edge, for a fillet you can see and reach
EDGE_BAND = 2.0  # mm: pads whose hole lies this close to the module's edge are its castellations
CUTOUT_CLEARANCE = 0.5  # mm around underside parts in a suggested board cutout
HEADER_SPACER = 2.5  # mm, plastic spacer of a standard 2.54 mm pin header
TIGHT_HOLE = 0.05  # mm of diametral clearance under which a lead is hard to insert and solder cannot flow down the hole
LEAD_KEYS = ("lead_diameter", "lead_width", "lead_thickness")


@dataclass
class Solid:
    """A solid of the placed model, in the footprint frame: box in X/Y (Y down), z range, and where it crosses
    the board's mid-plane (None when that is not known, for parts given by hand)."""

    name: str
    box: BBox
    zmin: float
    zmax: float
    loops: list[tuple[BBox, float | None]] | None = None

    def describe(self) -> str:
        cx, cy = self.box.center
        return f"{self.name} ({self.box.width:.2f} x {self.box.height:.2f} mm at ({cx:.2f}, {cy:.2f}))"


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    measured: dict = field(default_factory=dict)
    checked: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _fp_box(b: dict) -> BBox:
    """3D frame box (Y up) to footprint frame (Y down)."""
    return BBox(b["xmin"], -b["ymax"], b["xmax"], -b["ymin"])


def solids_from_model(parts: list[dict]) -> list[Solid]:
    """The "parts" of model.inspect (placed, 3D frame) as Solids in the footprint frame."""
    out = []
    for p in parts:
        loops = [(_fp_box(lp["bbox"]), lp.get("area")) for lp in p.get("section") or []]
        out.append(Solid(p.get("name", "solid"), _fp_box(p["bbox"]), p["bbox"]["zmin"], p["bbox"]["zmax"], loops))
    return out


def solids_from_table(parts: list[dict], standoff: float = 0.0) -> list[Solid]:
    """Parts under a module as the datasheet or a photo gives them: {name?, x, y, size_x, size_y, height}, footprint
    frame, height below the module's underside; standoff is how far that underside sits above the board."""
    out = []
    for i, p in enumerate(parts):
        x, y, w, h = (float(p[k]) for k in ("x", "y", "size_x", "size_y"))
        out.append(Solid(p.get("name") or f"part {i + 1}", BBox(x - w / 2, y - h / 2, x + w / 2, y + h / 2),
                         standoff - float(p["height"]), standoff))
    return out


# --- pad numbering --------------------------------------------------------------------------------


def _monotonic(seq: list[int]) -> bool:
    return all(b > a for a, b in zip(seq, seq[1:])) or all(b < a for a, b in zip(seq, seq[1:]))


def _swap_that_fixes(seq: list[int]) -> tuple[int, int] | None:
    for i in range(len(seq)):
        for j in range(i + 1, len(seq)):
            t = list(seq)
            t[i], t[j] = t[j], t[i]
            if _monotonic(t):
                return seq[i], seq[j]
    return None


def _lines(pads: list[Pad], axis: int, tol: float = 0.05) -> list[list[Pad]]:
    """Pads of one size that share an X (axis 0: a column) or a Y (axis 1: a row), in order along it."""
    by_key: dict[tuple, list[Pad]] = {}
    for p in pads:
        size = tuple(sorted((round(p.width, 2), round(p.height, 2))))
        by_key.setdefault(size, []).append(p)
    out = []
    for group in by_key.values():
        group = sorted(group, key=lambda p: (p.x, p.y)[axis])
        line: list[Pad] = []
        for p in group:
            if line and abs((p.x, p.y)[axis] - (line[0].x, line[0].y)[axis]) > tol:
                out.append(line)
                line = []
            line.append(p)
        out.append(line)
    return [sorted(line, key=lambda p: (p.x, p.y)[1 - axis]) for line in out if len(line) >= 3]


def check_numbering_order(fp: Footprint, r: Report) -> None:
    """Along every row and column of like pads the numbers should run one way. A pair out of order is
    usually two pads swapped by hand (the datasheet decides which pin is where)."""
    pads = [p for p in fp.signal_pads() if p.number.isdigit()]
    for axis, what in ((0, "column at x"), (1, "row at y")):
        for line in _lines(pads, axis):
            seq = [int(p.number) for p in line]
            if _monotonic(seq):
                continue
            at = (line[0].x, line[0].y)[axis]
            swap = _swap_that_fixes(seq)
            if swap:
                r.warnings.append(f"pads {swap[0]} and {swap[1]} look swapped: along the {what} = {at:g} the numbers run {seq}; "
                                  "check them against the datasheet's pinout")
            else:
                r.warnings.append(f"pad numbers along the {what} = {at:g} run {seq}, not in order; fine only if the datasheet numbers them so")
    r.checked.append("pad numbering order")


def _fp_position(p: Pad) -> tuple[float, float]:
    """Where a datasheet would put the pin: the hole for through-hole pads, the copper's centre for SMD."""
    return (p.x, p.y) if p.drill else p.copper_center


def compare_pad_positions(fp: Footprint, table: list[dict], sym: Symbol | None = None, tolerance: float = POSITION_TOLERANCE) -> dict:
    """Check every pad's number against the datasheet's pin at that spot.

    table: [{number, x, y, name?}], the datasheet's pin positions (a dimension drawing or a pinout read off a
    drawing), in any origin and with Y up or down: the frame that fits best is used. With sym, and names in the
    table, a wrong number is also said in signal terms (which net would reach which pin).
    """
    fp_rows = [(p.number, _fp_position(p)) for p in fp.pads if p.number and p.kind != "np_thru_hole"]
    ds_rows = [(str(row["number"]), (float(row["x"]), float(row["y"]))) for row in table]
    fp_count = collections.Counter(n for n, _ in fp_rows)
    ds_count = collections.Counter(n for n, _ in ds_rows)
    # Numbers on several pads (a connector's shell legs, split thermal pads) are matched as a set, after the frame is fitted on the rest.
    repeated = {n for n in set(fp_count) & set(ds_count) if fp_count[n] > 1 or ds_count[n] > 1}
    pads = {n: xy for n, xy in fp_rows if n not in repeated}
    ds = {n: xy for n, xy in ds_rows if n not in repeated}
    names = {str(row["number"]): row.get("name") for row in table}
    shared = sorted(set(pads) & set(ds), key=symbol_check._sort_key)
    errors: list[str] = []
    if not shared:
        msg = "no pad number of the footprint is in the datasheet table" if not repeated else \
            "every pad number in the datasheet table is on several pads; give at least two single pins so the frame can be fitted"
        return {"ok": False, "errors": [msg], "warnings": []}

    best = None
    for flip in (1.0, -1.0):  # datasheet Y down as KiCad, or Y up
        dx = statistics.median(pads[n][0] - ds[n][0] for n in shared)
        dy = statistics.median(pads[n][1] - flip * ds[n][1] for n in shared)
        place = {n: (ds[n][0] + dx, flip * ds[n][1] + dy) for n in ds}
        hits = sum(math.dist(pads[n], place[n]) <= tolerance for n in shared)
        if best is None or hits > best[0]:
            best = (hits, flip, (dx, dy), place)
    _, flip, (dx, dy), place = best

    def pad_at(pt, among=None):
        near = [(math.dist(pt, xy), n) for n, xy in (among or fp_rows)]
        d, n = min(near)
        return n if d <= tolerance else None

    group_matched = 0
    for n in sorted(repeated, key=symbol_check._sort_key):
        at = [xy for m, xy in fp_rows if m == n]
        for x, y in (xy for m, xy in ds_rows if m == n):
            pt = (x + dx, flip * y + dy)
            if any(math.dist(pt, q) <= tolerance for q in at):
                group_matched += 1
                continue
            other = pad_at(pt)
            where = f"; pad {other} is there" if other else ""
            d = min(math.dist(pt, q) for q in at)
            errors.append(f"the datasheet puts a pin {n} where the footprint has no pad {n} (nearest is {d:.2f} mm away){where}")
        if fp_count[n] < ds_count[n]:
            errors.append(f"the datasheet has {ds_count[n]} pins numbered {n}, the footprint {fp_count[n]} pads")
    pin_names = {}
    if sym is not None:
        for pin in sym.pins:
            for n in pin.numbers:
                pin_names.setdefault(n, pin.name)
    moved, swapped, seen = [], [], set()
    for n in shared:
        if math.dist(pads[n], place[n]) <= tolerance or n in seen:
            continue
        other = pad_at(place[n])
        if other is not None and other in ds and pad_at(place[other]) == n:
            seen |= {n, other}
            swapped.append((n, other))
        else:
            moved.append((n, other, math.dist(pads[n], place[n])))
    for a, b in swapped:
        msg = f"pads {a} and {b} are swapped: the datasheet puts pin {a} where the footprint has pad {b}, and pin {b} where it has pad {a}"
        if sym is not None and names.get(a) and names.get(b):
            msg += (f"; with this symbol the board would wire its {pin_names.get(b, '?')} (pin {b}) to the part's {names[a]} "
                    f"and its {pin_names.get(a, '?')} (pin {a}) to the part's {names[b]}")
        errors.append(msg)
    for n, other, d in moved:
        where = f"; pad {other} is there" if other else ""
        errors.append(f"pad {n} is {d:.2f} mm from where the datasheet puts pin {n}{where}")
    only_ds = sorted(set(ds_count) - set(fp_count), key=symbol_check._sort_key)
    only_fp = sorted(set(fp_count) - set(ds_count), key=symbol_check._sort_key)
    if only_ds:
        errors.append(f"datasheet pins with no pad: {only_ds[:15]}")
    warnings = [f"pads not in the datasheet table: {only_fp[:15]}"] if only_fp else []
    warnings += [f"the footprint has {fp_count[n]} pads numbered {n}, the datasheet {ds_count[n]} pins; fine for extra shield or "
                 "thermal pads the drawing leaves out" for n in sorted(repeated, key=symbol_check._sort_key) if fp_count[n] > ds_count[n]]
    return {
        "ok": not errors, "errors": errors, "warnings": warnings,
        "frame": {"datasheet_y": "down" if flip > 0 else "up", "offset": [round(dx, 4), round(dy, 4)]},
        "matched": len(shared) - len(moved) - 2 * len(swapped) + group_matched,
    }


# --- holes and leads ------------------------------------------------------------------------------


def _hole_core(p: Pad) -> tuple[tuple[float, float], tuple[float, float], float]:
    """A hole as the segment its centre line runs along (a point for a round hole) and its radius."""
    if not p.drill_y or abs(p.drill_y - p.drill) < 1e-9:
        return (p.x, p.y), (p.x, p.y), p.drill / 2
    long, short = max(p.drill, p.drill_y), min(p.drill, p.drill_y)
    half = (long - short) / 2
    r = math.radians(-p.rotation)  # as Pad.copper_center
    ux, uy = (math.cos(r), math.sin(r)) if p.drill >= p.drill_y else (-math.sin(r), math.cos(r))
    return (p.x - half * ux, p.y - half * uy), (p.x + half * ux, p.y + half * uy), short / 2


def _in_hole(p: Pad, pt: tuple[float, float]) -> bool:
    a, b, radius = _hole_core(p)
    return check._point_segment(pt, a, b) <= radius + 1e-9


def _fit(p: Pad, outline: list[tuple[float, float]]) -> float:
    """Diametral clearance of a lead outline in the pad's hole; negative when the lead does not fit.

    A slot is judged per axis, as fabs and IPC-7251 size it (length against the lead's width, width against
    its thickness), not by the lead's corners in the slot's rounded ends.
    """
    a, b, radius = _hole_core(p)
    if a == b:
        return 2 * (radius - max(math.dist(pt, a) for pt in outline))
    length = math.dist(a, b)
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    along = max(abs((x - p.x) * ux + (y - p.y) * uy) for x, y in outline)
    across = max(abs(-(x - p.x) * uy + (y - p.y) * ux) for x, y in outline)
    return min(length + 2 * radius - 2 * along, 2 * radius - 2 * across)


def _lead_outline(box: BBox, area: float | None) -> list[tuple[float, float]]:
    """Points around a lead's cross-section: a circle when its area says round (pi/4 of its box), else the box."""
    cx, cy = box.center
    if area is not None and area < 0.85 * box.width * box.height:
        rad = max(box.width, box.height) / 2
        return [(cx + rad * math.cos(k * math.pi / 16), cy + rad * math.sin(k * math.pi / 16)) for k in range(32)]
    return check._box_corners(box)


def _some(items: list, limit: int = 12) -> str:
    shown = ", ".join(str(i) for i in items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _judge_fits(fits: list[tuple[Pad, float, str]], r: Report, source: str) -> None:
    allow = ipc.HOLE_ALLOWANCE
    tight: dict[tuple, list[str]] = {}
    for p, gap, lead in fits:
        hole = check._hole(p)
        r.measured.setdefault("min_hole_clearance", gap)
        r.measured["min_hole_clearance"] = round(min(r.measured["min_hole_clearance"], gap), 4)
        if gap < -1e-6:
            tight.setdefault(("error", hole, lead), []).append(p.number)
        elif gap < TIGHT_HOLE - 1e-6:
            tight.setdefault(("warning", hole, lead), []).append(p.number)
    for (level, hole, lead), numbers in tight.items():
        nums = sorted({n or "(unnumbered)" for n in numbers}, key=symbol_check._sort_key)
        if level == "error":
            r.errors.append(f"pads {_some(nums)}: the {lead} lead ({source}) does not fit the {hole} mm hole")
        else:
            r.warnings.append(f"pads {_some(nums)}: the {lead} lead ({source}) leaves under {TIGHT_HOLE} mm in the {hole} mm hole, too "
                              f"tight to insert and solder; IPC-7251 adds {allow['least']}/{allow['nominal']}/{allow['most']} mm "
                              "(least/nominal/most) to the lead")


def check_leads(fp: Footprint, leads: dict, r: Report) -> None:
    """Holes against the datasheet's leads: {lead_diameter} or {lead_width, lead_thickness} for every plated hole,
    or {pad number or "*": {...}} per pad. Values as the datasheet gives them; the maximum is what must fit."""
    per_pad = leads if not any(k in leads for k in LEAD_KEYS) else {"*": leads}
    fits, incomplete = [], []
    for p in fp.pads:
        if p.kind != "thru_hole" or not p.drill:
            continue
        spec = per_pad.get(p.number, per_pad.get("*"))
        if not spec:
            continue
        if spec.get("lead_diameter") is not None:
            d = ipc.dim(spec["lead_diameter"]).max
            box, area, label = BBox(p.x - d / 2, p.y - d / 2, p.x + d / 2, p.y + d / 2), 0.0, f"{d:g} mm round"
        elif spec.get("lead_width") is not None and spec.get("lead_thickness") is not None:
            w, t = sorted((ipc.dim(spec["lead_width"]).max, ipc.dim(spec["lead_thickness"]).max), reverse=True)
            if p.drill_y and (p.drill >= p.drill_y) == bool(round(p.rotation / 90) % 2):
                w, t = t, w  # the slot runs along Y: so does the lead's width
            box, area, label = BBox(p.x - w / 2, p.y - t / 2, p.x + w / 2, p.y + t / 2), None, f"{max(w, t):g} x {min(w, t):g} mm"
        else:
            incomplete.append(p.number or "(unnumbered)")
            continue
        fits.append((p, _fit(p, _lead_outline(box, area)), label))
    if incomplete:
        nums = sorted(set(incomplete), key=symbol_check._sort_key)
        r.skipped.append(f"holes of pads {_some(nums)} against their leads: a flat lead needs both lead_width and lead_thickness, "
                         "a round one lead_diameter")
    if fits:
        _judge_fits(fits, r, "datasheet")
        r.checked.append("holes against the datasheet's leads")


# --- the 3D model, solid by solid -----------------------------------------------------------------


def check_solids(fp: Footprint, solids: list[Solid], r: Report, cutout: bool = False, board_thickness: float = BOARD_THICKNESS,
                 model_z: float = 0.0) -> None:
    """Bodies against the courtyard, parts that go into the board, leads in their holes.

    A solid below the board's surface is a lead when it crosses the board's mid-plane only inside holes; anything
    else there (a part under a module, a body modelled too low) would hit the host board unless the board is cut
    out under it. model_z: the model's Z offset, which is the standoff when the part is raised (on headers).
    """
    holes = [p for p in fp.pads if p.drill]
    court = [e for g in fp.on_layer("F.CrtYd") for e in check._graphic_edges(g)]
    cuts = [e for g in fp.on_layer("Edge.Cuts") for e in check._graphic_edges(g)]
    into_board: list[tuple[Solid, float]] = []
    fits = []
    tails: dict[int, float] = {}  # plated hole -> lowest point of the leads in it
    for s in solids:
        below = s.zmin < -Z_TOLERANCE
        if s.loops is None or (below and not s.loops):
            # No section: a small solid centred in a hole is taken for a lead. That includes a lead or a peg
            # cut short of the board's mid-plane, where the section is taken.
            hole = next((p for p in holes if _in_hole(p, s.box.center)), None)
            if below and hole is not None and max(s.box.width, s.box.height) < 2 * max(hole.drill, hole.drill_y or 0):
                if s.loops is None:
                    # A box cannot tell a round lead from a square one: take it as round, the kinder reading.
                    fits.append((hole, _fit(hole, _lead_outline(s.box, 0.0)), f"{max(s.box.width, s.box.height):.2f} mm"))
                # else a model's lead or peg cut short of the mid-plane: no section to size it by, and its box is too coarse
                tails[id(hole)] = min(tails.get(id(hole), 0.0), s.zmin)
            elif below:
                into_board.append((s, -s.zmin))
        else:
            foreign = False
            for box, area in s.loops:
                hole = next((p for p in holes if _in_hole(p, box.center)), None)
                if hole is None:
                    foreign = True
                else:
                    label = f"{max(box.width, box.height):.2f} mm round" if area is not None and area < 0.85 * box.width * box.height \
                        else f"{box.width:.2f} x {box.height:.2f} mm"
                    fits.append((hole, _fit(hole, _lead_outline(box, area)), label))
                    tails[id(hole)] = min(tails.get(id(hole), 0.0), s.zmin)
            if below and (foreign or not s.loops):
                into_board.append((s, -s.zmin))
        if court and s.zmax > Z_TOLERANCE:
            outside = [c for c in check._box_corners(s.box) if not check._inside(c, court)]
            if outside:
                cb = fp.courtyard
                sides = {"left": cb.xmin - s.box.xmin, "right": s.box.xmax - cb.xmax, "top": cb.ymin - s.box.ymin, "bottom": s.box.ymax - cb.ymax}
                side, over = max(sides.items(), key=lambda kv: kv[1])
                over_txt = f"{over:.2f} mm past the courtyard on the {side}" if over > 0.01 else "outside the courtyard"
                r.warnings.append(f"model: {s.describe()} reaches {over_txt}; the courtyard should cover the body")
    if court:
        r.checked.append("model bodies against the courtyard")
    if fits:
        _judge_fits(fits, r, "model")
        r.checked.append("model leads in their holes")
    short = sorted({p.number or "(unnumbered)" for p in holes if p.kind == "thru_hole" and id(p) in tails
                    and tails[id(p)] > -board_thickness + Z_TOLERANCE}, key=symbol_check._sort_key)
    if short:
        stop = max(-tails[id(p)] for p in holes if id(p) in tails and (p.number or "(unnumbered)") in short)
        r.warnings.append(f"model: the leads in pads {_some(short, 24)} stop {stop:.2f} mm below the board's top, inside a "
                          f"{board_thickness:g} mm board; a through-hole lead should come out of the underside so the joint "
                          "can be inspected (IPC-A-610). Check the datasheet's tail length, or the board_thickness given")
    if tails:
        r.checked.append("through-hole leads coming out of the board")
    r.checked.append("model parts going into the board")

    if not into_board:
        return
    r.measured["into_board"] = [{"solid": s.name, "depth": round(d, 4), "box": [round(v, 4) for v in (s.box.xmin, s.box.ymin, s.box.xmax, s.box.ymax)]}
                                for s, d in into_board]
    uncovered = [(s, d) for s, d in into_board if not (cuts and all(check._inside(c, cuts) for c in check._box_corners(s.box)))]
    for s, d, small in _group([sd for sd in into_board if sd not in uncovered]):
        r.warnings.append(f"model: {s.describe()}{_beside(small)} goes {d:.2f} mm into the board, inside the footprint's Edge.Cuts cutout")
    if not uncovered:
        return
    depth = max(d for _, d in uncovered)
    u = BBox(min(s.box.xmin for s, _ in uncovered) - CUTOUT_CLEARANCE, min(s.box.ymin for s, _ in uncovered) - CUTOUT_CLEARANCE,
             max(s.box.xmax for s, _ in uncovered) + CUTOUT_CLEARANCE, max(s.box.ymax for s, _ in uncovered) + CUTOUT_CLEARANCE)
    r.measured["suggested_cutout"] = [round(v, 2) for v in (u.xmin, u.ymin, u.xmax, u.ymax)]
    groups = _group(uncovered)
    what = "; ".join(f"{s.describe()}{_beside(small)} goes {d:.2f} mm{' through' if d > board_thickness else ''} into the board"
                     for s, d, small in groups[:6])
    if len(groups) > 6:
        what += f"; and {len(groups) - 6} more parts"
    if cutout:
        r.warnings.append(f"model: {what}; you said the board is cut out there, so check the cutout covers "
                          f"({u.xmin:.2f}, {u.ymin:.2f}) to ({u.xmax:.2f}, {u.ymax:.2f})")
        return
    them, they = ("it", "it") if len(groups) == 1 else ("them", "they")
    box = f"({u.xmin:.2f}, {u.ymin:.2f}) to ({u.xmax:.2f}, {u.ymax:.2f}), {CUTOUT_CLEARANCE} mm clear of the parts"
    cut = f"make the footprint's Edge.Cuts cutout cover {them} ({box})" if cuts else \
        f"cut the board out under {them} (an Edge.Cuts rectangle in the footprint from {box})"
    raise_ = f"raise the part at least {depth:.2f} mm"
    if holes and HEADER_SPACER - model_z > depth:
        raise_ += f" (on pin headers: model Z offset {HEADER_SPACER} mm, the spacer of a 2.54 mm header)"
    r.errors.append(f"model: {what}, so {they} would hit the host board. Either {cut}, or {raise_}")


SMALL_SOLID = 1.0  # mm: a solid this small beside a bigger one is taken for one of its leads
BESIDE = 0.5  # mm


def _group(items: list[tuple[Solid, float]]) -> list[tuple[Solid, float, int]]:
    """Fold small solids (leads, terminals) into the bigger solid they sit beside, so a part under a module is
    reported once rather than once per lead. Returns (solid, deepest depth, small solids folded in), biggest first."""
    order = sorted(items, key=lambda sd: -sd[0].box.width * sd[0].box.height)
    groups: list[list] = []
    for s, d in order:
        if max(s.box.width, s.box.height) < SMALL_SOLID:
            host = next((g for g in groups if max(g[0].box.width, g[0].box.height) >= SMALL_SOLID
                         and check._box_gap(g[0].box, s.box) <= BESIDE), None)
            if host is not None:
                host[1], host[2] = max(host[1], d), host[2] + 1
                continue
        groups.append([s, d, 0])
    return [tuple(g) for g in groups]


def _beside(small: int) -> str:
    return f" with {small} small solid{'s' if small > 1 else ''} beside it (its leads)" if small else ""


# --- castellated modules --------------------------------------------------------------------------


def module_outline(solids: list[Solid]) -> BBox | None:
    """A module's own board in its model: the solid with the largest area that is board-thin and sits on or above the host."""
    boards = [s for s in solids if s.zmin >= -Z_TOLERANCE and s.zmax - s.zmin <= 2.0]
    if not boards:
        return None
    return max(boards, key=lambda s: s.box.width * s.box.height).box


def check_castellated(fp: Footprint, outline: BBox | None, r: Report, castellated: bool | None = None) -> None:
    """Castellated pads against the module's edge.

    On the module's own board (pads KiCad marks castellated) the edge must cut each hole about in half. On the host
    board's footprint for such a module (castellated=True, or auto when through-hole pads have their copper moved
    off the hole, as cadmate makes them) each edge pad's copper must reach past the module's edge: the solder
    fillet forms there, where it can be seen and reworked; a pad that stops under the module joins only by the
    castellation's inside, if at all.
    """
    own = [p for p in fp.pads if p.castellated]
    shifted = [p for p in fp.pads if p.kind == "thru_hole" and p.offset != (0.0, 0.0)]
    host = castellated is True or (castellated is None and not own and len(shifted) >= 3)
    if not own and not host:
        return
    cuts = [e for g in fp.on_layer("Edge.Cuts") for e in check._graphic_edges(g)]
    if own:
        if not cuts and outline is None:
            r.skipped.append("castellated holes on the board edge: give body (the module's outline) or draw Edge.Cuts in the footprint")
        else:
            edges = cuts or check._edges(check._box_corners(outline), True)
            off = []
            for p in own:
                d = min(check._point_segment((p.x, p.y), a, b) for a, b in edges)
                if p.drill and d > p.drill / 4:
                    off.append((p.number, d))
            if off:
                r.warnings.append(f"castellated pads whose hole the board edge does not cut in half: "
                                  f"{[f'{n} ({d:.2f} mm off)' for n, d in off[:10]]}")
            r.checked.append("castellated holes on the board edge")
    if not host:
        return
    if outline is None:
        r.skipped.append("castellated pads reaching past the module's edge: give body (the module's outline), or link a model")
        return
    short, worst = [], None
    for p in fp.pads:
        if not check._copper(p) or p.kind == "np_thru_hole":
            continue
        cx, cy = (p.x, p.y)
        dist = {"left": cx - outline.xmin, "right": outline.xmax - cx, "top": cy - outline.ymin, "bottom": outline.ymax - cy}
        side, d = min(dist.items(), key=lambda kv: abs(kv[1]))
        if abs(d) > EDGE_BAND:
            continue
        b = p.box
        out = {"left": outline.xmin - b.xmin, "right": b.xmax - outline.xmax, "top": outline.ymin - b.ymin, "bottom": b.ymax - outline.ymax}[side]
        worst = out if worst is None else min(worst, out)
        if out < CASTELLATED_OUTSIDE - 1e-6:
            short.append((p.number, out))
    if worst is None:
        r.skipped.append("castellated pads reaching past the module's edge: no pad lies near the module's outline")
        return
    r.measured["castellated_copper_outside"] = round(worst, 4)
    inside = [f"{n} ({-o:.2f} mm inside)" for n, o in short if o < 0]
    near = [f"{n} ({o:.2f} mm)" for n, o in short if o >= 0]
    if inside:
        r.errors.append(f"castellated pads that stop short of the module's edge: {inside[:10]}; the castellation has no copper "
                        f"outside to solder to, so extend them about 1 mm past the edge")
    if near:
        r.warnings.append(f"castellated pads only just past the module's edge: {near[:10]}; {CASTELLATED_OUTSIDE} mm or more "
                          f"(about 1 mm) gives a fillet you can see and rework")
    r.checked.append("castellated pads reaching past the module's edge")


# --- all together ---------------------------------------------------------------------------------


def review_footprint(fp: Footprint, solids: list[Solid] | None = None, datasheet_pads: list[dict] | None = None,
                     leads: dict | None = None, body: dict | None = None, castellated: bool | None = None,
                     cutout: bool = False, board_thickness: float = BOARD_THICKNESS, sym: Symbol | None = None,
                     courtyard_clearance: float = check.COURTYARD_CLEARANCE, model_z: float = 0.0, pins_vs_pads: bool = True) -> dict:
    """Review a footprint with whatever second sources there are; see the review_footprint tool for the arguments."""
    base = check.check_footprint(fp, courtyard_clearance=courtyard_clearance)
    r = Report(list(base["errors"]), list(base["warnings"]), dict(base["measured"]))
    r.checked.append("footprint rules (courtyard, pad gaps, annular rings, silkscreen)")

    check_numbering_order(fp, r)
    if datasheet_pads:
        cmp = compare_pad_positions(fp, datasheet_pads, sym)
        r.errors += cmp["errors"]
        r.warnings += cmp["warnings"]
        r.measured["vs_datasheet_pins"] = {k: cmp[k] for k in ("frame", "matched") if k in cmp}
        r.checked.append("pad numbers against the datasheet's pin positions")
    else:
        r.skipped.append("pad numbers against the datasheet: give datasheet_pads (pin numbers and positions)")
    if sym is not None and pins_vs_pads:
        vs = symbol_check.compare_with_footprint(sym, fp)
        r.errors += vs["errors"]
        r.checked.append("symbol pins against pads")

    if leads:
        check_leads(fp, leads, r)
    elif any(p.kind == "thru_hole" for p in fp.pads) and not solids:
        r.skipped.append("holes against the leads: give leads (lead_diameter, or lead_width and lead_thickness) or a model with leads")

    if solids:
        check_solids(fp, solids, r, cutout, board_thickness, model_z)
    else:
        r.skipped.append("model checks (bodies in the courtyard, parts going into the board, leads in holes): no model was read")

    outline = None
    if body:
        outline = BBox(*(float(body[k]) for k in ("xmin", "ymin", "xmax", "ymax")))
        r.measured["module_outline"] = {"source": "body", "box": [outline.xmin, outline.ymin, outline.xmax, outline.ymax]}
    elif solids and (castellated or any(p.castellated for p in fp.pads) or
                     (castellated is None and sum(p.kind == "thru_hole" and p.offset != (0.0, 0.0) for p in fp.pads) >= 3)):
        outline = module_outline(solids)
        if outline is not None:
            r.measured["module_outline"] = {"source": "model", "box": [round(v, 4) for v in (outline.xmin, outline.ymin, outline.xmax, outline.ymax)]}
    check_castellated(fp, outline, r, castellated)

    return {"ok": not r.errors, "errors": r.errors, "warnings": r.warnings, "measured": r.measured, "checked": r.checked, "skipped": r.skipped}


def review_file(path: str | Path, model_path: str | None = None, expected: dict | None = None, cutout: bool = False,
                underside_parts: list[dict] | None = None, standoff: float = 0.0, board_thickness: float = BOARD_THICKNESS,
                project_dir: Path | None = None, **options) -> dict:
    """Review a footprint file with its linked model (or model_path, placed as the footprint places its model).

    The model is read in FreeCAD when it is installed; underside_parts (see solids_from_table) stand in for a
    model, or add parts it lacks. options go to review_footprint. Model findings start with "model: ".
    """
    fp_path = Path(path).expanduser()
    fp = footprint.read(fp_path)
    placement = fp.models[0] if fp.models else Model(path="")
    if model_path:
        placement = replace(placement, path=model_path)
    errors: list[str] = []
    warnings: list[str] = []
    skipped: list[str] = []
    solids: list[Solid] = []
    out: dict = {}
    if not placement.path:
        warnings.append("model: the footprint links no 3D model")
    else:
        out["model_link"] = placement.path
        try:
            model_file = model.resolve(placement.path, footprint_dir=fp_path.parent, project_dir=project_dir)
        except ValueError as e:  # a KiCad path variable this process does not have; KiCad itself may
            warnings.append(f"model: {e}")
        else:
            out["model_file"] = str(model_file)
            found, problem = model._step_for(model_file)
            if found is None and not (model_file.exists() or model_file.with_suffix(".step").exists()):
                errors.append(f"model: {model_file} not found; KiCad will show the board without this part")
            elif found is None:
                errors.append(f"model: {problem}")
            elif not apps.find_freecadcmd():
                warnings.append("model: FreeCAD not found, so the model was not read; its solids were not checked")
            else:
                mr = model.check_file(fp, found, placement, expected, cutout, by_solid=True, board_thickness=board_thickness)
                errors += [f"model: {e}" for e in mr["errors"]]
                warnings += [f"model: {w}" for w in mr["warnings"]]
                out["model_check"] = mr.get("measured")
                out["model_solids"] = mr.get("solids")
                solids = solids_from_model(mr.get("parts", []))
    if underside_parts:
        solids += solids_from_table(underside_parts, standoff)
    r = review_footprint(fp, solids or None, cutout=cutout, board_thickness=board_thickness, model_z=placement.offset[2], **options)
    errors += r["errors"]
    warnings += r["warnings"]
    return {"ok": not errors, "errors": errors, "warnings": warnings, "measured": r["measured"], "checked": r["checked"],
            "skipped": skipped + r["skipped"]} | out

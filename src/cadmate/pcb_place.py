"""Placement help: put each bypass cap at its IC power pin and each crystal (with its load caps) at its MCU.

propose() reads the critical-net classification and returns moves (ref, from, to, why); nothing changes on the board.
apply() makes the moves the user agreed to, on the open board as one undo step or into a saved file.
"""

from __future__ import annotations

import math
from itertools import product
from pathlib import Path

from . import pcb_critical, pcb_flow, sexpr
from .pcb_board import Board, Pad, Part, Point, _rotate, dist

GAP = 0.15  # mm kept between courtyards
STEP = 0.25  # mm search step
SLIDE = 8.0  # mm a part may slide along the IC edge from its ideal spot
OUT = 6.0  # mm it may be pushed further out from the edge

Box = tuple[float, float, float, float]


def _box(part: Part) -> Box:
    pts = part.courtyard or [c for p in part.pads for c in ((p.x - p.w / 2 - 0.25, p.y - p.h / 2 - 0.25), (p.x + p.w / 2 + 0.25, p.y + p.h / 2 + 0.25))]
    if not pts:
        return part.x, part.y, part.x, part.y
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _hits(a: Box, b: Box, gap: float = GAP) -> bool:
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def _turn(vec: Point, want: Point) -> int:
    """The quarter turn (degrees, KiCad's sense) that points vec most nearly along want."""
    return max((0, 90, 180, 270), key=lambda t: (_dot(_rotate(*vec, t), want), -t))


def _dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _side(box: Box, p: Point, pad: Pad | None = None) -> Point:
    """The outward direction of the IC edge a pin is on: along a long pad's axis when it has one (gull-wing and QFN pins
    point out of the part), otherwise toward the nearest edge, scaled by the part's size."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = p[0] - cx, p[1] - cy
    if pad is not None and max(pad.w, pad.h) > 1.3 * min(pad.w, pad.h):
        ax = _rotate(1.0 if pad.w >= pad.h else 0.0, 0.0 if pad.w >= pad.h else 1.0, pad.angle)
        if abs(ax[0]) > abs(ax[1]):
            return (1.0, 0.0) if dx >= 0 else (-1.0, 0.0)
        return (0.0, 1.0) if dy >= 0 else (0.0, -1.0)
    if abs(dx) / max(x1 - x0, 1e-6) >= abs(dy) / max(y1 - y0, 1e-6):
        return (1.0, 0.0) if dx >= 0 else (-1.0, 0.0)
    return (0.0, 1.0) if dy >= 0 else (0.0, -1.0)


def _edge(box: Box, d: Point) -> float:
    return {(-1.0, 0.0): box[0], (1.0, 0.0): box[2], (0.0, -1.0): box[1], (0.0, 1.0): box[3]}[d]


def _r(v: float) -> float:
    return round(v, 4) + 0.0


class _Plan:
    def __init__(self, b: Board, movers: set[str]):
        self.b = b
        self.outline = b.bbox()
        self.taken: list[tuple[str, Box]] = [(p.ref, _box(p)) for p in b.parts if p.ref not in movers]
        self.at: dict[str, tuple[float, float, float, Box]] = {}  # ref -> new x, y, rotation, box

    def where(self, part: Part, pad: Pad) -> Point:
        """A pad's position with its part where the plan has put it."""
        if part.ref not in self.at:
            return pad.x, pad.y
        x, y, rot, _ = self.at[part.ref]
        dx, dy = _rotate(pad.x - part.x, pad.y - part.y, (rot - part.rotation) % 360)
        return x + dx, y + dy

    def box(self, part: Part) -> Box:
        return self.at[part.ref][3] if part.ref in self.at else _box(part)

    def fit(self, part: Part, turn: int, point: Point, line_up: Point, d: Point, edge: float) -> tuple[float, float, Box] | None:
        """Put part (turned by turn) just outside edge in direction d, with point (on the part) level with line_up.

        Slides along the edge, then outward, to the nearest spot clear of every other courtyard and inside the outline."""
        cx, cy = part.x, part.y
        b0 = _box(part)
        rel = [_rotate(x - cx, y - cy, turn) for x, y in ((b0[0], b0[1]), (b0[2], b0[1]), (b0[0], b0[3]), (b0[2], b0[3]))]
        ex0, ex1 = min(p[0] for p in rel), max(p[0] for p in rel)
        ey0, ey1 = min(p[1] for p in rel), max(p[1] for p in rel)
        px, py = _rotate(point[0] - cx, point[1] - cy, turn)
        if d[0]:
            x = edge + GAP - ex0 if d[0] > 0 else edge - GAP - ex1
            y = line_up[1] - py
        else:
            y = edge + GAP - ey0 if d[1] > 0 else edge - GAP - ey1
            x = line_up[0] - px
        perp = (-d[1], d[0])
        n_s, n_o = int(SLIDE / STEP), int(OUT / STEP)
        steps = sorted(product(range(-n_s, n_s + 1), range(n_o + 1)), key=lambda t: (math.hypot(t[0], t[1] * 1.5), t[1], abs(t[0]), t[0]))
        for s, o in steps:
            nx = x + (s * perp[0] + o * d[0]) * STEP
            ny = y + (s * perp[1] + o * d[1]) * STEP
            box = (nx + ex0, ny + ey0, nx + ex1, ny + ey1)
            if self.outline and not (box[0] >= self.outline[0] and box[1] >= self.outline[1] and box[2] <= self.outline[2] and box[3] <= self.outline[3]):
                continue
            if any(_hits(box, t) for r, t in self.taken if r != part.ref):
                continue
            return nx, ny, box
        return None

    def put(self, part: Part, turn: int, spot: tuple[float, float, Box]) -> None:
        x, y, box = spot
        self.at[part.ref] = (x, y, (part.rotation + turn) % 360, box)
        self.taken.append((part.ref, box))


def _pad(b: Board, key: str) -> Pad | None:
    ref, _, num = key.rpartition(".")
    part = b.part(ref)
    return next((p for p in part.copper_pads if p.number == num), None) if part else None


def _move(part: Part, plan: _Plan, item: str, why: str, **extra) -> dict:
    x, y, rot, _ = plan.at[part.ref]
    return {"ref": part.ref, "from": {"x": _r(part.x), "y": _r(part.y), "rotation": _r(part.rotation)},
            "to": {"x": _r(x), "y": _r(y), "rotation": _r(rot)}, "for": item, "why": why} | extra


def propose(b: Board, roles: dict | None = None, limits: dict | None = None, everything: bool = False) -> dict:
    """Moves that put bypass caps at their pins and crystals at their MCU pins. Parts already within the limits stay,
    unless everything is set."""
    plan_items = pcb_critical.classify(b, roles, limits=limits, only=["decap", "crystal"])
    lim = plan_items["limits"]
    decaps, crystals, kept, cannot, lonely = [], [], [], [], []
    for it in plan_items["items"]:
        if it["class"] == "decap" and it["connections"]:
            pp, pin = _pad(b, it["connections"][0]["a"]["pad"]), _pad(b, it["connections"][0]["b"]["pad"])
            if pp and pin:
                if everything or dist((pp.x, pp.y), (pin.x, pin.y)) > lim["decap_distance"]:
                    decaps.append((it, pp, pin))
                else:
                    kept.append({"for": it["id"], "why": f"{pp.ref} is already {dist((pp.x, pp.y), (pin.x, pin.y)):.1f} mm from {pin.name}"})
        elif it["class"] == "decap":
            lonely.append(it)
        elif it["class"] == "crystal":
            crystals.append(it)

    # a power pin with no cap within the classifier's reach gets the nearest free small cap on its net, however far
    ctx = pcb_critical.Ctx(b, pcb_critical.infer_roles(b, roles), {}, lim)
    used = {pp.ref for _, pp, _ in decaps} | {k["for"] for k in kept} | {r for it in crystals for r in it["refs"]}
    used |= {it["refs"][0] for it in plan_items["items"] if it["class"] == "decap" and it["connections"]}
    free = [(c, pp) for c, pp, _, f in ctx.caps() if ctx.role(pp.net) == "power" and (f is None or f <= 1.01e-6) and c.ref not in used]
    for it in lonely:
        pin = _pad(b, it["id"].split(":", 1)[1])
        pick = min((t for t in free if pin and t[1].net == pin.net), key=lambda t: dist((t[1].x, t[1].y), (pin.x, pin.y)), default=None)
        if pick is None:
            cannot.append({"for": it["id"], "why": it["title"] + "; add a cap to the schematic first"})
            continue
        free.remove(pick)
        decaps.append((it, pick[1], pin))

    xtal_jobs = []
    for it in crystals:
        x = b.part(it["id"].split(":", 1)[1])
        pins = [(_pad(b, c["a"]["pad"]), _pad(b, c["b"]["pad"])) for c in it["connections"]
                if c["a"]["pad"].startswith(x.ref + ".") and "pad" in c["b"] and not c["b"]["pad"].startswith(x.ref + ".")]
        loads = [(_pad(b, c["a"]["pad"]), _pad(b, c["b"]["pad"])) for c in it["connections"]
                 if "pad" in c["b"] and c["b"]["pad"].startswith(x.ref + ".") and not c["a"]["pad"].startswith(x.ref + ".")]
        if len(pins) != 2 or pins[0][1].ref != pins[1][1].ref:
            cannot.append({"for": it["id"], "why": f"{x.ref} doesn't have two pads going to one IC; place it by hand"})
            continue
        far = max(dist((a.x, a.y), (p.x, p.y)) for a, p in pins)
        move_x = everything or far > lim["crystal_distance"]
        move_c = [(cp, xp) for cp, xp in loads if move_x or everything or dist((cp.x, cp.y), (xp.x, xp.y)) > lim["decap_distance"]]
        if not move_x and not move_c:
            kept.append({"for": it["id"], "why": f"{x.ref} is {far:.1f} mm from {pins[0][1].ref} and its load caps are next to it"})
            continue
        xtal_jobs.append((it, x, pins, move_x, move_c))

    movers = {pp.ref for _, pp, _ in decaps} | {x.ref for _, x, _, mx, _ in xtal_jobs if mx} | \
             {cp.ref for *_, mc in xtal_jobs for cp, _ in mc}
    plan = _Plan(b, movers)
    moves = []

    for it, pp, pin in decaps:
        cap, ic = b.part(pp.ref), b.part(pin.ref)
        if cap.ref in plan.at:
            continue
        icbox = plan.box(ic)
        d = _side(icbox, (pin.x, pin.y), pin)
        turn = _turn((pp.x - cap.x, pp.y - cap.y), (-d[0], -d[1]))
        spot = plan.fit(cap, turn, (pp.x, pp.y), (pin.x, pin.y), d, _edge(icbox, d))
        if spot is None:
            cannot.append({"for": it["id"], "why": f"no free spot for {cap.ref} next to {pin.name}; make room around {ic.ref}"})
            continue
        plan.put(cap, turn, spot)
        after = dist(plan.where(cap, pp), (pin.x, pin.y))
        moves.append(_move(cap, plan, it["id"], f"bypass cap for {pin.name} ({pin.net}): power pad toward the pin, ground pad out",
                           distance_mm={"before": _r(dist((pp.x, pp.y), (pin.x, pin.y))), "after": _r(after)}))

    for it, x, pins, move_x, move_c in xtal_jobs:
        (a1, p1), (a2, p2) = pins
        ic = b.part(p1.ref)
        if move_x:
            icbox = plan.box(ic)
            mid = ((p1.x + p2.x) / 2, (p1.y + p2.y) / 2)
            d = _side(icbox, mid, p1)
            turn = _turn((a2.x - a1.x, a2.y - a1.y), (p2.x - p1.x, p2.y - p1.y))
            spot = plan.fit(x, turn, ((a1.x + a2.x) / 2, (a1.y + a2.y) / 2), mid, d, _edge(icbox, d))
            if spot is None:
                cannot.append({"for": it["id"], "why": f"no free spot for {x.ref} next to {ic.ref}; make room around it"})
                continue
            plan.put(x, turn, spot)
            before = max(dist((a.x, a.y), (p.x, p.y)) for a, p in pins)
            after = max(dist(plan.where(x, a), (p.x, p.y)) for a, p in pins)
            moves.append(_move(x, plan, it["id"], f"crystal at {ic.ref}'s {p1.pinfunction or p1.number}/{p2.pinfunction or p2.number} pins",
                               distance_mm={"before": _r(before), "after": _r(after)}))
        else:
            d = _side(plan.box(ic), ((p1.x + p2.x) / 2, (p1.y + p2.y) / 2), p1)
        for cp, xp in move_c:
            c = b.part(cp.ref)
            if c.ref in plan.at:
                continue
            xbox = plan.box(x)
            target = plan.where(x, xp)
            turn = _turn((cp.x - c.x, cp.y - c.y), (-d[0], -d[1]))
            spot = plan.fit(c, turn, (cp.x, cp.y), target, d, _edge(xbox, d))
            if spot is None:
                cannot.append({"for": it["id"], "why": f"no free spot for load cap {c.ref} next to {x.ref}"})
                continue
            plan.put(c, turn, spot)
            moves.append(_move(c, plan, it["id"], f"load cap beside {x.ref} pad {xp.number}, ground pad away from the crystal",
                               distance_mm={"before": _r(dist((cp.x, cp.y), (xp.x, xp.y))), "after": _r(dist(plan.where(c, cp), target))}))

    return {"ok": True, "board": b.name, "moves": moves, "kept": kept, "cannot": cannot,
            "table": _table(moves),
            "next": ("Show the user the moves (table) and ask before applying anything. Then apply_placement with the moves "
                     "they accept, unchanged or edited. Run classify_critical_nets again after, before routing.")
            if moves else "nothing to move"}


def _table(moves: list[dict]) -> str:
    if not moves:
        return ""
    rows = ["| part | from (x, y, rot) | to (x, y, rot) | pad to pin, mm | why |", "|---|---|---|---|---|"]
    for m in moves:
        f, t, d = m["from"], m["to"], m.get("distance_mm", {})
        rows.append(f"| {m['ref']} | {f['x']:g}, {f['y']:g}, {f['rotation']:g}° | {t['x']:g}, {t['y']:g}, {t['rotation']:g}° | "
                    f"{d.get('before', 0):.1f} → {d.get('after', 0):.1f} | {m['why']} |")
    return "\n".join(rows)


# --- applying -------------------------------------------------------------------------------------------


def _num(v: float) -> str:
    s = f"{round(v, 6):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _at(node: sexpr.Node, x: float, y: float, angle: float) -> str:
    rest = [a for a in node.atoms()[2:] if not _is_float(a)]
    a = angle % 360
    vals = [_num(x), _num(y)] + ([_num(a)] if abs(a) > 1e-9 and abs(a - 360) > 1e-9 else [])
    return "(at " + " ".join(vals + rest) + ")"


def _is_float(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return True


def move_text(text: str, moves: dict[str, tuple[float, float, float]]) -> str:
    """The board text with each footprint in moves at its new x, y and rotation. Pads and texts turn with it: KiCad
    stores their angle including the footprint's rotation."""
    root = sexpr.parse(text)
    edits: list[tuple[int, int, str]] = []
    for fp in root.children("footprint"):
        ref = next((p.atoms()[1] for p in fp.children("property") if p.atoms()[:1] == ["Reference"] and len(p.atoms()) > 1), None)
        if ref is None:
            t = next((t for t in fp.children("fp_text") if t.atoms()[:1] == ["reference"]), None)
            ref = t.atoms()[1] if t is not None and len(t.atoms()) > 1 else None
        if ref not in moves:
            continue
        at = fp.child("at")
        old = at.floats() if at is not None else [0.0, 0.0]
        old_rot = old[2] if len(old) > 2 else 0.0
        x, y, rot = moves[ref]
        delta = rot - old_rot
        if at is not None:
            edits.append((at.start, at.end, _at(at, x, y, rot)))
        else:
            i = text.index("\n", fp.start) if "\n" in text[fp.start:fp.end] else fp.end - 1
            edits.append((i, i, f" (at {_num(x)} {_num(y)}{' ' + _num(rot % 360) if rot % 360 else ''})"))
        if not delta % 360:
            continue
        for child in fp.items:
            if isinstance(child, sexpr.Node) and child.head in ("pad", "property", "fp_text"):
                a = child.child("at")
                if a is not None:
                    f = a.floats()
                    edits.append((a.start, a.end, _at(a, f[0] if f else 0.0, f[1] if len(f) > 1 else 0.0, (f[2] if len(f) > 2 else 0.0) + delta)))
    for s, e, new in sorted(edits, reverse=True):
        text = text[:s] + new + text[e:]
    return text


def _clean(moves: list[dict]) -> dict[str, dict]:
    out = {}
    for m in moves:
        to = m.get("to", m)
        if "ref" not in m or not all(k in to for k in ("x", "y")):
            raise ValueError(f"each move needs ref and to {{x, y, rotation}}: {m!r}")
        out[m["ref"]] = {"x": float(to["x"]), "y": float(to["y"]), "rotation": float(to["rotation"]) if to.get("rotation") is not None else None,
                         "from": m.get("from")}
    return out


def _stale(b: Board, want: dict[str, dict]) -> tuple[list[str], list[str]]:
    missing, moved = [], []
    for ref, m in want.items():
        p = b.part(ref)
        if p is None:
            missing.append(ref)
        elif m["from"] and (dist((p.x, p.y), (m["from"]["x"], m["from"]["y"])) > 0.01
                            or abs(((p.rotation - m["from"].get("rotation", p.rotation)) + 180) % 360 - 180) > 0.01):
            moved.append(ref)
    return missing, moved


def overlaps(b: Board, refs: set[str]) -> list[list[str]]:
    """Pairs of courtyards that overlap where at least one part is in refs."""
    boxes = [(p.ref, _box(p)) for p in b.parts if p.courtyard]
    return sorted({tuple(sorted((r1, r2))) for i, (r1, a) in enumerate(boxes) for r2, c in boxes[i + 1:]
                   if (r1 in refs or r2 in refs) and _hits(a, c, -0.001)})


def apply(path: str | None, moves: list[dict], out_path: str | None = None, in_place: bool | None = None) -> dict:
    """Make the given moves, on the open board (one undo step) or into a saved board."""
    if not moves:
        raise pcb_flow.FlowError("no moves given; propose_placement lists them")
    want = _clean(moves)
    b, live = pcb_flow.load(path)
    missing, moved = _stale(b, want)
    if missing:
        raise pcb_flow.FlowError(f"no part {', '.join(missing)} on this board")
    if moved:
        raise pcb_flow.FlowError(f"{', '.join(moved)} moved since the proposal (from doesn't match); propose_placement again")
    final = {ref: (m["x"], m["y"], m["rotation"] if m["rotation"] is not None else b.part(ref).rotation) for ref, m in want.items()}
    out: dict = {"ok": True, "moved": sorted(final)}
    if live is not None:
        from . import kicad_pcb_live

        def run(kicad, board):
            place = kicad_pcb_live._namespace(kicad, board)["place"]
            for ref, (x, y, rot) in final.items():
                place(ref, x, y, rot)
            return len(final)

        r = kicad_pcb_live.commit(f"place {len(final)} parts", run)
        if not r.get("ok"):
            raise pcb_flow.FlowError(r.get("error", "the edit failed"))
        out["written"] = f"the open board, as one undo step ('cadmate: place {len(final)} parts'); not saved yet"
        after, _ = pcb_flow.load(None)
    else:
        src = Path(path).expanduser()
        dst, snap = pcb_flow.target(path, out_path, "placement", in_place)
        text = move_text(src.read_text(encoding="utf-8"), final)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8", newline="\n")
        pro = src.with_suffix(".kicad_pro")
        if pro.exists() and dst.with_suffix(".kicad_pro") != pro:
            dst.with_suffix(".kicad_pro").write_text(pro.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        out["written"] = str(dst)
        if snap:
            out["snapshot"] = snap
        from .pcb_board import parse

        after = parse(text, dst.name)
    out["courtyard_overlaps"] = overlaps(after, set(final))
    out["next"] = ("classify_critical_nets again: the decap and crystal items should now be 'route', then route_critical_nets"
                   + ("; fix the courtyard overlaps first" if out["courtyard_overlaps"] else ""))
    return out


def propose_path(path: str | None = None, roles: dict | None = None, limits: dict | None = None, everything: bool = False) -> dict:
    b, live = pcb_flow.load(path)
    out = propose(b, roles, limits, everything)
    out["source"] = "the open PCB editor, unsaved edits included" if live is not None else str(Path(path).expanduser())
    return out

"""Automatic placement for perfboard designs, steered by rough hints.

Each part in the design may carry:
- "at" and "fixed": true: it stays exactly there (rotation too).
- "at" alone: a starting point, and it's kept near there.
- "near": "C5": keep it close to that hole.
- "region": "left", "right", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right", "center" or
  "edge": keep it in that part of the board ("edge": its pins on the outer ring of holes, for connectors).
- "keep_rotation": true: don't turn it (its "rotation" stays).
Anything else is free. The placer starts greedy (most-connected part first, each where it adds the least wire) and
then anneals: parts are nudged, moved, turned and swapped, and a change is kept when it shortens the nets, keeps the
hints and leaves room round the pins, or now and then when it doesn't, so the placement can get out of a corner.
The cost counts what makes a perfboard easy to wire: short nets (half-perimeter of each net's pins), no two pins of
different nets side by side (no room for a track between), and the hints. Placements are then routed, and the one
that routes best is kept.
"""

from __future__ import annotations

import math
import random

from . import protoboard as pb

REGIONS = ("left", "right", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right", "center", "edge")
HINT_WEIGHT = 3.0
CROWD_SIDE = 3.0  # two pins of different nets side by side
CROWD_DIAG = 0.8  # diagonal neighbours
CROWD_NEAR = 0.6  # two holes apart: room for one track, not two
EDGE = 0.4  # a pin on the outer ring of holes can only be reached from three sides


class _State:
    def __init__(self, d: pb.Design, spec: dict):
        self.d = d
        self.spec = spec  # ref -> {"fixed", "near", "region", "keep_rotation"}
        self.net_of = d.net_of_pin()
        self.nets = {n: rps for n, rps in d.nets.items() if len(rps) >= 2}
        self.part_nets: dict[str, set] = {}
        for n, rps in self.nets.items():
            for r, _ in rps:
                self.part_nets.setdefault(r, set()).add(n)

        self._offsets = {r: (dict(p.d.pins), pb.covered_offsets(p.d)) for r, p in d.parts.items()}
        self._cache: dict = {}

    def footprint(self, ref, at, rot):
        """Pin holes and covered holes of a part at a pose (cached)."""
        key = (ref, at, rot)
        got = self._cache.get(key)
        if got is None:
            pins, cover = self._offsets[ref]

            def hole(o):
                x, y = pb.rotate(o, rot)
                return at[0] + round(x), at[1] + round(y)

            got = ({k: hole(o) for k, o in pins.items()}, {hole(o) for o in cover})
            if len(self._cache) > 200000:
                self._cache.clear()
            self._cache[key] = got
        return got

    def legal(self, ref, at, rot, occ_pins, occ_cover) -> bool:
        pins, cover = self.footprint(ref, at, rot)
        for h in pins.values():
            if not self.d.on_board(h) or h in occ_pins or h in occ_cover:
                return False
        for h in cover:
            if not self.d.on_board(h) or h in occ_pins or h in occ_cover:
                return False
        return True


def _region_box(name: str, cols: int, rows: int):
    c3, r3 = cols / 3, rows / 3
    x0, x1, y0, y1 = 0, cols - 1, 0, rows - 1
    if "left" in name:
        x1 = c3
    if "right" in name:
        x0 = cols - 1 - c3
    if "top" in name:
        y1 = r3
    if "bottom" in name:
        y0 = rows - 1 - r3
    if name == "center":
        x0, x1, y0, y1 = c3, cols - 1 - c3, r3, rows - 1 - r3
    return x0, x1, y0, y1


# each unordered pair of holes once: offsets with dx > 0, or dx == 0 and dy > 0
_RING = [(dx, dy, CROWD_SIDE if abs(dx) + abs(dy) == 1 else CROWD_DIAG if max(abs(dx), abs(dy)) == 1 else CROWD_NEAR)
         for dx in range(0, 3) for dy in range(-2, 3) if (dx > 0 or dy > 0) and max(abs(dx), abs(dy)) <= 2]


def _hint_cost(st: _State, ref: str, pins: dict) -> float:
    s = st.spec.get(ref) or {}
    hs = list(pins.values())
    if not hs:
        return 0.0
    cx = sum(h[0] for h in hs) / len(hs)
    cy = sum(h[1] for h in hs) / len(hs)
    cost = 0.0
    if s.get("near") is not None:
        nx, ny = s["near"]
        cost += HINT_WEIGHT * max(0.0, abs(cx - nx) + abs(cy - ny) - 1.0)
    reg = s.get("region")
    if reg == "edge":
        d = st.d
        gap = min(min(h[0], d.cols - 1 - h[0], h[1], d.rows - 1 - h[1]) for h in hs)
        cost += HINT_WEIGHT * gap
    elif reg:
        x0, x1, y0, y1 = _region_box(reg, st.d.cols, st.d.rows)
        dx = max(0.0, x0 - cx, cx - x1)
        dy = max(0.0, y0 - cy, cy - y1)
        cost += HINT_WEIGHT * (dx + dy)
    return cost


def _cost(st: _State, poses: dict) -> float:
    pins_of = {ref: st.footprint(ref, *poses[ref])[0] for ref in poses}
    total = 0.0
    for n, rps in st.nets.items():
        hs = [pins_of[r][p] for r, p in rps if r in pins_of]
        if len(hs) >= 2:
            xs = [h[0] for h in hs]
            ys = [h[1] for h in hs]
            total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    where = {}
    for ref, pins in pins_of.items():
        for pin, h in pins.items():
            where[h] = (ref, st.net_of.get((ref, pin)))
    d = st.d
    for h, (ref, net) in where.items():
        for dx, dy, w in _RING:
            o = where.get((h[0] + dx, h[1] + dy))
            if o and o[0] != ref and (net is None or o[1] != net):
                total += w
        if (h[0] in (0, d.cols - 1) or h[1] in (0, d.rows - 1)) and (st.spec.get(ref) or {}).get("region") != "edge":
            total += EDGE
    for ref, pins in pins_of.items():
        total += _hint_cost(st, ref, pins)
    return total


def _occupancy(st: _State, poses: dict, skip: str | None = None):
    occ_pins, occ_cover = set(), set()
    for ref, (at, rot) in poses.items():
        if ref == skip:
            continue
        pins, cover = st.footprint(ref, at, rot)
        occ_pins |= set(pins.values())
        occ_cover |= cover
    return occ_pins, occ_cover


def _rotations(st: _State, ref: str, current: int) -> list[int]:
    s = st.spec.get(ref) or {}
    return [current] if s.get("fixed") or s.get("keep_rotation") else [0, 90, 180, 270]


def _greedy(st: _State, poses: dict, movable: list, rng: random.Random) -> list:
    """Place the unplaced parts one by one, most-connected first, each at its cheapest legal spot."""
    failed = []
    order = sorted(movable, key=lambda r: (-len(st.part_nets.get(r, ())), -len(st.d.parts[r].d.pins), rng.random()))
    for ref in order:
        if ref in poses:
            continue
        occ_pins, occ_cover = _occupancy(st, poses)
        best = None
        for rot in _rotations(st, ref, st.d.parts[ref].rotation):
            for x in range(-4, st.d.cols + 4):
                for y in range(-4, st.d.rows + 4):
                    if not st.legal(ref, (x, y), rot, occ_pins, occ_cover):
                        continue
                    pins, _ = st.footprint(ref, (x, y), rot)
                    c = _hint_cost(st, ref, pins) + _partial(st, poses, ref, pins) + rng.random() * 0.01
                    if best is None or c < best[0]:
                        best = (c, (x, y), rot)
        if best is None:
            failed.append(ref)
            continue
        poses[ref] = (best[1], best[2])
    return failed


def _partial(st: _State, poses: dict, ref: str, pins: dict) -> float:
    """Wire added by ref's nets, counting only parts already placed, plus crowding against them."""
    total = 0.0
    placed = {r: st.footprint(r, *poses[r])[0] for r in poses}
    for n in st.part_nets.get(ref, ()):
        hs = [pins[p] for r, p in st.nets[n] if r == ref]
        hs += [placed[r][p] for r, p in st.nets[n] if r in placed and r != ref]
        if len(hs) >= 2:
            xs = [h[0] for h in hs]
            ys = [h[1] for h in hs]
            total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    near = {}
    for r, ps in placed.items():
        for pin, h in ps.items():
            near[h] = st.net_of.get((r, pin))
    for pin, h in pins.items():
        net = st.net_of.get((ref, pin))
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                if dx or dy:
                    o = (h[0] + dx, h[1] + dy)
                    if o in near and (net is None or near[o] != net):
                        m = max(abs(dx), abs(dy))
                        total += CROWD_SIDE if abs(dx) + abs(dy) == 1 else CROWD_DIAG if m == 1 else CROWD_NEAR
    return total


def _anneal(st: _State, poses: dict, movable: list, rng: random.Random, iters: int) -> dict:
    if not movable:
        return poses
    cur = _cost(st, poses)
    best, best_cost = dict(poses), cur
    t0 = max(1.0, cur / max(1, len(movable)) * 0.5)
    for i in range(iters):
        t = t0 * (0.002 ** (i / iters))
        ref = rng.choice(movable)
        (at, rot) = poses[ref]
        kind = rng.random()
        trial = dict(poses)
        if kind < 0.55:
            span = 1 if rng.random() < 0.6 else 3
            trial[ref] = ((at[0] + rng.randint(-span, span), at[1] + rng.randint(-span, span)), rot)
        elif kind < 0.7:
            trial[ref] = ((rng.randrange(st.d.cols), rng.randrange(st.d.rows)), rot)
        elif kind < 0.85:
            rots = _rotations(st, ref, rot)
            trial[ref] = (at, rng.choice(rots))
        else:
            other = rng.choice(movable)
            if other == ref:
                continue
            trial[ref], trial[other] = (poses[other][0], rot), (at, poses[other][1])
        changed = [r for r in trial if trial[r] != poses[r]]
        if not changed:
            continue
        ok = True
        for r in changed:
            occ_pins, occ_cover = _occupancy(st, trial, skip=r)
            if not st.legal(r, *trial[r], occ_pins, occ_cover):
                ok = False
                break
        if not ok:
            continue
        c = _cost(st, trial)
        if c <= cur or rng.random() < math.exp(-(c - cur) / t):
            poses, cur = trial, c
            if c < best_cost:
                best, best_cost = dict(trial), c
    return best


def _spec(d: pb.Design) -> tuple[dict, dict, list]:
    """Read the hints. Returns (spec per ref, starting poses, problems)."""
    spec, poses, problems = {}, {}, []
    for item in d.raw.get("parts", []):
        ref = str(item.get("ref", "")).strip()
        s = {"fixed": bool(item.get("fixed")), "keep_rotation": bool(item.get("keep_rotation"))}
        if item.get("near") is not None:
            try:
                s["near"] = pb.parse_hole(item["near"])
            except pb.DesignError as e:
                problems.append(f"{ref}: near: {e}")
        reg = item.get("region")
        if reg:
            if reg not in REGIONS:
                problems.append(f"{ref}: region '{reg}' unknown; use one of {', '.join(REGIONS)}")
            else:
                s["region"] = reg
        if "at" in item:
            at = pb.parse_hole(item["at"])
            poses[ref] = (at, d.parts[ref].rotation)
            if not s["fixed"] and "near" not in s:
                s["near"] = at
        elif s["fixed"]:
            problems.append(f"{ref}: fixed but has no 'at'")
            s["fixed"] = False
        spec[ref] = s
    return spec, poses, problems


def place(design, seed: int = 1, iters: int | None = None) -> dict:
    """One placement. Returns {"ok", "design", "unplaced", "problems", "cost"}."""
    d = design if isinstance(design, pb.Design) else pb.load(design)
    spec, start, problems = _spec(d)
    st = _State(d, spec)
    rng = random.Random(seed)
    fixed = {r: start[r] for r in start if spec[r]["fixed"]}
    movable = [r for r in d.parts if not spec.get(r, {}).get("fixed")]
    # fixed parts must be legal among themselves
    poses: dict = {}
    for r, pose in fixed.items():
        occ_pins, occ_cover = _occupancy(st, poses)
        if not st.legal(r, *pose, occ_pins, occ_cover):
            problems.append(f"{r} is fixed at {pb.hole_name(pose[0])} but collides with another fixed part or leaves the board")
        poses[r] = pose
    # parts with a starting spot go there first when it's free; the rest are placed greedily
    for r in movable:
        if r in start:
            occ_pins, occ_cover = _occupancy(st, poses)
            if st.legal(r, *start[r], occ_pins, occ_cover):
                poses[r] = start[r]
    unplaced = _greedy(st, poses, [r for r in movable if r not in poses], rng)
    live = [r for r in movable if r in poses]
    n = iters if iters is not None else min(30000, 1500 * max(1, len(live)))
    poses = _anneal(st, poses, live, rng, n)
    raw = dict(d.raw)
    parts = []
    for item in d.raw.get("parts", []):
        item = dict(item)
        ref = str(item.get("ref", "")).strip()
        if ref in poses:
            at, rot = poses[ref]
            item["at"] = pb.hole_name(at)
            item["rotation"] = rot
        parts.append(item)
    raw["parts"] = parts
    # hand-drawn wires only make sense if nothing moved under them
    raw["wires"] = list(d.raw.get("wires", [])) if not live else []
    if unplaced:
        problems.append("no room for " + ", ".join(unplaced) + ": use a bigger board or fewer hints")
    return {"ok": not unplaced and not problems, "design": raw, "unplaced": unplaced, "problems": problems,
            "cost": round(_cost(st, poses), 1)}


def place_and_route(design, tries: int = 3, seed: int = 1, route: bool = True, iters: int | None = None) -> dict:
    """Place several times with different seeds, route each, keep the one that routes best (fewest open nets, then
    jumpers and via wires, then shortest wiring)."""
    d0 = design if isinstance(design, pb.Design) else pb.load(design)
    best = None
    tried = []
    for k in range(max(1, tries)):
        p = place(d0.raw, seed=seed + k, iters=iters)
        entry = {"seed": seed + k, "placement_cost": p["cost"], "problems": p["problems"]}
        if route and not p["unplaced"]:
            chk = pb.check(p["design"])
            if not chk["ok"]:
                entry["route"] = "placement has errors"
                score = (10**6, 0, 0, 0)
                r = {"ok": False, "design": p["design"], "issues": chk["issues"]}
            else:
                r = pb.route(p["design"])
                score = (sum(len(o["groups"]) - 1 for o in r["open"]), r.get("jumpers", 0), len(r.get("via_wires", [])),
                         p["cost"])
                entry |= {"open": [o["net"] for o in r["open"]], "jumpers": r.get("jumpers", 0)}
        else:
            r = {"ok": p["ok"], "design": p["design"]}
            score = (len(p["unplaced"]), 0, 0, p["cost"])
        tried.append(entry)
        if best is None or score < best[0]:
            best = (score, p, r)
    _, p, r = best
    out = dict(r)
    out["placement"] = {"unplaced": p["unplaced"], "problems": p["problems"], "tries": tried}
    out["ok"] = bool(r.get("ok")) and not p["unplaced"]
    return out

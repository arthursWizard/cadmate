"""Route the critical connections from pcb_critical: short, direct, on one layer where the rule asks for it, locked.

A grid router (A*, octilinear moves, 0.05 mm grid by default) that only needs to be good at what the critical items are:
short pad-to-pad connections, a via into the ground pour beside a pad, and two nets of a pair routed together. Clearance
is checked against every other-net pad, track, via and the board edge, with the net classes' clearances, first on the
grid with a margin and then again exactly on the final segments. What it can't route it reports, with the reason, for
the user to route by hand; it never routes something that breaks clearance.

The result goes into the board open in the PCB editor (one undo step, every segment locked) or into a copy of a
.kicad_pcb file.
"""

from __future__ import annotations

import heapq
import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .pcb_board import Board, Pad, Track, Via, dist, point_in_polygon, point_seg

Point = tuple[float, float]

GRID = 0.05  # mm
MAX_EXPANSIONS = 200_000
DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


@dataclass
class Shape:
    kind: str  # pad, track, via, edge, mask (an opening in the solder mask: nothing routed may sit in it)
    net: str | None
    clearance: float
    pad: Pad | None = None
    a: Point | None = None
    b: Point | None = None
    half: float = 0.0  # track half width, via radius
    poly: list[Point] | None = None

    def gap(self, p: Point) -> float:
        """Distance from p to this shape's copper edge."""
        if self.kind in ("mask", "keepout"):
            if point_in_polygon(p, self.poly):
                return -1.0
            return min(point_seg(p, a, b)[0] for a, b in zip(self.poly, self.poly[1:] + self.poly[:1]))
        if self.kind == "pad":
            return self.pad.distance(p)
        if self.kind == "via":
            return dist(p, self.a) - self.half
        d, _ = point_seg(p, self.a, self.b)
        return d - self.half

    def box(self) -> tuple[float, float, float, float]:
        if self.kind in ("mask", "keepout"):
            xs, ys = [q[0] for q in self.poly], [q[1] for q in self.poly]
            return min(xs), min(ys), max(xs), max(ys)
        if self.kind == "pad":
            r = math.hypot(self.pad.w, self.pad.h) / 2
            return self.pad.x - r, self.pad.y - r, self.pad.x + r, self.pad.y + r
        if self.kind == "via":
            return self.a[0] - self.half, self.a[1] - self.half, self.a[0] + self.half, self.a[1] + self.half
        return (min(self.a[0], self.b[0]) - self.half, min(self.a[1], self.b[1]) - self.half,
                max(self.a[0], self.b[0]) + self.half, max(self.a[1], self.b[1]) + self.half)


class Obstacles:
    """Every piece of copper per layer, bucketed for nearby lookups."""

    BUCKET = 1.0

    def __init__(self, b: Board):
        self.b = b
        self.layers = list(b.copper_layers)
        self.buckets: dict[str, dict[tuple[int, int], list[Shape]]] = {l: {} for l in self.layers}
        self.edge: list[Shape] = []
        rules = b.rules or {}
        self.min_clearance = rules.get("min_clearance") or 0.2
        self.edge_clearance = max(rules.get("min_copper_edge_clearance") or 0.0, 0.3)
        self.hole_to_hole = rules.get("min_hole_to_hole") or 0.25
        hole_clr = max(self.min_clearance, rules.get("min_hole_clearance") or 0.25)
        for p in b.pads:
            for l in p.layers:
                if l in self.buckets:  # an unplated hole keeps the hole clearance, not the copper one
                    self.add(l, Shape("pad", p.net, hole_clr if p.kind == "np_thru_hole" else self.clr(p.net), pad=p))
        for t in b.tracks:
            for s, e in t.pieces():
                self.add(t.layer, Shape("track", t.net, self.clr(t.net), a=s, b=e, half=t.width / 2))
        for v in b.vias:
            for l in self._via_layers(v.layers):
                self.add(l, Shape("via", v.net, self.clr(v.net), a=(v.x, v.y), half=v.size / 2))
        for l, poly in b.keepouts:  # rule areas with tracks or vias not allowed: nothing routed goes in
            if l in self.buckets:
                self.add(l, Shape("keepout", None, 0.0, poly=poly))
        for l, poly in b.mask_openings:  # copper there would be bare, and bridge to whatever else the opening bares
            if l in self.buckets:
                self.add(l, Shape("mask", None, self.min_clearance, poly=poly))
        self.edge = [Shape("edge", None, self.edge_clearance, a=s, b=e) for s, e in b.edges]
        box = b.bbox()
        self.outline = box
        self.max_clr = max([self.min_clearance] + [c.clearance or 0 for c in b.net_classes.values()])
        self.ebuckets: dict[tuple[int, int], list[Shape]] = {}
        for s in self.edge:
            for k in self._keys(s.box()):
                self.ebuckets.setdefault(k, []).append(s)

    def _via_layers(self, ends: list[str]) -> list[str]:
        if len(ends) < 2 or ends[0] not in self.layers or ends[1] not in self.layers:
            return self.layers
        i, j = sorted((self.layers.index(ends[0]), self.layers.index(ends[1])))
        return self.layers[i:j + 1]

    def clr(self, net: str | None) -> float:
        nc = self.b.net_class(net) if net else None
        return max(self.min_clearance, (nc.clearance if nc and nc.clearance else 0) or 0)

    def _keys(self, box) -> list[tuple[int, int]]:
        x0, y0, x1, y1 = box
        B = self.BUCKET
        return [(i, j) for i in range(math.floor(x0 / B), math.floor(x1 / B) + 1) for j in range(math.floor(y0 / B), math.floor(y1 / B) + 1)]

    def add(self, layer: str, s: Shape) -> tuple[str, Shape]:
        for k in self._keys(s.box()):
            self.buckets[layer].setdefault(k, []).append(s)
        return layer, s

    def remove(self, added: list[tuple[str, Shape]]) -> None:
        for layer, s in added:
            for k in self._keys(s.box()):
                lst = self.buckets[layer].get(k, [])
                if s in lst:
                    lst.remove(s)

    def add_track(self, t: Track) -> list[tuple[str, Shape]]:
        return [self.add(t.layer, Shape("track", t.net, self.clr(t.net), a=s, b=e, half=t.width / 2)) for s, e in t.pieces()]

    def add_via(self, v: Via) -> list[tuple[str, Shape]]:
        return [self.add(l, Shape("via", v.net, self.clr(v.net), a=(v.x, v.y), half=v.size / 2)) for l in self.layers]

    def near(self, layer: str, p: Point, reach: float) -> list[Shape]:
        B = self.BUCKET
        bk = self.buckets[layer]
        out: list[Shape] = []
        for i in range(math.floor((p[0] - reach) / B), math.floor((p[0] + reach) / B) + 1):
            for j in range(math.floor((p[1] - reach) / B), math.floor((p[1] + reach) / B) + 1):
                hit = bk.get((i, j))
                if hit:
                    out += hit  # a shape in two buckets is checked twice; cheaper than de-duplicating
        return out

    def clear(self, layer: str, p: Point, net: str | frozenset, half: float, margin: float = 0.0) -> tuple[bool, str | None]:
        """Whether copper of this net (or these nets, for a pair routed as one), half wide, centred at p, keeps clearance to
        everything else. Returns (ok, what's in the way)."""
        nets = net if isinstance(net, frozenset) else (net,)
        own = max(self.clr(n) for n in nets)
        reach = half + max(own, self.max_clr) + margin + 0.01
        for s in self.near(layer, p, reach):
            if s.net is not None and s.net in nets:
                continue
            need = half + (0.0 if s.kind == "keepout" else max(own, s.clearance)) + margin
            if s.gap(p) < need - 1e-6:
                return False, (s.pad.name if s.kind == "pad" else "a solder mask opening" if s.kind == "mask"
                               else "a keepout area" if s.kind == "keepout" else f"{s.kind} on {s.net or 'no net'}")
        if self.outline:
            x0, y0, x1, y1 = self.outline
            if not (x0 < p[0] < x1 and y0 < p[1] < y1):
                return False, "board edge"
        B = self.BUCKET
        e = half + self.edge_clearance + margin
        for i in range(math.floor((p[0] - e) / B), math.floor((p[0] + e) / B) + 1):
            for j in range(math.floor((p[1] - e) / B), math.floor((p[1] + e) / B) + 1):
                for s in self.ebuckets.get((i, j), ()):
                    if s.gap(p) < half + self.edge_clearance + margin - 1e-6:
                        return False, "board edge"
        return True, None


# --- routing one connection --------------------------------------------------------------------------


@dataclass
class Result:
    conn: dict
    item: str
    ok: bool
    why: str = ""
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    length: float = 0.0
    note: str = ""
    shapes: list = field(default_factory=list)
    over: bool = False  # routed, but longer than the item's rule allows

    def to_dict(self) -> dict:
        c = self.conn
        b = c["b"].get("pad") or f"via to {c['b'].get('via')}"
        out = {"item": self.item, "net": c["net"], "from": c["a"]["pad"], "to": b, "ok": self.ok}
        if self.ok:
            out |= {"length_mm": round(self.length, 2), "vias": len(self.vias), "segments": len(self.tracks),
                    "layers": sorted({t.layer for t in self.tracks}), "width_mm": self.tracks[0].width if self.tracks else None}
            if self.over:
                out["over_rule"] = True
            if self.note:
                out["note"] = self.note
        else:
            out["why"] = self.why
        return out


class Router:
    def __init__(self, b: Board, grid: float = GRID):
        self.b = b
        self.g = grid
        self.obs = Obstacles(b)
        self.soft: list[tuple[list[Point], set[str], set[str], float]] = []  # (area, layers, nets allowed, cost factor)
        self.pads = {}
        for p in b.pads:
            self.pads.setdefault(f"{p.ref}.{p.number}", []).append(p)

    def pad(self, end: dict) -> Pad | None:
        hits = self.pads.get(end["pad"], [])
        if not hits:
            return None
        return min(hits, key=lambda p: dist((p.x, p.y), (end.get("x", p.x), end.get("y", p.y))))

    def via_size(self, net: str) -> tuple[float, float]:
        nc = self.b.net_class(net)
        rules = self.b.rules or {}
        size = (nc.via_diameter if nc and nc.via_diameter else None) or max(rules.get("min_via_diameter") or 0, 0.6)
        drill = (nc.via_drill if nc and nc.via_drill else None) or max(rules.get("min_through_hole_diameter") or 0, 0.3)
        return size, drill

    def _via_fits(self, p: Point, net: str, size: float, margin: float = 0.0) -> bool:
        if not all(self.obs.clear(l, p, net, size / 2, margin)[0] for l in self.obs.layers):
            return False
        drill = self.via_size(net)[1]
        for v in self.b.vias:  # hole to hole applies between vias of the same net too
            if dist(p, (v.x, v.y)) < (v.drill + drill) / 2 + self.obs.hole_to_hole:
                return False
        return True

    def _segment_clear(self, layer: str, a: Point, b: Point, net: str, half: float, ends: tuple[Pad, ...] = ()) -> tuple[bool, str | None]:
        """Exact check along a segment; points inside the end pads are the footprint's own copper and skipped."""
        n = max(1, int(dist(a, b) / (self.g / 4)))
        for k in range(n + 1):
            t = k / n
            p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            if any(e.contains(p) for e in ends):
                continue
            ok, what = self.obs.clear(layer, p, net, half)
            if not ok:
                return False, what
        return True, None

    # a via into the pour beside a pad -----------------------------------------------------------------

    def via_beside(self, conn: dict, item: str) -> Result:
        pad = self.pad(conn["a"])
        if pad is None:
            return Result(conn, item, False, f"no pad {conn['a']['pad']}")
        net = conn["net"]
        if pad.kind == "thru_hole":
            return Result(conn, item, True, "through-hole pad reaches every layer; no via needed")
        size, drill = self.via_size(net)
        w = conn["width"]
        layer = next((l for l in pad.layers if l in self.obs.layers), self.obs.layers[0])
        best = None
        r0 = max(pad.w, pad.h) / 2 + size / 2 + 0.05
        for k in range(12):
            r = r0 + k * 0.1
            for i in range(16):
                a = i * math.pi / 8
                p = (pad.x + r * math.cos(a), pad.y + r * math.sin(a))
                if not self._via_fits(p, net, size, 0.0):
                    continue
                if not self._segment_clear(layer, (pad.x, pad.y), p, net, w / 2, (pad,))[0]:
                    continue
                best = p
                break
            if best:
                break
        if best is None:
            pours = conn["b"].get("pour_layers") or []
            if layer in pours:
                return Result(conn, item, True, note=f"no room for a {size} mm via within {r0 + 1.1:.1f} mm, so it is left to the "
                                                    f"{net} pour on {layer}; check the pour reaches the pad with a short path")
            return Result(conn, item, False, f"no room for a {size} mm via within {r0 + 1.1:.1f} mm of {pad.name}, and no {net} pour on {layer}")
        p = best
        t = Track((pad.x, pad.y), p, w, layer, net, locked=True)
        v = Via(p[0], p[1], size, drill, [self.obs.layers[0], self.obs.layers[-1]], net)
        return Result(conn, item, True, tracks=[t], vias=[v], length=t.length)

    def commit(self, res: Result) -> None:
        """Make a routed result part of the board, so later connections keep clear of it."""
        for t in res.tracks:
            res.shapes += self.obs.add_track(t)
        for v in res.vias:
            res.shapes += self.obs.add_via(v)
        self.b.tracks += res.tracks
        self.b.vias += res.vias

    def rip_up(self, res: Result) -> None:
        self.obs.remove(res.shapes)
        res.shapes = []
        ids = {id(t) for t in res.tracks} | {id(v) for v in res.vias}
        self.b.tracks = [t for t in self.b.tracks if id(t) not in ids]
        self.b.vias = [v for v in self.b.vias if id(v) not in ids]

    # pad to pad -------------------------------------------------------------------------------------

    def route(self, conn: dict, item: str, follow: list[Track] | None = None, pitch: float | None = None) -> Result:
        res = self._route(conn, item, follow, pitch, 3.0)
        if not res.ok and res.why.startswith("no path within the search area"):
            wider = self._route(conn, item, follow, pitch, 8.0)  # a detour around something in the way
            if wider.ok:
                return wider
        return res

    def _route(self, conn: dict, item: str, follow: list[Track] | None, pitch: float | None, around: float) -> Result:
        a, b = self.pad(conn["a"]), self.pad(conn["b"])
        if a is None or b is None:
            return Result(conn, item, False, f"pad {conn['a']['pad'] if a is None else conn['b']['pad']} not on the board")
        net = conn["net"]
        if a.net != net or b.net != net:
            return Result(conn, item, False, f"pads are on {a.net} and {b.net}, not {net}")
        w = conn["width"]
        half = w / 2
        la = [l for l in a.layers if l in self.obs.layers]
        lb = [l for l in b.layers if l in self.obs.layers]
        max_vias = conn.get("max_vias")
        common = [l for l in la if l in lb]
        if max_vias == 0 and not common:
            max_vias = 1  # the pads are on different sides: one via is the minimum
        layers = (common if max_vias == 0 else self.obs.layers) or self.obs.layers
        size, drill = self.via_size(net)
        g = self.g
        ox, oy = a.x, a.y  # grid origin on the start pad's centre
        span = dist((a.x, a.y), (b.x, b.y))
        pad_margin = max(around, span * 0.5)
        x0, x1 = min(a.x, b.x) - pad_margin, max(a.x, b.x) + pad_margin
        y0, y1 = min(a.y, b.y) - pad_margin, max(a.y, b.y) + pad_margin
        margin = g * 0.36  # a straight step between two clear grid points can come this much closer than the points
        cache: dict[tuple, bool] = {}

        def xy(i: int, j: int) -> Point:
            return ox + i * g, oy + j * g

        def free(li: int, i: int, j: int) -> bool:
            k = (li, i, j)
            if k not in cache:
                p = xy(i, j)
                lay = layers[li]
                if not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1):
                    cache[k] = False
                elif (lay in a.layers and a.contains(p, -0.0)) or (lay in b.layers and b.contains(p, -0.0)):
                    cache[k] = self.obs.clear(lay, p, net, min(half, 0.05), 0.0)[0] or True
                else:
                    cache[k] = self.obs.clear(lay, p, net, half, margin)[0]
            return cache[k]

        def goal(li: int, i: int, j: int) -> bool:
            return layers[li] in b.layers and b.contains(xy(i, j))

        fb: dict[tuple[int, int], list[Track]] = {}  # the partner's segments, bucketed by 0.5 mm
        for t in (follow or []) if pitch is not None else []:
            for i in range(math.floor((min(t.start[0], t.end[0]) - pitch - 0.5) / 0.5), math.floor((max(t.start[0], t.end[0]) + pitch + 0.5) / 0.5) + 1):
                for j in range(math.floor((min(t.start[1], t.end[1]) - pitch - 0.5) / 0.5), math.floor((max(t.start[1], t.end[1]) + pitch + 0.5) / 0.5) + 1):
                    fb.setdefault((i, j), []).append(t)

        def bonus(p: Point) -> float:
            """Cheaper to run alongside the partner at the pair's pitch: the pair stays coupled."""
            near = fb.get((math.floor(p[0] / 0.5), math.floor(p[1] / 0.5)))
            if not near:
                return 1.0
            d = min(point_seg(p, t.start, t.end)[0] for t in near)
            return 0.85 if abs(d - pitch) <= g * 0.75 else 1.0

        def h(i: int, j: int) -> float:
            p = xy(i, j)
            dx, dy = abs(p[0] - b.x), abs(p[1] - b.y)
            return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)

        via_cost = 1.5 if max_vias != 0 else math.inf
        turn = g * 0.6
        starts = [li for li, l in enumerate(layers) if l in a.layers]
        if not starts:
            return Result(conn, item, False, f"{a.name} isn't on a routable layer")
        heap: list = []
        bon: dict = {}
        soft = [a for a in self.soft if net not in a[2]]
        vfit: dict = {}
        best: dict = {}
        prev: dict = {}
        for li in starts:
            s = (li, 0, 0, 0)
            best[s] = 0.0
            heapq.heappush(heap, (h(0, 0), 0.0, s, -1))
        found = None
        n = 0
        while heap:
            f, cost, s, d = heapq.heappop(heap)
            if cost > best.get(s, math.inf) + 1e-9:
                continue
            li, i, j, nv = s
            if goal(li, i, j):
                found = s
                break
            n += 1
            if n > MAX_EXPANSIONS:
                break
            for di, (dx, dy) in enumerate(DIRS):
                ni, nj = i + dx, j + dy
                if not free(li, ni, nj):
                    continue
                step = g * (math.sqrt(2) if dx and dy else 1.0)
                if fb or soft:
                    if (li, ni, nj) not in bon:
                        q = xy(ni, nj)
                        bon[(li, ni, nj)] = (bonus(q) if fb else 1.0) * next(
                            (f for area, lays, ok, f in soft if layers[li] in lays and point_in_polygon(q, area)), 1.0)
                    step *= bon[(li, ni, nj)]
                c = cost + step + (turn if d not in (-1, di) else 0.0)
                if d != -1 and DIRS[d][0] == -dx and DIRS[d][1] == -dy:
                    continue
                t = (li, ni, nj, nv)
                if c < best.get(t, math.inf) - 1e-9:
                    best[t] = c
                    prev[t] = s
                    heapq.heappush(heap, (c + h(ni, nj), c, t, di))
            if via_cost < math.inf and (max_vias is None or nv < max_vias) and len(layers) > 1:
                p = xy(i, j)
                if (i, j) not in vfit:
                    vfit[(i, j)] = not (a.contains(p) or b.contains(p)) and self._via_fits(p, net, size, margin)
                if vfit[(i, j)]:
                    for lj in range(len(layers)):
                        if lj == li:
                            continue
                        t = (lj, i, j, nv + 1)
                        c = cost + via_cost
                        if c < best.get(t, math.inf) - 1e-9:
                            best[t] = c
                            prev[t] = s
                            heapq.heappush(heap, (c + h(i, j), c, t, -1))
        if found is None:
            why = "no path within the search area" if n <= MAX_EXPANSIONS else "search gave up (too many steps)"
            ok, what = self.obs.clear(layers[starts[0]], (b.x, b.y), net, half)
            if not ok:
                why += f"; the target pad is boxed in by {what} at width {w} mm"
            return Result(conn, item, False, why)
        # walk back: points with their layer
        chain = []
        s = found
        while True:
            chain.append(s)
            if s not in prev:
                break
            s = prev[s]
        chain.reverse()
        pts = [((xy(st[1], st[2])), layers[st[0]]) for st in chain]
        pts = [((a.x, a.y), pts[0][1])] + pts[1:]
        if pts[-1][0] != (b.x, b.y):
            pts.append(((b.x, b.y), pts[-1][1]))
        runs = _split_layers(pts)
        tracks, vias = [], []
        for k, (lay, run) in enumerate(runs):
            run = _simplify(run)
            run = self._straighten(lay, run, net, half, (a, b))
            for p, q in zip(run, run[1:]):
                if dist(p, q) > 1e-6:
                    tracks.append(Track(p, q, w, lay, net, locked=True))
            if k + 1 < len(runs):
                p = run[-1]
                vias.append(Via(p[0], p[1], size, drill, [self.obs.layers[0], self.obs.layers[-1]], net))
        for t in tracks:  # exact check, no grid margin
            ok, what = self._segment_clear(t.layer, t.start, t.end, net, half, (a, b))
            if not ok:
                return Result(conn, item, False, f"the routed path came too close to {what}; route this one by hand")
        return Result(conn, item, True, tracks=tracks, vias=vias, length=sum(t.length for t in tracks))

    # a pair as one -------------------------------------------------------------------------------------

    def route_pair(self, cp: dict, cn: dict, item: str, gap: float) -> tuple[Result, Result] | None:
        got = self._pair_one_layer(cp, cn, item, gap)
        if got is None and cp.get("max_vias") != 0:
            got = self._pair_with_vias(cp, cn, item, gap)
        return got

    def _pair_with_vias(self, cp: dict, cn: dict, item: str, gap: float) -> tuple[Result, Result] | None:
        """The pads are on different layers: a pair of vias side by side near the start, then coupled on each layer."""
        ap, bp, an, bn = self.pad(cp["a"]), self.pad(cp["b"]), self.pad(cn["a"]), self.pad(cn["b"])
        if None in (ap, bp, an, bn):
            return None
        la = [l for l in self.obs.layers if l in ap.layers and l in an.layers]
        lb = [l for l in self.obs.layers if l in bp.layers and l in bn.layers]
        if not la or not lb or la[0] == lb[0]:
            return None
        size, drill = self.via_size(cp["net"])
        vp = size + max(self.obs.clr(cp["net"]), gap)  # via pitch: the two vias keep clearance to each other
        m0 = ((ap.x + an.x) / 2, (ap.y + an.y) / 2)
        m1 = ((bp.x + bn.x) / 2, (bp.y + bn.y) / 2)
        ang = math.atan2(m1[1] - m0[1], m1[0] - m0[0])
        first_side = _side(m0, m1, (ap.x, ap.y)) or 1
        for r, da, side in ((r, da, sd) for r in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0) for da in (0, 0.785, -0.785, 1.571, -1.571)
                             for sd in (first_side, -first_side)):
            if True:  # each site, both ways round: which net's via sits on which side decides whether the hops can stay uncrossed
                site = (m0[0] + r * math.cos(ang + da), m0[1] + r * math.sin(ang + da))
                n = (-math.sin(ang + da), math.cos(ang + da))  # unit normal; _side calls this side 1
                vpos = (site[0] + n[0] * side * vp / 2, site[1] + n[1] * side * vp / 2)
                vneg = (site[0] - n[0] * side * vp / 2, site[1] - n[1] * side * vp / 2)
                if not (self._via_fits(vpos, cp["net"], size) and self._via_fits(vneg, cn["net"], size)):
                    continue
                vias = [Via(vpos[0], vpos[1], size, drill, [self.obs.layers[0], self.obs.layers[-1]], cp["net"]),
                        Via(vneg[0], vneg[1], size, drill, [self.obs.layers[0], self.obs.layers[-1]], cn["net"])]
                pv = [Pad("via", "+", cp["net"], vpos[0], vpos[1], size, size, 0, "circle", list(self.obs.layers), "thru_hole"),
                      Pad("via", "-", cn["net"], vneg[0], vneg[1], size, size, 0, "circle", list(self.obs.layers), "thru_hole")]
                added = [sh for v in vias for sh in self.obs.add_via(v)]
                first = self._pair_pads(ap, an, pv[0], pv[1], la[0], cp, cn, item, gap)
                second = self._pair_pads(pv[0], pv[1], bp, bn, lb[0], cp, cn, item, gap) if first else None
                self.obs.remove(added)
                if first and second:
                    out = []
                    for k in range(2):
                        tr = first[k].tracks + second[k].tracks
                        out.append(Result(first[k].conn, item, True, tracks=tr, vias=[vias[k]], length=sum(t.length for t in tr),
                                          note="routed as a coupled pair, with a via pair"))
                    return out[0], out[1]
        return None

    def _pair_pads(self, ap: Pad, an: Pad, bp: Pad, bn: Pad, layer: str, cp: dict, cn: dict, item: str, gap: float):
        return self._pair_geometry(ap, an, bp, bn, layer, cp, cn, item, gap)

    def _pair_one_layer(self, cp: dict, cn: dict, item: str, gap: float) -> tuple[Result, Result] | None:
        """Both nets of a pair hop together: the centreline is routed as one track as wide as the pair, then split into the two
        tracks at width and gap, so they stay coupled and nearly equal in length. Only on one layer; None when that can't work
        (the pads are on different layers, the pair would have to cross over, or the split comes too close to something)."""
        ap, bp, an, bn = self.pad(cp["a"]), self.pad(cp["b"]), self.pad(cn["a"]), self.pad(cn["b"])
        if None in (ap, bp, an, bn):
            return None
        common = [l for l in self.obs.layers if all(l in p.layers for p in (ap, bp, an, bn))]
        if not common:
            return None
        return self._pair_geometry(ap, an, bp, bn, common[0], cp, cn, item, gap)

    def _pair_geometry(self, ap: Pad, an: Pad, bp: Pad, bn: Pad, layer: str, cp: dict, cn: dict, item: str, gap: float):
        w = cp["width"]
        pitch = w + gap
        nets = frozenset({cp["net"], cn["net"]})
        start = ((ap.x + an.x) / 2, (ap.y + an.y) / 2)
        end = ((bp.x + bn.x) / 2, (bp.y + bn.y) / 2)
        # leave and enter each pad pair square to the line joining its two pads, as far as the pads reach
        s_out, e_in = _breakout(ap, an, end), _breakout(bp, bn, start)
        half = pitch / 2 + w / 2
        mid = self._search_single(s_out, e_in, layer, nets, half, (ap, bp, an, bn))
        if mid is None:
            return None
        line = [start] + self._straighten(layer, _simplify(mid), nets, half, (ap, bp, an, bn)) + [end]
        line = _simplify(line)
        if len(line) < 2:
            return None
        side = _side(line[0], line[1], (ap.x, ap.y))
        if side == 0 or _side(line[-2], line[-1], (bp.x, bp.y)) != side:
            return None  # the positive net would have to cross the negative one
        out = []
        for c, a, b, sgn in ((cp, ap, bp, side), (cn, an, bn, -side)):
            off = _offset(line, sgn * pitch / 2)
            # leave each pad straight for the first coupled corner, not for the offset of the centreline's very start
            pts = _simplify([(a.x, a.y)] + (off[1:-1] if len(off) > 2 else off) + [(b.x, b.y)])
            pts = self._straighten(layer, pts, c["net"], w / 2, (a, b))
            tracks = [Track(p, q, w, layer, c["net"], locked=True) for p, q in zip(pts, pts[1:]) if dist(p, q) > 1e-6]
            for t in tracks:
                if not self._segment_clear(layer, t.start, t.end, c["net"], w / 2, (a, b))[0]:
                    return None
            out.append(Result(c, item, True, tracks=tracks, length=sum(t.length for t in tracks), note="routed as a coupled pair"))
        # the two halves keep clear of each other where they leave the pads
        for t in out[0].tracks:
            for u in out[1].tracks:
                if _seg_gap(t, u) < self.obs.clr(cp["net"]) - 1e-3 and not (ap.contains(t.start) or bp.contains(t.end)):
                    return None
        return out[0], out[1]

    def _search_single(self, start: Point, end: Point, layer: str, net, half: float, ends: tuple[Pad, ...]) -> list[Point] | None:
        g = self.g
        span = dist(start, end)
        m = max(3.0, span * 0.5)
        x0, x1, y0, y1 = min(start[0], end[0]) - m, max(start[0], end[0]) + m, min(start[1], end[1]) - m, max(start[1], end[1]) + m
        margin = g * 0.36
        cache: dict = {}

        def xy(i, j):
            return start[0] + i * g, start[1] + j * g

        def free(i, j):
            if (i, j) not in cache:
                p = xy(i, j)
                cache[(i, j)] = x0 <= p[0] <= x1 and y0 <= p[1] <= y1 and self.obs.clear(layer, p, net, half, margin)[0]
            return cache[(i, j)]

        def h(i, j):
            p = xy(i, j)
            dx, dy = abs(p[0] - end[0]), abs(p[1] - end[1])
            return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)

        best, prev = {(0, 0): 0.0}, {}
        heap = [(h(0, 0), 0.0, (0, 0), -1)]
        n = 0
        while heap:
            f, cost, (i, j), d = heapq.heappop(heap)
            if cost > best.get((i, j), math.inf) + 1e-9:
                continue
            if dist(xy(i, j), end) <= g * 0.75:
                pts = [(i, j)]
                while pts[-1] in prev:
                    pts.append(prev[pts[-1]])
                return [start] + [xy(*c) for c in reversed(pts[:-1])] + [end]
            n += 1
            if n > MAX_EXPANSIONS:
                return None
            for di, (dx, dy) in enumerate(DIRS):
                k = (i + dx, j + dy)
                if not free(*k) and dist(xy(*k), end) > g * 0.75:
                    continue
                c = cost + g * (math.sqrt(2) if dx and dy else 1.0) + (g * 0.6 if d not in (-1, di) else 0.0)
                if c < best.get(k, math.inf) - 1e-9:
                    best[k] = c
                    prev[k] = (i, j)
                    heapq.heappush(heap, (c + h(*k), c, k, di))
        return None

    def _straighten(self, layer: str, pts: list[Point], net: str, half: float, ends: tuple[Pad, ...] = ()) -> list[Point]:
        """Replace stretches of the grid path with one straight or one 45°-then-straight jog where that stays clear."""
        if len(pts) <= 2:
            return pts
        out = [pts[0]]
        i = 0
        while i < len(pts) - 1:
            j = len(pts) - 1
            while j > i + 1:
                via = _jog(pts[i], pts[j])
                if all(self._segment_clear(layer, p, q, net, half, ends)[0] for p, q in zip([pts[i]] + via, via + [pts[j]])):
                    out += via + [pts[j]]
                    break
                j -= 1
            else:
                out.append(pts[i + 1])
                j = i + 1
            i = j
        return _simplify(out)


def _breakout(p: Pad, n: Pad, toward: Point) -> Point:
    """Just beyond a pad pair, square to the line joining the two pads, on the side facing toward."""
    mx, my = (p.x + n.x) / 2, (p.y + n.y) / 2
    dx, dy = n.x - p.x, n.y - p.y
    L = math.hypot(dx, dy) or 1.0
    ux, uy = -dy / L, dx / L
    if (toward[0] - mx) * ux + (toward[1] - my) * uy < 0:
        ux, uy = -ux, -uy
    reach = max(max(p.w, p.h), max(n.w, n.h)) / 2 + 0.2
    return mx + ux * reach, my + uy * reach


def _side(a: Point, b: Point, p: Point) -> int:
    """Which side of the line a→b the point p is on: 1 left, -1 right (in KiCad's Y-down frame), 0 on it."""
    c = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    return 0 if abs(c) < 1e-9 else (1 if c > 0 else -1)


def _offset(line: list[Point], d: float) -> list[Point]:
    """The polyline moved sideways by d, toward the side _side calls 1 when d is positive, with mitred corners."""
    def normal(a, b):
        L = dist(a, b) or 1.0
        return (-(b[1] - a[1]) / L, (b[0] - a[0]) / L)

    segs = [(a, b) for a, b in zip(line, line[1:]) if dist(a, b) > 1e-9]
    if not segs:
        return []
    ns = [normal(a, b) for a, b in segs]
    out = [(segs[0][0][0] + ns[0][0] * d, segs[0][0][1] + ns[0][1] * d)]
    for k in range(1, len(segs)):
        n1, n2 = ns[k - 1], ns[k]
        scale = d / max(1e-6, 1 + n1[0] * n2[0] + n1[1] * n2[1])
        p = segs[k][0]
        out.append((p[0] + (n1[0] + n2[0]) * scale, p[1] + (n1[1] + n2[1]) * scale))
    e = segs[-1][1]
    out.append((e[0] + ns[-1][0] * d, e[1] + ns[-1][1] * d))
    return out


def _seg_gap(t: Track, u: Track) -> float:
    from .pcb_board import seg_seg

    return seg_seg(t.start, t.end, u.start, u.end) - t.width / 2 - u.width / 2


def _split_layers(pts: list[tuple[Point, str]]) -> list[tuple[str, list[Point]]]:
    runs: list[tuple[str, list[Point]]] = []
    for p, l in pts:
        if runs and runs[-1][0] == l:
            runs[-1][1].append(p)
        else:
            if runs:
                runs[-1][1].append(p) if runs[-1][1][-1] != p else None
            runs.append((l, [p]))
    return runs


def _simplify(pts: list[Point]) -> list[Point]:
    out: list[Point] = []
    for p in pts:
        if out and dist(out[-1], p) < 1e-9:
            continue
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            cross = (b[0] - a[0]) * (p[1] - b[1]) - (b[1] - a[1]) * (p[0] - b[0])
            dot = (b[0] - a[0]) * (p[0] - b[0]) + (b[1] - a[1]) * (p[1] - b[1])
            if abs(cross) < 1e-9 and dot > 0:
                out[-1] = p
                continue
        out.append(p)
    return out


def _jog(a: Point, b: Point) -> list[Point]:
    """Points between a and b for an octilinear path: none when a-b is straight or 45°, else one bend (diagonal first)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) < 1e-9 or abs(dy) < 1e-9 or abs(abs(dx) - abs(dy)) < 1e-9:
        return []
    m = min(abs(dx), abs(dy))
    return [(a[0] + math.copysign(m, dx), a[1] + math.copysign(m, dy))]


# --- routing a plan --------------------------------------------------------------------------------------


PROMOTE = 3  # items moved to the front, one at a time, when they failed or broke their rule


def plane_nets(b: Board) -> dict[str, list[str]]:
    """Nets with a zone on an inner layer, and those layers: the planes of a 4+ layer board."""
    outer = {b.copper_layers[0], b.copper_layers[-1]} if b.copper_layers else set()
    planes = {n: [l for l in ls if l in b.copper_layers and l not in outer] for n, ls in b.zone_nets.items()}
    return {n: ls for n, ls in planes.items() if ls}


def _pad_joined(b: Board, p: Pad) -> bool:
    """A via or track end of the pad's own net already in the pad."""
    if any(v.net == p.net and p.contains((v.x, v.y)) for v in b.vias):
        return True
    return any(t.net == p.net and t.layer in p.layers and (p.contains(t.start) or p.contains(t.end)) for t in b.tracks)


def plane_fanout(b: Board, grid: float = GRID) -> dict:
    """A via beside every SMD pad on a plane net, into the plane, locked; added to the board.

    Freerouting 2.5 doesn't fan out to planes on "power" layers: it routes those pins pad to pad on the outer layers,
    and most fail. With a via down at each pad first, the plane joins them, and Freerouting only routes signals.
    Pads that already have a via or track of their net are left alone (the critical pass, or stitching vias)."""
    planes = plane_nets(b)
    out: dict = {"planes": planes, "tracks": [], "vias": [], "no_room": []}
    if not planes:
        return out
    r = Router(b, grid)
    outer = (b.copper_layers[0], b.copper_layers[-1])
    done: set[str] = set()
    # biggest copy of a pad first: an exposed pad is listed again for its paste windows
    for p in sorted(b.pads, key=lambda p: -p.w * p.h):
        name = f"{p.ref}.{p.number}"
        layer = next((l for l in p.layers if l in outer), None)
        if p.net not in planes or p.kind != "smd" or layer is None or name in done:
            continue
        done.add(name)
        if _pad_joined(b, p):
            continue
        nc = b.net_class(p.net)
        w = min((nc.track_width if nc and nc.track_width else None) or 0.25, min(p.w, p.h))
        conn = {"net": p.net, "width": w, "a": {"pad": name, "x": p.x, "y": p.y},
                "b": {"pour_layers": [l for l in b.zone_nets.get(p.net, []) if l in outer]}}
        res = r.via_beside(conn, "plane_fanout")
        if res.ok and (res.tracks or res.vias):
            r.commit(res)
            out["tracks"] += res.tracks
            out["vias"] += res.vias
        elif not res.ok or res.note:
            out["no_room"].append({"pad": name, "net": p.net, "why": res.why or res.note})
    return out


def route_plan(b: Board, plan: dict, ids: list[str] | None = None, skip: list[str] | None = None, grid: float = GRID) -> dict:
    """Route every connection of the plan's items whose action is "route" (or the ids given), in the plan's order.

    An item that fails or comes out over its rule (a crystal trace the long way round, a pair out of skew) is often boxed
    in by items routed before it. So the worst such item is moved to the front and everything routed again, a few times;
    a move is kept when the run comes out better."""
    items = [it for it in plan["items"] if (it["id"] in ids if ids else it["action"] == "route") and it["id"] not in (skip or [])]
    tracks0, vias0 = list(b.tracks), list(b.vias)

    def run(order: list[dict]) -> tuple[int, list[Result], list[dict], dict[str, int]]:
        b.tracks, b.vias = list(tracks0), list(vias0)
        results = _route_once(b, plan, order, grid)
        pairs = _pair_report(plan, items, results)
        bad: dict[str, int] = {}
        for x in results:
            if not x.ok or x.over:
                bad[x.item] = bad.get(x.item, 0) + (3 if not x.ok else 1)
        for e in pairs:
            if e.get("within") is False:
                bad[e["item"]] = bad.get(e["item"], 0) + 1
        return sum(bad.values()), results, pairs, bad

    front: list[str] = []  # promotions that helped, in the order they were made
    score, results, pairs, bad = run(items)
    best = (score, results, pairs, [])
    tried: set[str] = set()
    for _ in range(PROMOTE):
        # the worst item first (a failure outweighs a rule broken), in plan order among equals
        nxt = next((it for it in sorted(items, key=lambda it: -bad.get(it["id"], 0)) if bad.get(it["id"]) and it["id"] not in tried), None)
        if nxt is None:
            break
        tried.add(nxt["id"])
        ids_ = [nxt["id"]] + front
        order = [next(it for it in items if it["id"] == i) for i in ids_] + [it for it in items if it["id"] not in ids_]
        score, results, pairs, got = run(order)
        if score < best[0]:
            front, bad = ids_, got
            best = (score, results, pairs, list(front))
    _, results, pairs, first = best
    tracks = [t for res in results for t in res.tracks]
    vias = [v for res in results for v in res.vias]
    b.tracks, b.vias = tracks0 + tracks, vias0 + vias
    by_item: dict[str, dict] = {}
    for res in results:
        e = by_item.setdefault(res.item, {"routed": 0, "failed": 0})
        e["routed" if res.ok else "failed"] += 1
    check = [res.to_dict() for res in results if res.ok and res.over] + [e for e in pairs if e.get("within") is False]
    return {"tracks": tracks, "vias": vias, "connections": [res.to_dict() for res in results], "items": by_item, "pairs": pairs,
            "routed": sum(1 for x in results if x.ok), "failed": sum(1 for x in results if not x.ok), "over_rule": check,
            "routed_first": first}


def _route_once(b: Board, plan: dict, items: list[dict], grid: float) -> list[Result]:
    r = Router(b, grid)
    for it in plan["items"]:  # keep other signals out from under the crystal (AN2867): costly to cross, not forbidden
        if it["class"] == "crystal":
            for ref in it["refs"]:
                part = b.part(ref)
                if part is not None and not ref.startswith(("U", "IC")) and part.courtyard:
                    r.soft.append((part.courtyard, set(b.copper_layers), set(it["nets"]) | {n for n in b.nets if r_ground(plan, n)}, 6.0))
    results: list[Result] = []
    pair_tracks: dict[str, list[Track]] = {}

    def one(it: dict, c: dict) -> Result:
        limit = it["rule"].get("max_length_mm")
        if "via" in c["b"]:
            return r.via_beside(c, it["id"])
        kw = {}
        if c.get("pair") and c["net"] != c["pair"]:
            kw = {"follow": pair_tracks.get(c["pair"]), "pitch": c["width"] + (it["rule"].get("gap_mm") or r.obs.clr(c["net"]))}
        res = r.route(c, it["id"], **kw)
        narrow = r.b.net_class(c["net"])
        narrow = max((narrow.track_width if narrow and narrow.track_width else 0) or 0, (b.rules or {}).get("min_track_width") or 0.1)
        too_long = res.ok and limit and res.length > limit
        if (not res.ok or too_long) and narrow < c["width"] - 1e-6:
            res2 = r.route(c | {"width": narrow}, it["id"], **kw)
            if res2.ok and (not res.ok or res2.length < res.length - 0.2):
                res2.note = f"necked down to the net class width {narrow:g} mm to get through"
                res = res2
        if res.ok and limit and res.length > limit:
            res.over = True
            res.note = (res.note + "; " if res.note else "") + f"{res.length:.1f} mm, longer than the rule's {limit:g} mm: check it, or fix the placement"
        if res.ok and c.get("pair") and c["net"] == c["pair"]:
            pair_tracks.setdefault(c["pair"], []).extend(res.tracks)
        return res

    def attempt(it: dict, conns: list[dict]) -> list[Result]:
        out = []
        if it["class"] == "diff_pair":
            conns = coupled(it, conns, out)
        for c in conns:
            res = one(it, c)
            if res.ok:
                r.commit(res)
            out.append(res)
        return out

    def coupled(it: dict, conns: list[dict], out: list[Result]) -> list[dict]:
        """Route the pair's hops that both nets make together as one; return the connections left for one-by-one routing."""
        pos, neg = it["nets"]
        cp = [c for c in conns if c["net"] == pos]
        cn = [c for c in conns if c["net"] == neg]
        if len(cp) != len(cn):
            return conns
        left = []
        gap = it["rule"].get("gap_mm") or r.obs.clr(pos)
        for a, b in zip(cp, cn):
            got = r.route_pair(a, b, it["id"], gap)
            if got is None:
                left += [a, b]
                continue
            for res in got:
                r.commit(res)
                out.append(res)
                pair_tracks.setdefault(pos, []).extend(res.tracks if res.conn["net"] == pos else [])
        return sorted(left, key=lambda c: c["net"] != pos)

    # tracks first, item by item; the ground vias go in afterwards, where there is room, so they don't wall in a track
    for it in items:
        conns = [c for c in it["connections"] if "via" not in c["b"]]
        got = attempt(it, conns)
        if any(not x.ok for x in got) and len(conns) > 1:
            # what failed may be boxed in by the item's own earlier tracks: try the failed ones first
            for x in got:
                if x.ok:
                    r.rip_up(x)
            for c in conns:
                if c.get("pair") == c["net"]:
                    pair_tracks.pop(c["pair"], None)
            first = [c for c, x in zip(conns, got) if not x.ok]
            again = attempt(it, first + [c for c, x in zip(conns, got) if x.ok])
            if sum(x.ok for x in again) > sum(x.ok for x in got):
                got = again
            else:
                for x in again:
                    if x.ok:
                        r.rip_up(x)
                for c in conns:
                    if c.get("pair") == c["net"]:
                        pair_tracks.pop(c["pair"], None)
                got = attempt(it, conns)
        results += got
    for it in items:
        results += attempt(it, [c for c in it["connections"] if "via" in c["b"]])
    return results


def r_ground(plan: dict, net: str) -> bool:
    return any(it["class"] == "return_path" and net in it["nets"] for it in plan["items"])


def _pair_report(plan: dict, items: list[dict], results: list[Result]) -> list[dict]:
    out = []
    for it in items:
        if it["class"] != "diff_pair":
            continue
        lens = {}
        for net in it["nets"]:
            rs = [x for x in results if x.item == it["id"] and x.conn["net"] == net]
            lens[net] = (sum(x.length for x in rs), all(x.ok for x in rs), sum(len(x.vias) for x in rs))
        (p, (lp, okp, vp)), (n, (ln, okn, vn)) = list(lens.items())[:2]
        e = {"item": it["id"], "complete": okp and okn, "lengths_mm": {p: round(lp, 2), n: round(ln, 2)}, "vias": {p: vp, n: vn}}
        if okp and okn:
            e["mismatch_mm"] = round(abs(lp - ln), 2)
            e["within"] = abs(lp - ln) <= it["rule"].get("max_mismatch_mm", 2.0)
        out.append(e)
    return out


# --- writing the result -----------------------------------------------------------------------------------


def _net_ref(text: str) -> dict[str, str]:
    """name -> the token a segment uses for its net: the code in boards with a net table, the quoted name otherwise."""
    m = {}
    for code, name in re.findall(r'^\s*\(net (\d+) "((?:[^"\\]|\\.)*)"\)\s*$', text, re.M):
        m[name.replace('\\"', '"')] = code
    return m


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def sexpr_items(text: str, tracks: list[Track], vias: list[Via], locked: bool = True) -> str:
    codes = _net_ref(text)

    def net(n: str) -> str:
        return f"(net {codes[n]})" if n in codes else f"(net {_q(n)})"

    lk = "\t\t(locked yes)\n" if locked else ""
    out = []
    for t in tracks:
        out.append(f'\t(segment\n\t\t(start {t.start[0]:.4f} {t.start[1]:.4f})\n\t\t(end {t.end[0]:.4f} {t.end[1]:.4f})\n\t\t(width {t.width:g})\n'
                   f'{lk}\t\t(layer {_q(t.layer)})\n\t\t{net(t.net)}\n\t\t(uuid {_q(str(uuid.uuid4()))})\n\t)\n')
    for v in vias:
        out.append(f'\t(via\n\t\t(at {v.x:.4f} {v.y:.4f})\n\t\t(size {v.size:g})\n\t\t(drill {v.drill:g})\n{lk}'
                   f'\t\t(layers {_q(v.layers[0])} {_q(v.layers[-1])})\n\t\t{net(v.net)}\n\t\t(uuid {_q(str(uuid.uuid4()))})\n\t)\n')
    return "".join(out)


def write_file(src: Path, dst: Path, tracks: list[Track], vias: list[Via], locked: bool = True) -> None:
    text = src.read_text()
    end = text.rstrip().rfind(")")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text[:end] + sexpr_items(text, tracks, vias, locked) + text[end:])
    pro = src.with_suffix(".kicad_pro")
    if pro.exists() and dst.with_suffix(".kicad_pro") != pro:
        dst.with_suffix(".kicad_pro").write_text(pro.read_text())


def apply_live(board, tracks: list[Track], vias: list[Via], locked: bool = True) -> int:
    """Create the tracks and vias on the open board, locked. Runs inside a kicad_pcb_live commit."""
    from kipy.board_types import BoardLayer, Net
    from kipy.board_types import Track as KTrack
    from kipy.board_types import Via as KVia
    from kipy.geometry import Vector2
    from kipy.util.units import from_mm

    def layer(name: str):
        return getattr(BoardLayer, "BL_" + name.replace(".", "_"))

    items = []
    for t in tracks:
        k = KTrack()
        k.start = Vector2.from_xy(from_mm(t.start[0]), from_mm(t.start[1]))
        k.end = Vector2.from_xy(from_mm(t.end[0]), from_mm(t.end[1]))
        k.width = from_mm(t.width)
        k.layer = layer(t.layer)
        k.net = Net(name=t.net)
        k.locked = locked
        items.append(k)
    for v in vias:
        k = KVia()
        k.position = Vector2.from_xy(from_mm(v.x), from_mm(v.y))
        k.diameter = from_mm(v.size)
        k.drill_diameter = from_mm(v.drill)
        k.net = Net(name=v.net)
        k.locked = locked
        items.append(k)
    if items:
        board.create_items(items)
    return len(items)

"""Finish what an autorouter left: take out the tracks that break clearance or the minimum width, then join every net
that is still in pieces with the grid router, closest pads first.

Freerouting, on a dense board, stops with a few connections open and squeezes some tracks past fine-pitch pins closer
than the rules allow. Both are what a person would fix by hand; this does the same, and reports what it couldn't. Only
unlocked tracks are taken out: locked ones were placed on purpose (the critical routes, the plane vias).
"""

from __future__ import annotations

import re
from pathlib import Path

from .pcb_board import Board, NetGraph, Pad, Track, Via, dist, point_in_polygon
from .pcb_route import GRID, Point, Router, sexpr_items

TRIES = 6  # end pairs tried per piece before it's reported


def _width(b: Board, net: str) -> float:
    nc = b.net_class(net)
    floor = (b.rules or {}).get("min_track_width") or 0.0
    return max(nc.track_width if nc and nc.track_width else 0.2, floor)


def _clash(r: Router, t: Track) -> str | None:
    """What other-net copper (or edge, or keepout) t comes too close to, checked exactly along its whole length, its own
    pads included: a track inside its pad can still stick out towards the next pin."""
    for a, c in t.pieces():
        n = max(1, int(dist(a, c) / (r.g / 4)))
        for k in range(n + 1):
            p = (a[0] + (c[0] - a[0]) * k / n, a[1] + (c[1] - a[1]) * k / n)
            ok, what = r.obs.clear(t.layer, p, t.net, t.width / 2 - 1e-3, masks=False)
            if not ok:
                return what
    return None


def _in_courtyard(b: Board, t: Track, ref: str) -> bool:
    part = b.part(ref)
    poly = part.courtyard if part else []
    if len(poly) < 3:
        return False
    return any(point_in_polygon(p, poly) for a, c in t.pieces() for p in (a, c, ((a[0] + c[0]) / 2, (a[1] + c[1]) / 2)))


def faulty(b: Board, necks: dict[str, list[tuple[str, float]]] | None = None, skip: set[str] = frozenset()) -> list[tuple[Track, str]]:
    """Unlocked tracks narrower than the board minimum or closer to other-net copper than the clearance allows. necks are
    the .kicad_dru's neck rules ({net: [(ref, min width)]}): a track that narrow inside that part's courtyard is allowed."""
    r = Router(b)
    floor = (b.rules or {}).get("min_track_width") or 0.0
    out = []
    for t in b.tracks:
        if t.locked or t.net in skip:
            continue
        if t.width < floor - 1e-4 and not any(t.width >= w - 1e-4 and _in_courtyard(b, t, ref) for ref, w in (necks or {}).get(t.net, [])):
            out.append((t, f"{t.width:g} mm wide, under the {floor:g} mm minimum"))
            continue
        what = _clash(r, t)
        if what:
            out.append((t, f"too close to {what}"))
    return out


def _parts(b: Board, net: str) -> list[tuple[list[Pad], list[tuple[Point, list[str], float]]]]:
    """The net's copper in connected pieces that hold a pad: each piece's pads, and the points a new track can end on
    (track ends and vias, with their layers and width)."""
    g = NetGraph(b, net)
    seen: set[int] = set()
    out = []
    for start in g.pad_node.values():
        if start in seen:
            continue
        stack, pads, pts = [start], [], []
        seen.add(start)
        while stack:
            n = stack.pop()
            kind, item, lay = g.nodes[n]
            if kind == "pad":
                pads.append(item)
            elif kind == "end":
                tw = min((t.width for t in b.tracks if t.net == net and t.layer == lay and item in (t.start, t.end)), default=0.2)
                pts.append((item, [lay], tw))
            elif kind == "via":
                pts.append(((item.x, item.y), list(b.copper_layers), item.size))
            for m, _, _ in g.edges[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        out.append((pads, pts))
    return sorted(out, key=lambda e: -len(e[0]))


def _end(r: Router, net: str, item, k: int) -> dict:
    """A route end: a pad, or a point on the net's copper standing in as a small pad."""
    if isinstance(item, Pad):
        return {"pad": f"{item.ref}.{item.number}", "x": item.x, "y": item.y}
    (x, y), layers, w = item
    name = f"~{k}"
    r.pads[name] = [Pad("~", str(k), net, x, y, w, w, 0.0, "circle", layers, "smd")]
    return {"pad": name, "x": x, "y": y}


def finish(b: Board, outer_only: bool = True, grid: float = GRID, necks: dict[str, list[tuple[str, float]]] | None = None,
           skip: set[str] = frozenset()) -> dict:
    """Rip up the faulty tracks and join every net still in pieces, on the board in place. A piece is joined to the
    nearest copper of the rest of its net, pad or track end, so a ripped neck is replaced by a short hop rather than a
    whole new route. A route is kept only if it clears all other-net copper exactly. Dead-end stubs left over are removed.
    Returns the tracks taken out, the tracks and vias added, and what is still open with the reason. With outer_only
    the new tracks keep to the outer layers, so the inner planes stay whole. Nets in skip (crystal, pairs, RF: routed
    with care, not by a grid router) are left as they are, and listed under by_hand if still open."""
    from .freerouting import prune_stubs

    open_before = sum(len(_parts(b, n)) - 1 for n in b.nets if n)
    bad = faulty(b, necks, skip)
    gone = {id(t) for t, _ in bad}
    b.tracks = [t for t in b.tracks if id(t) not in gone]
    outer = [l for l in b.copper_layers if l in ("F.Cu", "B.Cu")]
    floor = (b.rules or {}).get("min_track_width") or 0.0
    r = Router(b, grid)
    tracks: list[Track] = []
    vias: list[Via] = []
    still = []
    k = 0
    by_hand = []
    for net in sorted(n for n in b.nets if n):
        if sum(1 for p in b.pads if p.net == net) < 2:
            continue
        if net in skip:
            if len(_parts(b, net)) > 1:
                by_hand.append(net)
            continue
        while True:
            parts = _parts(b, net)
            if len(parts) < 2:
                break
            joined = False
            for i, (pads, pts) in enumerate(parts[1:], 1):  # each smaller piece, to the nearest copper of the rest
                mine = pads + pts
                rest = [q for j, (ps, qs) in enumerate(parts) if j != i for q in ps + qs]

                def xy(q):
                    return (q.x, q.y) if isinstance(q, Pad) else q[0]

                pairs = sorted(((dist(xy(a), xy(c)), n, a, c) for n, (a, c) in enumerate((a, c) for a in mine for c in rest)),
                               key=lambda e: e[:2])
                why = None
                for _, _, a, c in [e for e in pairs if isinstance(e[2], Pad) or isinstance(e[3], Pad)][:TRIES]:
                    for w in sorted({_width(b, net), floor or _width(b, net)}, reverse=True):  # the board minimum if full width won't go
                        k += 1
                        conn = {"net": net, "width": w, "a": _end(r, net, a, k), "b": _end(r, net, c, -k)}
                        if outer_only and outer:
                            conn["layers"] = outer
                        res = r.route(conn, "finish")
                        if res.ok:
                            what = next((x for x in (_clash(r, t) for t in res.tracks) if x), None)
                            if what:
                                res.ok, res.why = False, f"the route came too close to {what}"
                        if res.ok:
                            r.commit(res)
                            tracks += res.tracks
                            vias += res.vias
                            joined = True
                            break
                        why = why or res.why
                    if joined:
                        break
                if joined:
                    break
                still.append({"net": net, "pads": sorted(p.name for p in pads)[:6], "why": why})
            if not joined:
                break
    for res_t in tracks:
        res_t.locked = False
    loose = [t for t in b.tracks if not t.locked or any(t is x for x in tracks)]
    fixed = [t for t in b.tracks if t.locked and not any(t is x for x in tracks)]
    b.tracks = fixed
    kept, _, _ = prune_stubs(b, loose, [])
    keep = {id(t) for t in kept}
    stubs = [t for t in loose if id(t) not in keep]
    b.tracks = fixed + kept
    tracks = [t for t in tracks if id(t) in keep]
    open_after = sum(len(_parts(b, n)) - 1 for n in b.nets if n)
    return {"ripped": [{"net": t.net, "layer": t.layer, "at": [round(t.start[0], 2), round(t.start[1], 2)], "why": w} for t, w in bad],
            "removed": [t for t, _ in bad] + [t for t in stubs if not any(t is x for x in tracks)],
            "tracks": tracks, "vias": vias, "open": still, "by_hand": by_hand, "open_before": open_before, "open_after": open_after}


def _key(layer: str, s: tuple, e: tuple) -> tuple:
    a, c = sorted([(round(s[0], 3), round(s[1], 3)), (round(e[0], 3), round(e[1], 3))])
    return (layer, a, c)


def write(src: Path, dst: Path, res: dict) -> int:
    """The board file with the removed segments taken out and the new copper added. Returns segments removed."""
    text = src.read_text()
    drop = {_key(t.layer, t.start, t.end) for t in res["removed"]}
    n = 0

    def cut(blk: str) -> bool:
        nonlocal n
        s = re.search(r"\(start ([-\d.]+) ([-\d.]+)\)", blk)
        e = re.search(r"\(end ([-\d.]+) ([-\d.]+)\)", blk)
        lay = re.search(r'\(layer "?([^")]+)"?\)', blk)
        if not (s and e and lay) or "(locked" in blk or blk.startswith("(segment locked"):
            return True
        if _key(lay[1], (float(s[1]), float(s[2])), (float(e[1]), float(e[2]))) in drop:
            n += 1
            return False
        return True

    out, i = [], 0
    for m in re.finditer(r"\(segment\b", text):
        if m.start() < i:
            continue
        depth, j = 0, m.start()
        while True:  # to the matching paren; a segment holds no parens inside its strings
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
            if depth == 0:
                break
        blk = text[m.start():j]
        keep = cut(blk)
        out.append(text[i:m.start()] if keep else text[i:m.start()].rstrip(" \t"))
        if keep:
            out.append(blk)
        elif text[j:j + 1] == "\n":
            j += 1
        i = j
    text = "".join(out) + text[i:]
    end = text.rstrip().rfind(")")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text[:end] + sexpr_items(text, res["tracks"], res["vias"], locked=False) + text[end:])
    return n

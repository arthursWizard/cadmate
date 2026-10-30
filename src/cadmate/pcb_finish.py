"""Finish what an autorouter left: take out the tracks that break clearance or the minimum width, then join every net
that is still in pieces with the grid router, closest pads first.

Freerouting, on a dense board, stops with a few connections open and squeezes some tracks past fine-pitch pins closer
than the rules allow. Both are what a person would fix by hand; this does the same, and reports what it couldn't. Only
unlocked tracks are taken out: locked ones were placed on purpose (the critical routes, the plane vias).
"""

from __future__ import annotations

import re
from pathlib import Path

from .pcb_board import Board, NetGraph, Track, Via, dist
from .pcb_route import GRID, Router, sexpr_items

TRIES = 4  # pad pairs tried per island before it's reported


def _width(b: Board, net: str) -> float:
    nc = b.net_class(net)
    floor = (b.rules or {}).get("min_track_width") or 0.0
    return max(nc.track_width if nc and nc.track_width else 0.2, floor)


def faulty(b: Board) -> list[tuple[Track, str]]:
    """Unlocked tracks narrower than the board minimum or closer to other-net copper than the clearance allows."""
    r = Router(b)
    floor = (b.rules or {}).get("min_track_width") or 0.0
    out = []
    for t in b.tracks:
        if t.locked:
            continue
        if t.width < floor - 1e-4:
            out.append((t, f"{t.width:g} mm wide, under the {floor:g} mm minimum"))
            continue
        ends = tuple(p for p in b.pads if p.net == t.net and t.layer in p.layers and (p.contains(t.start) or p.contains(t.end)))
        for a, c in t.pieces():
            ok, what = r._segment_clear(t.layer, a, c, t.net, t.width / 2, ends)
            if not ok:
                out.append((t, f"too close to {what}"))
                break
    return out


def finish(b: Board, outer_only: bool = True, grid: float = GRID) -> dict:
    """Rip up the faulty tracks (and the stubs they leave) and route the open connections, on the board in place.
    Returns the tracks taken out, the tracks and vias added, and what is still open with the reason. With outer_only
    the new tracks keep to the outer layers, so the inner planes stay whole."""
    from .freerouting import prune_stubs

    bad = faulty(b)
    gone = {id(t) for t, _ in bad}
    fixed = [t for t in b.tracks if t.locked]
    loose = [t for t in b.tracks if not t.locked and id(t) not in gone]
    b.tracks = fixed
    kept, _, _ = prune_stubs(b, loose, [])  # what hung off the ripped tracks goes too
    keep = {id(t) for t in kept}
    removed = [t for t, _ in bad] + [t for t in loose if id(t) not in keep]
    b.tracks = fixed + kept
    outer = [l for l in b.copper_layers if l in ("F.Cu", "B.Cu")]
    r = Router(b, grid)
    tracks: list[Track] = []
    vias: list[Via] = []
    still = []
    for net in sorted(n for n in b.nets if n):
        if sum(1 for p in b.pads if p.net == net) < 2:
            continue
        while True:
            isl = sorted(NetGraph(b, net).islands(), key=len, reverse=True)
            if len(isl) < 2:
                break
            joined = False
            for part in isl[1:]:  # each smaller piece, to whatever of the net is nearest
                rest = [p for g in isl if g is not part for p in g]
                pairs = sorted(((dist((a.x, a.y), (c.x, c.y)), k, a, c) for k, (a, c) in enumerate((a, c) for a in part for c in rest)))
                why = None
                for _, _, a, c in pairs[:TRIES]:
                    conn = {"net": net, "width": min(_width(b, net), a.w, a.h, c.w, c.h),
                            "a": {"pad": f"{a.ref}.{a.number}", "x": a.x, "y": a.y},
                            "b": {"pad": f"{c.ref}.{c.number}", "x": c.x, "y": c.y}}
                    if outer_only and outer:
                        conn["layers"] = outer
                    res = r.route(conn, "finish")
                    if res.ok:
                        r.commit(res)
                        tracks += res.tracks
                        vias += res.vias
                        joined = True
                        break
                    why = why or res.why
                if joined:
                    break
                still.append({"net": net, "pads": sorted(p.name for p in part)[:6], "why": why})
            if not joined:
                break
    return {"ripped": [{"net": t.net, "layer": t.layer, "at": [round(t.start[0], 2), round(t.start[1], 2)], "why": w} for t, w in bad],
            "removed": removed, "tracks": tracks, "vias": vias, "open": still}


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

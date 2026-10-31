"""Pictures and a build list for a protoboard design: the component side, and the solder side mirrored the way you see
it when you turn the board over, both with the same row letters and column numbers so you can copy them hole by hole."""

from __future__ import annotations

import math
from html import escape
from pathlib import Path

from . import protoboard as pb

P = 30.0  # px per hole
M = 36.0  # margin for the row and column labels
HEAD = 34.0  # panel title
FONT = "Helvetica, Arial, 'DejaVu Sans', sans-serif"

BOARD = "#f1e4c3"
BOARD_EDGE = "#b49a62"
HOLE_FILL = "#fffaf0"
HOLE_EDGE = "#9b8655"
COPPER = "#d9b57a"
COPPER_EDGE = "#a7824a"
GUIDE = "#cdb98a"
INK = "#2b2b2b"
OPEN = "#e0245e"

KIND_FILL = {
    "resistor": ("#a9cbe8", INK), "diode": ("#f2b36b", INK), "capacitor": ("#f6d47c", INK),
    "electrolytic": ("#5b6fa8", "#ffffff"), "led": ("#f59a9a", INK), "ic": ("#3d3d3d", "#ffffff"),
    "header": ("#2f2f2f", "#ffffff"), "transistor": ("#3a3a3a", "#ffffff"), "regulator": ("#4d4d4d", "#ffffff"),
    "connector": ("#5aae61", "#ffffff"), "switch": ("#8a8a8a", "#ffffff"), "pot": ("#3f6fb0", "#ffffff"),
    "crystal": ("#c4c4c4", INK), "generic": ("#bfb7a8", INK),
}

PALETTE = ["#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2", "#bcbd22", "#3b5bdb",
           "#0b8457", "#c2185b", "#6d4c41", "#00838f", "#7b1fa2", "#ef6c00", "#558b2f"]
POWER = ("VCC", "VDD", "V+", "+5V", "+3V3", "+3.3V", "+12V", "+9V", "VIN", "VBAT", "+V", "VBUS")
GROUND = ("GND", "0V", "VSS", "V-", "GNDA", "AGND", "DGND")


def net_colors(d: pb.Design) -> dict[str, str]:
    out, i = {}, 0
    for n in sorted(d.nets):
        u = n.upper()
        if u in GROUND or u.startswith("GND"):
            out[n] = "#1c1c1c"
        elif u in POWER or u.startswith("+") or u.startswith("VCC"):
            out[n] = "#d62728"
        else:
            out[n] = PALETTE[i % len(PALETTE)]
            i += 1
    return out


class _View:
    def __init__(self, d: pb.Design, ox: float, oy: float, mirror: bool):
        self.d, self.ox, self.oy, self.mirror = d, ox, oy, mirror

    def x(self, bx: float) -> float:
        c = (self.d.cols - 1 - bx) if self.mirror else bx
        return self.ox + M + c * P + P / 2

    def y(self, by: float) -> float:
        return self.oy + HEAD + M + by * P + P / 2

    def pt(self, h) -> tuple[float, float]:
        return self.x(h[0]), self.y(h[1])


def _t(x, y, s, size, fill=INK, anchor="middle", weight="normal", extra=""):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size:.1f}" fill="{fill}" '
            f'text-anchor="{anchor}" dominant-baseline="central" font-weight="{weight}"{extra}>{escape(str(s))}</text>')


def _halo(x, y, s, size, fill=INK, weight="bold", halo="#ffffff", width=3.0):
    """Text with a halo, drawn twice (some SVG renderers ignore paint-order)."""
    return (_t(x, y, s, size, halo, weight=weight, extra=f' stroke="{halo}" stroke-width="{width}" stroke-linejoin="round"')
            + _t(x, y, s, size, fill, weight=weight))


def _shape_svg(v: _View, p: pb.Placed, s: dict, fill: str, stroke: str, sw: float, extra: str = "") -> str:
    if "rect" in s:
        x0, y0, x1, y1 = s["rect"]
        a, b = p.to_board(x0, y0), p.to_board(x1, y1)
        xs = sorted([v.x(a[0]), v.x(b[0])])
        ys = sorted([v.y(a[1]), v.y(b[1])])
        r = min(4.0, (xs[1] - xs[0]) / 4, (ys[1] - ys[0]) / 4)
        return (f'<rect x="{xs[0]:.1f}" y="{ys[0]:.1f}" width="{xs[1] - xs[0]:.1f}" height="{ys[1] - ys[0]:.1f}" rx="{r:.1f}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>')
    if "circle" in s:
        cx, cy, r = s["circle"]
        c = p.to_board(cx, cy)
        return f'<circle cx="{v.x(c[0]):.1f}" cy="{v.y(c[1]):.1f}" r="{r / pb.PITCH * P:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>'
    if "halfdisk" in s:
        cx, cy, r = s["halfdisk"]
        pts = []
        for i in range(0, 19):
            a = math.pi * i / 18
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        bp = [p.to_board(x, y) for x, y in pts]
        d = " ".join(f"{v.x(x):.1f},{v.y(y):.1f}" for x, y in bp)
        return f'<polygon points="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>'
    return ""


def _body_box(v: _View, p: pb.Placed) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for s in p.d.body:
        for k in ("rect", "circle", "halfdisk"):
            if k in s:
                a = s[k]
                cands = [(a[0], a[1]), (a[2], a[3])] if k == "rect" else [(a[0] - a[2], a[1] - a[2]), (a[0] + a[2], a[1] + a[2])]
                for c in cands:
                    bx, by = p.to_board(*c)
                    xs.append(v.x(bx))
                    ys.append(v.y(by))
    if not xs:
        pts = [v.pt(h) for h in p.pin_holes().values()]
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _label_spot(v: _View, p: pb.Placed, cx: float, cy: float, w: float, h: float) -> tuple[float, float]:
    """Move a label off the part's own pins: try the centre, then nudge up, down, left, right."""
    pins = [v.pt(q) for q in p.pin_holes().values()]
    r = P * 0.22

    def clear(x, y):
        return all(not (x - w / 2 - r < px < x + w / 2 + r and y - h / 2 - r < py < y + h / 2 + r) for px, py in pins)

    for k in (0.0, 0.5, 0.75, 1.0):
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)) if k else ((0, 0),):
            x, y = cx + dx * k * P, cy + dy * k * P
            if clear(x, y):
                return x, y
    return cx, cy


def _marks_svg(v: _View, p: pb.Placed, text_color: str) -> list[str]:
    out = []
    for m in p.d.marks:
        if "band" in m:
            out.append(_shape_svg(v, p, {"rect": m["band"]}, "#444444", "none", 0))
        elif "notch" in m:
            cx, cy, r = m["notch"]
            c = p.to_board(cx, cy)
            inner = p.to_board(cx + r * 0.7, cy)
            out.append(f'<circle cx="{v.x(inner[0]):.1f}" cy="{v.y(inner[1]):.1f}" r="{r * 0.75 / pb.PITCH * P:.1f}" fill="#8a8a8a"/>')
        elif "dot" in m:
            cx, cy, r = m["dot"]
            c = p.to_board(cx, cy)
            out.append(f'<circle cx="{v.x(c[0]):.1f}" cy="{v.y(c[1]):.1f}" r="{max(2.2, r / pb.PITCH * P):.1f}" fill="#e8e8e8"/>')
        elif "plus" in m:
            c = p.to_board(*m["plus"])
            out.append(_t(v.x(c[0]), v.y(c[1]), "+", P * 0.5, text_color, weight="bold"))
        elif "flat" in m:
            cx, cy, r = m["flat"]
            # flat on the cathode (pin 1) side: a short chord across the rim
            a1, a2 = p.to_board(cx - r * 0.9, cy - r * 0.45), p.to_board(cx - r * 0.9, cy + r * 0.45)
            out.append(f'<line x1="{v.x(a1[0]):.1f}" y1="{v.y(a1[1]):.1f}" x2="{v.x(a2[0]):.1f}" y2="{v.y(a2[1]):.1f}" stroke="#7a1f1f" stroke-width="2.5"/>')
        elif "tab" in m:
            out.append(_shape_svg(v, p, {"rect": m["tab"]}, "#b9b9b9", "#555", 0.8))
        elif "circle_mark" in m:
            cx, cy, r = m["circle_mark"]
            c = p.to_board(cx, cy)
            out.append(f'<circle cx="{v.x(c[0]):.1f}" cy="{v.y(c[1]):.1f}" r="{r / pb.PITCH * P:.1f}" fill="none" stroke="{text_color}" stroke-width="1.2" opacity="0.7"/>')
    return out


def _frame(v: _View, title: str, subtitle: str) -> list[str]:
    d = v.d
    w, h = d.cols * P, d.rows * P
    x0, y0 = v.ox + M, v.oy + HEAD + M
    out = [_t(v.ox + M, v.oy + 12, title, 17, INK, "start", "bold"),
           _t(v.ox + M, v.oy + 28, subtitle, 11.5, "#666", "start"),
           f'<rect x="{x0 - 8:.1f}" y="{y0 - 8:.1f}" width="{w + 16:.1f}" height="{h + 16:.1f}" rx="7" fill="{BOARD}" stroke="{BOARD_EDGE}" stroke-width="2"/>']
    for k in range(5, d.cols, 5):  # guides every five holes, between holes, to count by
        gx = (v.x(k - 0.5))
        out.append(f'<line x1="{gx:.1f}" y1="{y0 - 6:.1f}" x2="{gx:.1f}" y2="{y0 + h + 6:.1f}" stroke="{GUIDE}" stroke-width="1" stroke-dasharray="3 4"/>')
    for k in range(5, d.rows, 5):
        gy = v.y(k - 0.5)
        out.append(f'<line x1="{x0 - 6:.1f}" y1="{gy:.1f}" x2="{x0 + w + 6:.1f}" y2="{gy:.1f}" stroke="{GUIDE}" stroke-width="1" stroke-dasharray="3 4"/>')
    for c in range(d.cols):
        bold = "bold" if (c + 1) % 5 == 0 or c == 0 else "normal"
        out.append(_t(v.x(c), y0 - 20, c + 1, 11, INK, weight=bold))
        out.append(_t(v.x(c), y0 + h + 20, c + 1, 11, INK, weight=bold))
    for r in range(d.rows):
        bold = "bold" if (r + 1) % 5 == 0 or r == 0 else "normal"
        out.append(_t(x0 - 20, v.y(r), pb.row_name(r), 11, INK, weight=bold))
        out.append(_t(x0 + w + 20, v.y(r), pb.row_name(r), 11, INK, weight=bold))
    return out


def _open_lines(v: _View, d: pb.Design, open_: list) -> list[str]:
    out = []
    for o in open_:
        groups = [[d.pin_hole(*s.rsplit(".", 1)) for s in g] for g in o["groups"]]
        for a, b in zip(groups, groups[1:]):
            pa, pb_ = min(((x, y) for x in a for y in b), key=lambda q: abs(q[0][0] - q[1][0]) + abs(q[0][1] - q[1][1]))
            (x1, y1), (x2, y2) = v.pt(pa), v.pt(pb_)
            out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{OPEN}" stroke-width="2.2" stroke-dasharray="6 4"/>')
    return out


def _jumper_names(d: pb.Design) -> dict[int, str]:
    out, k = {}, 0
    for i, w in enumerate(d.wires):
        if w.side == "jumper":
            k += 1
            out[i] = f"W{k}"
    return out


def _pads(v: _View, d: pb.Design, r_pad: float) -> list[str]:
    out = []
    for c in range(d.cols):
        for r in range(d.rows):
            x, y = v.pt((c, r))
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * r_pad:.1f}" fill="{COPPER}" stroke="{COPPER_EDGE}" stroke-width="1"/>')
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * 0.11:.1f}" fill="{HOLE_FILL}"/>')
    return out


def _tracks(v: _View, d: pb.Design, colors, side: str, ghost: bool = False) -> list[str]:
    """Solder tracks on one side; ghost: the other side's, thin and dashed, to orient by."""
    out = []
    for w in d.wires:
        if w.side != side:
            continue
        col = colors.get(w.net, "#888")
        pts = " ".join(f"{v.x(h[0]):.1f},{v.y(h[1]):.1f}" for h in w.path)
        if ghost:
            out.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" stroke-dasharray="1 5" '
                       f'stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>')
            continue
        out.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="{P * 0.34:.1f}" stroke-linecap="round" stroke-linejoin="round"/>')
        for h in pb.wire_holes(w):
            x, y = v.pt(h)
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * 0.24:.1f}" fill="{col}"/>')
    return out


def _via_names(d: pb.Design) -> list[tuple[str, str, tuple[int, int]]]:
    if d.pads != "double-unplated":
        return []
    return [(f"V{i + 1}", n, h) for i, (n, h) in enumerate(pb.vias(d))]


def _vias(v: _View, d: pb.Design, colors) -> list[str]:
    out = []
    for name, net, h in _via_names(d):
        x, y = v.pt(h)
        s = P * 0.36
        col = colors.get(net, "#888")
        out.append(f'<rect x="{x - s:.1f}" y="{y - s:.1f}" width="{2 * s:.1f}" height="{2 * s:.1f}" fill="none" stroke="{col}" '
                   f'stroke-width="2.4" transform="rotate(45 {x:.1f} {y:.1f})"/>')
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * 0.1:.1f}" fill="#fff" stroke="#222" stroke-width="1"/>')
        out.append(_halo(x - P * 0.42, y + P * 0.42, name, 9, col))
    return out


_PADS_TEXT = {"single": "pads on the bottom only", "double": "pads both sides, plated through (a hole's two pads are joined)",
              "double-unplated": "pads both sides, not plated (diamond V1…: via wire, soldered both sides)"}


def _top(v: _View, d: pb.Design, colors, open_) -> list[str]:
    sub = "parts, top tracks and insulated jumper wires, seen from above" if d.double else "parts and insulated jumper wires, seen from above"
    out = _frame(v, "TOP · component side", sub)
    pin_holes = d.pin_owner()
    if d.double:
        out += _pads(v, d, 0.3)
    else:
        for c in range(d.cols):
            for r in range(d.rows):
                x, y = v.pt((c, r))
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * 0.15:.1f}" fill="{HOLE_FILL}" stroke="{HOLE_EDGE}" stroke-width="1"/>')
    out += _tracks(v, d, colors, "top")
    out += _tracks(v, d, colors, "bottom", ghost=True) if d.pads == "double-unplated" else []
    net_of = d.net_of_pin()
    labels = []
    for p in sorted(d.parts.values(), key=lambda q: q.ref):
        fill, tc = KIND_FILL.get(p.d.kind, KIND_FILL["generic"])
        for s in p.d.body:
            out.append(_shape_svg(v, p, s, fill, "#333", 1.3, ' fill-opacity="0.92"'))
        out += _marks_svg(v, p, tc)
        x0, y0, x1, y1 = _body_box(v, p)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        tall0 = (y1 - y0) > (x1 - x0) * 1.6 and (x1 - x0) < P * 1.6
        two0 = p.value and (max(x1 - x0, y1 - y0) > P * 2.2)
        lw = max(len(p.ref), len(p.value) if two0 else 0) * 7.5 + 4
        lh = 28 if two0 else 15
        if tall0 and (x1 - x0) < P * 1.2:  # one hole wide and standing up: label beside it, upright
            labels.append(_halo(x1 + 5 + len(p.ref) * 3.6, cy - (6 if p.value else 0), p.ref, 12, INK))
            if p.value:
                labels.append(_halo(x1 + 5 + len(p.value) * 3.2, cy + 8, p.value, 10, INK, weight="normal"))
            continue
        cx, cy = _label_spot(v, p, cx, cy, lh if tall0 else lw, lw if tall0 else lh)
        tall = (y1 - y0) > (x1 - x0) * 1.6 and (x1 - x0) < P * 1.6
        rot = f' transform="rotate(-90 {cx:.1f} {cy:.1f})"' if tall else ""
        two = p.value and (max(x1 - x0, y1 - y0) > P * 2.2)
        if two:
            labels.append(_t(cx, cy - 6.5, p.ref, 12, tc, weight="bold", extra=rot) if not tall else
                          _t(cx - 6.5, cy, p.ref, 12, tc, weight="bold", extra=f' transform="rotate(-90 {cx - 6.5:.1f} {cy:.1f})"'))
            labels.append(_t(cx, cy + 7, p.value, 10.5, tc, extra=rot) if not tall else
                          _t(cx + 7, cy, p.value, 10.5, tc, extra=f' transform="rotate(-90 {cx + 7:.1f} {cy:.1f})"'))
        else:
            labels.append(_t(cx, cy, p.ref, 12, tc, weight="bold", extra=rot))
    # jumpers
    names = _jumper_names(d)
    for i, w in enumerate(d.wires):
        if w.side != "jumper":
            continue
        col = colors.get(w.net, "#888")
        (x1, y1), (x2, y2) = v.pt(w.path[0]), v.pt(w.path[-1])
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#222" stroke-width="{P * 0.3:.1f}" stroke-linecap="round"/>')
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{col}" stroke-width="{P * 0.22:.1f}" stroke-linecap="round"/>')
        for x, y in ((x1, y1), (x2, y2)):
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * 0.12:.1f}" fill="#fff" stroke="#222" stroke-width="1"/>')
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        out.append(f'<rect x="{mx - 13:.1f}" y="{my - 8:.1f}" width="26" height="16" rx="4" fill="#fff" stroke="{col}" stroke-width="1.5"/>')
        out.append(_t(mx, my, names[i], 10, INK, weight="bold"))
    out += _vias(v, d, colors)
    # pins on top of bodies
    for h, (ref, pin) in pin_holes.items():
        x, y = v.pt(h)
        col = colors.get(net_of.get((ref, pin)), "#9a9a9a")
        if pin == "1":
            s = P * 0.2
            out.append(f'<rect x="{x - s:.1f}" y="{y - s:.1f}" width="{2 * s:.1f}" height="{2 * s:.1f}" fill="#d8d8d8" stroke="{col}" stroke-width="2.6"/>')
        else:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * 0.19:.1f}" fill="#d8d8d8" stroke="{col}" stroke-width="2.6"/>')
    out += labels
    out += _open_lines(v, d, open_)
    return out


def _bottom(v: _View, d: pb.Design, colors, open_) -> list[str]:
    out = _frame(v, "BOTTOM · solder side (board turned over, left-right mirrored)",
                 "solder bridges and bare wire pad to pad; dashed: wiring on the other side")
    out += _pads(v, d, 0.34)
    # ghost parts
    for p in d.parts.values():
        for s in p.d.body:
            out.append(_shape_svg(v, p, s, "none", "#6f6f6f", 1.2, ' stroke-dasharray="4 3" opacity="0.8"'))
    out += _tracks(v, d, colors, "bottom")
    out += _tracks(v, d, colors, "top", ghost=True)
    out += _vias(v, d, colors)
    names = _jumper_names(d)
    jumper_labels = []
    for i, w in enumerate(d.wires):
        if w.side != "jumper":
            continue
        col = colors.get(w.net, "#888")
        (x1, y1), (x2, y2) = v.pt(w.path[0]), v.pt(w.path[-1])
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{col}" stroke-width="2" stroke-dasharray="2 5" stroke-linecap="round"/>')
        for x, y in ((x1, y1), (x2, y2)):
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * 0.3:.1f}" fill="none" stroke="{col}" stroke-width="2.4"/>')
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * 0.1:.1f}" fill="#fff"/>')
        jumper_labels.append(_halo((x1 + x2) / 2 + (10 if x1 == x2 else 0), (y1 + y2) / 2 - (0 if x1 == x2 else 10), names[i], 10, col))
    # pins: lead ends, with the pin number
    net_of = d.net_of_pin()
    for h, (ref, pin) in d.pin_owner().items():
        x, y = v.pt(h)
        col = colors.get(net_of.get((ref, pin)), "#777")
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{P * 0.16:.1f}" fill="#f4f4f4" stroke="#222" stroke-width="1.6"/>')
        out.append(_halo(x + P * 0.36, y - P * 0.33, f"{ref}.{pin}", 8.5, "#111"))
    for p in d.parts.values():
        x0, y0, x1, y1 = _body_box(v, p)
        cx, cy = _label_spot(v, p, (x0 + x1) / 2, (y0 + y1) / 2, len(p.ref) * 7 + 4, 14)
        out.append(_halo(cx, cy, p.ref, 11, "#555"))
    out += jumper_labels
    out += _open_lines(v, d, open_)
    return out


def _legend(d: pb.Design, colors, open_, x, y, width) -> tuple[list[str], float]:
    out = [_t(x, y + 10, "Nets", 14, INK, "start", "bold")]
    counts = {n: len(rps) for n, rps in d.nets.items()}
    col_w = 170
    per_row = max(1, int(width // col_w))
    for i, n in enumerate(sorted(d.nets)):
        cx = x + (i % per_row) * col_w
        cy = y + 32 + (i // per_row) * 22
        out.append(f'<rect x="{cx:.1f}" y="{cy - 6:.1f}" width="24" height="12" rx="3" fill="{colors[n]}"/>')
        out.append(_t(cx + 32, cy, f"{n} ({counts[n]} pins)", 11.5, INK, "start"))
    rows = math.ceil(len(d.nets) / per_row) if d.nets else 0
    yy = y + 32 + rows * 22 + 8
    jumpers = sum(1 for w in d.wires if w.side == "jumper")
    runs = sum(1 for w in d.wires if w.side == "bottom")
    top = sum(1 for w in d.wires if w.side == "top")
    nv = len(_via_names(d))
    bits = [f"{len(d.parts)} parts", f"{runs} bottom runs"] + ([f"{top} top runs"] if d.double else []) + \
        [f"{jumpers} jumper wires (W1…)"] + ([f"{nv} via wires (V1…)"] if d.pads == "double-unplated" else [])
    summary = " · ".join(bits) + f" · board {d.cols} × {d.rows} holes, 2.54 mm pitch, {_PADS_TEXT[d.pads]}"
    out.append(_t(x, yy, summary, 11.5, "#444", "start"))
    yy += 20
    out.append(_t(x, yy, "Square pin = pin 1.  Row letters and column numbers match on both sides: the bottom is mirrored, not renumbered.", 11.5, "#444", "start"))
    yy += 20
    if open_:
        out.append(f'<line x1="{x:.1f}" y1="{yy:.1f}" x2="{x + 24:.1f}" y2="{yy:.1f}" stroke="{OPEN}" stroke-width="2.2" stroke-dasharray="6 4"/>')
        out.append(_t(x + 32, yy, "Not connected yet: " + ", ".join(o["net"] for o in open_), 11.5, OPEN, "start", "bold"))
        yy += 20
    return out, yy - y


def svg(design, open_: list | None = None) -> str:
    d = design if isinstance(design, pb.Design) else pb.load(design)
    if open_ is None:
        open_ = pb.connectivity(d)["open"]
    colors = net_colors(d)
    pw = d.cols * P + 2 * M
    ph = d.rows * P + 2 * M + HEAD
    gap = 50.0
    pad = 24.0
    side_by_side = d.cols <= d.rows * 1.5
    if side_by_side:
        top_v = _View(d, pad, pad + 40, False)
        bot_v = _View(d, pad + pw + gap, pad + 40, True)
        width = 2 * pw + gap + 2 * pad
        legend_y = pad + 40 + ph + 16
    else:
        top_v = _View(d, pad, pad + 40, False)
        bot_v = _View(d, pad, pad + 40 + ph + gap, True)
        width = pw + 2 * pad
        legend_y = pad + 40 + 2 * ph + gap + 16
    leg, leg_h = _legend(d, colors, open_, pad + M, legend_y, width - 2 * pad - M)
    height = legend_y + leg_h + pad
    title = d.title or "Protoboard layout"
    body = [f'<rect x="0" y="0" width="{width:.0f}" height="{height:.0f}" fill="#ffffff"/>',
            _t(pad + M, pad + 10, title, 20, INK, "start", "bold")]
    body += _top(top_v, d, colors, open_)
    body += _bottom(bot_v, d, colors, open_)
    body += leg
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
            f'viewBox="0 0 {width:.0f} {height:.0f}">\n' + "\n".join(body) + "\n</svg>\n")


def build_list(design, open_: list | None = None) -> str:
    """The same layout as steps: parts with their holes, jumpers, then bottom wiring net by net."""
    d = design if isinstance(design, pb.Design) else pb.load(design)
    if open_ is None:
        open_ = pb.connectivity(d)["open"]
    L = [f"# {d.title or 'Protoboard layout'}", "",
         f"Perfboard, {d.cols} columns x {d.rows} rows, 2.54 mm pitch, {_PADS_TEXT[d.pads]}. Holes are row letter + column number, "
         "A1 at the top left seen from the component side. On the solder side A1 is at the top right.", "", "## 1. Parts (top)", ""]

    def key(p):
        return (min(h[1] for h in p.pin_holes().values()), min(h[0] for h in p.pin_holes().values()))

    for p in sorted(d.parts.values(), key=key):
        pins = ", ".join(f"{k}:{pb.hole_name(h)}" for k, h in p.pin_holes().items())
        v = f" {p.value}" if p.value else ""
        L.append(f"- **{p.ref}**{v} ({p.d.name}, rotation {p.rotation}): pins {pins}")
        for n in p.d.notes:
            L.append(f"  - {n}")
    names = _jumper_names(d)
    step = 1
    jumpers = [(names[i], w) for i, w in enumerate(d.wires) if w.side == "jumper"]
    vias = _via_names(d)
    if vias:
        step += 1
        L += ["", f"## {step}. Via wires (unplated board)", "",
              "Push a short bare wire through the hole and solder it on both sides, so the top and bottom pads join.", ""]
        for n, net, h in vias:
            L.append(f"- **{n}** {net}: {pb.hole_name(h)}")
    if jumpers:
        step += 1
        L += ["", f"## {step}. Jumper wires (top, insulated)", ""]
        for n, w in jumpers:
            L.append(f"- **{n}** {w.net}: {pb.hole_name(w.path[0])} to {pb.hole_name(w.path[-1])} ({len(pb.wire_top_cover(w)) - 1} holes)")
    note = "A run of one hole is a solder bridge between neighbouring pads; longer runs are bare tinned wire soldered at every pad."
    for side, title in (("top", "Top wiring (component side)"), ("bottom", "Bottom wiring (solder side)")):
        by_net: dict[str, list] = {}
        for w in d.wires:
            if w.side == side:
                by_net.setdefault(w.net, []).append(w)
        if not by_net and side == "top":
            continue
        step += 1
        L += ["", f"## {step}. {title}", "", note, ""]
        for net in sorted(by_net):
            parts = []
            for w in by_net[net]:
                segs = " → ".join(pb.hole_name(h) for h in w.path)
                n = len(pb.wire_holes(w)) - 1
                parts.append(f"{segs} ({'bridge' if n == 1 else f'wire over {n + 1} pads'})")
            L.append(f"- **{net}**: " + "; ".join(parts))
    if open_:
        L += ["", "## Still open", ""]
        for o in open_:
            L.append(f"- **{o['net']}**: " + " | ".join(", ".join(g) for g in o["groups"]))
    L += ["", "## Nets", ""]
    for n in sorted(d.nets):
        L.append(f"- **{n}**: " + ", ".join(f"{r}.{p}" for r, p in d.nets[n]))
    return "\n".join(L) + "\n"


def render(design, out: str | Path, width: int = 1800) -> dict:
    """Write <out>.svg, <out>.png and <out>-build.md. out is a path without extension (or with .png)."""
    from .kicad_live import LiveError, _svg_to_png

    d = design if isinstance(design, pb.Design) else pb.load(design)
    conn = pb.connectivity(d)
    base = Path(out)
    if base.suffix.lower() in (".png", ".svg"):
        base = base.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)
    s, p, md = base.with_suffix(".svg"), base.with_suffix(".png"), base.parent / f"{base.name}-build.md"
    s.write_text(svg(d, conn["open"]), encoding="utf-8")
    md.write_text(build_list(d, conn["open"]), encoding="utf-8")
    result = {"svg": str(s), "build_list": str(md), "open": conn["open"], "issues": conn["issues"]}
    try:
        tmp = base.parent / f".{base.name}-for-png.svg"  # the converter rewrites the size, keep the SVG as drawn
        tmp.write_text(s.read_text(encoding="utf-8"), encoding="utf-8")
        _svg_to_png(tmp, p, width)
        tmp.unlink(missing_ok=True)
        result["png"] = str(p)
    except (LiveError, OSError) as e:
        result["png_error"] = str(e)
    return result

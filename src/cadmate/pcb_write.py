"""Start a board from a schematic: every part's footprint, with its pads on the schematic's nets, on a new .kicad_pcb.

This is KiCad's "Update PCB from Schematic" for a board that doesn't exist yet, done from the command line so it
works without the PCB editor open. The parts come from KiCad's own netlist of the sheet (kicad-cli), so hierarchy,
global labels and power symbols join nets exactly as KiCad joins them. Footprints are found as KiCad finds them
(fp-lib-table, then KiCad's installed libraries).

The first placement follows the schematic: parts sit where their symbols sit on the sheet, scaled down, then nudged
apart until no courtyards overlap. Parts drawn near each other on the schematic (a regulator and its caps, an MCU and
its decoupling) start near each other on the board. It is a starting point for placement, not a placement: the
placement help and the person's own judgement come next. A board outline goes around the parts, or the size given.

Each footprint keeps the link to its schematic symbol, so KiCad's Update PCB from Schematic works on the board later
and DRC's schematic parity check passes. Units are mm in KiCad's board frame (Y down).
"""

from __future__ import annotations

import math
import re
import subprocess
import tempfile
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path

from . import apps, kicad_libs, project, sexpr

PCB_FORMAT = "20250926"  # (kicad_pcb ...) that KiCad 10 reads
NS = uuidlib.UUID("2d6b8f1e-7c3a-5b90-8e4d-1a9f6c2e7b30")  # cadmate's namespace for board UUIDs
GAP = 1.0  # mm kept between courtyards in the first placement
ORIGIN = (100.0, 100.0)  # where the board's top left corner goes on KiCad's page

LAYERS = {
    2: ['(0 "F.Cu" signal)', '(2 "B.Cu" signal)'],
    4: ['(0 "F.Cu" signal)', '(4 "In1.Cu" signal)', '(6 "In2.Cu" signal)', '(2 "B.Cu" signal)'],
}
USER_LAYERS = ['(9 "F.Adhes" user "F.Adhesive")', '(11 "B.Adhes" user "B.Adhesive")', '(13 "F.Paste" user)',
               '(15 "B.Paste" user)', '(5 "F.SilkS" user "F.Silkscreen")', '(7 "B.SilkS" user "B.Silkscreen")',
               '(1 "F.Mask" user)', '(3 "B.Mask" user)', '(17 "Dwgs.User" user "User.Drawings")',
               '(19 "Cmts.User" user "User.Comments")', '(21 "Eco1.User" user "User.Eco1")',
               '(23 "Eco2.User" user "User.Eco2")', '(25 "Edge.Cuts" user)', '(27 "Margin" user)',
               '(31 "F.CrtYd" user "F.Courtyard")', '(29 "B.CrtYd" user "B.Courtyard")', '(35 "F.Fab" user)',
               '(33 "B.Fab" user)']


class BoardWriteError(ValueError):
    pass


@dataclass
class Comp:
    ref: str
    value: str
    footprint: str
    symbol_uuid: str = ""  # the schematic symbol's path, e.g. "/<uuid>", for the board-to-schematic link
    sheet_name: str = "/"
    sheet_file: str = ""
    sch_at: tuple[float, float] | None = None  # where its symbol sits on the schematic
    fields: dict = field(default_factory=dict)


@dataclass(eq=False)
class Placed:
    comp: Comp
    text: str  # the footprint's own .kicad_mod text
    court: tuple[float, float, float, float]  # courtyard box around the footprint's origin
    at: tuple[float, float] = (0.0, 0.0)

    def box(self, gap: float = 0.0) -> tuple[float, float, float, float]:
        c = self.court
        return (self.at[0] + c[0] - gap, self.at[1] + c[1] - gap, self.at[0] + c[2] + gap, self.at[1] + c[3] + gap)


# --- netlist ------------------------------------------------------------------------------------------------------


def netlist(sch: Path) -> tuple[list[Comp], dict[str, list[str]]]:
    """Parts and nets of a schematic (and its sub-sheets) from KiCad's own netlist export."""
    cli = apps.find_kicad_cli()
    if not cli:
        raise BoardWriteError("this needs kicad-cli to read the schematic's netlist; install KiCad 10 "
                              "(or give the parts and nets directly)")
    out = Path(tempfile.mkdtemp(prefix="cadmate-net-")) / "net.net"
    try:
        proc = subprocess.run([cli, "sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(out), str(sch)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                              check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise BoardWriteError(f"kicad-cli netlist export failed: {e}") from None
    if not out.exists():
        raise BoardWriteError(f"kicad-cli wrote no netlist (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-800:]}")
    return parse_netlist(out.read_text(encoding="utf-8", errors="replace"))


def _val(node: sexpr.Node | None) -> str:
    return node.atoms()[0] if node is not None and node.atoms() else ""


def parse_netlist(text: str) -> tuple[list[Comp], dict[str, list[str]]]:
    root = sexpr.parse(text)
    comps = []
    for c in (root.child("components") or sexpr.Node()).children("comp"):
        props = {_val(p.child("name")): _val(p.child("value")) for p in c.children("property")}
        if "exclude_from_board" in props:
            continue
        sp = c.child("sheetpath")
        stamps = _val(c.child("tstamps")) or _val(c.child("tstamp"))
        sheet_stamps = _val(sp.child("tstamps")) if sp is not None else "/"
        comps.append(Comp(ref=_val(c.child("ref")), value=_val(c.child("value")), footprint=_val(c.child("footprint")),
                          symbol_uuid=(sheet_stamps.rstrip("/") + "/" + stamps.split()[0]) if stamps else "",
                          sheet_name=_val(sp.child("names")) if sp is not None else "/",
                          sheet_file=props.get("Sheetfile", ""),
                          fields={k: v for k, v in props.items() if k not in ("Sheetname", "Sheetfile")}))
    nets: dict[str, list[str]] = {}
    for n in (root.child("nets") or sexpr.Node()).children("net"):
        nets[_val(n.child("name"))] = [f"{_val(x.child('ref'))}.{_val(x.child('pin'))}" for x in n.children("node")]
    return comps, nets


def sheet_positions(sch: Path) -> dict[str, tuple[float, float]]:
    """Where each part's symbol sits on the top sheet, by reference (unit 1, or the first unit found)."""
    try:
        root = sexpr.parse(sch.read_text(encoding="utf-8", errors="replace"))
    except (OSError, sexpr.SexprError):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for s in root.children("symbol"):
        at = s.child("at")
        if at is None or len(at.floats()) < 2:
            continue
        ref = ""
        for inst in s.children("instances"):
            for proj in inst.children("project"):
                for path in proj.children("path"):
                    ref = ref or _val(path.child("reference"))
        if not ref:
            ref = next((p.atoms()[1] for p in s.children("property") if p.atoms()[:1] == ["Reference"] and len(p.atoms()) > 1), "")
        if ref and not ref.startswith("#"):
            out.setdefault(ref, (at.floats()[0], at.floats()[1]))
    return out


# --- footprints ---------------------------------------------------------------------------------------------------


def _courtyard(fp: sexpr.Node) -> tuple[float, float, float, float]:
    """The footprint's courtyard box (front or back), else its pads with 0.25 mm around them."""
    pts = []
    for g in fp.items[1:]:
        if not isinstance(g, sexpr.Node) or not (g.head or "").startswith("fp_"):
            continue
        lay = _val(g.child("layer"))
        if not lay.endswith("CrtYd"):
            continue
        for k in ("start", "end", "center", "mid"):
            n = g.child(k)
            if n is not None and len(n.floats()) >= 2:
                pts.append(tuple(n.floats()[:2]))
        if g.head == "fp_circle" and g.child("center") is not None and g.child("end") is not None:
            (cx, cy), (ex, ey) = g.child("center").floats()[:2], g.child("end").floats()[:2]
            r = math.hypot(ex - cx, ey - cy)
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
        pl = g.child("pts")
        if pl is not None:
            pts += [tuple(xy.floats()[:2]) for xy in pl.children("xy")]
    if pts:
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    for p in fp.children("pad"):
        at, size = p.child("at"), p.child("size")
        if at is None or size is None:
            continue
        x, y = at.floats()[:2]
        w, h = size.floats()[:2]
        if len(at.floats()) > 2 and round(at.floats()[2]) % 180 == 90:
            w, h = h, w
        pts += [(x - w / 2 - 0.25, y - h / 2 - 0.25), (x + w / 2 + 0.25, y + h / 2 + 0.25)]
    if not pts:
        return (-1.0, -1.0, 1.0, 1.0)
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _instance(p: Placed, pad_nets: dict[str, str], board_uuid: uuidlib.UUID) -> str:
    """The footprint as it sits on the board: placed, named, linked to its symbol, its pads on their nets."""
    q = sexpr.quote
    text = p.text
    root = sexpr.parse(text)
    c = p.comp
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement) in text
    drop = ("version", "generator", "generator_version", "tedit", "uuid", "at", "path", "sheetname", "sheetfile",
            "layer")
    have_props = set()
    for item in root.items[2:]:
        if not isinstance(item, sexpr.Node):
            continue
        if item.head in drop:
            edits.append((item.start, item.end, ""))
        elif item.head == "property" and item.atoms():
            name = item.atoms()[0]
            have_props.add(name)
            new = {"Reference": c.ref, "Value": c.value, "Footprint": c.footprint}.get(name)
            if new is not None:
                # the value is the second atom: replace the quoted string after the name
                m = re.match(r'\(property\s+"(?:[^"\\]|\\.)*"\s+"(?:[^"\\]|\\.)*"', text[item.start:item.end])
                if m:
                    edits.append((item.start, item.start + m.end(), f"(property {q(name)} {q(new)}"))
        elif item.head == "pad" and item.atoms():
            net = pad_nets.get(item.atoms()[0])
            old = item.child("net")
            if old is not None:
                edits.append((old.start, old.end, ""))
            if net is not None:
                edits.append((item.end - 1, item.end - 1, f" (net {q(net)})"))
    edits.sort(key=lambda e: e[0], reverse=True)
    for a, b, r in edits:
        text = text[:a] + r + text[b:]
    # new UUIDs for the footprint and everything in it: two R_0603s must not share them
    n = [0]

    def fresh(_m: re.Match) -> str:
        n[0] += 1
        return f'(uuid "{uuidlib.uuid5(board_uuid, f"fp:{c.ref}:{n[0]}")}")'
    text = re.sub(r'\(uuid\s+"?[0-9a-fA-F-]+"?\s*\)', fresh, text)
    head = re.match(r'\(footprint\s+(?:"(?:[^"\\]|\\.)*"|\S+)', text)
    if head is None:
        raise BoardWriteError(f"{c.ref}: {c.footprint} isn't a footprint file")
    extra = ['(layer "F.Cu")', f'(uuid "{uuidlib.uuid5(board_uuid, f"fp:{c.ref}")}")',
             f"(at {_fmt(p.at[0])} {_fmt(p.at[1])})"]
    for name, val in (("Reference", c.ref), ("Value", c.value), ("Footprint", c.footprint)):
        if name not in have_props:
            hide = "" if name == "Reference" else " (hide yes)"
            layer = "F.SilkS" if name == "Reference" else "F.Fab"
            extra.append(f"(property {q(name)} {q(val)} (at 0 0 0) (layer {q(layer)}){hide} "
                         f"(effects (font (size 1 1) (thickness 0.15))))")
    if c.symbol_uuid:
        extra += [f"(path {q(c.symbol_uuid)})", f"(sheetname {q(c.sheet_name or '/')})",
                  f"(sheetfile {q(c.sheet_file)})"]
    body = text[head.end():]
    return f"\t(footprint {q(c.footprint)}\n\t\t" + "\n\t\t".join(extra) + body.rstrip() + "\n"


# --- placement ----------------------------------------------------------------------------------------------------


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _place(parts: list[Placed], scale: float) -> None:
    """Parts where the schematic has them, scaled down, then each one moved to the nearest free spot in turn
    (biggest first), so nothing overlaps. Parts with no position on the top sheet go in rows below."""
    with_pos = [p for p in parts if p.comp.sch_at is not None]
    without = [p for p in parts if p.comp.sch_at is None]
    if with_pos:
        x0 = min(p.comp.sch_at[0] for p in with_pos)
        y0 = min(p.comp.sch_at[1] for p in with_pos)
        for p in with_pos:
            p.at = (round((p.comp.sch_at[0] - x0) * scale * 2) / 2, round((p.comp.sch_at[1] - y0) * scale * 2) / 2)
    done: list[Placed] = []
    for p in sorted(with_pos, key=lambda p: -(p.court[2] - p.court[0]) * (p.court[3] - p.court[1])):
        want = p.at
        for r in range(400):  # rings of 0.5 mm steps around where it wants to be
            ring = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1) if max(abs(dx), abs(dy)) == r]
            ring.sort(key=lambda d: (d[0] ** 2 + d[1] ** 2, d))
            spot = None
            for dx, dy in ring:
                p.at = (want[0] + dx * 0.5, want[1] + dy * 0.5)
                if not any(_overlaps(p.box(GAP / 2), q.box(GAP / 2)) for q in done):
                    spot = p.at
                    break
            if spot is not None:
                break
        done.append(p)
    # the rest in rows under what's placed
    y = max((q.box()[3] for q in done), default=0.0) + 5.0
    x, row_h = 0.0, 0.0
    for p in sorted(without, key=lambda p: p.comp.ref):
        w, h = p.court[2] - p.court[0], p.court[3] - p.court[1]
        if x > 0 and x + w > 80:
            x, y, row_h = 0.0, y + row_h + GAP, 0.0
        p.at = (round((x - p.court[0]) * 2) / 2, round((y - p.court[1]) * 2) / 2)
        x += w + GAP
        row_h = max(row_h, h)
        done.append(p)


# --- the board ----------------------------------------------------------------------------------------------------


def write(out: str, comps: list[Comp], nets: dict[str, list[str]], libraries: dict[str, str] | None = None,
          size: list[float] | None = None, layers: int = 2, scale: float = 0.5, replace: bool = False) -> dict:
    """Write a new board with these parts on these nets. See board_from_schematic for the usual way in."""
    out_path = Path(out).expanduser()
    if out_path.suffix != ".kicad_pcb":
        raise BoardWriteError("the board path must end in .kicad_pcb")
    if layers not in LAYERS:
        raise BoardWriteError("layers must be 2 or 4")
    if out_path.exists():
        if not replace:
            raise BoardWriteError(f"{out_path} exists; this starts new boards. To bring schematic changes into a board "
                                  f"you've placed, use Update PCB from Schematic in KiCad")
        if not _written_by_cadmate(out_path):
            raise BoardWriteError(f"{out_path} wasn't started by cadmate, so writing over it would lose someone's "
                                  f"work; use Update PCB from Schematic in KiCad")
    fps = kicad_libs.Footprints(out_path.parent, libraries)
    placed: list[Placed] = []
    no_footprint, not_found = [], []
    for c in comps:
        if not c.footprint:
            no_footprint.append(c.ref)
            continue
        try:
            f = fps.get(c.footprint)
            text = f.read_text(encoding="utf-8", errors="replace")
            court = _courtyard(sexpr.parse(text))
        except (kicad_libs.LibraryError, OSError, sexpr.SexprError) as e:
            not_found.append(f"{c.ref}: {e}")
            continue
        placed.append(Placed(comp=c, text=text, court=court))
    if not placed:
        raise BoardWriteError("no footprints to place: " + "; ".join((no_footprint and [f"no footprint field on {no_footprint}"] or []) + not_found[:5]))
    _place(placed, scale)

    # board outline: around the parts with a margin, or the size asked for, parts centred in it
    bx0 = min(p.box()[0] for p in placed)
    by0 = min(p.box()[1] for p in placed)
    bx1 = max(p.box()[2] for p in placed)
    by1 = max(p.box()[3] for p in placed)
    margin = 3.0
    if size:
        w, h = float(size[0]), float(size[1])
        fits = w >= bx1 - bx0 and h >= by1 - by0
        ox = ORIGIN[0] + (w - (bx1 - bx0)) / 2 - bx0
        oy = ORIGIN[1] + (h - (by1 - by0)) / 2 - by0
    else:
        w, h = math.ceil(bx1 - bx0 + 2 * margin), math.ceil(by1 - by0 + 2 * margin)
        ox, oy = ORIGIN[0] + margin - bx0, ORIGIN[1] + margin - by0
        fits = True
    for p in placed:
        p.at = (round(p.at[0] + ox, 3), round(p.at[1] + oy, 3))

    pad_nets: dict[str, dict[str, str]] = {}
    for net, pins in nets.items():
        for spec in pins:
            ref, _, pin = spec.partition(".")
            pad_nets.setdefault(ref, {})[pin] = net
    board_uuid = _board_uuid(out_path)
    q = sexpr.quote
    lines = [f"(kicad_pcb\n\t(version {PCB_FORMAT})\n\t(generator \"cadmate\")\n\t(generator_version \"1.0\")",
             "\t(general (thickness 1.6) (legacy_teardrops no))", '\t(paper "A4")',
             "\t(layers\n\t\t" + "\n\t\t".join(LAYERS[layers] + USER_LAYERS) + "\n\t)",
             "\t(setup (pad_to_mask_clearance 0) (allow_soldermask_bridges_in_footprints no))"]
    for p in placed:
        lines.append(_instance(p, pad_nets.get(p.comp.ref, {}), board_uuid))
    lines.append(f"\t(gr_rect (start {_fmt(ORIGIN[0])} {_fmt(ORIGIN[1])}) (end {_fmt(ORIGIN[0] + w)} {_fmt(ORIGIN[1] + h)}) "
                 f"(stroke (width 0.1) (type default)) (fill no) (layer \"Edge.Cuts\") "
                 f"(uuid {q(str(uuidlib.uuid5(board_uuid, 'outline')))}))")
    lines.append("\t(embedded_fonts no)\n)\n")
    text = "\n".join(lines)
    sexpr.parse(text)

    # every pad that should be on a net is, and no other
    missing = _check_pads(text, pad_nets, {p.comp.ref for p in placed})
    if missing:
        raise BoardWriteError("the board doesn't match the nets, so nothing was written (a cadmate bug: please report "
                              "it): " + "; ".join(missing[:5]))

    kept = project.keep([out_path], "board-write")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8", newline="\n")
    result = {"ok": True, "path": str(out_path), "footprints": len(placed), "nets": len([n for n in nets if n]),
              "board_mm": [w, h], "layers": layers,
              "next": "check the parity and DRC below, then place the parts: critical parts first (decoupling at its "
                      "pins, crystal at its MCU, connectors at the edge); the routing tools come after placement"}
    if kept:
        result["previous_kept"] = kept
    if not fits:
        result["too_small"] = f"the parts need about {bx1 - bx0:.1f} x {by1 - by0:.1f} mm before any routing room"
    if no_footprint:
        result["no_footprint"] = no_footprint
        result["no_footprint_note"] = "these symbols have no Footprint field, so they aren't on the board"
    if not_found:
        result["footprint_not_found"] = not_found
    unpadded = sorted(f"{ref}.{pin}" for ref, pins in pad_nets.items() if ref in {p.comp.ref for p in placed}
                      for pin in pins if not _has_pad(next(p for p in placed if p.comp.ref == ref).text, pin))
    if unpadded:
        result["pins_without_pads"] = unpadded[:40]
        result["pins_without_pads_note"] = "the footprint has no pad with this pin's number: wrong footprint, or pins " \
                                           "numbered differently from the pads"
    return result


def _has_pad(text: str, num: str) -> bool:
    return any(p.atoms()[:1] == [num] for p in sexpr.parse(text).children("pad"))


def _check_pads(text: str, pad_nets: dict[str, dict[str, str]], refs: set[str]) -> list[str]:
    root = sexpr.parse(text)
    problems = []
    for fp in root.children("footprint"):
        ref = next((p.atoms()[1] for p in fp.children("property") if p.atoms()[:1] == ["Reference"]), "")
        want = pad_nets.get(ref, {})
        for pad in fp.children("pad"):
            num = pad.atoms()[0] if pad.atoms() else ""
            got = _val(pad.child("net")) or None
            if got != want.get(num):
                problems.append(f"{ref} pad {num} is on {got!r}, should be on {want.get(num)!r}")
    return problems


def _written_by_cadmate(path: Path) -> bool:
    try:
        g = sexpr.parse(path.read_text(encoding="utf-8", errors="replace")).child("generator")
    except (OSError, sexpr.SexprError):
        return False
    return g is not None and g.atoms()[:1] == ["cadmate"]


def _board_uuid(path: Path) -> uuidlib.UUID:
    return uuidlib.uuid5(NS, "board:" + path.stem)


def from_schematic(sch: str, out: str | None = None, libraries: dict[str, str] | None = None,
                   size: list[float] | None = None, layers: int = 2, scale: float = 0.5, replace: bool = False,
                   drc: bool = True) -> dict:
    sch_path = Path(sch).expanduser()
    if sch_path.suffix != ".kicad_sch" or not sch_path.is_file():
        raise BoardWriteError(f"{sch_path}: give the project's root .kicad_sch")
    out_path = Path(out).expanduser() if out else sch_path.with_suffix(".kicad_pcb")
    comps, nets = netlist(sch_path)
    pos = sheet_positions(sch_path)
    for c in comps:
        c.sch_at = pos.get(c.ref)
        c.sheet_file = c.sheet_file or sch_path.name
    result = write(str(out_path), comps, nets, libraries=libraries, size=size, layers=layers, scale=scale,
                   replace=replace)
    if drc:
        from . import pcb_review
        rep = pcb_review.run_drc(out_path)
        if rep is None:
            result["drc"] = "kicad-cli not found"
        elif "error" in rep:
            result["drc"] = rep["error"]
        else:
            parity = rep.get("schematic_parity", [])
            result["parity"] = {"issues": len(parity),
                                "examples": [v.get("description", "") for v in parity[:5]]}
            result["drc"] = pcb_review.drc_summary(rep)
            result["drc_note"] = "unconnected items are expected before routing; parity issues are not"
    return result

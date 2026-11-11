"""What a board house can make: its stated minimums, and the same with a safety buffer.

A fab's minimum is a floor, not a target. At the limit the board can still be made, but the yield drops. So each limit has
two levels. Below the hard one (the fab's own number) is an error. Between the two is allowed but listed, for the designer
to keep only where it is really needed. Values are for 1 oz outer copper, standard service, read from the fabs' capability
pages (JLCPCB and PCBWay October 2026; OSH Park, AISLER and Eurocircuits November 2026). Each house's figures and where
they come from are in SOURCES; check the page before ordering, since houses change them.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from . import versions

MARGIN = 0.25  # the buffer over a fab's minimum: 25 %, so 0.10 mm becomes 0.125 mm

SOURCES = {
    "jlcpcb": "JLCPCB PCB capabilities (jlcpcb.com/capabilities/pcb-capabilities): trace width/spacing 0.10/0.10 mm on 1-2 "
              "layers and 0.09/0.09 mm multilayer (1 oz); pad to track 'Min. 0.1 mm (stay well above if possible)'; via hole to "
              "via hole 0.2 mm, pad hole to pad hole 0.45 mm; copper to routed edge ≥0.2 mm; 'Preferred Min. Via hole size: "
              "0.2mm', and a 0.2-0.25 mm hole with a via pad under 0.45 mm costs more.",
    "pcbway": "PCBWay capabilities (pcbway.com/capabilities.html): standard table 'Min manufacturable trace is 4mil(0.1mm)', "
              "'Min manufacturable spacing is 4mil(0.1mm)'; drills under 0.2 mm cost extra; annular ring 0.15 mm; advanced table, "
              "normal process: outer 1 oz width/spacing 5/6 mil, via hole to hole ≥11 mil, component hole to hole ≥16 mil, line to "
              "CNC-milled edge 0.25 mm. The via pad (0.5 mm) is cadmate's: 0.2 mm drill plus the 0.15 mm ring each side.",
    "oshpark": "OSH Park service pages (docs.oshpark.com/services/two-layer/, /four-layer/, /six-layer/): 2-layer trace width and "
               "spacing 6 mil (0.1524 mm), minimum drill 10 mil (0.254 mm), annular ring 5 mil (0.127 mm); 4- and 6-layer trace "
               "and spacing 5 mil (0.127 mm), ring 4 mil (0.1016 mm), drill 10 mil on 4 layers and 8 mil on 6; copper 15 mil "
               "(0.381 mm) from the board edge; '5 mil (0.127mm) clearance is recommended between holes'. Pad to track isn't "
               "stated, so the spacing is used. Via pads are cadmate's: the drill plus the ring each side. The multilayer row is "
               "the 4-layer one, so a 6-layer board is held to the 10 mil drill.",
    "aisler": "AISLER design rules (community.aisler.net/t/2-layer-1-6-mm-35-m-hasl-design-rules/3735 and "
              "/t/4-layer-35-m-enig-design-rules/3733, updated 07/07/2025): 2-layer HASL trace 200 µm, copper spacing 150 µm, via "
              "drill 0.3 mm with a 200 µm ring, drill spacing 300 µm, copper to edge 300 µm; 4-layer ENIG trace and spacing 125 µm, "
              "via drill 0.25 mm with a 100 µm ring, drill spacing and edge 300 µm. A 2-layer ENIG board allows 125/125 µm and a "
              "0.25 mm drill (community.aisler.net/t/pcb-design-rules/41); cadmate holds 2 layers to the HASL row, since it can't "
              "tell the finish. Pad to track isn't stated, so the spacing is used. Via pads are cadmate's: the drill plus the "
              "ring each side.",
    "eurocircuits": "Eurocircuits STANDARD pool (eurocircuits.com/services/standard-pool/): pooled track/spacing 0.100 mm, "
                    "smallest finished PTH 0.100 mm, outer pad 'Finished Hole Size + 0.300mm' and inner pad 'Finished Hole "
                    "Size + 0.350mm' (a through via has its pad on every layer, so a multilayer via is +0.35), copper to routed edge 0.250 mm on "
                    "outer layers and 0.400 mm on inner ones; drill to drill 'is 0.25mm (10mil), ... measured edge to edge of the "
                    "drill TOOLSIZE', and 'TOOLSIZE = ENDSIZE + 0.100mm' "
                    "(eurocircuits.com/technical-guidelines/pcb-design-guidelines/classification/ and /drilled-holes/), so "
                    "0.35 mm between finished holes. Price rises with the pattern and drill class; the PCB proto service "
                    "(eurocircuits.com/services/pcb-proto/) takes 0.150 mm track/spacing and 0.250 mm holes, which cadmate uses "
                    "as the preferred values. Pad to track isn't stated, so the spacing is used.",
}

NAMES = {"jlcpcb": "JLCPCB", "pcbway": "PCBWay", "oshpark": "OSH Park", "aisler": "AISLER", "eurocircuits": "Eurocircuits"}

# hard minimums, mm. via_drill and via_diameter are the smallest via at standard price: both fabs drill 0.15 mm for more
PROFILES = {
    "jlcpcb": {"track": 0.10, "clearance": 0.10, "pad_to_track": 0.10, "via_drill": 0.20, "via_diameter": 0.45,
               "via_hole_to_hole": 0.20, "pth_hole_to_hole": 0.45, "edge": 0.20},
    "jlcpcb-multilayer": {"track": 0.09, "clearance": 0.09, "pad_to_track": 0.10, "via_drill": 0.20, "via_diameter": 0.45,
                          "via_hole_to_hole": 0.20, "pth_hole_to_hole": 0.45, "edge": 0.20},
    # PCBWay's via pad is worked out (0.2 mm drill + 2 × its 0.15 mm ring), not quoted
    "pcbway": {"track": 0.10, "clearance": 0.10, "pad_to_track": 0.10, "via_drill": 0.20, "via_diameter": 0.50,
               "via_hole_to_hole": 0.28, "pth_hole_to_hole": 0.41, "edge": 0.25},
    "oshpark": {"track": 0.1524, "clearance": 0.1524, "pad_to_track": 0.1524, "via_drill": 0.254, "via_diameter": 0.508,
                "via_hole_to_hole": 0.127, "pth_hole_to_hole": 0.127, "edge": 0.381},
    "oshpark-multilayer": {"track": 0.127, "clearance": 0.127, "pad_to_track": 0.127, "via_drill": 0.254, "via_diameter": 0.457,
                           "via_hole_to_hole": 0.127, "pth_hole_to_hole": 0.127, "edge": 0.381},
    "aisler": {"track": 0.20, "clearance": 0.15, "pad_to_track": 0.15, "via_drill": 0.30, "via_diameter": 0.70,
               "via_hole_to_hole": 0.30, "pth_hole_to_hole": 0.30, "edge": 0.30},
    "aisler-multilayer": {"track": 0.125, "clearance": 0.125, "pad_to_track": 0.125, "via_drill": 0.25, "via_diameter": 0.45,
                          "via_hole_to_hole": 0.30, "pth_hole_to_hole": 0.30, "edge": 0.30},
    # Eurocircuits sizes holes as finished (ENDSIZE); its 0.25 mm drill spacing is between tools 0.1 mm larger
    "eurocircuits": {"track": 0.10, "clearance": 0.10, "pad_to_track": 0.10, "via_drill": 0.10, "via_diameter": 0.40,
                     "via_hole_to_hole": 0.35, "pth_hole_to_hole": 0.35, "edge": 0.25},
    "eurocircuits-multilayer": {"track": 0.10, "clearance": 0.10, "pad_to_track": 0.10, "via_drill": 0.10, "via_diameter": 0.45,
                                "via_hole_to_hole": 0.35, "pth_hole_to_hole": 0.35, "edge": 0.40},  # inner pad +0.35, edge 0.4
}

# where a fab names a comfortable value of its own, that is the preferred one instead of the margin's
PREFERRED = {
    "pcbway": {"track": 0.127, "clearance": 0.152},  # its normal process, 1 oz outer: 5/6 mil
    "eurocircuits": {"track": 0.15, "clearance": 0.15, "pad_to_track": 0.15, "via_drill": 0.25},  # its PCB proto service's limits
    "*": {"via_drill": 0.30, "via_diameter": 0.60},  # JLCPCB's and PCBWay's standard via; a comfortable one elsewhere too
}

# what a house asks for beside the Gerbers, where its pages say and cadmate has no column format of its own for it
UPLOAD = {
    "aisler": "AISLER takes the .kicad_pcb itself (or the AISLER Push plugin) and matches parts by an MPN field (Mpn, MPN "
              "or AISLER_MPN); upload the generic BOM if they ask for one (community.aisler.net/t/aisler-push-for-kicad/88)",
    "eurocircuits": "Eurocircuits asks for a BOM (designators and manufacturer part numbers) and a CPL (placement list) "
                    "beside the board data: the generic BOM and placement files are those "
                    "(eurocircuits.com/technical-guidelines/assembly-manufacturing-technology/)",
}


def profile(fab: str | None) -> str | None:
    """The rule profile cadmate has for a board house ("jlcpcb", "pcbway", "oshpark", "aisler", "eurocircuits"), or None
    for none or one it doesn't know. Spelling is loose: "OSH Park", "osh-park" and "Euro Circuits" all count."""
    key = re.sub(r"[\s._-]", "", (fab or "").lower())
    return key if key in SOURCES else None


def resolve(fab: str | None, near: str | Path | None = None) -> dict:
    """Which board house a step should check against. An explicit fab wins ("none" means no fab); otherwise the
    project's choice (project_fab); otherwise none. cadmate never assumes one.

    {"fab": profile key or None, "house": as the user named it, "assembly": True/False/None, "distributor",
    "layers", "from": "argument" | "project" | "not asked" | "not in a project"}, plus "ask_user" when the project
    hasn't asked yet. An explicit fab replaces only the house: whether it's assembled, the distributor and the layer
    count still come from the project, since they're about the order, not the house."""
    from . import project  # it imports this module

    root = project.find_root(near) if near else None
    s = project.fab_settings(root) if root else None
    rest = {k: (s or {}).get(k) for k in ("assembly", "distributor", "layers")}
    if fab is not None:
        house = None if fab.strip().lower() in project.UNDECIDED else fab.strip()
        return {"fab": profile(house), "house": house, **rest, "from": "argument"}
    if root is None:
        return {"fab": None, "house": None, **rest, "from": "not in a project"}
    if s is None:
        return {"fab": None, "house": None, **rest, "from": "not asked", "ask_user": project.FAB_QUESTION,
                "then": "record the answers with project_fab"}
    return {"fab": profile(s["house"]), "house": s["house"], **rest, "from": "project"}


def limits(fab: str, layers: int = 2, margin: float = MARGIN) -> dict:
    """{rule: {"hard": mm, "preferred": mm}} for the fab; a board with more than 2 copper layers gets the multilayer row
    where the fab has one."""
    key = profile(fab)
    if key is None:
        raise ValueError(f"no profile for fab '{fab}'; known: {', '.join(SOURCES)}")
    if layers > 2 and f"{key}-multilayer" in PROFILES:
        key = f"{key}-multilayer"
    up = lambda v: math.ceil(v * (1 + margin) * 1000 - 1e-6) / 1000  # noqa: E731
    named = PREFERRED["*"] | PREFERRED.get(key.split("-")[0], {})
    return {"fab": key, "margin": margin, "source": SOURCES[key.split("-")[0]],
            "rules": {k: {"hard": v, "preferred": max(up(v), named.get(k, 0))} for k, v in PROFILES[key].items()}}


BEGIN, END = "# cadmate: necks into pads, begin", "# cadmate: necks into pads, end"


def neck_rules(warnings: list[dict], hard: float) -> str:
    """KiCad custom rules letting a net's tracks go down to the fab's minimum inside the courtyard of a part it necks into,
    and nowhere else. Only for necks into pads; a track squeezed between other copper stays a DRC error."""
    seen = sorted({(w["net"], w["ref"]) for w in warnings if w.get("ref")})
    if not seen:
        return ""
    out = [BEGIN]
    for net, ref in seen:
        q = net.replace("'", "\\'")
        out += [f'(rule "cadmate neck {net} into {ref}"',
                f"  (condition \"A.Type == 'Track' && A.NetName == '{q}' && A.intersectsCourtyard('{ref}')\")",
                f"  (constraint track_width (min {hard:g}mm)))"]
    out.append(END)
    return "\n".join(out) + "\n"


def read_neck_rules(text: str) -> dict[str, list[tuple[str, float]]]:
    """The neck rules in a .kicad_dru (cadmate's, or written the same way): {net: [(part ref, min width mm)]}."""
    out: dict[str, list[tuple[str, float]]] = {}
    for m in re.finditer(r"A\.NetName == '((?:[^'\\]|\\.)*)' && A\.intersectsCourtyard\('([^']+)'\)\"\)\s*"
                         r"\(constraint track_width \(min ([\d.]+)mm\)", text):
        out.setdefault(m[1].replace("\\'", "'"), []).append((m[2], float(m[3])))
    return out


def merge_rules(existing: str, block: str) -> str:
    """The board's .kicad_dru with cadmate's block replaced (or added, or removed when block is empty)."""
    text = existing or ""
    if BEGIN in text and END in text:
        a, b = text.index(BEGIN), text.index(END) + len(END)
        text = (text[:a] + text[b:].lstrip("\n")).rstrip() + "\n"
    if "(version" not in text:  # an empty rules file isn't one: KiCad wants the version line
        text = f"(version {versions.DRU_FORMAT})\n" + text
    return text.rstrip() + "\n" + ("\n" + block if block else "")

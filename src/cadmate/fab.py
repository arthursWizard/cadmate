"""What a board house can make: its stated minimums, and the same with a safety buffer.

A fab's minimum is a floor, not a target. At the limit the board can still be made, but the yield drops. So each limit has
two levels. Below the hard one (the fab's own number) is an error. Between the two is allowed but listed, for the designer
to keep only where it is really needed. Values are for 1 oz outer copper, standard service, read from the fabs' capability
pages (October 2026).
"""

from __future__ import annotations

import math

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
}

# hard minimums, mm. via_drill and via_diameter are the smallest via at standard price: both fabs drill 0.15 mm for more
PROFILES = {
    "jlcpcb": {"track": 0.10, "clearance": 0.10, "pad_to_track": 0.10, "via_drill": 0.20, "via_diameter": 0.45,
               "via_hole_to_hole": 0.20, "pth_hole_to_hole": 0.45, "edge": 0.20},
    "jlcpcb-multilayer": {"track": 0.09, "clearance": 0.09, "pad_to_track": 0.10, "via_drill": 0.20, "via_diameter": 0.45,
                          "via_hole_to_hole": 0.20, "pth_hole_to_hole": 0.45, "edge": 0.20},
    # PCBWay's via pad is worked out (0.2 mm drill + 2 × its 0.15 mm ring), not quoted
    "pcbway": {"track": 0.10, "clearance": 0.10, "pad_to_track": 0.10, "via_drill": 0.20, "via_diameter": 0.50,
               "via_hole_to_hole": 0.28, "pth_hole_to_hole": 0.41, "edge": 0.25},
}

# where a fab names a comfortable value of its own, that is the preferred one instead of the margin's
PREFERRED = {
    "pcbway": {"track": 0.127, "clearance": 0.152},  # its normal process, 1 oz outer: 5/6 mil
    "*": {"via_drill": 0.30, "via_diameter": 0.60},  # the standard via at both fabs; smaller ones cost more
}


def limits(fab: str, layers: int = 2, margin: float = MARGIN) -> dict:
    """{rule: {"hard": mm, "preferred": mm}} for the fab; a board with more than 2 copper layers gets the multilayer row
    where the fab has one."""
    key = fab.lower().strip()
    if key == "jlcpcb" and layers > 2:
        key = "jlcpcb-multilayer"
    if key not in PROFILES:
        raise ValueError(f"no profile for fab '{fab}'; known: jlcpcb, pcbway")
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


def merge_rules(existing: str, block: str) -> str:
    """The board's .kicad_dru with cadmate's block replaced (or added, or removed when block is empty)."""
    text = existing or ""
    if BEGIN in text and END in text:
        a, b = text.index(BEGIN), text.index(END) + len(END)
        text = (text[:a] + text[b:].lstrip("\n")).rstrip() + "\n"
    if "(version" not in text:  # an empty rules file isn't one: KiCad wants the version line
        text = "(version 1)\n" + text
    return text.rstrip() + "\n" + ("\n" + block if block else "")

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
              "normal process: via hole to hole ≥11 mil, line to CNC-milled edge 0.25 mm.",
}

# hard minimums, mm
PROFILES = {
    "jlcpcb": {"track": 0.10, "clearance": 0.10, "via_drill": 0.20, "via_diameter": 0.45, "hole_to_hole": 0.20, "edge": 0.20},
    "jlcpcb-multilayer": {"track": 0.09, "clearance": 0.09, "via_drill": 0.20, "via_diameter": 0.45, "hole_to_hole": 0.20,
                          "edge": 0.20},
    "pcbway": {"track": 0.10, "clearance": 0.10, "via_drill": 0.20, "via_diameter": 0.50, "hole_to_hole": 0.28, "edge": 0.25},
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
    return {"fab": key, "margin": margin, "source": SOURCES[key.split("-")[0]],
            "rules": {k: {"hard": v, "preferred": up(v)} for k, v in PROFILES[key].items()}}

"""Two through-hole parts, each made whole (symbol, footprint, 3D model) with generate_part.

    uv run python examples/through-hole/make_parts.py /path/to/kicad/project [--overwrite]

- NE555P, DIP-8: TI's datasheet (SLFS022, package P, JEDEC MS-001): pins in two rows 7.62 mm apart
  at 2.54 mm pitch, leads b 0.36-0.56 mm by c 0.20-0.36 mm, body 6.35 x 9.81 mm (E1 x D, nominal),
  seated height A 5.33 mm max, standoff A1 0.38 mm min, lead below the seating plane L 2.92-3.81 mm.
- A 1x04 pin header, 2.54 mm pitch: 0.64 mm square posts, plastic body 2.54 x 10.16 x 2.5 mm.

Holes and pads come from the leads (IPC-7251, nominal density); the 3D models are cadmate's
through-hole models: a body box, a lead in every hole, shoulders into the DIP's sides and
posts above the header.
"""

import json
import sys
from pathlib import Path

from cadmate import part

NE555 = [
    {"number": 1, "name": "GND", "type": "G"}, {"number": 2, "name": "TRIG", "type": "I"},
    {"number": 3, "name": "OUT", "type": "O"}, {"number": 4, "name": "RST#", "type": "I"},
    {"number": 5, "name": "CONT", "type": "passive", "side": "top"}, {"number": 6, "name": "THRES", "type": "I"},
    {"number": 7, "name": "DISCH", "type": "I"}, {"number": 8, "name": "VCC", "type": "P"},
]
DIP_LEAD = {"lead_width": [0.36, 0.56], "lead_thickness": [0.20, 0.36]}
DIP8 = {
    "body_width": 6.35, "body_length": 9.81,
    # Pin 1 top left, counted down the left row and up the right one, as the datasheet's top view.
    "pad_rows": [{"first": 1, "count": 4, "x": 0, "y": 0, "dy": 2.54} | DIP_LEAD,
                 {"first": 5, "count": 4, "x": 7.62, "y": 7.62, "dy": -2.54} | DIP_LEAD],
}
HEADER = {
    "body_width": 2.54, "body_length": 10.16,
    "pad_rows": [{"first": 1, "count": 4, "x": 0, "y": 0, "dy": 2.54, "lead_width": 0.64, "lead_thickness": 0.64}],
}
HEADER_PINS = [{"number": n, "name": f"Pin_{n}", "type": "passive"} for n in range(1, 5)]

PARTS = [
    ({"name": "NE555P", "pins": NE555, "description": "Precision Timers, 555 compatible, PDIP-8", "keywords": "single timer 555"},
     {"family": "custom", "name": "DIP-8_W7.62mm", "params": DIP8},
     {"height": 5.33, "standoff": 0.38, "lead_below": 3.3, "lead_width": 0.46, "lead_thickness": 0.28},
     "http://www.ti.com/lit/ds/symlink/ne555.pdf"),
    ({"name": "Conn_01x04_test", "pins": HEADER_PINS, "reference": "J", "description": "Generic connector, single row, 01x04",
      "keywords": "connector header"},
     {"family": "custom", "name": "PinHeader_1x04_P2.54mm_Vertical", "params": HEADER, "courtyard_clearance": 0.5},
     {"height": 2.5, "lead_below": 3.0, "lead_above": 6.0, "lead_width": 0.64, "lead_thickness": 0.64},
     "https://www.samtec.com/products/tsw"),
]

project = Path(sys.argv[1]).expanduser()
for sym, fp, model, datasheet in PARTS:
    r = part.generate_part(project, "cadmate_parts", sym, fp, model, datasheet=datasheet, overwrite="--overwrite" in sys.argv)
    print(sym["name"], json.dumps({k: r.get(k) for k in ("ok", "errors", "warnings")}, indent=1))
    print("  holes:", json.dumps(r.get("footprint", {}).get("report", {}).get("ipc7251_holes")))

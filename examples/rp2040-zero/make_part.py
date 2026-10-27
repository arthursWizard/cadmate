"""Make the Waveshare RP2040-Zero as one part: symbol, footprint and the 3D model from build_rp2040_zero.py.

    FreeCADCmd build_rp2040_zero.py        # writes RP2040-Zero.step next to it
    uv run python examples/rp2040-zero/make_part.py /path/to/kicad/project

Pinout from Waveshare's wiki (pinout image): seen from the top with USB-C at the top, GP0..GP8 down the
right edge (pins 1-9), GP9..GP13 along the bottom from right to left (10-14), then up the left edge
GP14, GP15, GP26..GP29, 3V3, GND, 5V (15-23). The wiki's FAQ confirms 3V3 is pin 21 and 5V pin 23.
Pin positions are the hand-made footprint's (2.54 mm pitch), in its frame, so the model built for that
footprint is linked with frame "datasheet" and follows the generator's move of the origin to pin 1.
The module has parts on its underside: mounted flat it needs a cutout in the carrier board under them;
on a header it clears them.
"""

import json
import shutil
import sys
from pathlib import Path

from cadmate import part

HERE = Path(__file__).parent
PITCH = 2.54
# Either way of mounting: each pin gets a plated hole for a 0.64 mm square header post (sized from the
# post, IPC-7251) under the module's hole, plus, with the same number, an SMD pad from the hole out to
# 1.0 mm past the module's edge, so a module soldered flat by its castellations gets a visible fillet.
EDGE_LEFT, EDGE_RIGHT, EDGE_BOTTOM = 1.16, 19.16, -0.95  # module outline in this frame (drawing: 18 x 23.5 mm)
HOLE_IN = 1.38  # drawing: hole centres 1.38 mm in from the edge
OUTSIDE, TAB_W = 1.0, 1.6
POST = {"lead_width": 0.64, "lead_thickness": 0.64}
TAB = HOLE_IN + OUTSIDE  # SMD pad length, hole centre to past the edge

pads = []
for i in range(9):  # right edge, top down: pins 1-9
    y = -22.86 + i * PITCH
    pads += [{"number": i + 1, "x": EDGE_RIGHT - HOLE_IN, "y": y} | POST,
             {"number": i + 1, "x": EDGE_RIGHT - HOLE_IN + TAB / 2, "y": y, "width": TAB, "height": TAB_W}]
for i in range(5):  # bottom, right to left: pins 10-14
    x = 15.24 - i * PITCH
    pads += [{"number": 10 + i, "x": x, "y": EDGE_BOTTOM - HOLE_IN} | POST,
             {"number": 10 + i, "x": x, "y": EDGE_BOTTOM - HOLE_IN + TAB / 2, "width": TAB_W, "height": TAB}]
for i in range(9):  # left edge, bottom up: pins 15-23
    y = -2.54 - i * PITCH
    pads += [{"number": 15 + i, "x": EDGE_LEFT + HOLE_IN, "y": y} | POST,
             {"number": 15 + i, "x": EDGE_LEFT + HOLE_IN - TAB / 2, "y": y, "width": TAB, "height": TAB_W}]

names = [f"GP{i}" for i in range(14)] + ["GP14", "GP15", "GP26", "GP27", "GP28", "GP29", "3V3", "GND", "5V"]
types = {"3V3": "power_out", "GND": "power_in", "5V": "power_in"}
groups = {"GP26": "adc", "GP27": "adc", "GP28": "adc", "GP29": "adc"}
pins = [{"number": n + 1, "name": name, "type": types.get(name, "bidirectional")} for n, name in enumerate(names)]
for p in pins:
    if p["name"].startswith("GP"):
        p["group"] = groups.get(p["name"], "gpio")

project = Path(sys.argv[1]).expanduser()
step = HERE / "RP2040-Zero.step"
project.mkdir(parents=True, exist_ok=True)
shutil.copy2(step, project / "RP2040-Zero.step")
result = part.generate_part(
    project, "cadmate_parts",
    {"name": "RP2040-Zero", "pins": pins, "description": "Waveshare RP2040 module, dual Cortex-M0+, 2MB flash, USB-C, castellated 23 pins",
     "keywords": "RP2040 Raspberry Pi microcontroller module Waveshare"},
    {"family": "custom", "name": "RP2040-Zero", "courtyard_clearance": 0.5,
     "params": {"body_width": 18.0, "body_length": 23.5, "body_center": [10.16, -12.7], "pads": pads}},
    {"path": "${KIPRJMOD}/RP2040-Zero.step", "frame": "datasheet"},
    datasheet="https://www.waveshare.com/wiki/RP2040-Zero",
    overwrite="--overwrite" in sys.argv,
)
print(json.dumps({k: result.get(k) for k in ("ok", "errors", "warnings", "written")}, indent=1))

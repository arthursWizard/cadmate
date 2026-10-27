"""Write sample footprints into a .pretty folder, to open in KiCad.

    uv run python examples/footprints/generate_samples.py /path/to/cadmate-samples.pretty

The soic and qfn samples use the same JEDEC values as KiCad's library, so
compare_footprints against KiCad's own files should report no differences.
"""

import json
import sys
from pathlib import Path

from cadmate import server

SAMPLES = [
    ("soic", {"pins": 8, "pitch": 1.27, "body_width": "3.9+/-0.1", "body_length": [4.8, 5.0], "overall_width": "6+/-0.2",
              "lead_width": [0.31, 0.51], "lead_length": [0.4, 1.27]}, {}),
    ("soic", {"pins": 14, "pitch": 0.65, "body_width": "4.4+/-0.1", "body_length": "5+/-0.1", "overall_width": 6.4,
              "lead_width": [0.17, 0.30], "lead_length": "0.6+/-0.15", "package": "TSSOP"}, {}),
    ("qfn", {"pins": 16, "pitch": 0.5, "body_width": [2.9, 3.0, 3.1], "body_length": [2.9, 3.0, 3.1], "lead_width": [0.18, 0.3],
             "lead_length": [0.3, 0.5], "ep_width": 1.7, "ep_length": 1.7, "heel_reduction": 0.05}, {}),
    ("qfn", {"pins_x": 0, "pins_y": 4, "pitch": 0.5, "body_width": 2, "body_length": 2, "lead_width": [0.2, 0.3],
             "lead_length": [0.25, 0.45], "ep_width": 1.2, "ep_length": 1.6}, {}),
    ("custom", {"body_width": 2.54, "body_length": 10.16,
                "pad_rows": [{"first": 1, "count": 4, "x": 0, "y": 0, "dy": 2.54, "width": 1.7, "height": 1.7, "drill": 1.0}]},
     {"name": "cadmate_Header_1x04_P2.54mm", "courtyard_clearance": 0.5}),
    # Holes and pads worked out from the lead sizes (IPC-7251): round leads, and flat tabs in slots.
    ("custom", {"body_width": 10, "body_length": 8,
                "pad_rows": [{"first": 1, "count": 2, "x": 0, "y": 0, "dx": 5.08, "lead_diameter": [1.0, 1.1]}]},
     {"name": "cadmate_Terminal_1x02_P5.08mm", "courtyard_clearance": 0.5}),
    ("custom", {"body_width": 9, "body_length": 14, "pads": [
        {"number": 1, "x": 0, "y": 0, "lead_width": 2.0, "lead_thickness": 0.5, "slot": True},
        {"number": 2, "x": 0, "y": 6, "lead_width": 2.0, "lead_thickness": 0.5, "slot": True},
        {"number": 3, "x": 4.7, "y": 3, "lead_width": 2.0, "lead_thickness": 0.5, "slot": True, "rotation": 90},
        {"number": "MP", "kind": "np_thru_hole", "x": 0, "y": 10, "drill": 1.6}]},
     {"name": "cadmate_SlottedTabs_Example", "courtyard_clearance": 0.5}),
]


def main() -> None:
    lib = Path(sys.argv[1]).expanduser()
    for family, params, options in SAMPLES:
        r = server.generate_footprint(family, params, str(lib), overwrite=True, **options)
        print(json.dumps({"name": r["name"], "ok": r["ok"], "errors": r["errors"], "warnings": r["warnings"]}))


if __name__ == "__main__":
    main()

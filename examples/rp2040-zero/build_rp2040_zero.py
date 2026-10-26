# Builds a 3D model of the Waveshare RP2040-Zero for the RP2040-Zero.kicad_mod
# footprint and exports it as STEP. Runs in FreeCAD 0.20+ (GUI Python console,
# Macro > Execute, or headless: FreeCADCmd build_rp2040_zero.py).
#
# Frame: the model is built in the footprint's own 3D frame (mm, Z up, Z = 0 is
# the top of the carrier PCB, footprint Y flipped), so it links with offset 0
# and rotation 0.
#
# Every dimension is a named variable. The comment says where each value comes from:
#   drawing    = Waveshare's dimension drawing (wiki, "Dimensions")
#   photo      = measured on that drawing's photo (about +-0.2 mm)
#   footprint  = RP2040-Zero.kicad_mod (cadmate read_footprint)
#   datasheet  = the component's own datasheet
#   ASSUMED    = typical value, not confirmed for this board
import os

import FreeCAD as App
import Part

V = App.Vector
OUT = os.environ.get("CADMATE_OUT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "RP2040-Zero.step"))

# Mounting: height of the module's underside above the carrier board.
# The user solders it flat (0). The RP2040, crystal and regulator are on the
# underside, so flat mounting needs a cutout in the carrier board under them;
# otherwise the module rests on the RP2040 (set MOUNT_Z = 0.9).
MOUNT_Z = 0.0                          # user: soldered flat

# Board
BOARD_XMIN, BOARD_XMAX = 1.16, 19.16   # drawing 18.00, placed on the footprint courtyard
BOARD_YMIN, BOARD_YMAX = 0.95, 24.45   # drawing 23.50, placed on the footprint courtyard (Y flipped)
BOARD_R = 1.0                          # drawing R1.00 corners
BOARD_T = 1.6                          # ASSUMED: user says standard thickness
HOLE_D = 0.8                           # footprint drill; drawing: hole centres 1.38 from the edge
PITCH = 2.54                           # drawing
SIDE_PIN_Y = [2.54 + i * PITCH for i in range(9)]    # footprint pads 1-9 and 15-23; drawing: first 1.59 from top
BOTTOM_PIN_X = [5.08 + i * PITCH for i in range(5)]  # footprint pads 10-14; drawing: 3.92 from right edge
EDGE_HALF_HOLE_D = 0.8                 # photo: castellated half hole at the edge, in line with each pin
PAD_W, PAD_T = 1.5, 0.035              # photo: gold pad from the edge to just past the hole

# USB-C receptacle (top-mount, overhanging the top edge)
USB_W = 8.94                           # datasheet (16-pin top-mount USB-C); photo agrees within 0.2
USB_XMAX = BOARD_XMAX - 4.67           # drawing: 4.67 from the right edge
USB_OVERHANG = 1.0                     # footprint F.Fab (photo suggests 1.0 to 1.3)
USB_L = 7.35                           # datasheet (typical)
USB_H = 3.16                           # datasheet (typical)
USB_R = 1.2                            # ASSUMED end radius

# Top side parts: (name, centre x, centre y, size x, size y, height)
TOP_PARTS = [
    ("Flash", 10.10, 12.13, 3.0, 2.0, 0.55),        # photo position; USON-8 3x2 size (datasheet)
    ("LED_WS2812", 10.10, 8.88, 2.0, 2.0, 0.84),    # photo position; WS2812B-2020 (datasheet)
]
BUTTONS = [                            # photo positions, between pins 26/15 and 5/7
    ("SW_BOOT", 5.60, 8.30),
    ("SW_RESET", 14.69, 8.30),
]
BTN_X, BTN_Y, BTN_H = 3.0, 4.0, 2.0    # photo footprint 3x4 (incl. legs ~3.2x4.3); height ASSUMED
BTN_CAP_X, BTN_CAP_Y, BTN_CAP_H = 1.6, 2.6, 0.5  # photo: oval plunger; height ASSUMED

# Bottom side parts, from Waveshare's back-view photo (mirrored into the top view).
BOTTOM_PARTS = [
    ("RP2040", 10.15, 12.13, 7.0, 7.0, 0.9),        # photo position; QFN-56 7x7x0.9 (datasheet)
    ("Crystal_12MHz", 10.15, 5.48, 3.2, 2.5, 0.8),  # photo position; 3225 size (photo 3.3x2.7)
    ("LDO", 10.03, 21.69, 1.6, 2.9, 1.1),           # photo position; SOT-23-5 turned 90 deg, height ASSUMED
]

# --- Build ---
doc = App.newDocument("RP2040_Zero")
Z_BOT = MOUNT_Z                        # underside of the module PCB
Z_TOP = MOUNT_Z + BOARD_T              # top of the module PCB


def box(x0, y0, z0, dx, dy, dz):
    return Part.makeBox(dx, dy, dz, V(x0, y0, z0))


def centred_box(cx, cy, z0, dx, dy, dz):
    return box(cx - dx / 2, cy - dy / 2, z0, dx, dy, dz)


def add(name, shape, colour):
    obj = doc.addObject("Part::Feature", name)
    obj.Shape = shape
    if App.GuiUp:
        obj.ViewObject.ShapeColor = colour
    return obj


def vertical_edges(shape):
    return [e for e in shape.Edges if abs(e.Vertexes[0].Point.z - e.Vertexes[1].Point.z) > 1e-6]


# Board: rounded corners, a plated hole per pin and a half hole at the edge
pcb = box(BOARD_XMIN, BOARD_YMIN, Z_BOT, BOARD_XMAX - BOARD_XMIN, BOARD_YMAX - BOARD_YMIN, BOARD_T)
pcb = pcb.makeFillet(BOARD_R, vertical_edges(pcb))
cuts, pads = [], []
pins = [(BOARD_XMIN + 1.38, y, BOARD_XMIN, y) for y in SIDE_PIN_Y]
pins += [(BOARD_XMAX - 1.38, y, BOARD_XMAX, y) for y in SIDE_PIN_Y]
pins += [(x, BOARD_YMIN + 1.38, x, BOARD_YMIN) for x in BOTTOM_PIN_X]
for hx, hy, ex, ey in pins:
    cuts.append(Part.makeCylinder(HOLE_D / 2, BOARD_T + 0.2, V(hx, hy, Z_BOT - 0.1)))
    cuts.append(Part.makeCylinder(EDGE_HALF_HOLE_D / 2, BOARD_T + 0.2, V(ex, ey, Z_BOT - 0.1)))
    # Pad: from the board edge to just past the hole, PAD_W wide, on both faces
    reach = abs(hx - ex) + abs(hy - ey) + HOLE_D / 2 + 0.3
    if hx != ex:  # side pin
        x0 = ex if hx > ex else ex - reach
        x0, x1, y0, y1 = x0, x0 + reach, hy - PAD_W / 2, hy + PAD_W / 2
    else:  # bottom pin
        x0, x1, y0, y1 = hx - PAD_W / 2, hx + PAD_W / 2, ey, ey + reach
    for z in (Z_TOP, Z_BOT - PAD_T):
        pads.append(box(x0, y0, z, x1 - x0, y1 - y0, PAD_T))
for c in cuts:
    pcb = pcb.cut(c)
pad_shape = Part.makeCompound(pads)
for c in cuts:
    pad_shape = pad_shape.cut(c)
add("PCB", pcb, (0.1, 0.25, 0.6))
add("Pads", pad_shape, (0.85, 0.7, 0.3))

# USB-C: box with rounded ends (rounded edges run along Y)
usb_y1 = BOARD_YMAX + USB_OVERHANG
usb = box(USB_XMAX - USB_W, usb_y1 - USB_L, Z_TOP, USB_W, USB_L, USB_H)
usb = usb.makeFillet(USB_R, [e for e in usb.Edges if abs(e.Vertexes[0].Point.y - e.Vertexes[1].Point.y) > 1e-6])
add("USB_C", usb, (0.8, 0.8, 0.82))

for name, cx, cy, dx, dy, h in TOP_PARTS:
    add(name, centred_box(cx, cy, Z_TOP, dx, dy, h), (0.95, 0.95, 0.95) if "LED" in name else (0.15, 0.15, 0.15))
for name, cx, cy in BUTTONS:
    body = centred_box(cx, cy, Z_TOP, BTN_X, BTN_Y, BTN_H)
    # Oval plunger, long axis along Y (an XY ellipse has its major axis along X, so turn it)
    cap = Part.Face(Part.Wire(Part.Ellipse(V(cx, cy, Z_TOP + BTN_H), BTN_CAP_Y / 2, BTN_CAP_X / 2).toShape()))
    cap.rotate(V(cx, cy, 0), V(0, 0, 1), 90)
    add(name, body.fuse(cap.extrude(V(0, 0, BTN_CAP_H))), (0.75, 0.75, 0.75))
for name, cx, cy, dx, dy, h in BOTTOM_PARTS:
    add(name, centred_box(cx, cy, Z_BOT - h, dx, dy, h), (0.15, 0.15, 0.15))

doc.recompute()
objs = doc.Objects
try:
    import Import  # keeps one named solid per part (and colours when run in the GUI)
    Import.export(objs, OUT)
except Exception:
    Part.makeCompound([o.Shape for o in objs]).exportStep(OUT)

bb = Part.makeCompound([o.Shape for o in objs]).BoundBox
print(f"wrote {OUT}")
print(f"bbox x {bb.XMin:.2f}..{bb.XMax:.2f}  y {bb.YMin:.2f}..{bb.YMax:.2f}  z {bb.ZMin:.2f}..{bb.ZMax:.2f}")

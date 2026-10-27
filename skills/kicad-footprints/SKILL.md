---
name: kicad-footprints
description: Make or review a KiCad footprint from a part's datasheet. Use when the user asks for a footprint or land pattern for a part, a .kicad_mod from a datasheet, or a check of a footprint they made or downloaded.
---

# KiCad footprint from a datasheet

Uses the cadmate MCP server. You read the datasheet; cadmate does the geometry and checks it.

## First: does it need a new footprint?

Most parts in standard packages (SOIC, TSSOP, QFN, SOT-23, 0603...) already have a footprint in KiCad's library, made by the same maths cadmate uses. Check the datasheet's package code against KiCad's library first and say so if it is there. Generate one when the part has no library footprint, the datasheet's dimensions differ from the JEDEC ones, or the user wants a different density.

## Workflow

1. Find the package drawing and, if there is one, the **recommended land pattern** (often called "recommended footprint", "PCB layout" or "solder pad layout"). Ask the user for the datasheet if you don't have it. Never guess a dimension; ask.
2. Pick the family from `list_footprint_families`:
   - Recommended land pattern given: `custom`, with the pads exactly as drawn. The manufacturer's pattern wins over IPC. For a QFN/DFN this means `pad_rows` for the four sides and the exposed pad in `pads` with `paste_coverage` (the datasheet's stencil figure, or 0.65) so its paste is split like the `qfn` family's; give `paste_grid: [columns, rows]` to match the stencil drawing's openings. Without it the exposed pad gets one full paste opening, which the review flags.
   - Otherwise gull-wing on two sides: `soic`. No-lead (QFN/DFN): `qfn`. Anything else: `custom`, deriving pads from the drawing and saying in your reply that you did.
3. Read the dimensions with their **tolerances**. For `soic` and `qfn` give min and max, not just nominal: `[0.31, 0.51]`, `[min, nom, max]` or `"6.0+/-0.2"`. The tolerances change the pad size; nominals alone give pads that are too short. When the drawing gives only a maximum (often the height, "A 1.0 MAX"), use it and tell the user you did. Package drawings and land patterns rarely survive text extraction: render those pages to images (for example with pypdfium2 or `pdftoppm`) and read the picture.
4. `generate_footprint` into a project-local library, for example `<project>/footprints.pretty` (see "Files"). Pass `datasheet` with the URL so it ends up in the description. It reviews the result and writes nothing if there are errors; fix the inputs and run again.
5. Show the user the warnings and the `report` (pad size, IPC values). If KiCad's library has the same package, run `compare_footprints` against it and report the differences.
6. Ask the user to open the footprint in KiCad's footprint editor and confirm pin 1 and the pad layout against the datasheet's drawing before using it.

To review an existing footprint, run `check_footprint`. Give it `family` and `params` from the datasheet to also compare it pad by pad with what IPC-7351B gives.

## Reviewing a footprint against its model, datasheet and symbol

`review_footprint` is the full review, for any footprint (downloaded, hand-made, generated). It runs `check_footprint`'s rules, then everything the second sources allow. Give it what you have; its `skipped` list says what each missing check needs, so ask the user for that rather than guessing.

- **Pad numbering.** On its own it flags numbers that run backwards along a row (two pads swapped by hand). With `datasheet_pads`, `[{number, x, y, name?}]` read off the datasheet's drawing or pinout (any origin, Y up or down), it checks every pad against the pin the datasheet puts there. Add `symbol_path` and pin names and it says which signal would land on which pin. The symbol alone has no positions, so it cannot catch a swap; the datasheet can.
- **The 3D model**, solid by solid in FreeCAD: bodies outside the courtyard (named with the side), parts that go into the board, and leads that miss or overfill their holes. A module with parts on its underside, soldered flat, is an error with both fixes spelled out: the Edge.Cuts cutout rectangle to draw in the footprint, or the standoff (a 2.54 mm header's 2.5 mm spacer clears most). Pass `cutout: true` only when the board is already cut out there. Without a model, give `underside_parts` from the datasheet or a photo.
- **Holes.** `leads` with the datasheet's lead (`lead_diameter`, or `lead_width` and `lead_thickness`, per pad number or for all) checks every hole fits it; slots are judged along and across.
- **Castellated modules.** Give `body`, the module's outline in footprint coordinates, unless the model has the module's board. On the host footprint every edge pad's copper must reach past the module's edge (about 1 mm; under 0.5 mm is a warning, stopping inside is an error). Pads KiCad marks castellated (the module's own board) must sit on the edge.

`check_part` runs the same review on the footprint a symbol links, and takes the same extra sources. Report errors first, then the warnings the user has to judge, then the skipped checks and what they need.

## Through-hole parts

Use `custom`. For each pin, give the **lead** rather than guessing a hole: `lead_diameter` for round leads, or `lead_width` and `lead_thickness` for square or flat ones, as `[min, max]` when the datasheet gives a range. cadmate works out the hole (maximum lead + 0.15/0.2/0.25 mm for least/nominal/most) and the pad (hole + 0.5/0.6/0.7 mm) per IPC-7251, rounded up to 0.05 mm. A rectangular lead gets a round hole that clears its diagonal. So a 0.64 mm square header pin gets a 1.15 mm hole; KiCad's own pin headers use 1.0 mm by long-standing convention, and `drill: 1.0` reproduces that when the user wants to match them.

- Flat tabs (DC jacks, USB shells, power connectors): add `slot: true` to get a slotted (oval) hole along X; `rotation: 90` for one along Y. Slots cost extra at some fabs, and KiCad's library only uses them for leads more than twice as wide as they are thick; cadmate warns below that.
- If the datasheet gives a recommended hole size, use its `drill` (a number, or `[x, y]` for a slot) and pad `width`/`height` instead; the manufacturer wins.
- Mounting holes and pegs without copper: `kind: np_thru_hole` with `drill`.
- Castellated modules that may also go on headers: one plated hole per pin under the module's hole, and `pad_offset: [dx, dy]` to move the pad's copper off the hole so it reaches about 1 mm past the module's edge (x, y stay the hole). Soldered flat, the fillet forms on the copper outside; on a header, the hole takes the post.
- Pin 1 gets a rectangular pad, the others round or oval; the origin goes on pin 1. For parts without polarity (resistors, non-polarised capacitors) give pin 1 `shape: circle` (or `oval`) so it does not suggest one. Use `courtyard_clearance: 0.5` for connectors.
- The report lists every hole size it worked out (`ipc7251_holes`); show it to the user.

## Reading the drawing (JEDEC letters)

| cadmate | JEDEC | What it is |
| --- | --- | --- |
| `body_width` | E1 (SOIC), E (QFN) | body across the rows of leads (X) |
| `body_length` | D | body along the rows (Y) |
| `overall_width` | E (SOIC) | lead tip to lead tip |
| `lead_width` | b | lead width |
| `lead_length` | L | the flat foot on the board (SOIC), or the terminal length (QFN); not the whole lead from the body |
| `pitch` | e | centre to centre |
| `ep_width`, `ep_length` | E2, D2 | exposed pad |

The same values feed `package_params` for the 3D model (`kicad-3d-models` skill), so read the drawing once and keep the numbers. `height` and `standoff` are accepted and ignored by the footprint generator.

## Conventions (KiCad library rules, applied by cadmate)

- mm, KiCad's footprint frame: **Y points down**. A pad drawn above the centre on the datasheet has a negative y. Datasheet land patterns are top views; check whether a drawing is a bottom view before copying it.
- SMD origin at the centre of the pads; through-hole origin at pin 1 (`custom` does this when `origin` is `auto`).
- Pin 1 top left for `soic` and `qfn`, counted anticlockwise seen from the top.
- Courtyard 0.25 mm around body and pads on a 0.01 mm grid; use `courtyard_clearance: 0.5` for connectors and large parts.
- Silkscreen 0.2 mm clear of pads, with an arrow at pin 1. Body outline on F.Fab with the pin 1 corner chamfered.
- Exposed pads get split paste openings (about 65% coverage), and the signal pads are kept 0.2 mm from the exposed pad. In `custom`, ask for it per pad with `paste_coverage`.
- Files are written in KiCad 10's format; cadmate needs KiCad 10 or later.

## Files

- Never write into KiCad's installed libraries.
- Project-local: `<project>/<name>.pretty/`, added in Preferences > Manage Footprint Libraries (Project tab).
- A personal library: a `.pretty` folder the user picks, added in the Global tab.
- `generate_footprint` refuses to overwrite a file unless asked, and keeps a `.bak` copy when it does.

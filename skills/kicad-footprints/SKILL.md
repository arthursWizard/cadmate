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
   - Recommended land pattern given: `custom`, with the pads exactly as drawn. The manufacturer's pattern wins over IPC.
   - Otherwise gull-wing on two sides: `soic`. No-lead (QFN/DFN): `qfn`. Anything else: `custom`, deriving pads from the drawing and saying in your reply that you did.
3. Read the dimensions with their **tolerances**. For `soic` and `qfn` give min and max, not just nominal: `[0.31, 0.51]`, `[min, nom, max]` or `"6.0+/-0.2"`. The tolerances change the pad size; nominals alone give pads that are too short.
4. `generate_footprint` into a project-local library, for example `<project>/footprints.pretty` (see "Files"). Pass `datasheet` with the URL so it ends up in the description. It reviews the result and writes nothing if there are errors; fix the inputs and run again.
5. Show the user the warnings and the `report` (pad size, IPC values). If KiCad's library has the same package, run `compare_footprints` against it and report the differences.
6. Ask the user to open the footprint in KiCad's footprint editor and confirm pin 1 and the pad layout against the datasheet's drawing before using it.

To review an existing footprint, run `check_footprint`. Give it `family` and `params` from the datasheet to also compare it pad by pad with what IPC-7351B gives.

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
- Exposed pads get split paste openings (about 65% coverage), and the signal pads are kept 0.2 mm from the exposed pad.
- Files are written in the KiCad 8 format, so KiCad 8 and later open them.

## Files

- Never write into KiCad's installed libraries.
- Project-local: `<project>/<name>.pretty/`, added in Preferences > Manage Footprint Libraries (Project tab).
- A personal library: a `.pretty` folder the user picks, added in the Global tab.
- `generate_footprint` refuses to overwrite a file unless asked, and keeps a `.bak` copy when it does.

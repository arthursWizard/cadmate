---
name: kicad-parts
description: Make a complete KiCad part from one datasheet, symbol, footprint and 3D model linked together, or check that an existing part's three pieces fit. Use when the user asks for a part, a library part, or "everything" for a component, or asks why a symbol, footprint and model don't line up, or wants part numbers (MPN, LCSC, DigiKey, Mouser) filled in on an existing schematic.
---

# A whole part from one datasheet

Uses the cadmate MCP server (and FreeCAD for the 3D model). You read the datasheet once; `generate_part` makes the symbol, the footprint and the 3D model from those values, links them and checks them together. It writes nothing until all three pass.

The `kicad-symbols`, `kicad-footprints` and `kicad-3d-models` skills hold the details for each piece (pin types, JEDEC letters, model conventions); follow them for reading the datasheet.

## Choosing the part, and its numbers

cadmate doesn't search distributors. For stock, price and alternatives, use a search tool for the distributor the user buys from (the project's `project_fab` says which, if any). For JLCPCB and LCSC, jlc-mcp from anthropics/ai-eda (MIT) searches with stock, price and a basic-parts filter. DigiKey and Mouser have their own APIs, which need the user's key. Don't pick a distributor for the user.

The manufacturer part number is the record that matters; distributor numbers are optional extras. Pass them as `symbol.fields`, for example `{"Manufacturer": "...", "MPN": "...", "LCSC": "C..."}`. They travel with the symbol into the schematic and the board, `export_fab` puts them in the BOM, and `check_ready_to_order` flags parts without one. Check that the orderable part's package is the one you're drawing: the same part number with a different suffix is often a different package. Put the distributor's package text in a `Package` field (or pass search results as `packages` to `check_ready_to_order`); the ready check compares it with the footprint and blocks the order on a mismatch, such as an 0805 part on 0603 pads or a SOT-23-5 on a SOT-23-6 footprint. It only compares what both sides state: a package it can't read is listed to check by eye.

## Numbers for a schematic that's already drawn

Most sheets use generic symbols (`Device:R`, `Device:C`) with only a value, so each part needs its numbers before an assembled board can be ordered. Don't send the user into KiCad to type them in one by one:

1. `check_ready_to_order` on the board (or `export_fab`'s `no_part_number`) lists the parts without a number. Group them by value and footprint, as a BOM would: one pick per group.
2. Find a part for each group with the user's search tool (jlc-mcp for JLCPCB/LCSC, the DigiKey or Mouser API). Match the footprint's package and the value's rating (voltage, tolerance, power), and prefer what the house stocks (JLCPCB's basic parts avoid a setup fee).
3. Show the user the picks as a table (refs, value, footprint, manufacturer, MPN, distributor number, package) and get their agreement. They are buying these.
4. `set_part_numbers` with the root schematic and `{"R1,R2,R5": {"Manufacturer": ..., "MPN": ..., "LCSC": ...}}`. Add `Package` with the distributor's package text so the ready check compares it with the footprint. Run it with `dry_run` first if the user wants to see the edit. It writes the schematic and the board beside it, and keeps the old files first. Ask the user to close them in KiCad before, or it refuses. Check `changed`: each part's value and footprint is listed beside its new number, so a wrong ref shows up there; `unknown_refs`, `shared_symbols`, `conflicts` and `warnings` all need a word to the user.
5. `check_ready_to_order` again, then `export_fab`.

## Workflow

1. Ask for the datasheet and the exact package (orderable part number) if you don't have them. Never guess a pin or a dimension; ask.
2. Read the pin table for that package into `symbol.pins` (see `kicad-symbols`), and the package drawing into `footprint.params` with tolerances (see `kicad-footprints`). Put the package height in the same params (`height`, and `standoff` for gull-wing parts); the model is built from them.
3. `environment`: check that FreeCAD is found. Without it the symbol and footprint are written with no model, and you should say so.
4. `generate_part` with `project_dir` (the KiCad project folder), `library` (one name for the three libraries, e.g. the project's name or `<project>_parts`), `symbol`, `footprint`, `datasheet` (URL). It returns every check; on errors, fix the inputs and run again. Common ones:
   - "symbol pins with no pad" / "pads with no symbol pin": the pin table and the package differ. Add the exposed pad (often pins + 1) or NC pins to the table, or check the package.
   - "the 3D model needs height": read A (and A1) off the drawing.
5. Show the user the warnings, the footprint `report` (pad sizes) and the symbol `report` (pin placement).
6. Ask the user to open the symbol in a schematic, place it, update the PCB from the schematic and look at the 3D viewer. Pin 1 of the model, the footprint's pin 1 and the symbol's pin 1 should all agree with the datasheet.

## What it writes

`generate_part` writes libraries into a project folder; it does not make the KiCad project itself. If the folder has no `.kicad_pro` yet, ask the user to create the project there in KiCad (File > New Project) so the project library tables are picked up.

- `<project>/<library>.kicad_sym`: the symbol, its Footprint field `<library>:<footprint name>` and footprint filters that match it.
- `<project>/<library>.pretty/<footprint name>.kicad_mod`, linking `${KIPRJMOD}/<library>.3dshapes/<footprint name>.step` at offset 0.
- `<project>/<library>.3dshapes/<footprint name>.step`, built in the footprint's frame, so it needs no offset or rotation.
- Entries for both libraries in the project's `sym-lib-table` and `fp-lib-table` (created if missing; other entries are kept).

Existing files are only replaced with `overwrite: true`, and the old files are kept (in `history/` inside a cadmate project, else as `.bak`).

## The 3D model

- `soic` and `qfn`: a proper package model, body and leads as separate solids, pin 1 dimple, leads on every pad.
- `custom`: a body box over the footprint's body outline from `standoff` to `height`, with a lead in every through-hole pad and a terminal under every SMD pad. With `standoff` 0 (QFN, LGA, a manufacturer's no-lead land pattern) the terminals stop at the body's edge, so a QFN entered as `custom` gets a model of the right size. Leads are round (`lead_diameter`, else the hole minus 0.2 mm) or square/flat (`lead_width` and `lead_thickness`), reach `lead_below` mm below the board's top face, the seating plane datasheets measure tail length from (3 by default, 0 for none; on a 1.6 mm board 3 leaves 1.4 mm out of the underside). `review_footprint` and `check_part` warn when a model's leads stop inside the board (`board_thickness`, 1.6 mm by default), and get a shoulder into the body's side when they stand beside it (DIP). `lead_above` makes header posts stand that far above the body. Good for clearances and enclosures. For a module or connector whose looks matter, build it in FreeCAD (see `kicad-3d-models`) and pass `model: {path, frame}`:
  - `frame: "datasheet"` when you built it in the datasheet's pad coordinates (the ones you gave the footprint). cadmate moves it with the footprint's origin, which goes to pin 1 for through-hole parts.
  - `frame: "footprint"` (default) when it is already in the footprint's frame; `offset` and `rotate` as KiCad's.
  - Put the STEP in the project (`<library>.3dshapes/`); cadmate links a path inside the project through `${KIPRJMOD}`.
  - To swap only the model of a part already made, use `link_model_to_footprint` and `check_part` rather than `generate_part` again, which rewrites the symbol and footprint too.
- `cutout: true` in `model` when the board has a cutout under the part, as for a castellated module with parts on its underside soldered flat; the model may then reach below the board.
- Models built headless (FreeCADCmd) keep their colours on FreeCAD 1.0 and later. With 0.20 or 0.21 they come out grey, and `generate_part` warns.

## Checking an existing part

`check_part` with the symbol library (and `name`) follows the symbol's Footprint field through the project's `fp-lib-table` to the footprint, then the footprint's model link to the STEP, and reports: missing footprint or model file, footprint filters that hide the footprint, pins without pads and pads without pins, the footprint review, and the model's placement over the pads. Give `pins` to compare the symbol with the datasheet's table, and `body_width`, `body_length`, `body_height` to check the model's size. As in the footprint families, width is along the footprint's X and length along its Y: a 5x7 mm QFN drawn tall is width 5, length 7. A model that fits the sizes turned 90 degrees is reported as such. The footprint gets `review_footprint`'s review (see the `kicad-footprints` skill), so `datasheet_pads`, `leads`, `body`, `castellated` and `underside_parts` work here too.

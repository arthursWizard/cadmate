---
name: kicad-3d-models
description: Build, check and link a 3D model for a KiCad footprint that has none. Use when the user asks for a missing 3D model, a STEP/WRL for a footprint, or a part body in FreeCAD from datasheet dimensions.
---

# 3D model for a KiCad footprint

For a new part, `generate_part` (skill `kicad-parts`) builds the model for `soic` and `qfn` packages from the footprint's own values, so start there. Use this skill for footprints that already exist, and for modules and connectors that need a real model rather than `custom`'s placeholder box.

Uses the cadmate MCP server (KiCad side and checks) and FreeCAD. When the user wants to look at it and iterate together, work live in their FreeCAD window (see "Working live" below). Otherwise write the model as a FreeCAD Python script (Part workbench, ending in a STEP export) and run it headless with the `freecadcmd` that `environment` reports: `freecadcmd model.py`. Then render it with `kicad-cli pcb render` on a test board, or ask the user to look at it in KiCad's 3D viewer.

## Workflow

1. `read_footprint` on the footprint. Note SMD or through-hole, pad count, pitch, pin 1 position, courtyard.
2. Get the body dimensions from the datasheet's package drawing. Ask the user for the datasheet or the part number if you don't have them. Never guess a dimension; ask. Use nominal values, not min or max; where the drawing gives only a maximum (often the height), use it and say so.
3. Pick the family from `list_package_families` and run `package_params` with the footprint path. Fix every error before modelling. Show the user the warnings.
4. Build the model in FreeCAD, live in the user's window or as a script run with `freecadcmd`, in the Part workbench so it stays parametric (see "Building" below). Look at a screenshot or render before going on.
5. Export STEP next to where the model will live (see "Files"). KiCad reads the STEP's colours. In a headless script, `ShapeColor` doesn't exist (there is no view object), so pass each object with one colour per face; this needs FreeCAD 1.0 or later:
   ```python
   import Import
   Import.export([(body, [(0.15, 0.15, 0.15)] * len(body.Shape.Faces)),
                  (leads, [(0.82, 0.82, 0.84)] * len(leads.Shape.Faces))], "part.step")
   ```
   `check_model_vs_footprint` reports `colours: false` when the STEP has none.
6. `link_model_to_footprint` with the path written using a KiCad variable, never an absolute path.
7. `check_model_vs_footprint` with the datasheet's overall size: `body_width` along the footprint's X, `body_length` along its Y (as in the footprint families), `body_height`. Fix every error and re-check. Report warnings to the user.
8. Ask the user to open the footprint in KiCad's 3D viewer and confirm before calling it done.

## Conventions (KiCad library rules)

- Units are mm. Z = 0 is the top of the PCB; the body sits on it (SMD standoff from the datasheet).
- **SMD origin**: the centre of the package, which is also the centre of the pads.
- **Through-hole origin**: pin 1, leads going down to negative Z (about 3 mm below the board unless the datasheet says otherwise).
- KiCad's footprint Y axis points down, the 3D Y axis points up. A pad at footprint (x, y) is at 3D (x, -y).
- Pin 1 of the model must sit on pad 1. Mark pin 1 on the body (a small dimple or chamfer) as the real part does.
- Leads are separate solids from the body, so colours can differ: body dark grey or black, leads metallic grey.
- Name model files after the footprint: `SOIC-8_3.9x4.9mm_P1.27mm.step`.

## Building

- One script per part, with all dimensions as named variables at the top, taken from `package_params`' checked output.
- Body: a box from `standoff` to `height`, with a small top chamfer (about 0.1 to 0.2 mm) when the drawing shows one.
- Gull-wing leads (soic, sot): a flat foot of `lead_length` on the board, a bend up to the body at mid height, `lead_width` wide, about 0.2 mm thick unless the datasheet gives `c`.
- No-lead (qfn): lead pads flush with the body bottom and side, plus the exposed pad if any.
- Fuse leads into one compound and keep the body separate; never fuse body and leads.
- Connectors: model the mating face (the opening and what is inside it, a USB-C tongue for example), since that is what enclosures are cut around. Shell legs, tails and pegs are their own solids, never fused to the body or the insulator: the review judges each solid by its box, so a peg fused to the insulator reads as the whole insulator going into the board.
- For `generate_part`'s `frame: "datasheet"`, build in the pad coordinates you gave the footprint, flipped as above: a pad at (x, y) there is at 3D (x, -y).

## Files

- Project-local parts: `${KIPRJMOD}/3dmodels/<name>.step`.
- A personal library: a folder the user picks, referenced through a KiCad path variable they define (Preferences > Configure Paths), for example `${MY_3DMODELS}/<name>.step`.
- VRML for KiCad uses a 0.1 inch unit. Export it with KiCad StepUp's exporter, which handles the scale, rather than FreeCAD's plain VRML export.

## Working live in the user's FreeCAD window

For iterating with the user: they look, comment or edit by hand, you edit on top. Fewer rounds are better, but expect some.

1. `freecad_live_status`, once per session before the first FreeCAD step. Pass its `tell_user` on: whether the bridge is on, and how to switch it from FreeCAD's Macro menu (`cadmate_bridge_start.FCMacro` starts it, `cadmate_bridge_stop.FCMacro` stops it). If it's off and they want to work live but have never started it, give them the `start` line to paste into FreeCAD's Python console (View > Panels > Python console); that first start saves both macros. Wait for their answer. Never start it any other way, and don't ask again before each edit.
2. `freecad_live_open` the model: a build script (it runs in their window and leaves its document open; copy it first if it exports over a STEP that's in use), an `.FCStd`, or a STEP.
3. `freecad_live_snapshot` and look at it before you say anything about it. Then tell the user it's there.
4. Each round: `freecad_live_state` first. Read `user_changes`: those are the user's hand edits since you last looked. Keep them; build on them; never revert one unless they ask. If an edit of theirs conflicts with the datasheet or the footprint, say so and ask.
5. `freecad_live_edit` with small edits to existing objects (a property, a placement, one feature added), one undo step per change you'd describe in one line, with that line as the label. Don't delete and rebuild objects, and don't rerun the build script: that throws away the user's edits. The objects of a build script are plain solids (`Part::Feature`); to resize one, replace its `Shape` and keep its `Placement` and colour, or ask the user whether to make it a parametric primitive first.
6. When the round's edits are done, `freecad_live_tidy_view` once, so the window is left presentable: isometric, the model fitted and centred, origins and the axis cross off. Not after every edit, and not when the user asked for origins on or for a view of their own (then pass `hide_origins=False`, their `view`, or `fit=False`). Don't show origins or every object in `doc.Objects` in edit code; if an edit's reply has `origins_shown` and the user didn't ask for them, the tidy step hides them again. Then snapshot, and tell the user in a line what you changed and why.
7. When the user agrees: `freecad_live_export_step` with the footprint and the datasheet's overall size, fix what the check finds, `freecad_live_save` the document next to the STEP, then link it as in steps 6 and 8 above.

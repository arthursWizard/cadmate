---
name: freecad-modelling
description: How to build and change a FreeCAD part (a printed case, a lid, a bracket, a knob) so its tree stays easy to change. Use before modelling any part in FreeCAD, before editing an existing .FCStd, and when the user asks to clean up or restructure a messy model.
---

# A FreeCAD part someone can change

A model can be correct and still be hard to change. The usual way it goes wrong, for a person or for you: a sketch
drawn on the top face of a pad, which moves or breaks when the pad changes; a size typed into six features; a fix
stacked on top of the feature it corrects instead of that feature being edited; thirty objects called `Sketch0xx` and
`Pocket0xx`. Every round adds to it, and after a few the user can no longer open the tree and change a wall thickness.

So the part is built as a tree a person can edit in FreeCAD, and `check_model_structure` checks it. Knowing the rules
isn't enough; run the check.

This is for parts that will be printed or machined, and changed. A 3D model of a component for KiCad is different:
it's built by a script, the script is its source, and the `kicad-3d-models` skill covers it.

## The source of truth

The `.FCStd` is the source, its tree is what you edit, and the user can edit it too. Don't keep a build script that
regenerates the part: rerunning it throws away the user's hand edits, and two sources drift apart. A script may build
the first version (headless with `freecadcmd`, or through `freecad_live_open`); put it in `.cadmate/work/` and delete
it once the `.FCStd` is saved. From then on, change the document.

## How the tree is built

1. **One Params sheet.** A Spreadsheet named `Params` holds every size that drives the part: outline, wall, floor and
   lid thickness, heights, clearances, screw and boss sizes, the positions that come from the board. Each cell has an
   alias (`wall`, `case_h`, `usb_z`) and a comment column saying where the value came from (brief, datasheet,
   `read_footprint`, measured). One sheet for the whole document, shared by every piece.
2. **One PartDesign Body per printed piece**, named for the piece (`top`, `base`), with the same names as the STEP
   pieces `check_enclosure` reads.
3. **Attach to origin planes or datum planes, never to faces.** Every sketch goes on the body's XY, XZ or YZ plane,
   with its attachment offset bound to Params when it isn't on the origin, or on a datum plane placed that way. A
   datum plane is attached to an origin plane, never to a face, edge or vertex. FreeCAD 1.x mitigates renumbered
   faces but doesn't remove the problem, and an attachment to a plane can't break at all.
4. **Fully constrained sketches.** Every point fixed to the origin, the axes, or a dimension. Dimensions that matter
   outside the sketch are bound to Params (`Params.wall`); one that is local to the sketch (a slot's end radius) gets a
   name so it reads as deliberate.
5. **Feature values from Params.** Pad and pocket lengths, fillet radii, shell thickness, pattern counts and spacing:
   an expression, not a number.
6. **Names that say what it is.** `outline`, `usb-cutout`, `screw-boss`, `lid-lip`, `wall-fillet`. The user finds the
   feature to change by its name.
7. **Order**: the main shape first (pad the outline, shell it), then cut-outs and bosses, then patterns, then
   fillets and chamfers last. Dress-up features pick edges, and edges renumber: keep them at the end so few features
   depend on them, and prefer a sketch with the rounded corner drawn in over a fillet on a vertical edge.
8. **Pieces that must fit each other** take the shared sizes from Params (the lid's lip from `wall` and `fit_gap`),
   never from the other piece's faces. Where a shape has to follow another body, use a SubShapeBinder of that body's
   sketch, not of its faces.

## Changing it

- **Edit the feature, don't stack a fix.** A wall that's too thin: change `Params.wall`. A port in the wrong place:
  change its sketch's dimension or Params entry. A hole that's too small: change the hole. Never add a pad that fills
  part of a pocket back in, or a pocket that trims a pad that came out too long.
- A new feature is for a new piece of design (a vent, a second port), not for correcting an old one.
- If a change can't be made by editing (the shape needs a different profile), rework the feature's sketch; if the
  sketch has to be redrawn, redraw it in place, on the same plane, keeping its name.
- Live in the user's window (`freecad_live_edit`): one change per step, with the step's label saying what changed.
  The rules in "Working live" of `kicad-3d-models` apply: `freecad_live_state` first, keep `user_changes`, tidy the
  view once at the end of a round.

In FreeCAD Python, the moves that keep a tree clean:

```python
import Sketcher

body = doc.getObject("Top")
xz = [f for f in body.Origin.OriginFeatures if f.Role == "XZ_Plane"][0]
sk = body.newObject("Sketcher::SketchObject", "Sketch")
sk.Label = "usb-cutout"
sk.AttachmentSupport = [(xz, "")]           # an origin plane, not (pad, "Face6")
sk.MapMode = "FlatFace"
sk.setExpression(".AttachmentOffset.Base.z", "Params.usb_y")
# ... draw the cut-out's outline and constrain it, then:
i = sk.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, 9.0))
sk.renameConstraint(i, "usb_w")
sk.setExpression(".Constraints.usb_w", "Params.usb_w + 2 * Params.fit_gap")
pocket = body.newObject("PartDesign::Pocket", "Pocket")
pocket.Label = "usb-port"
pocket.Profile = sk
pocket.Type = "ThroughAll"
params = doc.getObject("Params")            # a size changes in Params, nowhere else
params.set(params.getCellFromAlias("wall"), "2.0")
```

## The check

`check_model_structure` reads the tree (a saved `.FCStd` by path, or the user's window without one) and reports:

- **error**: a sketch or datum on a generated face, edge or vertex; a sketch that isn't fully constrained; a feature in
  error; sizes with no Params sheet at all. Fix these before showing the part.
- **warning**: bare numbers (feature values not bound, unnamed sketch dimensions not bound); default names; more than
  one parameter sheet; features suppressed or past the tip; a very long body; a sketch on a binder's face; solids with
  no history (`Part::Feature`) where a printed piece should be a Body. Fix them unless there's a reason, and say the
  reason. An imported board inside an `App::Part` is fine and isn't flagged.
- **info**: unused sketches. Delete leftovers; a reference sketch used by expressions is fine.

Run it after every round of modelling, with `check_enclosure` for a case, and before telling the user a step is done.
Its report sits beside the `.FCStd` (`<name>-structure.json`), and `project_check` flags it when it's missing, older
than the document, or failing.

## Tidying a model that's already messy

1. `project_snapshot` with a label like `before-restructure`.
2. `check_model_structure`, and note each piece's bounding box (`freecad_live_state`) and volume (`Shape.Volume`):
   the tidy must not change the shape.
3. Add the Params sheet and move the sizes into it, binding each feature and sketch dimension as you go.
4. Re-attach face-attached sketches to an origin or datum plane at the same position (attachment offset from
   Params), working from the first feature down; recompute and compare after each.
5. Fold stacked fixes into the feature they correct, and delete what's left over (unused sketches, suppressed
   features, features past the tip).
6. Name everything.
7. Check again, and compare volumes and boxes with step 2. If a body has more errors than features worth keeping,
   rebuilding it cleanly from Params is often faster: ask the user first, since it replaces their tree.

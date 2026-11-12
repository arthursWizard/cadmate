"""cadmate MCP server: the bridge between KiCad and FreeCAD.

These tools cover the KiCad side, FreeCAD models (headless through freecadcmd, or
live in the user's window through cadmate's bridge) and the checks that tie the two together.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from . import (apps, check, enclosure, fab_export, footprint, freerouting, generate, ipc, kicad_live, kicad_pcb_live, live, model, packages, part, part_numbers, pcb_flow, pcb_place, pcb_review, pcb_write, ready,
               pcb_route, project, protoboard, release, protoboard_place, protoboard_render, review, schematic_review, schematic_write, structure, symbol, symbol_check, symbol_generate)
from .footprint import read as _read_footprint  # generate_symbol's footprint argument shadows the module

INSTRUCTIONS = """\
cadmate keeps each design as ONE active project with a fixed layout and a single history folder. Follow this in every
session that creates or changes design files, whoever asked for the change:

- Apply every change to the active project in place. Never create side-by-side versions: no main-v2/,
  main-routed.kicad_pcb, main-hand3.kicad_pcb, enclosure-variant-b/, *.bak, or copies "to keep the old one".
- To keep the old state, call project_snapshot first (label like "before-rework"). It copies the files into
  history/<YYYYMMDD-HHMM>_<label>/ at the same relative paths and logs it in history/HISTORY.md; project_restore puts
  one back. Snapshot when the user has seen or approved the current state, or before a big or risky change. The routing
  tools snapshot the board themselves when they change it in place.
- Layout: no folder holds just one thing. One kind of design (KiCad, FreeCAD, protoboard): its files at the project
  root; several: kicad/, freecad/, protoboard/. In each, one board (<board>.kicad_pro|.kicad_sch|.kicad_pcb|.kicad_dru),
  one FreeCAD part (<part>.FCStd, its STEPs, assembly.step, renders/) or one protoboard (<name>.json with its .png,
  .svg and -build.md) sits directly there; two or more get a folder each, named for what it is
  (main/, sensor/; enclosure/, bracket/, knob/: take the part's name from the conversation or the brief, never a
  generic "case"). KiCad's lib/ (project footprints, symbols, 3D models) sits in the KiCad area. Also fab/<board>/
  (generated), tools/ (generator scripts: the design's source, never archived or deleted), docs/, history/,
  .cadmate/work/ (scratch). docs/ is for the user's documents and the brief, never for design outputs. Going from one
  to two, use project_add_tool, project_add_board, project_add_part or project_add_protoboard: they snapshot, move the
  files and fix the paths. Never move them by hand.
- To compare options, build them in .cadmate/work/, show the user, apply the pick to the active project, delete the rest.
- Never rename a KiCad project: its schematic annotations are tied to the name. Use ${KIPRJMOD}-relative library paths.
- Never assume a board house. When a KiCad project starts, ask the user once where the board gets made (project_init
  and project_fab give the question): house, assembly, distributor, layers. Any of it can stay undecided. Record the
  answers with project_fab; the review, routing and export tools read them from the project, so don't ask again.
- Keep docs/brief.md: every requirement the user states, as a checklist. Re-read it before each stage and before
  showing a result; project_check lists the open items, and a stage isn't done until you've said which are met.
- Report DRC as errors, warnings, unconnected and parity, with the warnings by type, never errors alone.
- project_init on a new design folder; project_check before telling the user a step is done.
- check_ready_to_order before telling the user a board can be ordered, and again after any change to it.
- When the user orders a board (or calls a revision final), project_release freezes it as revision A, B, ... in
  releases/<board>/<rev>/ with what changed since the last one. Never edit a release; release the next revision.
- check_enclosure before showing the user an enclosure: collisions, openings nobody declared, joints with a gap or a step.
The cadmate-project skill has the reasons and the edge cases.

FreeCAD work can happen live in the user's open FreeCAD window, not only on saved files. Before the first FreeCAD step
in a session, call freecad_live_status and pass its tell_user to the user: whether the bridge is on, and how to start
or stop it (FreeCAD's Macro menu) if they want it the other way. Wait for their answer, then work. Do this once per
session, not before each change. If a FreeCAD tool's reply carries freecad_bridge, the check hadn't happened yet: pass
that on before your next step."""

server = MCPServer("cadmate", instructions=INSTRUCTIONS)

@server.tool()
def environment() -> dict:
    """Report which KiCad and FreeCAD installs cadmate found, their versions, and what works without them."""
    kicad, freecad = apps.kicad_info(), apps.freecad_info()
    return {
        "kicad": kicad.to_dict(),
        "freecad": freecad.to_dict(),
        "tools": {
            "read_footprint": True,
            "package_params": True,
            "link_model_to_footprint": True,
            "check_model_vs_footprint": freecad.available,
            "generate_footprint": True,
            "check_footprint": True,
            "review_footprint": True,
            "review_footprint_model": freecad.available,
            "compare_footprints": True,
            "generate_symbol": True,
            "check_symbol": True,
            "read_symbol": True,
            "compare_symbols": True,
            "generate_part": True,
            "generate_part_model": freecad.available,
            "check_part": True,
            "freecad_live": _live_running(),
            "kicad_live": True,
            "kicad_live_snapshot": kicad.available,
            "kicad_pcb_live": _pcb_running(),
            "review_schematic": True,
            "review_schematic_erc": kicad.available,
            "review_pcb_layout": True,
            "review_pcb_layout_drc": kicad.available,
            "classify_critical_nets": True,
            "route_critical_nets": True,
            "autoroute_rest": freerouting.status(),
            "protoboard": True,
            "check_enclosure": freecad.available,
            "check_model_structure": freecad.available,
        },
    }


@server.tool()
def read_footprint(path: str) -> dict:
    """Read a KiCad footprint (.kicad_mod): pads, pitch, pin 1, courtyard box and linked 3D models. Units are mm, Y down."""
    return footprint.read(path).to_dict()


@server.tool()
def list_package_families() -> dict:
    """List the package families cadmate knows and the datasheet dimensions each one needs."""
    return packages.FAMILIES


@server.tool()
def package_params(family: str, params: dict, footprint_path: str | None = None) -> dict:
    """Check datasheet dimensions for a package family before modelling.

    Reports missing values, impossible combinations, and pin count or pitch
    mismatches against the footprint when footprint_path is given.
    """
    fp = footprint.read(footprint_path) if footprint_path else None
    return packages.check_params(family, params, fp)


@server.tool()
def check_model_vs_footprint(
    footprint_path: str,
    model_path: str | None = None,
    body_length: float | None = None,
    body_width: float | None = None,
    body_height: float | None = None,
) -> dict:
    """Check a STEP model against its footprint using FreeCAD (headless).

    Uses the footprint's first linked model and its offset/rotation unless
    model_path is given. Checks centring over the pads, seating height,
    courtyard overlap, and body size against the datasheet values passed in: body_width along the footprint's X,
    body_length along its Y, as in the footprint families (a QFN-40 drawn 5 mm wide and 7 mm tall is width 5,
    length 7), each including the leads where they stick out.
    """
    fp_path = Path(footprint_path).expanduser()
    fp = footprint.read(fp_path)
    placement = fp.models[0] if fp.models else footprint.Model(path="")
    if model_path is None:
        if not fp.models:
            return {"ok": False, "errors": ["footprint has no linked model; pass model_path"]}
        model_path = placement.path
    resolved = model.resolve(model_path, fp_path.parent)
    expected = {k: v for k, v in (("length", body_length), ("width", body_width), ("height", body_height)) if v is not None}
    return _with_offer(model.check_file(fp, resolved, placement, expected or None))


@server.tool()
def link_model_to_footprint(
    footprint_path: str,
    model_path: str,
    offset: list[float] | None = None,
    rotate: list[float] | None = None,
    scale: list[float] | None = None,
    replace_existing: bool = True,
) -> dict:
    """Link a 3D model into a footprint file, keeping the original (history/ in a cadmate project, else a .bak).

    model_path is written as given, so KiCad variables like
    ${KICAD10_3DMODEL_DIR} or ${KIPRJMOD} stay portable. Close the footprint
    in KiCad's editor first, or reload it afterwards.
    """
    path = Path(footprint_path).expanduser()
    text = path.read_text(encoding="utf-8")
    model = footprint.Model(
        path=model_path,
        offset=tuple(offset or (0.0, 0.0, 0.0)),
        scale=tuple(scale or (1.0, 1.0, 1.0)),
        rotate=tuple(rotate or (0.0, 0.0, 0.0)),
    )
    new_text = footprint.link_model(text, model, replace=replace_existing)
    footprint.parse(new_text)  # never write a file we cannot read back
    backup = project.keep([path], "model-link")
    path.write_text(new_text, encoding="utf-8", newline="\n")
    return {"written": str(path), "backup": str(backup), "models": [m.__dict__ for m in footprint.parse(new_text).models]}


@server.tool()
def list_footprint_families() -> dict:
    """List the footprint families cadmate can generate, the datasheet values each needs, and the density levels."""
    return {"families": generate.FAMILIES, "densities": list(ipc.DENSITIES), "default_density": "nominal"}


@server.tool()
def generate_footprint(
    family: str,
    params: dict,
    library_dir: str,
    name: str | None = None,
    density: str = "nominal",
    courtyard_clearance: float | None = None,
    datasheet: str | None = None,
    model_path: str | None = None,
    description: str | None = None,
    tags: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Generate a KiCad footprint from datasheet dimensions and write it into a .pretty library folder.

    soic and qfn compute IPC-7351B pads from the package dimensions; give each
    dimension as the datasheet does: a number, [min, max], [min, nom, max] or
    "6.0+/-0.2". custom places pads exactly as the datasheet's recommended land
    pattern gives them. The result is checked before it is written; nothing is
    written when the check finds errors. An existing file is only replaced with
    overwrite=True, and the old file is kept (history/ in a cadmate project, else .bak). description and tags replace the
    family's own (KiCad's library style: "USB 3.2 Type-C receptacle, hybrid
    SMD/THT, https://...", tags "USB C Type-C receptacle").
    """
    lib = Path(library_dir).expanduser()
    if lib.suffix != ".pretty":
        return {"ok": False, "errors": ["library_dir must be a KiCad footprint library folder ending in .pretty"]}
    result = generate.generate(family, params, name=name, density=density, courtyard_clearance=courtyard_clearance,
                               datasheet=datasheet, model_path=model_path, description=description, tags=tags)
    text = result.pop("text")
    if not result["ok"]:
        return result
    path = lib / f"{result['name']}.kicad_mod"
    if path.exists() and not overwrite:
        result["ok"] = False
        result["errors"] = [f"{path} exists; pass overwrite=True to replace it (the old one is kept: in history/ inside a cadmate project, else as .bak)"]
        return result
    lib.mkdir(parents=True, exist_ok=True)
    kept = project.keep([path], "footprint")
    path.write_text(text, encoding="utf-8", newline="\n")
    result["written"] = str(path)
    if kept:
        result["previous"] = kept
    return result


@server.tool()
def check_footprint(
    path: str,
    family: str | None = None,
    params: dict | None = None,
    courtyard_clearance: float = check.COURTYARD_CLEARANCE,
) -> dict:
    """Review a KiCad footprint: touching pads, pads outside the courtyard, silkscreen on copper, and library conventions.

    With family and params (as for generate_footprint), the footprint is also
    compared pad by pad with the IPC-7351B footprint those datasheet values give.
    """
    fp = footprint.read(path)
    result = check.check_footprint(fp, courtyard_clearance=courtyard_clearance)
    if family and params:
        expected = generate.generate(family, params)
        if not expected["ok"]:
            result["warnings"].append(f"could not build the expected footprint from params: {expected['errors']}")
        else:
            cmp = check.compare_footprints(fp, footprint.parse(expected["text"]))
            result["vs_datasheet"] = cmp
            if cmp["only_in_candidate"] or cmp["only_in_reference"]:
                result["errors"].append("pad numbers differ from what the datasheet values give")
            elif not cmp["same_pads"]:
                result["warnings"].append(
                    f"pads differ from the IPC-7351B result by up to {cmp['max_pad_difference']} mm; fine if they follow the datasheet's own land pattern"
                )
            if fp.pitch() is not None and expected["summary"]["pitch"] is not None and abs(fp.pitch() - expected["summary"]["pitch"]) > packages.PITCH_TOLERANCE:
                result["errors"].append(f"pitch {fp.pitch()} mm differs from the datasheet's {expected['summary']['pitch']} mm")
            result["ok"] = not result["errors"]
    return result


@server.tool()
def review_footprint(
    path: str,
    model_path: str | None = None,
    datasheet_pads: list[dict] | None = None,
    leads: dict | None = None,
    body: dict | None = None,
    castellated: bool | None = None,
    underside_parts: list[dict] | None = None,
    standoff: float = 0.0,
    cutout: bool = False,
    board_thickness: float = review.BOARD_THICKNESS,
    symbol_path: str | None = None,
    symbol_name: str | None = None,
    courtyard_clearance: float = check.COURTYARD_CLEARANCE,
) -> dict:
    """Review any footprint, generated or not, against its 3D model, the datasheet and the symbol.

    Runs check_footprint's rules (courtyard, pad gaps, annular rings, silkscreen), then whatever the extra
    sources allow. Coordinates are the footprint editor's (mm, Y down).
    - Pad numbering: numbers running backwards along a row of pads (two pads swapped). datasheet_pads,
      [{number, x, y, name?}] read off the datasheet's drawing or pinout (any origin, Y up or down), checks every
      pad's number against the pin the datasheet puts there; with symbol_path and names it says which signal
      would reach which pin.
    - The linked model (or model_path, placed as the footprint places its model), solid by solid, through
      FreeCAD: bodies outside the courtyard; parts that go into the board (a module with parts on its underside
      soldered flat), with the cutout or standoff that fixes it; leads that miss or overfill their holes.
      cutout: the board is cut out under the part already. Without a model, underside_parts
      [{name?, x, y, size_x, size_y, height}] (height below the module's underside, which sits standoff mm above
      the board) stand in for it.
    - leads: the datasheet's lead, {lead_diameter} or {lead_width, lead_thickness}, for every plated hole, or
      {pad number or "*": {...}}; checks each hole fits the lead with IPC-7251's allowance.
    - Castellated modules: body {xmin, ymin, xmax, ymax}, the module's outline (else taken from the model's
      board). On the host board's footprint, every edge pad's copper must reach past the module's edge;
      castellated True/False forces this on or off (auto: through-hole pads whose copper is moved off the hole).
      Pads KiCad marks castellated (the module's own board) must have their hole cut in half by the edge.
    "checked" and "skipped" say which reviews ran and what input the others need.
    """
    sym = symbol.read(symbol_path, symbol_name) if symbol_path else None
    return review.review_file(path, model_path, cutout=cutout, underside_parts=underside_parts, standoff=standoff,
                              board_thickness=board_thickness, datasheet_pads=datasheet_pads, leads=leads, body=body,
                              castellated=castellated, sym=sym, courtyard_clearance=courtyard_clearance)


@server.tool()
def compare_footprints(path: str, reference_path: str, align: bool = True) -> dict:
    """Compare a footprint pad by pad with a reference, for example KiCad's library footprint for the same package.

    Footprints whose origins differ (one on pin 1, one on the pads' centre) are aligned on their pads first and the
    offset is reported as origin_offset; align=False compares them as they are.
    """
    return check.compare_footprints(footprint.read(path), footprint.read(reference_path), align=align)


@server.tool()
def generate_symbol(
    name: str,
    pins: list[dict],
    library_path: str,
    description: str | None = None,
    keywords: str | None = None,
    datasheet: str | None = None,
    footprint: str | None = None,
    footprint_path: str | None = None,
    reference: str = "U",
    fp_filters: list[str] | None = None,
    pin_length: float | None = None,
    unit_shapes: dict[str, str] | None = None,
    fields: dict[str, str] | None = None,
    overwrite: bool = False,
) -> dict:
    """Generate a KiCad schematic symbol from the datasheet's pin table and put it into a .kicad_sym library.

    pins: one entry per package pin, [{number, name, type, side?, group?, shape?, unit?, alternates?}].
    type is a KiCad electrical type (input, output, bidirectional, tri_state, passive, power_in,
    power_out, open_collector, open_emitter, no_connect, ...) or datasheet shorthand (I, O, I/O, P,
    G, OD, NC); it may be left out for pins named as power, ground or NC. Pins of one group sit
    together on one side; side (left/right/top/bottom) overrides the placement worked out from the
    type. unit splits a part into units (one per amplifier, plus one for power); unit_shapes
    {"1": "opamp", ...} draws those units as KiCad's opamp triangle (one +, one -, one output, and
    optionally a supply each way), the rest are rectangles. Active-low names are
    written ~{RST}; /RST, !RST and RST# are converted. fields adds hidden fields such as {"Manufacturer": ...,
    "MPN": ..., "LCSC": ..., "DigiKey": ...}: the part numbers go with the symbol into the schematic, the board and the BOM.

    The layout follows KiCad's library conventions (100 mil grid, inputs left, outputs right, supplies
    on top, grounds at the bottom, same-name power pins stacked, NC pins hidden). footprint is the
    default footprint as Library:Name; footprint_path, a local .kicad_mod, also checks that every pad
    has a pin and every pin a pad. The symbol is checked before it is written; nothing is written when
    the check finds errors. An existing symbol of the same name is only replaced with overwrite=True,
    and the old library is kept (history/ in a cadmate project, else .bak). Other symbols in the library are left byte for byte.
    """
    lib = Path(library_path).expanduser()
    if lib.suffix != ".kicad_sym":
        return {"ok": False, "errors": ["library_path must be a KiCad symbol library file ending in .kicad_sym"]}
    result = symbol_generate.generate(name, pins, reference=reference, footprint=footprint, datasheet=datasheet,
                                      description=description, keywords=keywords, fp_filters=fp_filters, pin_length=pin_length,
                                      unit_shapes=unit_shapes, fields=fields)
    text = result.pop("text")
    if not result["ok"]:
        return result
    if footprint_path:
        vs = symbol_check.compare_with_footprint(symbol.parse_symbol(text), _read_footprint(footprint_path))
        result["vs_footprint"] = vs
        if vs["errors"]:
            result["ok"] = False
            result["errors"] = vs["errors"]
            return result
    existing = lib.read_text(encoding="utf-8") if lib.exists() else None
    try:
        new_text = symbol.put_in_library(existing, text, name, replace=overwrite)
    except FileExistsError:
        result["ok"] = False
        result["errors"] = [f"{lib.name} already has {name}; pass overwrite=True to replace it (the old one is kept: in history/ inside a cadmate project, else as .bak)"]
        return result
    symbol.parse_library(new_text)  # never write a library we cannot read back
    lib.parent.mkdir(parents=True, exist_ok=True)
    kept = project.keep([lib], "symbol") if existing is not None else None
    lib.write_text(new_text, encoding="utf-8", newline="\n")
    result["written"] = str(lib)
    if kept:
        result["previous"] = kept
    return result


@server.tool()
def check_symbol(
    path: str,
    name: str | None = None,
    pins: list[dict] | None = None,
    footprint_path: str | None = None,
) -> dict:
    """Review a KiCad symbol against KiCad's library conventions (KLC): grid, pin lengths, stacking, types, power pin placement, fields.

    path is a .kicad_sym library (give name when it holds several symbols) or a file in a
    .kicad_symdir folder. With pins (the datasheet's pin table, as for generate_symbol) every pin is
    checked against the datasheet; with footprint_path, pins and pads are matched by number.
    """
    sym = symbol.read(path, name)
    result = symbol_check.check_symbol(sym)
    if pins:
        vs = symbol_check.compare_with_table(sym, pins)
        result["vs_datasheet"] = vs
        result["errors"] += vs["errors"]
        result["warnings"] += vs["warnings"]
    if footprint_path:
        vs = symbol_check.compare_with_footprint(sym, _read_footprint(footprint_path))
        result["vs_footprint"] = vs
        result["errors"] += vs["errors"]
    result["ok"] = not result["errors"]
    return result


@server.tool()
def read_symbol(path: str, name: str | None = None) -> dict:
    """Read a KiCad symbol: fields, pins (number, name, type, side, unit, hidden) and body. Units are mm, Y up."""
    return symbol.read(path, name).to_dict()


@server.tool()
def compare_symbols(path: str, reference_path: str, name: str | None = None, reference_name: str | None = None) -> dict:
    """Compare a symbol pin by pin with a reference, for example KiCad's library symbol for the same part."""
    return symbol_check.compare_symbols(symbol.read(path, name), symbol.read(reference_path, reference_name))


@server.tool()
def generate_part(
    project_dir: str,
    library: str,
    symbol: dict,
    footprint: dict,
    model: dict | None = None,
    datasheet: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Make a whole part from one datasheet: symbol, footprint and 3D model, linked to each other and checked together.

    symbol: {name, pins, description?, keywords?, reference?, fp_filters?, pin_length?, unit_shapes?, fields?}, as for
    generate_symbol; fields carries the part numbers (Manufacturer, MPN, and LCSC, DigiKey or Mouser numbers).
    footprint: {family, params, name?, density?, courtyard_clearance?, description?, tags?}, as for generate_footprint. Put the package
    height (and for soic the standoff) in params too; the 3D model is built from the same values.
    model: extra values for the 3D model (lead_thickness, terminal_thickness, standoff, lead_below, lead_diameter),
    or {path, offset?, rotate?, frame?} to link a model made by hand (frame "datasheet" when it was built in the
    datasheet's pad coordinates, so it follows the footprint's origin), or {skip: true}. Add cutout: true
    when the board has a cutout under the part (a module with parts underneath, soldered flat).

    Writes <project_dir>/<library>.kicad_sym, <library>.pretty/<footprint>.kicad_mod and
    <library>.3dshapes/<footprint>.step, the symbol's Footprint field set to <library>:<footprint> and the model
    linked as ${KIPRJMOD}/<library>.3dshapes/<footprint>.step, and adds the libraries to the project's
    sym-lib-table and fp-lib-table. Every pin must have a pad and every pad a pin, and the model must sit on the
    pads; nothing is written when any check finds errors. Existing files are only replaced with overwrite=True,
    keeping the old files (history/ in a cadmate project, else .bak). The model needs FreeCAD; without it, symbol and footprint are written without a model.
    """
    r = part.generate_part(project_dir, library, symbol, footprint, model, datasheet=datasheet, overwrite=overwrite)
    return r if model and model.get("skip") else _with_offer(r)


@server.tool()
def check_part(
    symbol_path: str,
    name: str | None = None,
    footprint_path: str | None = None,
    pins: list[dict] | None = None,
    body_length: float | None = None,
    body_width: float | None = None,
    body_height: float | None = None,
    cutout: bool = False,
    datasheet_pads: list[dict] | None = None,
    leads: dict | None = None,
    body: dict | None = None,
    castellated: bool | None = None,
    underside_parts: list[dict] | None = None,
) -> dict:
    """Review a part as KiCad will use it: the symbol, the footprint its Footprint field names, and the model that footprint links.

    Finds the footprint through the project's fp-lib-table (or <library>.pretty next to the symbol library) unless
    footprint_path is given, and checks each of the three plus the links: the footprint exists, the symbol's footprint
    filters accept it, every pin has a pad and every pad a pin, the model file exists and sits on the pads. With pins
    (the datasheet's pin table) the symbol is also checked pin by pin; body_* are the datasheet's overall model size,
    body_width along the footprint's X and body_length along its Y, as in the footprint families.
    cutout: the board has a cutout under the part, so a model may reach below it. The footprint and model get
    review_footprint's review; datasheet_pads, leads, body, castellated and underside_parts are its extra sources.
    """
    expected = {k: v for k, v in (("length", body_length), ("width", body_width), ("height", body_height)) if v is not None}
    options = {k: v for k, v in (("datasheet_pads", datasheet_pads), ("leads", leads), ("body", body), ("castellated", castellated),
                                 ("underside_parts", underside_parts)) if v is not None}
    return part.check_part(symbol_path, name, footprint_path, pins, expected or None, cutout, options)


# The interactive loop in the user's FreeCAD window, through the bridge they start (live.py).

_live_session = live.Session()
_bridge_offered = False  # the user was told at the start of FreeCAD work whether the bridge is on; once per session


def _bridge_offer(running: bool | None = None) -> dict | None:
    """live.offer() the first time it's asked for in this server (one per Claude session), None after."""
    global _bridge_offered
    if _bridge_offered:
        return None
    _bridge_offered = True
    return live.offer(_live_running() if running is None else running)


def _with_offer(reply: dict) -> dict:
    """A FreeCAD tool's reply, with the start-of-work bridge check added when it is the session's first."""
    offer = _bridge_offer()
    return reply | {"freecad_bridge": offer} if offer and isinstance(reply, dict) else reply


def _live_running() -> bool:
    try:
        return live.call("ping", timeout=2)["ok"]
    except live.BridgeUnavailable:
        return False


def _live(op: str, offer: bool = True, **args) -> dict:
    """Call the bridge, reporting the user's edits since the last call alongside the reply."""
    add = _with_offer if offer else (lambda r: r)
    try:
        changes = _live_session.user_changes()
        reply = live.call(op, **args)
    except live.BridgeUnavailable as e:
        return add(live.not_running() | {"error": str(e)})
    if not reply.get("ok") and str(reply.get("error", "")).startswith("unknown op"):
        reply = reply | live.outdated()
    return add(reply | {"user_changes": changes})


@server.tool()
def freecad_live_status() -> dict:
    """Check whether the user's FreeCAD window is reachable for live editing, and how to start the bridge if not.

    The first call in a session adds tell_user: what to say to the user before
    FreeCAD work starts (the bridge is on or off, and how to switch it).

    The live tools work on the FreeCAD window the user has open, through a
    bridge script the user starts from FreeCAD's Python console (the returned
    "start" line) the first time, and after that from FreeCAD's Macro menu
    (the cadmate_bridge_start macro the bridge saves); cadmate never starts it on its own. Every command shows in
    FreeCAD's Report view and every edit is one undo step named "cadmate: ...".
    """
    try:
        reply = live.call("ping", timeout=5)
        if reply.get("bridge") != live.BRIDGE_VERSION:
            raise live.BridgeUnavailable(f"the bridge running in FreeCAD is version {reply.get('bridge')}, cadmate needs {live.BRIDGE_VERSION}; "
                                         "paste the start line again to replace it")
    except live.BridgeUnavailable as e:
        reply = live.not_running() | {"error": str(e)}
    else:
        reply = reply | {"running": True}
    offer = _bridge_offer(reply["running"])
    return reply | {k: v for k, v in offer.items() if k != "running"} if offer else reply


@server.tool()
def freecad_live_state(doc: str | None = None) -> dict:
    """Read a document in the user's FreeCAD window as it is now, and what the user changed by hand since cadmate last looked.

    Each object has its name, label, type, editable properties (lengths,
    angles, links...), placement (position, yaw/pitch/roll in degrees),
    colour, visibility and exact bounding box [xmin, ymin, zmin, xmax, ymax,
    zmax] in mm. user_changes lists the objects the user created, deleted or
    edited (and which properties), and undo/redo they did. doc defaults to the
    active document. Read this before every edit and build on what is there.
    """
    return _live("state", doc=doc)


@server.tool()
def freecad_live_edit(code: str, label: str, doc: str | None = None) -> dict:
    """Run Python in the user's FreeCAD window as one undo step named "cadmate: <label>".

    Edit what is there, don't rebuild: change the properties of existing
    objects (doc.getObject("Box").Length = 25), move them, add features on top
    of them. Objects the user edited keep their edits that way. The code sees
    App, Gui, Part, V (App.Vector) and doc (the document); set `result` to
    return a value. The document is recomputed afterwards. If the code raises,
    the whole step is undone and the error comes back. origins_shown in the
    reply lists origins (Part and Body axes and planes) the code made visible,
    such as by showing every object in doc.Objects; hide them again unless the
    user asked for them. label is short and says what changed ("move USB-C
    0.5 mm toward the edge"); the user sees it in the undo list and the Report
    view.
    """
    return _live("run", code=code, label=label, doc=doc)


@server.tool()
def freecad_live_open(path: str, doc: str | None = None) -> dict:
    """Open a model in the user's FreeCAD window to work on it together.

    .FCStd opens the document; STEP, IGES or BREP is imported into a new
    document (or into doc); a FreeCAD Python script (a model build script,
    .py or .FCMacro) is run in the window, leaving the document it builds open.
    The view is fitted; a saved .FCStd keeps the origins and sketches its owner
    left showing. A body whose tip feature has no solid shows its last good
    feature instead, and is listed under no_solid: tell the user.
    """
    return _live("open", path=str(Path(path).expanduser()), doc=doc)


@server.tool()
def freecad_live_tidy_view(doc: str | None = None, view: str = "isometric", fit: bool = True,
                           hide_origins: bool = True, hide_helpers: bool = True) -> dict:
    """Leave the user's FreeCAD window presentable once a batch of changes is done.

    Brings the document's view to the front, turns it to a standard view
    (isometric, front, rear, top, bottom, left, right; "" keeps the user's
    angle), fits the model in the window, and hides the origins (the axes and
    planes of every Part and Body), the axis cross, and the sketches, datum
    planes/lines/points and binders inside each PartDesign Body (the layout
    sketch too), so the solid is what the user sees. Call it once at the end
    of a round of edits, before telling the user it's ready, not after every
    edit. Pass hide_origins=False, hide_helpers=False, another view, or
    fit=False when the user asked for origins or sketches on or for a
    particular view. Not an undo step; nothing in the model changes.

    If a body's tip feature has no solid (it failed, e.g. a pad on an
    over-constrained sketch, or came out empty), the body would show only its
    sketches. The tidy then shows the last feature before the tip that has a
    solid, plus the failing feature's sketch, and lists each such body under
    no_solid. Tell the user which feature failed and fix it (the tip is not
    moved). Returns what it hid and no_solid.
    """
    return _live("tidy_view", doc=doc, view=view, fit=fit, hide_origins=hide_origins, hide_helpers=hide_helpers)


@server.tool()
def freecad_live_snapshot(doc: str | None = None, view: str = "isometric", width: int = 800, height: int = 600) -> Image:
    """Picture of a document in the user's FreeCAD window, from a standard view, fitted to the model.

    view: isometric, front, rear, top, bottom, left, right, or "" to keep the
    user's current view (also skips the fit). The user's view is put back
    after the picture is taken.
    """
    r = _live("snapshot", offer=False, doc=doc, view=view, width=width, height=height, fit=bool(view))
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or json.dumps(r))
    return Image(path=r["file"])


@server.tool()
def freecad_live_save(doc: str | None = None, path: str | None = None) -> dict:
    """Save a document in the user's FreeCAD window, to path (.FCStd) or where it was last saved."""
    return _live("save", doc=doc, path=str(Path(path).expanduser()) if path else None)


@server.tool()
def freecad_live_export_step(
    path: str,
    doc: str | None = None,
    objects: list[str] | None = None,
    footprint_path: str | None = None,
    body_length: float | None = None,
    body_width: float | None = None,
    body_height: float | None = None,
) -> dict:
    """Export the model in the user's FreeCAD window as STEP, with colours, and check it against its footprint.

    Exports the named objects, or every visible solid that no other object is
    built from. With footprint_path, runs the same check as
    check_model_vs_footprint on the written file, using the footprint's model
    placement and the body sizes passed in (body_width along the footprint's X,
    body_length along its Y, body_height from the board's top surface up, not
    counting parts or leads below the board). Use the footprint the model
    will be linked into: its model offset and rotation are applied. Link it
    with link_model_to_footprint once it passes.
    """
    out = Path(path).expanduser()
    r = _live("export", path=str(out), doc=doc, objects=objects)
    if not r.get("ok") or not footprint_path:
        return r
    fp = footprint.read(Path(footprint_path).expanduser())
    placement = fp.models[0] if fp.models else footprint.Model(path="")
    expected = {k: v for k, v in (("length", body_length), ("width", body_width), ("height", body_height)) if v is not None}
    return r | {"check": model.check_file(fp, out, placement, expected or None)}


@server.tool()
def check_model_structure(path: str | None = None, doc: str | None = None) -> dict:
    """Check how a FreeCAD part is built, so it stays easy to change: sketches, datums, external geometry and binders
    that reference faces or edges the model generates (they move or break when that feature changes), sketches not
    fully constrained, features that change nothing (a cut that misses the part), sizes and attachment offsets not
    driven from one Params sheet (a constant typed as an expression still counts as typed; a sketch dimension local to
    its sketch may be named instead), fillets or chamfers before other features, FreeCAD's default names, features
    broken, suppressed or past the tip, unused sketches, and solids with no history or Part-workbench CSG where a
    printed piece should be a PartDesign Body. It can't see a fix stacked on a feature that still changes the shape.

    path: a saved .FCStd, or the folder holding the only one; checked headless. Without path, the document open in
    the user's FreeCAD window (doc, default the active one) is read through the live bridge, as it is now, without
    changing it. The report is saved beside the .FCStd as <name>-structure.json (from the window only when the
    document is saved and unchanged); project_check flags it when it's missing, stale or failing. Run it after each
    round of modelling and before showing the part; the freecad-modelling skill has the rules and how to fix each
    finding. Component models built by a script (kicad-3d-models) are dead solids by design: the script is the source.
    """
    if path:
        try:
            return _with_offer(structure.check(path))
        except (structure.StructureError, RuntimeError, OSError, ValueError) as e:
            return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
    r = _live("read", code=structure.live_code(), label="read model structure", doc=doc)
    if not isinstance(r.get("result"), dict):
        return r
    return structure.from_live(r["result"]) | {k: v for k, v in r.items() if k in ("user_changes", "freecad_bridge")}


# Footprints and symbols open in KiCad's editors, which have no API: cadmate works on the saved file (kicad_live.py).


def _kicad_live(fn, *args, **kw) -> dict:
    try:
        return fn(*args, **kw)
    except (kicad_live.LiveError, ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}


@server.tool()
def kicad_live_state(path: str, name: str | None = None) -> dict:
    """Read a footprint or symbol the user has open in KiCad, and what they changed by hand since cadmate last looked.

    path: a .kicad_mod, or a .kicad_sym library with name (needed when it holds several symbols). KiCad's footprint
    and symbol editors have no API, so cadmate works on the saved file: ask the user to save in KiCad (Cmd/Ctrl+S)
    before each round, or their latest edits aren't in it yet. user_changes lists, pad by pad or pin by pin, what
    differs from what cadmate last read or wrote, plus the raw text diff; first_look means there is nothing to compare
    with yet. Also returns the item's text (edit it with kicad_live_edit), a summary, the footprint or symbol check,
    and cadmate's steps so far. Read this before every edit and build on what is there.
    """
    return _kicad_live(kicad_live.state, path, name)


@server.tool()
def kicad_live_edit(path: str, edits: list[dict], label: str, name: str | None = None) -> dict:
    """Edit a footprint or symbol open in KiCad on top of the user's saved edits, as one step named by label.

    edits: [{old, new, all?}], text replacements inside the footprint (or inside the one symbol of the library), applied
    in order to the text as it is now (kicad_live_state gives it); old must occur once unless all is true. Change what
    is there: a pad's (at ...) or (size ...), a pin's (length ...), a field's value. Never rewrite the whole item or
    regenerate it, which throws away the user's edits. The result is parsed and checked before it is written; the reply
    gives the user's changes found first, what this step changed, every check error (new_errors and fixed_errors
    say what this step did) and "kicad": what the user has to do to see the change in KiCad, which KiCad 10 doesn't
    pick up by itself. Pass that on. Saving from an editor that still shows the old version writes over the step;
    kicad_live_state then reports it as overwrote_cadmate_step.
    label is one line the user will recognise in the history ("pads 2 and 3 0.1 mm longer").
    """
    return _kicad_live(kicad_live.edit, path, edits, label, name)


@server.tool()
def kicad_live_undo(path: str, name: str | None = None) -> dict:
    """Take back cadmate's last step on a footprint or symbol, keeping the user's hand edits made since.

    KiCad doesn't know cadmate's steps, so cadmate keeps them itself. When the user has changed
    the same text since, the undo is refused; make the reverse change as a new edit instead.
    """
    return _kicad_live(kicad_live.undo, path, name)


@server.tool()
def kicad_live_snapshot(path: str, name: str | None = None, width: int = 900) -> Image:
    """Picture of a footprint or symbol as saved, drawn by kicad-cli the way KiCad plots it.

    Look at it after an edit before telling the user what changed. It shows the file, so the user's unsaved edits in
    KiCad aren't in it.
    """
    try:
        return Image(path=kicad_live.snapshot(path, name, width))
    except (kicad_live.LiveError, OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(str(e)) from e


# The board open in KiCad's PCB editor, through KiCad's API (kicad_pcb_live.py).


def _pcb_running() -> bool:
    try:
        kicad_pcb_live.connect(timeout_ms=1500)
        return True
    except kicad_pcb_live.PcbUnavailable:
        return False


def _pcb(fn, *args, **kw) -> dict:
    try:
        return fn(*args, **kw)
    except kicad_pcb_live.PcbUnavailable as e:
        return {"ok": False, "running": False, "error": str(e), "how": kicad_pcb_live.ENABLE_HOW}
    except Exception as e:  # API errors: KiCad busy, bad request
        return {"ok": False, "error": f"{e.__class__.__name__}: {kicad_pcb_live.busy_hint(e)}"}


@server.tool()
def kicad_pcb_state(footprints: bool = True, tracks_on_nets: list[str] | None = None) -> dict:
    """Read the board open in the user's KiCad PCB editor, as it is now (saved or not), and what they changed since cadmate last looked.

    Needs KiCad's API switched on (KiCad > Settings… > Plugins > Enable KiCad API, then restart KiCad); the reply says
    how when it isn't. Returns user_changes (items added, removed or changed by the user since cadmate's last look or
    edit: footprints with ref and placement, tracks and vias with net, zones, graphics), the user's current selection
    (what "this one" means), counts by type, every footprint's ref, value, library, x, y (mm, Y down), rotation and
    side, and with tracks_on_nets the tracks and vias on those nets. Read this before every edit and build on it.
    """
    return _pcb(kicad_pcb_live.state, footprints, tracks_on_nets)


@server.tool()
def kicad_pcb_edit(code: str, label: str) -> dict:
    """Edit the board in the user's KiCad PCB editor as one undo step named "cadmate: <label>".

    code is Python using kicad-python (kipy), run inside one commit: if it raises, nothing changes on the board. It sees
    board (kipy Board), kicad, kipy, Vector2, Angle, BoardLayer, mm(x) (mm to KiCad's nanometres), to_mm(nm), math,
    footprint(ref) (the FootprintInstance with that reference) and place(ref, x=None, y=None, rotation=None) (mm,
    degrees). Change items and send them back with board.update_items(items); add with board.create_items, remove with
    board.remove_items. Example: f = footprint("C3"); f.position = Vector2.from_xy_mm(12.5, 8); board.update_items(f).
    Set result to return a value. Edit what is there and keep the user's changes; don't delete and recreate their
    items. The reply gives the user's changes found first and what this step changed. label is one line the user sees
    in KiCad's undo list ("C3 closer to U1's VDD pin").
    """
    return _pcb(kicad_pcb_live.edit, code, label)


@server.tool()
def kicad_pcb_undo() -> dict:
    """Take back cadmate's last step on the board with KiCad's own undo.

    Refused unless the board is exactly as that step left it: KiCad's undo takes back whatever came last, so a later
    change by the user would be undone instead. If KiCad's undo still takes back something else, cadmate redoes it.
    """
    return _pcb(kicad_pcb_live.undo)


@server.tool()
def kicad_pcb_save() -> dict:
    """Save the board in the user's KiCad PCB editor. Only when the user asks or agrees."""
    return _pcb(kicad_pcb_live.save)


@server.tool()
def kicad_pcb_snapshot(layers: str = "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Fab,Edge.Cuts", width: int = 1400) -> Image:
    """Picture of the board in the user's PCB editor as it is now, unsaved edits included, plotted by kicad-cli.

    KiCad writes a copy to a temporary folder for this; the user's file is untouched. layers is KiCad's comma list.
    """
    try:
        return Image(path=kicad_pcb_live.snapshot(layers, width))
    except (kicad_pcb_live.PcbUnavailable, kicad_live.LiveError, OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(str(e)) from e


@server.tool()
def review_schematic(path: str, limits: dict[str, float] | None = None, erc: bool = True) -> dict:
    """Review a schematic sheet (.kicad_sch) for readability, plus KiCad's ERC when kicad-cli is found.

    Checks: text colliding with other text or running over symbols; symbols drawn on top of each other; items
    outside the drawing area or under the title block; four-way junctions; ground symbols pointing up and
    supplies pointing down; and how many two-terminal parts (R, C, L, D, F, Y) connect only through net labels
    instead of wires (limits={"passive_label_share": 0.5}). stats gives the label and wire counts. Each finding
    has where, why and the source of the rule; text widths are estimates, so check flagged spots by eye.
    """
    try:
        return schematic_review.review_file(path, limits=limits, erc=erc)
    except schematic_review.ReviewError as e:
        return {"ok": False, "error": str(e)}


@server.tool()
def write_schematic(path: str, parts: list[dict], nets: dict[str, list[str]], no_connect: list[str] | None = None,
                    notes: list[dict] | None = None, paper: str = "A4", title: str = "",
                    libraries: dict[str, str] | None = None, show_names: list[str] | None = None,
                    replace: bool = False, plan_only: bool = False, erc: bool = True) -> dict:
    """Draw a new schematic sheet (.kicad_sch) from parts and nets, then review it (review_schematic and ERC).

    parts: [{ref, symbol: "Library:Name", value, footprint, block, at: [x, y], rot, mirror, unit, fields: {name:
    value}, ref_at, value_at}]; only ref and symbol are needed. Libraries are found as KiCad finds them (the project's
    sym-lib-table, KiCad's own, its installed symbols), or pass libraries {nickname: .kicad_sym or .kicad_symdir path}.
    Parts without at get a first layout: the biggest part of each block first, the parts wired to it beside the pins
    they connect to. Put parts that belong together (an MCU and its decoupling, a regulator and its caps) in one block.
    nets: {name: ["U1.3", "C1.VDD", ...]} by pin number or unique name. Supply and ground nets (+3V3, VCC, GND...) get
    a power symbol at each pin and a PWR_FLAG when nothing on the sheet drives them; pins in the same block are wired;
    nets leaving their block get a label; show_names labels a net even where it's wired. no_connect: unused pins.
    notes: [{text, at}]. Coordinates are mm, Y down, on the 1.27 mm grid.
    plan_only returns each part's outline and pin positions without writing, to plan positions first. It writes new
    sheets only (replace=True writes over one cadmate wrote, keeping the old one in history/ or as .bak); it doesn't edit a
    sheet someone drew. Fix what the review reports, then write again with replace=True.
    """
    try:
        return schematic_write.write(path, parts, nets, no_connect=no_connect, notes=notes, paper=paper, title=title,
                                     libraries=libraries, show_names=show_names, replace=replace, plan_only=plan_only,
                                     erc=erc)
    except (schematic_write.WriteError, OSError) as e:
        return {"ok": False, "error": str(e)}


@server.tool()
def board_from_schematic(schematic: str, out_path: str | None = None, libraries: dict[str, str] | None = None,
                         size: list[float] | None = None, layers: int = 2, scale: float = 0.5,
                         replace: bool = False, drc: bool = True) -> dict:
    """Start a new board (.kicad_pcb) from a schematic: every part's footprint, its pads on the schematic's nets.

    schematic is the project's root .kicad_sch; the parts and nets come from KiCad's own netlist (needs kicad-cli),
    so sub-sheets, global labels and power symbols join as KiCad joins them. Footprints come from each symbol's
    Footprint field, found through the project's fp-lib-table, KiCad's, or its installed libraries (or pass libraries
    {nickname: .pretty folder}). out_path defaults to the schematic's name with .kicad_pcb, beside it, so KiCad pairs
    them. Parts start where their symbols sit on the sheet (scale 0.5: 10 mm on the sheet is 5 mm on the board),
    moved apart until no courtyards overlap, with a board outline around them, or size [w, h] in mm. Parts on
    sub-sheets have no position on the top sheet, so they go in rows below the rest; overlapping lists any part that
    found no free spot. layers is 2 or 4. Each footprint stays linked to its symbol and gets its fields (hidden) and its
    DNP and exclude-from-BOM/position flags, as Update PCB from Schematic does; the reply has KiCad's schematic parity
    check and DRC. This starts boards: it replaces the empty board KiCad's New Project makes (keeping a copy), but
    won't write over a board with anything on it that cadmate didn't write. Placement comes next: propose_placement.
    """
    try:
        return pcb_write.from_schematic(schematic, out_path, libraries=libraries, size=size, layers=layers, scale=scale,
                                        replace=replace, drc=drc)
    except (pcb_write.BoardWriteError, OSError) as e:
        return {"ok": False, "error": str(e)}


@server.tool()
def review_pcb_layout(path: str | None = None, currents: dict[str, float] | None = None, roles: dict[str, str] | None = None,
                      fab: str | None = None, limits: dict[str, float] | None = None, drc: bool = True) -> dict:
    """Review a routed board's layout, by hand or by an autorouter: what an experienced reviewer would point out.

    path is a .kicad_pcb; leave it out to review the board open in KiCad's PCB editor (unsaved edits included).
    Checks: each IC power pin's nearest bypass cap (placement distance, track length against the straight line, vias,
    and the loop through the ground pin); crystal traces (length, vias, other signals alongside or through its area,
    ground under it); signals near switching nodes; tracks narrower than their net class; current per IPC-2221 for the
    nets in currents ({net: amps}), and the capacity of power nets without one; vias per net; fast signals crossing
    gaps in the ground pour; the board house's track, via and edge minimums and, for JLCPCB, its SMD spacing (fab:
    "jlcpcb", "pcbway", "oshpark", "aisler", "eurocircuits", or "none"; left out, the project's choice from project_fab, else no house is assumed: the reply's
    fab says which was used and not_checked what was skipped); and
    KiCad's own DRC (unrouted nets, clearances) when kicad-cli is found; drc gives errors, warnings, unconnected and parity with the types: report all four. Net roles are inferred from names, pin types,
    power symbols and parts; net_roles says what was inferred and why. Correct them with roles ({net: "ground" |
    "power" | "switching" | "crystal" | "fast" | "sensitive" | "signal"}). limits overrides cadmate's own thresholds
    (listed in the reply under limits; they are heuristics, not standards). Each finding has a location, the reasoning
    and a source key into sources. not_checked says which rules didn't apply and why.
    """
    try:
        kw = dict(currents=currents, roles=roles, fab=fab, limits=limits, drc=drc)
        return pcb_review.review_file(path, **kw) if path else pcb_review.review_live(**kw)
    except kicad_pcb_live.PcbUnavailable as e:
        return {"ok": False, "running": False, "error": str(e), "how": kicad_pcb_live.ENABLE_HOW,
                "or": "pass path to review a saved .kicad_pcb instead"}
    except (pcb_review.ReviewError, OSError, ValueError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


def _flow_call(fn, **kw) -> dict:
    try:
        return fn(**kw)
    except kicad_pcb_live.PcbUnavailable as e:
        return {"ok": False, "running": False, "error": str(e), "how": kicad_pcb_live.ENABLE_HOW,
                "or": "pass path to work on a saved .kicad_pcb instead"}
    except (pcb_flow.FlowError, freerouting.RouteError, OSError, ValueError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@server.tool()
def classify_critical_nets(path: str | None = None, roles: dict[str, str] | None = None, currents: dict[str, float] | None = None,
                           overrides: dict[str, str] | None = None, limits: dict[str, float] | None = None,
                           classes: list[str] | None = None) -> dict:
    """Find the connections on a board that must be routed with care, before routing: the first step of routing a board.

    path is a .kicad_pcb; leave it out for the board open in KiCad's PCB editor. Classes, in routing order: RF/antenna
    feeds, switching-regulator hot loops, decoupling cap to pin, crystals and oscillators, differential pairs (USB,
    Ethernet, LVDS, HDMI, PCIe, CAN) with length matching, ESD/TVS protection, sensitive analog (feedback, ADC, references,
    sense), reset/boot/strap nets, high-current paths (IPC-2221 widths from currents), and ground return/pours. Each item
    has the parts, nets and connections, the rule with its numbers, the sources (keys into sources), why cadmate put it in
    that class, any placement problem, and an action: route (cadmate routes and locks it), place (move a part first),
    hand (route by hand), netclass (width for the autorouter), ask (needs a current), check (review after routing).
    table is the classification as Markdown: show it to the user and let them confirm or override before routing.
    overrides {item id: action} changes an item's action; roles {net: role} and currents {net: amps} change what is found;
    classes limits the classes run. limits overrides cadmate's heuristic numbers (in limits).
    """
    return _flow_call(pcb_flow.classify, path=path, roles=roles, currents=currents, overrides=overrides, limits=limits, classes=classes)


@server.tool()
def route_critical_nets(path: str | None = None, out_path: str | None = None, roles: dict[str, str] | None = None,
                        currents: dict[str, float] | None = None, overrides: dict[str, str] | None = None,
                        limits: dict[str, float] | None = None, items: list[str] | None = None, dry_run: bool = False,
                        in_place: bool | None = None) -> dict:
    """Route the critical connections that classify_critical_nets marked "route" (or only the item ids in items), and lock them.

    Pass the same roles, currents and overrides the user confirmed. Short, direct tracks on one layer where the rule asks
    for it, ground pads into the pour with a via at the pad, pairs routed together; clearances from the net classes are
    kept, and anything that can't be routed within its rule is listed under by_hand with the reason instead. Every
    segment and via added is locked, so Freerouting routes around them; nothing else is locked. On the open board the
    result is one undo step (not saved).
    pairs reports each pair's lengths and mismatch against its rule. dry_run routes without writing anything.
    Saved boards: inside a cadmate project the board itself is changed, after a snapshot of its files into history/
    (snapshot in the result; project_restore undoes the step); in_place=False writes to .cadmate/work/ instead.
    Outside a project it goes to out_path, default <name>-critical.kicad_pcb.
    """
    return _flow_call(pcb_flow.route_critical, path=path, out_path=out_path, roles=roles, currents=currents, overrides=overrides,
                      limits=limits, items=items, dry_run=dry_run, in_place=in_place)


@server.tool()
def propose_placement(path: str | None = None, roles: dict[str, str] | None = None, limits: dict[str, float] | None = None,
                      everything: bool = False) -> dict:
    """Propose moves that put each bypass cap at its IC power pin and each crystal, with its load caps, at its MCU pins.

    Nothing changes on the board. path is a .kicad_pcb; leave it out for the board open in KiCad's PCB editor. Built from
    classify_critical_nets' decap and crystal items: a cap goes just outside the IC's courtyard beside its pin, turned so
    its power pad faces the pin and its ground pad points out; a crystal goes beside the XI/XO pins, turned to match them,
    with its load caps just beyond it. Each spot is clear of every other courtyard and inside the board outline; parts
    already within the limits stay unless everything is set. moves lists ref, from, to {x, y, rotation}, the item it is for,
    why, and the pad-to-pin distance before and after; table is the same as Markdown. Show the user the table and apply
    only the moves they agree to, with apply_placement. Lay out connectors, mounting holes and big parts first: this
    places the small parts around them.
    """
    return _flow_call(pcb_place.propose_path, path=path, roles=roles, limits=limits, everything=everything)


@server.tool()
def apply_placement(moves: list[dict], path: str | None = None, out_path: str | None = None, in_place: bool | None = None) -> dict:
    """Make the placement moves the user agreed to: each {ref, to: {x, y, rotation}} as propose_placement gave it (or edited).

    Moves carrying from are refused if that part has moved since the proposal. On the open board (no path) all moves are one
    undo step, not saved. With a path, the result goes to out_path, or in a cadmate project into the board itself after a
    snapshot (in_place). courtyard_overlaps lists any that remain around the moved parts. Run classify_critical_nets again
    after: the decap and crystal items should then be ready to route.
    """
    return _flow_call(pcb_place.apply, path=path, moves=moves, out_path=out_path, in_place=in_place)


@server.tool()
def autoroute_rest(path: str | None = None, out_path: str | None = None, currents: dict[str, float] | None = None,
                   roles: dict[str, str] | None = None, passes: int = 30, timeout: int = 1200, fab: str | None = None,
                   margin: float = 0.25, neck_rules: bool = True, in_place: bool | None = None) -> dict:
    """Let Freerouting route everything still unrouted, around the locked critical tracks, and bring the result back.

    Needs Freerouting's jar (KiCad's Freerouting plugin has one), java, and KiCad's bundled Python for the DSN export;
    environment() says what was found. Nets with a current get the IPC-2221 width as a Freerouting class, capped at the
    smallest pad on the net (widths_capped says where). On the way back, tracks narrower than their net class are widened
    (widened lists them) and zones are refilled. On the open board the result is one undo step. unrouted lists nets
    still not joined. Then run review_pcb_layout.
    Saved boards: inside a cadmate project the board itself is changed, after a snapshot of its files into history/
    (snapshot in the result; project_restore undoes the step); in_place=False writes to .cadmate/work/ instead.
    Outside a project it goes to out_path, default <name>-routed.kicad_pcb.
    On a board with inner-layer planes, each SMD pad on a plane net first gets a locked via into its plane (plane_fanout
    says how many, and which pads had no room), since Freerouting doesn't route to planes itself.

    fab_check sorts Freerouting's narrow tracks against the fab's minimums ("jlcpcb", "pcbway", "oshpark", "aisler"
    or "eurocircuits"; left out, the
    project's choice from project_fab; with no house chosen it's skipped and KiCad's DRC checks the board's own rules):
    errors are under the fab's own minimum and must be rerouted, warnings are between it and the minimum plus margin
    (25 % by default) and stay only where there's no room. Freerouting necks tracks into small pads and can't be told not to
    from its command line. With neck_rules, the necks into pads the fab can make get a KiCad custom rule (the board's
    .kicad_dru, cadmate's block only) allowing them inside that part's courtyard, so DRC flags only what needs fixing.
    """
    return _flow_call(pcb_flow.autoroute, path=path, out_path=out_path, currents=currents, roles=roles, passes=passes, timeout=timeout,
                      fab_name=fab, margin=margin, neck_rules=neck_rules, in_place=in_place)


@server.tool()
def finish_routing(path: str, out_path: str | None = None, outer_only: bool = True, in_place: bool | None = None) -> dict:
    """Clean up after autoroute_rest, the way you would by hand: tracks that break clearance or are under the board's
    minimum width (Freerouting squeezes some past fine-pitch pins) are taken out with the stubs they leave, then every net
    still in pieces is joined with cadmate's grid router, nearest pads first, and the zones are refilled. Locked tracks
    are never touched. With outer_only (default) new tracks stay on the outer layers so inner planes stay whole.
    path is a saved .kicad_pcb. still_open lists what it couldn't join, with the reason, to route by hand. Run KiCad's
    DRC on the result.
    Saved boards: inside a cadmate project the board itself is changed, after a snapshot of its files into history/
    (snapshot in the result; project_restore undoes the step); in_place=False writes to .cadmate/work/ instead.
    Outside a project it goes to out_path, default <name>-finished.kicad_pcb.
    """
    return _flow_call(pcb_flow.finish_routing, path=path, out_path=out_path, outer_only=outer_only, in_place=in_place)


@server.tool()
def export_fab(path: str, out_dir: str | None = None, fab: str | None = None) -> dict:
    """Manufacturing files from a finished .kicad_pcb, for any board house: Gerbers and drill files (zipped, ready to
    upload), a BOM (Designator, Quantity, Value, Footprint, Manufacturer, MPN, and the LCSC, DigiKey or Mouser numbers
    the parts have) and a placement file (Designator, Value, Footprint, X, Y in mm, Rotation, Side). With JLCPCB as the
    house, its own BOM (Comment, Designator, Footprint, LCSC Part #) and CPL come too, and no_lcsc_number lists parts
    without one. An assembly drawing comes for any house: <board>-assembly-top.pdf, and -bottom.pdf (mirrored) when parts
    sit underneath, from the fabrication layers (outlines and references); not_on_assembly_drawing lists parts whose
    footprint draws nothing there. fab: "jlcpcb", another house's name, or "none"; left out, the project's choice from
    project_fab, else none is assumed. For a house with no column format of its own, note says what it asks for. DNP and excluded parts are left out; no_part_number lists parts with no part number of any kind.
    Zones are refilled on a copy first. Run DRC first. Goes to out_dir (default <project>/fab/<board name> in a cadmate
    project, else <board folder>/fab/<board name>); the Gerbers there are regenerated, not added to.
    """
    try:
        return fab_export.export(Path(path), Path(out_dir) if out_dir else None, fab=fab)
    except (fab_export.ExportError, OSError, ValueError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@server.tool()
def export_board_step(path: str, out: str | None = None, part: str | None = None) -> dict:
    """The board as a STEP with its parts' 3D models (kicad-cli, DNP parts left out), to fit an enclosure around it in
    FreeCAD. In a cadmate project with FreeCAD it goes to the FreeCAD part's pcb-models/<board>.step (part names which,
    when there are several); otherwise beside the board, or to out. no_3d_model lists parts without a model (or only
    hidden ones), model_not_loaded parts whose model link is missing or VRML-only, with what kicad-cli said: all are
    missing from the STEP and from check_enclosure's collisions, so tell the user. Re-export after the board changes."""
    try:
        return fab_export.export_step(Path(path), Path(out) if out else None, part)
    except (fab_export.ExportError, OSError, ValueError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@server.tool()
def check_ready_to_order(path: str, fab: str | None = None, currents: dict[str, float] | None = None,
                         roles: dict[str, str] | None = None, fab_dir: str | None = None,
                         packages: dict[str, str] | None = None) -> dict:
    """Before a board is ordered: every check it needs, in one call, sorted into what blocks the order (blocking), what
    to decide with the user (to_look_at), what passed, and what couldn't run (not_checked). Runs KiCad's DRC with
    schematic parity and its ERC, the layout review (with currents and roles as for review_pcb_layout), part numbers in
    the BOM (blocking when the house assembles it; for JLCPCB assembly, LCSC numbers too; numbers for the project's
    distributor), each part's package as bought against its footprint (from a Package field, or packages
    {ref: package text as the distributor lists it, e.g. "SOT-23-5", "0805", "QFN-32-EP(5x5)"}), the copper layer count against the project's, the fab files the house needs, all newer than the
    board (fab_dir if they were exported somewhere other than export_fab's default), and, in a cadmate project, the brief's unticked requirements, project errors and the enclosure check. ready
    is true only when DRC ran and nothing blocks. fab as for review_pcb_layout; with no house chosen it isn't assumed,
    and ask_user is the question to put to the user. Give the user ready or not first, then every blocking item, and
    DRC as all four numbers. path is a saved .kicad_pcb."""
    try:
        return ready.check(path, fab=fab, currents=currents, roles=roles, fab_dir=fab_dir, packages=packages)
    except (ready.ReadyError, pcb_review.ReviewError, OSError, ValueError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@server.tool()
def set_part_numbers(schematic: str, parts: dict[str, dict[str, str]], board: str | None = None,
                     dry_run: bool = False) -> dict:
    """Write part numbers onto parts already in a schematic, many at once, and onto the board beside it: for a sheet
    drawn with generic symbols (an R with a value) that needs numbers before it's ordered.

    parts: {ref: {field: value}}, a key may list refs that get the same part ("R1,R2,R7"), e.g.
    {"R1,R2": {"Manufacturer": "Yageo", "MPN": "RC0603FR-0710KL", "LCSC": "C98220"}}. Any field but Reference, Value
    and Footprint; "" empties one. Part-number fields (Manufacturer, MPN, LCSC, DigiKey, Mouser, Package) are matched
    however the sheet spells them (MFR Part Number, LCSC Part #): an existing one is updated where it is, a new one takes
    the spelling the sheet already uses, else the plain name. schematic is the root .kicad_sch; parts on sub-sheets are
    found through the hierarchy, and every unit of a part gets the fields. A sheet used more than once shares one set
    of fields, so a ref there sets its copies too (shared_symbols), and different numbers for them are refused
    (conflicts). board defaults to the .kicad_pcb beside the schematic; footprints get the same fields, so export_fab
    and check_ready_to_order see them without an Update PCB. Only the named fields change; every other byte of the
    files stays. The old files are kept first (a snapshot in history/ inside a project, else .bak). Refused while KiCad
    has a file open. changed gives each part's value and footprint beside its new fields: check them against what was
    picked. dry_run shows the changes without writing. cadmate doesn't search distributors: find the parts with the
    user's search tool, agree the picks with the user, then set them here."""
    try:
        return part_numbers.set_part_numbers(schematic, parts, board=board, dry_run=dry_run)
    except (part_numbers.PartNumberError, OSError, UnicodeDecodeError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


# Projects: one active project, a fixed layout, snapshots in history/ (project.py, skills/cadmate-project).


def _project(fn, *a, **kw) -> dict:
    try:
        return fn(*a, **kw)
    except (project.ProjectError, OSError, ValueError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@server.tool()
def project_init(root: str, name: str | None = None, boards: list[str] | None = None,
                 tools: list[str] | None = None, parts: list[str] | None = None,
                 protoboards: list[str] | None = None) -> dict:
    """Make a design folder a cadmate project: cadmate-project.json, README.md, CLAUDE.md (the rules, for every session
    working there), tools/, docs/ with brief.md, history/ with HISTORY.md, and .cadmate/work/ for scratch.

    tools: any of "kicad", "freecad", "protoboard"; pass what the design uses (ask if it isn't clear). One keeps its
    files at the project root (with lib/ and fab/ for KiCad); several make a folder each (kicad/, freecad/,
    protoboard/). Left out, it goes by the files already there, else kicad and freecad. boards, parts, protoboards:
    their names, if known. One board, part or protoboard sits directly in its tool's place, so no folder is made; two or more get a folder each. Name a part for what it is (enclosure, bracket, knob),
    from the conversation or the brief. Safe on an existing folder: it only adds what's missing, never moves or
    overwrites; run project_check after to see what's out of place. For a KiCad project whose fab choices were never
    asked, ask_user is the question to put to the user once; record the answers with project_fab."""
    return _project(project.init, root, name, boards, tools, parts, protoboards)


@server.tool()
def project_add_tool(path: str, tool: str) -> dict:
    """Add KiCad, FreeCAD or protoboards to a project. To a one-tool project: snapshots the whole project first, then
    moves the existing tool's files from the root into kicad/ (boards and lib/ together, so ${KIPRJMOD} paths still
    work), freecad/ or protoboard/, and the new tool's files lying at the root into its folder. With folders already,
    it just makes the new one's. Updates cadmate-project.json. Nothing inside a file is rewritten. Ask the user to save and
    close the files in KiCad and FreeCAD first; they reopen them from the new place after."""
    return _project(project.add_tool, path, tool)


@server.tool()
def project_add_board(path: str, board: str | None = None) -> dict:
    """Add a second board. The board sitting directly in the KiCad area (the root, or kicad/) moves into a folder named
    after it, after a snapshot, and its ${KIPRJMOD} paths (lib tables, 3D model links) get the ../ they now need; board
    is the new one's name, and gets its folder too. With board folders already, it only makes the new folder. Ask the
    user to save and close the board in KiCad first."""
    return _project(project.add_board, path, board)


@server.tool()
def project_add_part(path: str, part: str | None = None, current: str | None = None) -> dict:
    """Add a second FreeCAD part. The part sitting directly in the FreeCAD area (the root, or freecad/) moves into a
    folder named for it, after a snapshot, with its STEPs, renders/ and enclosure files; part is the new one's name,
    and gets its folder too. current: what the part already there is (enclosure, bracket), from the conversation or the
    brief; left out, it's taken from the part's only .FCStd. Ask the user to save and close the files in FreeCAD first."""
    return _project(project.add_part, path, part, current)


@server.tool()
def project_add_protoboard(path: str, name: str | None = None, current: str | None = None) -> dict:
    """Add a second protoboard. The one sitting directly in the protoboard area (the root, or protoboard/) moves into a
    folder named for it with its renders and build list, after a snapshot; name is the new one's, and gets its folder
    too. current: the name of the one already there; left out, it's taken from its only design JSON."""
    return _project(project.add_protoboard, path, name, current)


@server.tool()
def project_snapshot(path: str, label: str, paths: list[str] | None = None, note: str = "") -> dict:
    """Keep the current state before changing it: copies it into history/<YYYYMMDD-HHMM>_<label>/ at the same relative
    paths and adds a line to history/HISTORY.md. Then make the change in the active project itself, never in a copy.

    path is the project or anything in it. label: lowercase words with dashes saying what state this is or what comes
    next ("approved-layout", "before-rework"). paths limits it (e.g. ["kicad/main"], ["freecad/enclosure"], or
    ["main.kicad_pcb"] for a lone board at the root); leave it out for the whole project. A KiCad board folder brings along the project libraries it references through ${KIPRJMOD}, so
    the copy opens with its footprints and 3D models. note says why, in a sentence: it goes into HISTORY.md."""
    return _project(project.snapshot, path, label, paths, note)


@server.tool()
def project_fab(path: str, house: str | None = None, assembly: bool | str | None = None, distributor: str | None = None,
                layers: int | str | None = None) -> dict:
    """Where the project's board gets made, as the user chose it: house (any name; cadmate has rule profiles for JLCPCB
    and PCBWay), assembly by the house (yes or no), the distributor parts come from, and copper layers. Ask the user once
    at the start of a KiCad project; with no arguments this only reads the choices, and gives the question (ask_user) if
    they were never asked. Fields left out keep their value; "undecided" clears one, and undecided is a fine answer.
    The choices go into cadmate-project.json and the brief's Fab section. review_pcb_layout, autoroute_rest and
    export_fab use them when not given a fab."""
    return _project(project.set_fab, path, house=house, assembly=assembly, distributor=distributor, layers=layers)


@server.tool()
def project_history(path: str) -> dict:
    """The project's snapshots, oldest first: name, label, when, note and what each holds."""
    return _project(project.history, path)


@server.tool()
def project_restore(path: str, name: str, paths: list[str] | None = None) -> dict:
    """Bring a snapshot back into the active project (all of it, or just paths such as ["kicad/main"]). The current
    state is snapshotted first, so a restore can itself be undone. Re-export fab/ for any board restored."""
    return _project(project.restore, path, name, paths)


@server.tool()
def project_release(path: str, revision: str | None = None, notes: str = "", board: str | None = None,
                    renders: list[str] | None = None, render: bool = True) -> dict:
    """Release a board revision, when the user orders it or calls a state final. Snapshots the whole project
    (history/<stamp>_release-<board>-<rev>/, never pruned) and freezes the board's fab files, renders and RELEASE.md into
    releases/<board>/<rev>/, with what changed since the previous revision: parts added, removed or changed, nets,
    copper layers, outline size, track and via counts. Run export_fab (and check_ready_to_order) first: it refuses fab files
    older than the board, and never overwrites a release.

    path: the .kicad_pcb, or the project when it has one board (else pass board). revision: A, B, ... or a number;
    left out, the next letter. notes: what the user says this revision is for or fixes. renders: project paths of
    pictures to include (a FreeCAD render, a renders/ folder); render adds top and bottom pictures through kicad-cli
    when it's installed. Warns when the board's title block shows another revision."""
    return _project(release.release, path, revision, notes, board, renders, render)


@server.tool()
def project_prune(path: str, keep: int = 10, delete: list[str] | None = None) -> dict:
    """Free space in history/: delete the automatic snapshots tools took before changing something (routing passes,
    restores, moves), keeping the newest keep. Snapshots taken on purpose with project_snapshot and released ones are
    never touched. Without delete it only lists what would go (would_delete) and the space freed: show the user, and on
    their yes call again with delete set to those names; only those still beyond the newest keep are deleted. Deleted
    snapshots can't be restored; HISTORY.md marks them pruned."""
    return _project(project.prune, path, keep, delete)


@server.tool()
def project_check(path: str) -> dict:
    """Check a project against the layout: files outside it or too deep, names that mark versions or variants
    (-v2, -routed, -hand3, variant-*, .bak), more than one board per board folder, a KiCad project not named
    after its folder, schematic annotations that belong to another project name (a renamed or copied project), library
    tables and 3D model links that are absolute, missing or outside the project, fab files older than their board or
    for a board that's gone, badly named snapshots, leftover scratch, an assembly whose enclosure check is missing,
    older than its STEPs or failing, the other tool's files at the root of a one-tool project (project_add_tool), and a
    board or part sitting loose beside board or part folders (project_add_board, project_add_part), a released revision
    changed after release, and automatic snapshots piling up (project_prune). The result says
    which tools the project uses and whether it's flat. Each finding has a fix. Run it before telling the user a step
    is done."""
    return _project(project.check, path)


# Enclosures: collisions, closed shells and flush joints on an assembly (enclosure.py, freecad_scripts/check_enclosure.py).


@server.tool()
def check_enclosure(path: str, spec: dict | str | None = None) -> dict:
    """Check a printed enclosure in FreeCAD before showing it: parts that collide (case with boards, components, flex
    cables, each other), openings in each shell that aren't declared (a slot left in a wall, a gap at a seam), and
    joints between case parts that don't sit flush (a gap, or one part standing proud of the other), plus part heights
    against the brief.

    path: the part's folder (the one with assembly.step), an assembly STEP whose children are the placed parts, named,
    or the project when only one part has an assembly. The case parts are the ones with a <piece>.step beside the
    assembly. spec (a dict or JSON path; default enclosure.json beside the assembly) declares the openings meant to be
    there, joint limits, heights and allowed overlaps: see enclosure.py for the format. Without a spec every opening is
    reported, so the first run is how you list them: write the intended ones into enclosure.json, keep the rest as
    errors to fix. The report is saved beside the assembly as enclosure-check.json; project_check flags it when it's
    missing, stale or failing. Takes a minute or two with a few boards; longer with min_clearance."""
    try:
        return _with_offer(enclosure.check(path, spec))
    except (enclosure.EnclosureError, RuntimeError, OSError, ValueError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


# Protoboards: through-hole parts on a perfboard's 2.54 mm hole grid (protoboard.py, protoboard_render.py).


def _proto(fn, *a, **kw) -> dict:
    try:
        return fn(*a, **kw)
    except (protoboard.DesignError, OSError, ValueError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@server.tool()
def list_protoboard_parts() -> dict:
    """Parts the protoboard tools know: pins in holes from pin 1, which holes the body covers, and lead notes (parts
    whose leads must be bent onto the 2.54 mm grid). A part not in the list can be given inline as a custom part."""
    return protoboard.list_parts()


@server.tool()
def check_protoboard(design: dict | str) -> dict:
    """Check a perfboard design (a dict, or the path of a JSON file holding one): pins off the board, two leads in one
    hole, a lead in a hole under another part's body, bodies on top of each other, unknown parts or pins in the nets,
    and for any wires already drawn: shorts, jumpers ending in a lead's hole or lying over a part, crossing jumpers,
    and which nets are complete. Holes are named row letter + column number ("C5"), A1 top left from the component
    side; a part's "at" is pin 1's hole and rotation turns it clockwise. See list_protoboard_parts for the parts."""
    return _proto(protoboard.check, design)


@server.tool()
def route_protoboard(design: dict | str, out_path: str | None = None, allow_jumpers: bool = True) -> dict:
    """Route a perfboard design on its hole grid: bottom-side solder bridges and bare wire pad to pad along rows and
    columns, top-side tracks the same way on unplated double-sided boards (board "pads": "double-unplated"; via_wires
    lists the holes that need a wire through), and insulated jumper wires (straight, each end in its own free hole)
    where neither side can get through. Wires already in the design are kept as they are. Placement isn't changed: if nets stay open, move parts
    (or draw those wires by hand in "wires") and route again. The routed design (with "wires") is returned and, when
    out_path is given, written there as JSON. Look at render_protoboard's picture before handing it to the user.

    In a cadmate project the design is a protoboard design like a board or a part: <name>.json directly in the
    protoboard area (the root, or protoboard/) when it's the only one, else <name>/<name>.json. Never in docs/.
    Write the result back over the design's own file (project_snapshot first if the old state matters), not a
    -routed copy."""
    r = _proto(protoboard.route, design, allow_jumpers=allow_jumpers)
    if out_path and r.get("design"):
        r["written"] = str(protoboard.save(r["design"], out_path))
    return r


@server.tool()
def place_protoboard(design: dict | str, out_path: str | None = None, tries: int = 3, route: bool = True) -> dict:
    """Place the parts of a perfboard design automatically, then route. Parts can steer it: "fixed": true with "at"
    keeps a part exactly where it is; "at" alone is a starting point it stays near; "near": "C5" keeps it close to a
    hole; "region": left / right / top / bottom / top-left / ... / center / edge (pins on the outer ring, for
    connectors) keeps it in that part of the board; "keep_rotation": true stops it turning. The placer keeps nets
    short and leaves room between pins of different nets. It places `tries` times with different seeds, routes each
    and keeps the one that routes best (fewest open nets, then jumpers and via wires); placement.tries shows them all.
    Hand-drawn wires are dropped unless every part is fixed. The result goes to out_path as JSON when given. Show
    the user the picture (render_protoboard) and ask what to move: they can answer with hints and you place again.

    In a cadmate project the design is a protoboard design like a board or a part: <name>.json directly in the
    protoboard area (the root, or protoboard/) when it's the only one, else <name>/<name>.json. Never in docs/.
    Write the result back over the design's own file, not a copy beside it."""
    try:
        r = protoboard_place.place_and_route(design, tries=tries, route=route)
    except (protoboard.DesignError, OSError, ValueError) as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
    if out_path and r.get("design"):
        r["written"] = str(protoboard.save(r["design"], out_path))
    return r


@server.tool()
def render_protoboard(design: dict | str, out: str, width: int = 1800) -> Image:
    """Draw a perfboard design for building by hand: the component side (parts, pin 1 square, jumper wires W1...) and
    the solder side mirrored as it looks when the board is turned over (bridges and bare wire in net colours, each
    lead labelled REF.PIN), with matching row letters and column numbers on both. Writes <out>.svg, <out>.png and
    <out>-build.md (parts with their holes, jumpers, bottom wiring net by net) and returns the PNG. Open connections
    are drawn as red dashed lines.

    In a cadmate project the design is a protoboard design like a board or a part: <name>.json directly in the
    protoboard area (the root, or protoboard/) when it's the only one, else <name>/<name>.json. Never in docs/.
    Pass out as the design's path without .json, so the picture and build list sit beside it."""
    try:
        r = protoboard_render.render(design, out, width)
    except (protoboard.DesignError, OSError, ValueError) as e:
        raise RuntimeError(str(e)) from e
    if "png" not in r:
        raise RuntimeError(f"{r.get('png_error')}; the SVG and build list are at {r['svg']} and {r['build_list']}")
    return Image(path=r["png"])


def main() -> None:
    # Keep the bridge copy that FreeCAD's cadmate_bridge_start macro runs in step with this version of cadmate, so a user
    # who starts the bridge from the macro without asking Claude first gets the current one.
    try:
        live.installed_bridge()
    except OSError:
        pass
    server.run()


if __name__ == "__main__":
    main()

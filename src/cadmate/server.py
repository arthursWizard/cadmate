"""cadmate MCP server: the bridge between KiCad and FreeCAD.

Modelling itself is done through a FreeCAD MCP server (see README); these tools
cover the KiCad side and the checks that tie the two together.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import apps, check, footprint, generate, ipc, packages, symbol, symbol_check, symbol_generate
from .footprint import read as _read_footprint  # generate_symbol's footprint argument shadows the module

server = MCPServer("cadmate")

_VAR = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _resolve(path: str, footprint_dir: Path | None = None) -> Path:
    """Expand ${VARS} (KiCad style) from the environment; ${KIPRJMOD} falls back to the footprint's folder."""

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in os.environ:
            return os.environ[name]
        if name == "KIPRJMOD" and footprint_dir is not None:
            return str(footprint_dir)
        raise ValueError(f"${{{name}}} is not set; pass model_path explicitly or set it in the server's environment")

    return Path(_VAR.sub(sub, path)).expanduser()


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
            "compare_footprints": True,
            "generate_symbol": True,
            "check_symbol": True,
            "read_symbol": True,
            "compare_symbols": True,
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
    courtyard overlap, and body size against the datasheet values passed in.
    """
    fp_path = Path(footprint_path).expanduser()
    fp = footprint.read(fp_path)
    placement = fp.models[0] if fp.models else footprint.Model(path="")
    if model_path is None:
        if not fp.models:
            return {"ok": False, "errors": ["footprint has no linked model; pass model_path"]}
        model_path = placement.path
    resolved = _resolve(model_path, fp_path.parent)
    if resolved.suffix.lower() in (".wrl", ".vrml"):
        step = resolved.with_suffix(".step")
        if not step.exists():
            return {"ok": False, "errors": [f"{resolved.name} is VRML; checks need the STEP next to it ({step.name})"]}
        resolved = step
    if not resolved.exists():
        return {"ok": False, "errors": [f"model file not found: {resolved}"]}
    info = apps.run_freecad_script(
        Path(__file__).parent / "freecad_scripts" / "inspect_model.py", env={"CADMATE_MODEL": str(resolved)}
    )
    expected = {k: v for k, v in (("length", body_length), ("width", body_width), ("height", body_height)) if v is not None}
    result = check.check_model(fp, info["bbox"], placement, expected or None)
    if not info.get("valid", True):
        result["errors"].append("FreeCAD reports the shape as invalid")
        result["ok"] = False
    if info.get("solids", 1) == 0:
        result["errors"].append("model contains no solids")
        result["ok"] = False
    result["model_file"] = str(resolved)
    return result


@server.tool()
def link_model_to_footprint(
    footprint_path: str,
    model_path: str,
    offset: list[float] | None = None,
    rotate: list[float] | None = None,
    scale: list[float] | None = None,
    replace_existing: bool = True,
) -> dict:
    """Link a 3D model into a footprint file, keeping a .bak copy of the original.

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
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8")
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
    overwrite: bool = False,
) -> dict:
    """Generate a KiCad footprint from datasheet dimensions and write it into a .pretty library folder.

    soic and qfn compute IPC-7351B pads from the package dimensions; give each
    dimension as the datasheet does: a number, [min, max], [min, nom, max] or
    "6.0+/-0.2". custom places pads exactly as the datasheet's recommended land
    pattern gives them. The result is checked before it is written; nothing is
    written when the check finds errors. An existing file is only replaced with
    overwrite=True, and a .bak copy is kept.
    """
    lib = Path(library_dir).expanduser()
    if lib.suffix != ".pretty":
        return {"ok": False, "errors": ["library_dir must be a KiCad footprint library folder ending in .pretty"]}
    result = generate.generate(family, params, name=name, density=density, courtyard_clearance=courtyard_clearance,
                               datasheet=datasheet, model_path=model_path)
    text = result.pop("text")
    if not result["ok"]:
        return result
    path = lib / f"{result['name']}.kicad_mod"
    if path.exists() and not overwrite:
        result["ok"] = False
        result["errors"] = [f"{path} exists; pass overwrite=True to replace it (a .bak copy is kept)"]
        return result
    lib.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(text, encoding="utf-8")
    result["written"] = str(path)
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
def compare_footprints(path: str, reference_path: str) -> dict:
    """Compare a footprint pad by pad with a reference, for example KiCad's library footprint for the same package."""
    return check.compare_footprints(footprint.read(path), footprint.read(reference_path))


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
    overwrite: bool = False,
) -> dict:
    """Generate a KiCad schematic symbol from the datasheet's pin table and put it into a .kicad_sym library.

    pins: one entry per package pin, [{number, name, type, side?, group?, shape?, unit?, alternates?}].
    type is a KiCad electrical type (input, output, bidirectional, tri_state, passive, power_in,
    power_out, open_collector, open_emitter, no_connect, ...) or datasheet shorthand (I, O, I/O, P,
    G, OD, NC); it may be left out for pins named as power, ground or NC. Pins of one group sit
    together on one side; side (left/right/top/bottom) overrides the placement worked out from the
    type. unit splits a part into units (one per amplifier, plus one for power). Active-low names are
    written ~{RST}; /RST, !RST and RST# are converted.

    The layout follows KiCad's library conventions (100 mil grid, inputs left, outputs right, supplies
    on top, grounds at the bottom, same-name power pins stacked, NC pins hidden). footprint is the
    default footprint as Library:Name; footprint_path, a local .kicad_mod, also checks that every pad
    has a pin and every pin a pad. The symbol is checked before it is written; nothing is written when
    the check finds errors. An existing symbol of the same name is only replaced with overwrite=True,
    and the library keeps a .bak copy. Other symbols in the library are left byte for byte.
    """
    lib = Path(library_path).expanduser()
    if lib.suffix != ".kicad_sym":
        return {"ok": False, "errors": ["library_path must be a KiCad symbol library file ending in .kicad_sym"]}
    result = symbol_generate.generate(name, pins, reference=reference, footprint=footprint, datasheet=datasheet,
                                      description=description, keywords=keywords, fp_filters=fp_filters, pin_length=pin_length)
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
        result["errors"] = [f"{lib.name} already has {name}; pass overwrite=True to replace it (a .bak copy is kept)"]
        return result
    symbol.parse_library(new_text)  # never write a library we cannot read back
    lib.parent.mkdir(parents=True, exist_ok=True)
    if existing is not None:
        shutil.copy2(lib, lib.with_suffix(lib.suffix + ".bak"))
    lib.write_text(new_text, encoding="utf-8")
    result["written"] = str(lib)
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


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()

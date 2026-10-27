"""A whole part from one datasheet: symbol, footprint and 3D model, linked and checked together.

generate_part builds all three in memory from one set of datasheet values,
links them (the symbol's Footprint field names the footprint, the footprint
links the model), checks each one and the links between them, and writes
nothing unless everything passes. The files go into a KiCad project folder:

    <project>/<library>.kicad_sym           the symbol
    <project>/<library>.pretty/<fp>.kicad_mod
    <project>/<library>.3dshapes/<fp>.step  linked as ${KIPRJMOD}/<library>.3dshapes/<fp>.step

and the two libraries are added to the project's sym-lib-table and
fp-lib-table, so KiCad finds them without a trip to the library manager.

check_part reviews such a trio, generated or not, starting from the symbol.
"""

from __future__ import annotations

import fnmatch
import shutil
import tempfile
from pathlib import Path

from . import apps, check, footprint, generate, model, sexpr, symbol, symbol_check, symbol_generate
from .footprint import BBox, Model

LIB_TABLE_VERSION = 7
SYMBOL_KEYS = {"name", "pins", "description", "keywords", "reference", "fp_filters", "pin_length", "unit_shapes"}
FOOTPRINT_KEYS = {"family", "params", "name", "density", "courtyard_clearance"}


def generate_part(
    project_dir: str | Path,
    library: str,
    symbol_spec: dict,
    footprint_spec: dict,
    model_spec: dict | None = None,
    datasheet: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Build, link, check and write a symbol, footprint and 3D model. See the generate_part tool for the arguments."""
    project = Path(project_dir).expanduser()
    errors: list[str] = []
    warnings: list[str] = []
    if not library or not symbol_check.valid_name(library):
        return _fail([f"library name {library!r} may only use letters, digits and _-.+,"])
    for spec, keys, label in ((symbol_spec, SYMBOL_KEYS, "symbol"), (footprint_spec, FOOTPRINT_KEYS, "footprint")):
        unknown = sorted(set(spec) - keys)
        if unknown:
            warnings.append(f"{label}: ignored unknown keys {unknown}")
    model_spec = dict(model_spec or {})

    # Footprint first: its name and pads decide the symbol's Footprint field and the model.
    family = footprint_spec.get("family")
    params = dict(footprint_spec.get("params") or {})
    fp_result = generate.generate(family, params, name=footprint_spec.get("name"), density=footprint_spec.get("density", "nominal"),
                                  courtyard_clearance=footprint_spec.get("courtyard_clearance"), datasheet=datasheet)
    warnings += [f"footprint: {w}" for w in fp_result["warnings"]]
    if not fp_result["ok"]:
        return _fail([f"footprint: {e}" for e in fp_result["errors"]], warnings)
    fp_name = fp_result["name"]
    fp_text = fp_result["text"]
    fp = footprint.parse(fp_text)
    body = BBox(**fp_result["summary"]["body"])

    # Symbol, pointing at the footprint, with every pin checked against a pad.
    sym_name = symbol_spec.get("name")
    if not sym_name:
        return _fail(["symbol: needs a name"], warnings)
    sym_result = symbol_generate.generate(
        sym_name, symbol_spec.get("pins") or [], reference=symbol_spec.get("reference", "U"), footprint=f"{library}:{fp_name}",
        datasheet=datasheet, description=symbol_spec.get("description"), keywords=symbol_spec.get("keywords"),
        fp_filters=symbol_spec.get("fp_filters"), pin_length=symbol_spec.get("pin_length"), unit_shapes=symbol_spec.get("unit_shapes"))
    warnings += [f"symbol: {w}" for w in sym_result["warnings"]]
    if not sym_result["ok"]:
        return _fail([f"symbol: {e}" for e in sym_result["errors"]], warnings)
    sym_text = sym_result["text"]
    sym = symbol.parse_symbol(sym_text)
    vs = symbol_check.compare_with_footprint(sym, fp)
    errors += [f"symbol vs footprint: {e}" for e in vs["errors"]]

    # 3D model: built from the same values, or an existing file placed on the footprint.
    lib_dir = project / f"{library}.pretty"
    shapes_dir = project / f"{library}.3dshapes"
    model_report: dict = {}
    model_file: Path | None = None
    placement: Model | None = None
    built_tmp: tempfile.TemporaryDirectory | None = None
    if model_spec.get("path"):
        try:
            placement = _existing_placement(model_spec, fp_result, warnings)
        except ValueError as e:
            return _fail(errors + [f"model: {e}"], warnings)
        model_report = {"source": "given", "link": placement.path}
        try:
            model_file = model.resolve(placement.path, project_dir=project)
        except ValueError as e:
            errors.append(f"model: {e}")
        else:
            if apps.find_freecadcmd():
                expected = {k: model_spec[k] for k in ("length", "width", "height") if model_spec.get(k) is not None}
                r = model.check_file(fp, model_file, placement, expected, bool(model_spec.get("cutout")))
                model_report["check"] = r
                errors += [f"model: {e}" for e in r["errors"]]
                warnings += [f"model: {w}" for w in r["warnings"]]
            else:
                warnings.append("model: FreeCAD not found, so the model's placement was not checked")
    elif not model_spec.get("skip"):
        values = params | {k: v for k, v in model_spec.items() if k not in ("skip", "path", "cutout")}
        try:
            planned = model.plan(family, values, fp, body)
        except (ValueError, TypeError) as e:
            return _fail(errors + [f"model: {e}"], warnings)
        placement = Model(path=f"${{KIPRJMOD}}/{library}.3dshapes/{fp_name}.step")
        offline = check.check_model(fp, planned["bbox"], placement, planned["expected"], bool(model_spec.get("cutout")))
        model_report = {"source": "built", "link": placement.path, "expected": planned["expected"], "planned_bbox": planned["bbox"]}
        errors += [f"model: {e}" for e in offline["errors"]]
        if not offline["errors"]:
            if apps.find_freecadcmd():
                built_tmp = tempfile.TemporaryDirectory()
                model_file = Path(built_tmp.name) / f"{fp_name}.step"
                try:
                    info = model.build(planned["solids"], model_file, fp_name)
                except (RuntimeError, OSError) as e:
                    errors.append(f"model: FreeCAD could not build it: {e}")
                else:
                    r = model.check_file(fp, model_file, placement, planned["expected"], bool(model_spec.get("cutout")))
                    model_report["check"] = r
                    model_report["colours"] = info.get("colours", False)
                    errors += [f"model: {e}" for e in r["errors"]]
                    warnings += [f"model: {w}" for w in r["warnings"]]
                    if not info.get("colours"):
                        warnings.append("model: built headless, so the STEP has no colours; KiCad shows it in its default grey")
            else:
                placement = None
                model_report["source"] = "not built"
                warnings.append("model: FreeCAD not found, so no 3D model was built or linked; run again where FreeCAD is installed")
    else:
        model_report = {"source": "skipped"}

    if placement is not None:
        fp_text = footprint.link_model(fp_text, placement)
        fp = footprint.parse(fp_text)

    sym_lib = project / f"{library}.kicad_sym"
    fp_file = lib_dir / f"{fp_name}.kicad_mod"
    step_file = shapes_dir / f"{fp_name}.step" if model_report.get("source") == "built" and model_file else None
    existing_sym = sym_lib.read_text(encoding="utf-8") if sym_lib.exists() else None
    try:
        new_lib = symbol.put_in_library(existing_sym, sym_text, sym_name, replace=overwrite)
    except FileExistsError:
        errors.append(f"symbol: {sym_lib.name} already has {sym_name}; pass overwrite=True to replace it (a .bak copy is kept)")
        new_lib = None
    for f in (fp_file, step_file):
        if f is not None and f.exists() and not overwrite:
            errors.append(f"{f} exists; pass overwrite=True to replace it (a .bak copy is kept)")

    result = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "symbol": {"name": sym_name, "footprint_field": f"{library}:{fp_name}", "fp_filters": sym_result["report"].get("fp_filters"),
                   "summary": sym_result["summary"], "report": sym_result["report"]},
        "footprint": {"name": fp_name, "summary": fp_result["summary"], "report": fp_result["report"]},
        "model": model_report,
        "pins_vs_pads": {k: vs[k] for k in ("pads", "pins")},
    }
    if errors:
        if built_tmp:
            built_tmp.cleanup()
        return result

    # Everything checked: write. Symbol library and footprint keep a .bak when replaced.
    project.mkdir(parents=True, exist_ok=True)
    written = []
    if existing_sym is not None:
        shutil.copy2(sym_lib, sym_lib.with_suffix(sym_lib.suffix + ".bak"))
    symbol.parse_library(new_lib)
    sym_lib.write_text(new_lib, encoding="utf-8")
    written.append(str(sym_lib))
    lib_dir.mkdir(parents=True, exist_ok=True)
    if fp_file.exists():
        shutil.copy2(fp_file, fp_file.with_suffix(fp_file.suffix + ".bak"))
    fp_file.write_text(fp_text, encoding="utf-8")
    written.append(str(fp_file))
    if step_file is not None:
        shapes_dir.mkdir(parents=True, exist_ok=True)
        if step_file.exists():
            shutil.copy2(step_file, step_file.with_suffix(step_file.suffix + ".bak"))
        shutil.copy2(model_file, step_file)
        written.append(str(step_file))
    if built_tmp:
        built_tmp.cleanup()
    written += add_to_lib_tables(project, library)
    result["written"] = written
    return result


def _fail(errors: list[str], warnings: list[str] | None = None) -> dict:
    return {"ok": False, "errors": errors, "warnings": warnings or []}


def _existing_placement(spec: dict, fp_result: dict, warnings: list[str]) -> Model:
    """Where a given model goes. frame "datasheet": it was built in the datasheet's coordinates (pads as given),
    so it moves with the footprint when the generator shifted the origin (custom footprints, origin pin 1 or centre)."""
    offset = list(spec.get("offset") or (0.0, 0.0, 0.0))
    frame = spec.get("frame", "footprint")
    if frame == "datasheet":
        dx, dy = fp_result["report"].get("shifted_by", [0.0, 0.0])
        offset[0] += dx
        offset[1] -= dy  # footprint Y down, 3D Y up
        if dx or dy:
            warnings.append(f"model: moved by ({dx:+g}, {-dy:+g}) mm, as the footprint's origin moved from the datasheet's")
    elif frame != "footprint":
        raise ValueError("model frame must be footprint or datasheet")
    return Model(path=spec["path"], offset=tuple(round(v, 6) for v in offset),
                 rotate=tuple(spec.get("rotate") or (0.0, 0.0, 0.0)), scale=tuple(spec.get("scale") or (1.0, 1.0, 1.0)))


# --- project library tables ------------------------------------------------------------------------


def add_to_lib_tables(project: Path, library: str) -> list[str]:
    """Add the library to the project's sym-lib-table and fp-lib-table, creating them if needed.

    Entries already there (by name) are left alone; the rest of each file is kept byte for byte.
    """
    written = []
    for table, head, uri in (("sym-lib-table", "sym_lib_table", f"${{KIPRJMOD}}/{library}.kicad_sym"),
                             ("fp-lib-table", "fp_lib_table", f"${{KIPRJMOD}}/{library}.pretty")):
        path = project / table
        text = path.read_text(encoding="utf-8") if path.exists() else f"({head}\n\t(version {LIB_TABLE_VERSION})\n)\n"
        root = sexpr.parse(text)
        if root.head != head:
            raise ValueError(f"{path} is not a KiCad {table}")
        names = {(lib.child("name").atoms() or [""])[0] for lib in root.children("lib") if lib.child("name")}
        if library in names:
            continue
        entry = f'\t(lib (name {sexpr.quote(library)}) (type "KiCad") (uri {sexpr.quote(uri)}) (options "") (descr "Made with cadmate"))'
        close = root.end - 1
        text = text[:close].rstrip() + "\n" + entry + "\n" + text[close:]
        sexpr.parse(text)
        path.write_text(text, encoding="utf-8")
        written.append(str(path))
    return written


def _lib_uri(project: Path, table: str, name: str) -> Path | None:
    path = project / table
    if not path.exists():
        return None
    for lib in sexpr.parse(path.read_text(encoding="utf-8")).children("lib"):
        if lib.child("name") and lib.child("name").atoms()[:1] == [name] and lib.child("uri"):
            try:
                return model.resolve(lib.child("uri").atoms()[0], project_dir=project)
            except ValueError:
                return None
    return None


# --- checking a part ------------------------------------------------------------------------------


def check_part(symbol_path: str | Path, name: str | None = None, footprint_path: str | Path | None = None,
               pins: list[dict] | None = None, expected: dict | None = None, cutout: bool = False) -> dict:
    """Review a symbol, the footprint its Footprint field names, and the model that footprint links, together."""
    errors: list[str] = []
    warnings: list[str] = []
    sym_path = Path(symbol_path).expanduser()
    project = sym_path.parent.parent if sym_path.parent.suffix == ".kicad_symdir" else sym_path.parent
    sym = symbol.read(sym_path, name)
    r = symbol_check.check_symbol(sym)
    if pins:
        vs = symbol_check.compare_with_table(sym, pins)
        r["errors"] += vs["errors"]
        r["warnings"] += vs["warnings"]
    errors += [f"symbol: {e}" for e in r["errors"]]
    warnings += [f"symbol: {w}" for w in r["warnings"]]
    out: dict = {"symbol": sym.name, "project_dir": str(project)}

    field = sym.prop("Footprint") or ""
    lib_name, _, fp_name = field.partition(":")
    if footprint_path:
        fp_file = Path(footprint_path).expanduser()
    elif lib_name and fp_name:
        lib = _lib_uri(project, "fp-lib-table", lib_name) or project / f"{lib_name}.pretty"
        fp_file = lib / f"{fp_name}.kicad_mod"
    else:
        errors.append("symbol: the Footprint field is empty; set it to Library:Footprint so KiCad places the right footprint")
        return {"ok": False, "errors": errors, "warnings": warnings} | out
    out["footprint_file"] = str(fp_file)
    if not fp_file.exists():
        errors.append(f"footprint: {field or fp_file.name} not found (looked for {fp_file}); check the Footprint field and the project's fp-lib-table")
        return {"ok": False, "errors": errors, "warnings": warnings} | out
    if lib_name and not footprint_path and _lib_uri(project, "fp-lib-table", lib_name) is None:
        warnings.append(f"footprint: library {lib_name!r} is not in the project's fp-lib-table, so KiCad will not find it there")
    fp = footprint.read(fp_file)
    if fp_name and fp.name != fp_name:
        warnings.append(f"footprint: the file is named {fp_name} but the footprint inside calls itself {fp.name}")

    filters = (sym.prop("ki_fp_filters") or "").split()
    if filters and not any(fnmatch.fnmatch(fp.name.lower(), f.lower()) for f in filters):
        warnings.append(f"symbol: footprint filters {filters} do not match {fp.name}, so KiCad's footprint chooser hides it")
    vs = symbol_check.compare_with_footprint(sym, fp)
    errors += [f"symbol vs footprint: {e}" for e in vs["errors"]]
    out["pins_vs_pads"] = {k: vs[k] for k in ("pads", "pins")}
    fr = check.check_footprint(fp)
    errors += [f"footprint: {e}" for e in fr["errors"]]
    warnings += [f"footprint: {w}" for w in fr["warnings"]]

    if not fp.models:
        warnings.append("model: the footprint links no 3D model")
    else:
        placement = fp.models[0]
        out["model_link"] = placement.path
        try:
            model_file = model.resolve(placement.path, footprint_dir=fp_file.parent, project_dir=project)
        except ValueError as e:
            errors.append(f"model: {e}")
        else:
            out["model_file"] = str(model_file)
            if not model_file.exists() and not model_file.with_suffix(".step").exists():
                errors.append(f"model: {model_file} not found; KiCad will show the board without this part")
            elif apps.find_freecadcmd():
                mr = model.check_file(fp, model_file, placement, expected, cutout)
                errors += [f"model: {e}" for e in mr["errors"]]
                warnings += [f"model: {w}" for w in mr["warnings"]]
                out["model_check"] = mr.get("measured")
            else:
                warnings.append("model: FreeCAD not found, so the model's placement was not checked")
    return {"ok": not errors, "errors": errors, "warnings": warnings} | out

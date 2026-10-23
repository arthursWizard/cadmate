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

from . import apps, check, footprint, packages

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


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()

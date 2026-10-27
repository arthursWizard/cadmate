"""Find KiCad and FreeCAD on this machine and report their versions.

Each app is looked up in this order: an environment variable, the PATH, then
the usual install locations for macOS, Linux and Windows. Nothing here assumes
a specific version; callers ask what is available and degrade gracefully.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

KICAD_CLI_ENV = "CADMATE_KICAD_CLI"
FREECADCMD_ENV = "CADMATE_FREECADCMD"

_KICAD_CANDIDATES = {
    "Darwin": ["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli", "/Applications/KiCad.app/Contents/MacOS/kicad-cli"],
    "Linux": ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli", "/var/lib/flatpak/exports/bin/org.kicad.KiCad"],
    "Windows": [r"C:\Program Files\KiCad\*\bin\kicad-cli.exe"],
}
_FREECAD_CANDIDATES = {
    "Darwin": [
        "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd",
        "/Applications/FreeCAD.app/Contents/Resources/bin/FreeCADCmd",
        "/Applications/FreeCAD.app/Contents/MacOS/FreeCADCmd",
    ],
    "Linux": ["/usr/bin/freecadcmd", "/usr/bin/FreeCADCmd", "/usr/local/bin/freecadcmd"],
    "Windows": [r"C:\Program Files\FreeCAD*\bin\freecadcmd.exe", r"C:\Program Files\FreeCAD*\bin\FreeCADCmd.exe"],
}


# The versions cadmate is built and tested against. Older ones are reported, not refused;
# newer ones are expected to work, since KiCad and FreeCAD read their older files.
MIN_VERSIONS = {"kicad": (10, 0), "freecad": (1, 1)}


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    m = re.match(r"\s*(\d+)\.(\d+)", version or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


@dataclass
class AppInfo:
    name: str
    path: str | None
    version: str | None
    note: str | None = None

    @property
    def available(self) -> bool:
        return self.path is not None

    @property
    def supported(self) -> bool | None:
        """True from the minimum version on; None when the version is unknown."""
        v = _version_tuple(self.version)
        return None if v is None else v >= MIN_VERSIONS[self.name]

    def to_dict(self) -> dict:
        d = asdict(self) | {"available": self.available, "supported": self.supported,
                            "minimum": ".".join(map(str, MIN_VERSIONS[self.name]))}
        if self.supported is False:
            d["note"] = f"{self.name} {self.version} is older than {d['minimum']}, the oldest version cadmate supports; files it writes may not open"
        return d


def _find(env_var: str, names: list[str], candidates: dict[str, list[str]]) -> str | None:
    override = os.environ.get(env_var)
    if override:
        return override if Path(override).exists() else None
    for n in names:
        hit = shutil.which(n)
        if hit:
            return hit
    for pattern in candidates.get(platform.system(), []):
        for hit in sorted(glob.glob(pattern), reverse=True):  # newest versioned folder first
            if Path(hit).exists():
                return hit
    return None


def find_kicad_cli() -> str | None:
    return _find(KICAD_CLI_ENV, ["kicad-cli"], _KICAD_CANDIDATES)


def find_freecadcmd() -> str | None:
    return _find(FREECADCMD_ENV, ["freecadcmd", "FreeCADCmd"], _FREECAD_CANDIDATES)


def kicad_info() -> AppInfo:
    path = find_kicad_cli()
    if not path:
        return AppInfo("kicad", None, None, f"kicad-cli not found; set {KICAD_CLI_ENV}")
    try:
        out = subprocess.run([path, "version"], capture_output=True, text=True, timeout=30)
        return AppInfo("kicad", path, out.stdout.strip() or None)
    except (OSError, subprocess.SubprocessError) as e:
        return AppInfo("kicad", path, None, f"could not run kicad-cli: {e}")


def run_freecad_script(script: Path, env: dict[str, str] | None = None, timeout: int = 300) -> dict:
    """Run a script under FreeCADCmd and return the JSON it writes to CADMATE_OUT."""
    path = find_freecadcmd()
    if not path:
        raise RuntimeError(f"FreeCADCmd not found; set {FREECADCMD_ENV}")
    with tempfile.TemporaryDirectory() as tmp:
        out_file = Path(tmp) / "out.json"
        run_env = os.environ | (env or {}) | {"CADMATE_OUT": str(out_file)}
        proc = subprocess.run([path, str(script)], capture_output=True, text=True, timeout=timeout, env=run_env)
        if not out_file.exists():
            raise RuntimeError(f"FreeCAD script produced no result (exit {proc.returncode}): {proc.stderr[-2000:] or proc.stdout[-2000:]}")
        return json.loads(out_file.read_text())


def freecad_info() -> AppInfo:
    path = find_freecadcmd()
    if not path:
        return AppInfo("freecad", None, None, f"FreeCADCmd not found; set {FREECADCMD_ENV}")
    try:
        result = run_freecad_script(Path(__file__).parent / "freecad_scripts" / "version.py", timeout=60)
        return AppInfo("freecad", path, result.get("version"))
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as e:
        return AppInfo("freecad", path, None, str(e))

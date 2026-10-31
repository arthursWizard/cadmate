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

from . import versions

KICAD_CLI_ENV = "CADMATE_KICAD_CLI"
FREECADCMD_ENV = "CADMATE_FREECADCMD"

# Install locations per platform.system(), tried after the environment variable and the PATH. ~ and ${VAR} are
# expanded (${VAR} works on Windows too), * is a glob, and of several matches the highest version wins.
# Flatpak and AppImage installs have no kicad-cli or freecadcmd file to find: point the environment variable at a
# one-line wrapper script instead (see the README).
_KICAD_CANDIDATES = {
    "Darwin": [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/Applications/KiCad.app/Contents/MacOS/kicad-cli",
        "~/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "~/Applications/KiCad.app/Contents/MacOS/kicad-cli",
    ],
    "Linux": ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"],
    "Windows": [
        r"${ProgramFiles}\KiCad\*\bin\kicad-cli.exe",
        r"${LOCALAPPDATA}\Programs\KiCad\*\bin\kicad-cli.exe",
    ],
}
_FREECAD_CANDIDATES = {
    "Darwin": [
        "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd",
        "/Applications/FreeCAD.app/Contents/Resources/bin/FreeCADCmd",
        "/Applications/FreeCAD.app/Contents/MacOS/FreeCADCmd",
        "~/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd",
    ],
    "Linux": [
        "/usr/bin/freecadcmd", "/usr/bin/FreeCADCmd", "/usr/local/bin/freecadcmd",
        "/usr/lib/freecad/bin/FreeCADCmd", "/snap/bin/freecad.cmd",
    ],
    "Windows": [
        r"${ProgramFiles}\FreeCAD*\bin\freecadcmd.exe",
        r"${ProgramFiles}\FreeCAD*\bin\FreeCADCmd.exe",
        r"${LOCALAPPDATA}\Programs\FreeCAD*\bin\freecadcmd.exe",
        r"${LOCALAPPDATA}\Programs\FreeCAD*\bin\FreeCADCmd.exe",
    ],
}

# Kept here for callers that import them from apps; the values live in versions.py.
MIN_VERSIONS = versions.MIN_VERSIONS


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    return versions.parse(version)


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
        """True from the minimum version on (newer ones included); None when the version is unknown."""
        st = versions.status(self.name, self.version)
        return None if st == "unknown" else st != "too_old"

    def to_dict(self) -> dict:
        st = versions.status(self.name, self.version)
        d = asdict(self) | {"available": self.available, "supported": self.supported, "status": st,
                            "minimum": versions.dotted(versions.MIN_VERSIONS[self.name]),
                            "tested": versions.dotted(versions.TESTED_VERSIONS[self.name])}
        if st == "too_old":
            d["note"] = f"{self.name} {self.version} is older than {d['minimum']}, the oldest version cadmate supports; files it writes may not open"
        elif st == "newer_than_tested":
            d["note"] = (f"{self.name} {self.version} is newer than {d['tested']}, the newest version cadmate was checked against; "
                         "it should work, but report anything odd (the version-specific parts are in cadmate/versions.py)")
        return d


def _expand(pattern: str) -> str:
    return os.path.expandvars(os.path.expanduser(pattern))


def _natural_key(path: str) -> tuple:
    """Sort key that orders numbers by value, so KiCad\\10.0 comes after KiCad\\9.0 and Python 3.11 after 3.9."""
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t.lower()) for t in re.split(r"(\d+)", path) if t)


def newest(patterns: list[str]) -> list[str]:
    """Existing files matching any of the patterns (~ and ${VAR} expanded), highest version first within a pattern."""
    out = []
    for pattern in patterns:
        hits = glob.glob(_expand(pattern))
        out += [h for h in sorted(hits, key=_natural_key, reverse=True) if Path(h).exists()]
    return out


def override_problem(env_var: str) -> str | None:
    """A note when the environment variable is set but names nothing that exists, else None."""
    value = os.environ.get(env_var)
    if value and not Path(_expand(value)).exists():
        return f"{env_var} is set to {value}, which doesn't exist"
    return None


def locate(env_var: str, names: list[str], candidates: dict[str, list[str]], system: str | None = None) -> str | None:
    """The program's path: the environment variable if set, else the PATH, else the platform's install locations."""
    override = os.environ.get(env_var)
    if override:
        path = _expand(override)
        return path if Path(path).exists() else None
    for n in names:
        hit = shutil.which(n)
        if hit:
            return hit
    hits = newest(candidates.get(system or platform.system(), []))
    return hits[0] if hits else None


def find_kicad_cli() -> str | None:
    return locate(KICAD_CLI_ENV, ["kicad-cli"], _KICAD_CANDIDATES)


def find_freecadcmd() -> str | None:
    return locate(FREECADCMD_ENV, ["freecadcmd", "FreeCADCmd"], _FREECAD_CANDIDATES)


def _not_found(what: str, env_var: str) -> str:
    return override_problem(env_var) or f"{what} not found; set {env_var} to its path"


def kicad_info() -> AppInfo:
    path = find_kicad_cli()
    if not path:
        return AppInfo("kicad", None, None, _not_found("kicad-cli", KICAD_CLI_ENV))
    try:
        out = subprocess.run([path, *versions.kicad_cli_args("version")], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
        return AppInfo("kicad", path, out.stdout.strip() or None)
    except (OSError, subprocess.SubprocessError) as e:
        return AppInfo("kicad", path, None, f"could not run kicad-cli: {e}")


def run_freecad_script(script: Path, env: dict[str, str] | None = None, timeout: int = 300) -> dict:
    """Run a script under FreeCADCmd and return the JSON it writes to CADMATE_OUT."""
    path = find_freecadcmd()
    if not path:
        raise RuntimeError(_not_found("FreeCADCmd", FREECADCMD_ENV))
    with tempfile.TemporaryDirectory() as tmp:
        out_file = Path(tmp) / "out.json"
        run_env = os.environ | (env or {}) | {"CADMATE_OUT": str(out_file)}
        proc = subprocess.run([path, str(script)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=run_env)
        if not out_file.exists():
            raise RuntimeError(f"FreeCAD script produced no result (exit {proc.returncode}): {proc.stderr[-2000:] or proc.stdout[-2000:]}")
        return json.loads(out_file.read_text(encoding="utf-8"))


def freecad_info() -> AppInfo:
    path = find_freecadcmd()
    if not path:
        return AppInfo("freecad", None, None, _not_found("FreeCADCmd", FREECADCMD_ENV))
    try:
        result = run_freecad_script(Path(__file__).parent / "freecad_scripts" / "version.py", timeout=60)
        return AppInfo("freecad", path, result.get("version"))
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as e:
        return AppInfo("freecad", path, None, str(e))

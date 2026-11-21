"""Where cadmate keeps its own files: one hidden .cadmate/ folder in the working folder, never the system temp folder.

The working folder is the cadmate project holding a file (the folder with cadmate-project.json), else the folder the
file is in (the folder holding it, for a .pretty, .3dshapes or .kicad_symdir library). Inside it:

- .cadmate/tmp/  scratch for one call (kicad-cli reports, board copies for DRC or Freerouting, pictures). Each call
                 makes its own folder and deletes it before returning; a folder left by a run that crashed is deleted
                 by the next call, and project_check reports any that are still there.
- .cadmate/undo/ the files a headless write replaced, newest few only (undo.py).
- .cadmate/live/ what the live editing tools remember about their steps, so their undo works across sessions.

.cadmate/ holds a .gitignore of "*", so git ignores it without a line in the user's own .gitignore. A .cadmate/ that
holds nothing else is removed again, so a call on a loose file leaves no trace beside it.

Only a call with no folder to go by (an unsaved document, a version check) uses the system temp folder, and that is
deleted before the call returns too.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

DOT = ".cadmate"
TMP = f"{DOT}/tmp"
UNDO = f"{DOT}/undo"
LIVE = f"{DOT}/live"
LIB_DIRS = (".pretty", ".3dshapes", ".kicad_symdir")


def base(near: str | Path | None) -> Path | None:
    """The working folder for a file or folder: its cadmate project, else the folder itself or the file's folder."""
    if near is None or str(near) == "":
        return None
    from . import project  # project imports this module's users

    p = Path(near).expanduser()
    root = project.find_root(p)
    if root is not None:
        return root
    p = p.resolve()
    d = p if p.is_dir() else p.parent
    while d.suffix in LIB_DIRS and d.parent != d:  # a library folder is KiCad's: the folder holding it
        d = d.parent
    return d


def dot(folder: Path) -> Path:
    """folder/.cadmate, made if needed, with the .gitignore that keeps it out of git."""
    d = folder / DOT
    d.mkdir(parents=True, exist_ok=True)
    ignore = d / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# cadmate's scratch, undo and live state; nothing here is part of the design\n*\n",
                          encoding="utf-8", newline="\n")
    return d


def tidy(folder: Path) -> None:
    """Remove .cadmate/tmp/ when empty, and .cadmate/ itself when nothing but its .gitignore is left."""
    d = folder / DOT
    for sub in (d / "tmp", d / "undo", d / "live"):
        with contextlib.suppress(OSError):
            sub.rmdir()  # only when empty
    with contextlib.suppress(OSError):
        if d.is_dir() and {p.name for p in d.iterdir()} <= {".gitignore"}:
            (d / ".gitignore").unlink(missing_ok=True)
            d.rmdir()


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, SystemError, ValueError):
        return True  # exists but not ours, or the OS can't say: leave it
    return True


def sweep(folder: Path) -> list[str]:
    """Delete scratch folders left in folder/.cadmate/tmp/ by a cadmate process that is gone (a crash, a kill)."""
    tmp = folder / TMP
    gone = []
    if not tmp.is_dir():
        return gone
    for d in tmp.iterdir():
        pid = d.name.split("-", 1)[0]
        if not pid.isdigit() or not _alive(int(pid)):
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d.name)
    return gone


def leftovers(folder: Path) -> list[Path]:
    """Scratch folders in folder/.cadmate/tmp/ right now."""
    tmp = folder / TMP
    return sorted(tmp.iterdir()) if tmp.is_dir() else []


@contextlib.contextmanager
def tmp(near: str | Path | None, prefix: str = "") -> Iterator[Path]:
    """A scratch folder for one call, in the working folder of near, deleted on the way out (error or not)."""
    folder = base(near)
    if folder is not None:
        try:
            dot(folder)
            sweep(folder)
            d = folder / TMP / f"{os.getpid()}-{prefix + '-' if prefix else ''}{uuid.uuid4().hex[:8]}"
            d.mkdir(parents=True)
        except OSError:  # a read-only folder: fall back to the system's, still deleted below
            tidy(folder)
            folder = None
    if folder is None:
        with tempfile.TemporaryDirectory(prefix=f"cadmate-{prefix}-" if prefix else "cadmate-") as t:
            yield Path(t)
        return
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
        tidy(folder)


class Scratch:
    """tmp() for code that can't sit in one with-block: .path now, .cleanup() when done (safe to call twice)."""

    def __init__(self, near: str | Path | None, prefix: str = ""):
        self._cm = tmp(near, prefix)
        self.path = self._cm.__enter__()

    def cleanup(self) -> None:
        if self._cm is not None:
            cm, self._cm = self._cm, None
            cm.__exit__(None, None, None)

    def __del__(self):
        self.cleanup()

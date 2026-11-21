"""Undo for the tools that rewrite saved files with no editor open: the last few states, in .cadmate/undo/.

A headless write (routing a saved board in place, replacing a footprint, symbol, schematic or board, writing part
numbers, moving files into folders, restoring a snapshot) has no undo of its own: KiCad and FreeCAD aren't involved, so
neither their undo list nor their backups on save cover it. Before such a write, save() copies the files it is about to
replace into .cadmate/undo/<time>_<label>/, at the same relative paths, and notes the files it will create. undo() puts
them back and removes what the write created. Only the newest KEEP entries are kept: this is an undo, not a history
(project_snapshot is the history, on request).

Edits made in the user's open KiCad or FreeCAD window don't come through here: those have the app's own undo.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import workdir

KEEP = 3
INFO = "UNDO.json"
TOOL = "undo_last_write"


class UndoError(RuntimeError):
    pass


def _folder(paths: list[Path]) -> Path:
    """The one working folder all paths share: their cadmate project, else the folder that holds them all."""
    bases = {workdir.base(p) for p in paths}
    if len(bases) == 1:
        return bases.pop()
    return Path(os.path.commonpath([str(p.resolve().parent) for p in paths]))


def _entries(folder: Path) -> list[Path]:
    d = folder / workdir.UNDO
    return sorted(e for e in d.iterdir() if e.is_dir() and (e / INFO).exists()) if d.is_dir() else []


def _info(e: Path) -> dict:
    try:
        return json.loads((e / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(paths: list[str | Path | None], why: str, created: list[str | Path] | None = None,
         now: datetime | None = None) -> dict | None:
    """Keep what paths hold now (files or folders), before a tool replaces, moves or deletes them; paths that don't exist
    yet, and created, are what the tool makes, which undo removes. None when there is nothing to keep or remove."""
    paths = [Path(p).expanduser() for p in paths if p]
    made = [Path(p).expanduser() for p in created or []]
    if not paths and not made:
        return None
    folder = _folder(paths + made)
    have = [p for p in paths if p.exists()]
    made += [p for p in paths if not p.exists()]
    now = now or datetime.now()
    label = re.sub(r"[^a-z0-9]+", "-", why.lower()).strip("-")[:40] or "write"
    root = workdir.dot(folder) / "undo"
    stamp, n = now.strftime('%Y%m%d-%H%M%S-%f'), 0
    while (dest := root / f"{stamp}{n:02d}_{label}").exists():  # the same instant twice: keep both, in order
        n += 1
    dest.mkdir(parents=True)
    rel = lambda p: p.resolve().relative_to(folder.resolve()).as_posix()  # noqa: E731
    for p in have:
        to = dest / rel(p)
        to.parent.mkdir(parents=True, exist_ok=True)
        if p.is_dir():
            shutil.copytree(p, to, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", "*-backups", "*.lck",
                                                                   "~*.lck", workdir.DOT))
        else:
            shutil.copy2(p, to)
    info = {"label": label, "why": why, "time": now.isoformat(timespec="seconds"),
            "kept": sorted({rel(p) for p in have}), "created": sorted({rel(p) for p in made} - {rel(p) for p in have})}
    (dest / INFO).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    for old in _entries(folder)[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    return {"name": dest.name, "folder": str(folder), "kept": info["kept"], "created": info["created"],
            "undo": f"{TOOL} with path {str(folder)!r} puts these back"}


def entries(near: str | Path) -> dict:
    """The undo entries for near's working folder, newest first."""
    folder = workdir.base(near)
    if folder is None:
        raise UndoError("give a file or folder in the working folder")
    out = [{"name": e.name} | {k: v for k, v in _info(e).items() if k in ("why", "time", "kept", "created")}
           for e in reversed(_entries(folder))]
    return {"ok": True, "folder": str(folder), "entries": out, "keeps": KEEP}


def undo(near: str | Path, name: str | None = None) -> dict:
    """Put back the files of the newest entry (or of name), remove the files that write created, and drop the entry.

    An older entry is refused while a newer one covers the same files: undo the newer one first."""
    folder = workdir.base(near)
    if folder is None:
        raise UndoError("give a file or folder in the working folder")
    es = _entries(folder)
    if not es:
        return {"ok": False, "error": f"nothing to undo in {folder}: no tool has rewritten a file here "
                                      f"(or the {KEEP} newest are already undone)"}
    e = next((x for x in es if x.name == name), None) if name else es[-1]
    if e is None:
        raise UndoError(f"no undo entry {name}; {TOOL} with list_only=True lists them")
    info = _info(e)
    touched = set(info.get("kept", [])) | set(info.get("created", []))
    later = [x.name for x in es[es.index(e) + 1:]
             if touched & (set(_info(x).get("kept", [])) | set(_info(x).get("created", [])))]
    if later:
        return {"ok": False, "error": f"{', '.join(later)} changed the same files later; undo that first"}
    removed = []
    for r in info.get("created", []):
        p = folder / r
        if p.is_dir():
            shutil.rmtree(p)
            removed.append(r)
        elif p.exists():
            p.unlink()
            removed.append(r)
    for r in info.get("kept", []):
        src, dst = e / r, folder / r
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
        for f in [dst, *dst.rglob("*")] if dst.is_dir() else [dst]:
            if f.is_file():
                f.touch()  # put back now: fab/ exported earlier must read as older than the board
    shutil.rmtree(e)
    workdir.tidy(folder)
    return {"ok": True, "undone": info.get("why", e.name), "restored": info.get("kept", []), "removed": removed,
            "left": len(_entries(folder)),
            "next": "if KiCad or FreeCAD has any of these files open, reload them there; re-export fab/ for a board"}

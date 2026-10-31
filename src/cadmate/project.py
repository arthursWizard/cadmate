"""One active project, a fixed layout, and a single history folder of snapshots.

The rules (skills/cadmate-project/SKILL.md has the why):

- Every change goes into the active project, in place. There are no side-by-side versions (-v2, -routed, variant-x).
- Before a change worth going back to, snapshot what's there into history/<YYYYMMDD-HHMM>_<label>/, same relative paths.
- The layout is fixed, so every file has one obvious place and depth.

A project is a folder with a cadmate-project.json marker. Nothing here needs KiCad or FreeCAD.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

MARKER = "cadmate-project.json"
LAYOUT_VERSION = 1
HISTORY = "history"
INDEX = "HISTORY.md"
WORK = ".cadmate/work"

# top-level entries and what each is for; anything else is flagged
LAYOUT = {
    MARKER: "marks the project root; name and layout version",
    "README.md": "what the project is, the boards, what to order where, open items, decisions",
    "CLAUDE.md": "points every Claude session at these rules",
    "kicad": "kicad/<board>/<board>.kicad_pro|.kicad_sch|.kicad_pcb|.kicad_dru and its lib tables; kicad/lib/ for project libraries",
    "case": "FreeCAD sources (.FCStd) and exports: <part>.step, assembly.step, renders/, pcb-models/",
    "fab": "fab/<board>/: generated fab files (Gerbers, drill, BOM, CPL); regenerate, don't edit",
    "tools": "generator scripts and their inputs: source code of the design, never archived",
    "docs": "datasheets, drawings, photos and notes from the user",
    HISTORY: "history/<YYYYMMDD-HHMM>_<label>/: snapshots taken before a change, and HISTORY.md, the index",
    ".cadmate": ".cadmate/work/: scratch (comparisons, routing temp files); safe to delete at any time",
}
SKIP_TOP = {HISTORY, ".cadmate", ".git", ".DS_Store"}
# how deep files may sit under each top-level folder (1 = directly inside it)
DEPTH = {"kicad": 3, "case": 2, "fab": 3, "docs": 2}  # tools/ is free-form
KICAD_LIB = "lib"
KICAD_DESIGN = (".kicad_pro", ".kicad_sch", ".kicad_pcb", ".kicad_dru", ".kicad_prl")
# names that mean "another version of the same thing": the sprawl this layout replaces
VERSIONED = re.compile(
    r"(?:[-_ ](?:v\d+|rev\d*[a-z]?|old|new|final|copy|backup|bak|orig|prev|tmp|temp|test|draft|wip|"
    r"routed|unrouted|finished\d*|critical|pass\d*|hand\d*|fixed)|\s\(\d+\))$|^variants?\b",
    re.I)
NAME_CHECKED = ("kicad", "case", "fab")  # tools/ and docs/ are free-form; kicad/lib holds library part names
KIPRJMOD = re.compile(r"\$\{KIPRJMOD\}")
SNAP_NAME = re.compile(r"^(\d{8}-\d{4}(?:\d{2})?)_([a-z0-9][a-z0-9-]*)$")

CLAUDE_MD = """# {name}: a cadmate project

Follow cadmate's project rules (skills/cadmate-project in the cadmate repo). In short:

- This folder is the one active project. Apply every change here, in place. Never make side-by-side copies such as
  main-v2/, main-routed.kicad_pcb, case/variant-x/ or *.bak.
- Before a change worth going back to (the user saw or approved the current state, or the change is big or risky),
  run cadmate's `project_snapshot` with a short label. It copies the current state into history/<YYYYMMDD-HHMM>_<label>/.
- Files go where the layout says: kicad/<board>/<board>.*, kicad/lib/, case/, fab/<board>/, tools/, docs/.
  Scratch goes in .cadmate/work/ and is deleted when done.
- Never rename a KiCad project; never move tools/ out: the scripts are the design's source.
- docs/brief.md holds every requirement the user stated. Add to it when they state one; re-read it before each stage
  and before showing a result; tick an item only once checked. `project_check` lists what is still open.
- Run `project_check` before telling the user a step is done.
"""

BRIEF = "docs/brief.md"
BRIEF_MD = """# {name}: brief

Every requirement the user has stated, in their words, with who said it and when. Tick one only when the design meets it
and you have checked it (render, DRC, measurement). Re-read this before each stage and before showing the user a result.

## Requirements

- [ ] (example) <what the user asked for: shape, size, part, look> (<who>, <date>)

## Decided

- (example) <a choice the user made, and what it was picked over> (<who>, <date>)
"""

README_MD = """# {name}

What this is, in two lines.

## Boards

| Board | Folder | Fab files | Notes |
| --- | --- | --- | --- |

## Case

## Ordering

## Open items

## Decisions
"""


class ProjectError(RuntimeError):
    pass


def find_root(path: str | Path) -> Path | None:
    """The project folder holding path (the folder with cadmate-project.json), or None outside a project."""
    p = Path(path).expanduser().resolve()
    for d in [p, *p.parents] if p.is_dir() else list(p.parents):
        if (d / MARKER).exists():
            return d
    return None


def _root(path: str | Path) -> Path:
    root = find_root(path)
    if root is None:
        raise ProjectError(f"{path} isn't in a cadmate project (no {MARKER} in it or above it); project_init makes one")
    return root


def init(root: str | Path, name: str | None = None, boards: list[str] | None = None) -> dict:
    """Make the layout in root (new or existing). Creates folders and files that are missing; never moves or overwrites."""
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = name or root.name
    made = []

    def mk(rel: str, text: str | None = None):
        p = root / rel
        if p.exists():
            return
        if text is None:
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        made.append(rel)

    mk(MARKER, json.dumps({"name": name, "layout": LAYOUT_VERSION, "created": datetime.now().isoformat(timespec="seconds")}, indent=2) + "\n")
    mk("README.md", README_MD.format(name=name))
    mk("CLAUDE.md", CLAUDE_MD.format(name=name))
    mk(BRIEF, BRIEF_MD.format(name=name))
    for d in ("kicad", f"kicad/{KICAD_LIB}", "case", "fab", "tools", "docs", HISTORY, WORK):
        mk(d)
    mk(f"{HISTORY}/{INDEX}", _index_header(name))
    for b in boards or []:
        _check_label(b, "board name")
        mk(f"kicad/{b}")
    return {"ok": True, "root": str(root), "name": name, "made": made,
            "next": "put each board in kicad/<board>/ named <board>.kicad_pro/.kicad_sch/.kicad_pcb; then project_check"}


def _index_header(name: str) -> str:
    return (f"# {name}: history\n\nSnapshots of the project taken before a change, oldest first. Each folder holds the "
            "files as they were, at the same paths as in the project.\n\n| Snapshot | Taken | What it is | Paths |\n| --- | --- | --- | --- |\n")


def _check_label(label: str, what: str = "label") -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", label or ""):
        raise ProjectError(f"{what} {label!r}: lowercase letters, digits and dashes, up to 40 characters, e.g. 'before-rework'")
    return label


def _rel(root: Path, p: str | Path) -> Path:
    q = Path(p).expanduser()
    q = (q if q.is_absolute() else root / q).resolve()
    try:
        return q.relative_to(root)
    except ValueError:
        raise ProjectError(f"{p} is outside the project {root}") from None


def _kicad_refs(root: Path, folder: Path) -> set[Path]:
    """Project paths a KiCad board folder points at through ${KIPRJMOD}: lib tables and 3D model links. A snapshot carries
    these too, or ${KIPRJMOD}/../lib/... inside the snapshot would point at nothing."""
    refs: set[Path] = set()
    texts = [f.read_text(errors="replace") for f in folder.iterdir()
             if f.is_file() and (f.name in ("fp-lib-table", "sym-lib-table") or f.suffix == ".kicad_pcb")]
    for text in texts:
        for m in re.finditer(r'\$\{KIPRJMOD\}/([^"\)\s]+)', text):
            target = (folder / m[1]).resolve()
            try:
                rel = target.relative_to(root)
            except ValueError:
                continue
            # a model file inside a .3dshapes folder: take the folder, so the library comes whole
            lib = next((p for p in [target, *target.parents] if p.suffix in (".pretty", ".3dshapes", ".kicad_symdir")), None)
            if lib is not None and lib.is_relative_to(root):
                rel = lib.relative_to(root)
            if (root / rel).exists():
                refs.add(rel)
    return refs


def _expand(root: Path, rels: list[Path]) -> list[Path]:
    """The paths to copy: what was asked, plus the libraries any KiCad board folder in it references."""
    out = set(rels)
    for r in rels:
        p = root / r
        folders = [p] if p.is_dir() else [p.parent]
        if p.is_dir():
            folders += [d for d in p.rglob("*") if d.is_dir()]
        for d in folders:
            if any(d.glob("*.kicad_pro")) or (d / "fp-lib-table").exists():
                out |= _kicad_refs(root, d)
    # drop anything already inside another chosen path
    keep = [r for r in out if not any(o != r and o in r.parents for o in out)]
    return sorted(keep)


def _top_paths(root: Path) -> list[Path]:
    return sorted(Path(p.name) for p in root.iterdir() if p.name not in SKIP_TOP)


def snapshot(path: str | Path, label: str, paths: list[str] | None = None, note: str = "",
             now: datetime | None = None) -> dict:
    """Copy the current state into history/<YYYYMMDD-HHMM>_<label>/ before changing it.

    paths (relative to the project, or absolute inside it) limits what is copied, e.g. ["kicad/main"]; leave it out for
    the whole project (history/ and .cadmate/ excepted). A KiCad board folder brings the project libraries it references
    through ${KIPRJMOD}, so the snapshot opens with its footprints and 3D models.
    """
    root = _root(path)
    _check_label(label)
    rels = [_rel(root, p) for p in paths] if paths else _top_paths(root)
    for r in rels:
        if r.parts and r.parts[0] in (HISTORY, ".cadmate"):
            raise ProjectError(f"{r} is history or scratch; snapshots don't copy those")
        if not (root / r).exists():
            raise ProjectError(f"nothing at {r} to snapshot")
    rels = _expand(root, rels)
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M")
    hist = root / HISTORY
    hist.mkdir(exist_ok=True)
    dest = hist / f"{stamp}_{label}"
    if dest.exists():  # two in one minute: seconds keep the order
        dest = hist / f"{now.strftime('%Y%m%d-%H%M%S')}_{label}"
    if dest.exists():
        raise ProjectError(f"{dest.name} already exists")
    files = 0
    for r in rels:
        src, dst = root / r, dest / r
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", "*-backups", "*.lck", "~*.lck"))
            files += sum(1 for f in dst.rglob("*") if f.is_file())
        else:
            shutil.copy2(src, dst)
            files += 1
    meta = {"label": label, "taken": now.isoformat(timespec="seconds"), "note": note, "paths": [str(r) for r in rels]}
    (dest / "SNAPSHOT.json").write_text(json.dumps(meta, indent=2) + "\n")
    index = hist / INDEX
    if not index.exists():
        index.write_text(_index_header(json.loads((root / MARKER).read_text()).get("name", root.name)))
    shown = ", ".join(str(r) for r in rels) if paths else "whole project"
    cell = (note or "").replace("|", "/").replace("\n", " ")
    with index.open("a") as f:
        f.write(f"| [{dest.name}]({dest.name}/) | {now.strftime('%Y-%m-%d %H:%M')} | {cell} | {shown} |\n")
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    return {"ok": True, "snapshot": str(dest), "name": dest.name, "paths": [str(r) for r in rels], "files": files,
            "size_mb": round(size / 1e6, 2), "index": str(index),
            "next": "make the change in the active project itself, not in a copy"}


def history(path: str | Path) -> dict:
    """The snapshots, oldest first, with their labels, notes and paths."""
    root = _root(path)
    out = []
    for d in sorted((root / HISTORY).glob("*")):
        if not d.is_dir():
            continue
        meta = {}
        if (d / "SNAPSHOT.json").exists():
            meta = json.loads((d / "SNAPSHOT.json").read_text())
        out.append({"name": d.name, "label": meta.get("label"), "taken": meta.get("taken"), "note": meta.get("note", ""),
                    "paths": meta.get("paths", []), "named_right": bool(SNAP_NAME.match(d.name))})
    return {"ok": True, "root": str(root), "snapshots": out}


def restore(path: str | Path, name: str, paths: list[str] | None = None, now: datetime | None = None) -> dict:
    """Put a snapshot's files back into the active project. The current state is snapshotted first, so this is undoable.
    paths limits it to part of the snapshot (project-relative, e.g. ["kicad/main"])."""
    root = _root(path)
    src = root / HISTORY / name
    if not src.is_dir():
        raise ProjectError(f"no snapshot {name}; project_history lists them")
    meta = json.loads((src / "SNAPSHOT.json").read_text()) if (src / "SNAPSHOT.json").exists() else {}
    rels = [_rel(root, p) for p in paths] if paths else [Path(p) for p in meta.get("paths", [])] or \
        [Path(p.name) for p in src.iterdir() if p.name != "SNAPSHOT.json"]
    missing = [str(r) for r in rels if not (src / r).exists()]
    if missing:
        raise ProjectError(f"not in {name}: {', '.join(missing)}")
    present = [str(r) for r in rels if (root / r).exists()]
    label = re.sub(r"[^a-z0-9-]", "", f"before-restore-{SNAP_NAME.match(name)[2] if SNAP_NAME.match(name) else 'snapshot'}")[:40]
    before = snapshot(root, label, present, note=f"automatic, before restoring {name}", now=now) if present else None
    for r in rels:
        s, d = src / r, root / r
        if d.is_dir():
            shutil.rmtree(d)
        elif d.exists():
            d.unlink()
        d.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copytree if s.is_dir() else shutil.copy2)(s, d)
    return {"ok": True, "restored": [str(r) for r in rels], "from": name,
            "previous_state": before["name"] if before else None,
            "next": "regenerate fab/ for any board restored; fab files aren't restored unless the snapshot had them"}


# ---------------------------------------------------------------------------------------------------------------------
# layout check


def _finding(sev: str, path: str, what: str, fix: str) -> dict:
    return {"severity": sev, "path": path, "problem": what, "fix": fix}


def _files(root: Path):
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts[0] in SKIP_TOP or p.name == ".DS_Store" or "__pycache__" in rel.parts or any(
                part.endswith("-backups") for part in rel.parts):
            continue
        yield p, rel


def check(path: str | Path) -> dict:
    """Everything in the project that breaks the layout rules, each with a fix. errors break KiCad or lose work;
    warnings are sprawl; info is housekeeping."""
    root = _root(path)
    found: list[dict] = []
    try:
        marker = json.loads((root / MARKER).read_text())
    except ValueError:
        marker = {}
        found.append(_finding("error", MARKER, "not valid JSON", "fix or delete it and run project_init again"))
    for p in sorted(root.iterdir()):
        if p.name in SKIP_TOP or p.name in LAYOUT:
            continue
        found.append(_finding("warning", p.name, "not part of the layout at the top level",
                              "move it to where it belongs (kicad/, case/, fab/, tools/, docs/) or into .cadmate/work/ if scratch"))
    for p, rel in _files(root):
        top = rel.parts[0]
        stem = p.name.split(".")[0] if p.is_file() else p.name
        if p.is_file() and (p.suffix in (".bak", ".orig") or p.name.endswith("~")):
            found.append(_finding("warning", str(rel), "a backup copy beside the original",
                                  "delete it; history/ holds earlier states (project_snapshot before changing)"))
        elif (top in NAME_CHECKED and VERSIONED.search(stem) and p.parent.name != "renders"
              and not (top == "kicad" and len(rel.parts) > 1 and rel.parts[1] == KICAD_LIB)):
            found.append(_finding("warning", str(rel), "the name marks a version or variant of something else",
                                  "apply the change to the original in place (project_snapshot first if the old state matters), "
                                  "then delete this one"))
        limit = DEPTH.get(top)
        if p.is_file() and limit and len(rel.parts) - 1 > limit:
            if not (top == "kicad" and len(rel.parts) > 1 and rel.parts[1] == KICAD_LIB):
                found.append(_finding("warning", str(rel), f"deeper than the layout allows under {top}/ ({limit} levels)",
                                      f"flatten it: {LAYOUT[top]}"))
    found += _check_kicad(root)
    found += _check_fab(root)
    from . import enclosure  # it imports this module
    found += enclosure.project_findings(root)
    work = root / WORK
    if work.exists() and any(work.iterdir()):
        n = sum(1 for f in work.rglob("*") if f.is_file())
        found.append(_finding("info", WORK, f"{n} scratch file(s)", "delete .cadmate/work/ contents once the step is done"))
    for d in sorted((root / HISTORY).glob("*")) if (root / HISTORY).exists() else []:
        if d.is_dir() and not SNAP_NAME.match(d.name):
            found.append(_finding("warning", f"{HISTORY}/{d.name}", "snapshot folder not named <YYYYMMDD-HHMM>_<label>",
                                  "rename it so history sorts by time, e.g. 20261031-1213_before-rework"))
    brief = read_brief(root)
    if brief is None:
        found.append(_finding("warning", BRIEF, "no brief", "write the user's requirements into docs/brief.md (project_init "
                                                               "makes a template) so later stages can check against them"))
    order = {"error": 0, "warning": 1, "info": 2}
    found.sort(key=lambda f: (order[f["severity"]], f["path"]))
    seen: set[str] = set()  # one finding per path: the most severe, then the most specific (KiCad's)
    found = [f for f in sorted(found, key=lambda f: (order[f["severity"]], "name marks" in f["problem"]))
             if not (f["path"] in seen or seen.add(f["path"]))]
    found.sort(key=lambda f: (order[f["severity"]], f["path"]))
    counts = {s: sum(1 for f in found if f["severity"] == s) for s in order}
    out = {"ok": counts["error"] == 0 and counts["warning"] == 0, "root": str(root), "name": marker.get("name", root.name),
           "counts": counts, "findings": found}
    if brief is not None:
        out["brief"] = brief | {"say": "before calling a stage done, check the result against every open item and tell the "
                                       "user which are met, which aren't yet and why"}
    return out


def read_brief(root: str | Path) -> dict | None:
    """docs/brief.md's checklist: requirements still open and those met. None without a brief."""
    f = Path(root) / BRIEF
    if not f.exists():
        return None
    items = re.findall(r"^\s*[-*] \[([ xX])\] (.+)$", f.read_text(errors="replace"), re.M)
    items = [(m, t.strip()) for m, t in items if not t.lower().startswith("(example)")]
    return {"open": [t for m, t in items if m == " "], "met": [t for m, t in items if m != " "]}


def _check_kicad(root: Path) -> list[dict]:
    out: list[dict] = []
    kicad = root / "kicad"
    if not kicad.exists():
        return out
    for p in sorted(kicad.glob("*.kicad_*")):
        out.append(_finding("warning", str(p.relative_to(root)), "KiCad file directly in kicad/",
                            "each board gets its own folder: kicad/<board>/<board>.kicad_pro"))
    libs = (".pretty", ".3dshapes", ".kicad_symdir", ".kicad_sym")
    for p in sorted(kicad.iterdir()):
        if p.suffix in libs:
            out.append(_finding("warning", str(p.relative_to(root)), "project library outside kicad/lib/",
                                "move it into kicad/lib/ and point the lib tables and model links at ${KIPRJMOD}/../lib/"))
    for board in sorted(d for d in kicad.iterdir() if d.is_dir() and d.name != KICAD_LIB and d.suffix not in libs):
        name = board.name
        rel = board.relative_to(root)
        pros = sorted(board.glob("*.kicad_pro"))
        if not pros:
            if any(board.glob("*.kicad_pcb")) or any(board.glob("*.kicad_sch")):
                out.append(_finding("error", str(rel), "board files with no .kicad_pro",
                                    f"KiCad needs {name}.kicad_pro beside the board for its rules and net classes"))
            continue
        for f in sorted(board.iterdir()):
            if f.suffix in KICAD_DESIGN and f.stem != name and not (f.suffix == ".kicad_sch" and _is_subsheet(board, f.name)):
                out.append(_finding("warning", str(f.relative_to(root)),
                                    f"a second {f.suffix} in the {name} board folder",
                                    f"one board per folder, named {name}{f.suffix}: apply its changes to that one and delete this"))
        if not (board / f"{name}.kicad_pro").exists():
            out.append(_finding("error", str(rel), f"the project isn't named after its folder ({pros[0].name})",
                                f"name the folder after the project ({pros[0].stem}) rather than renaming the KiCad project"))
        for tbl in ("fp-lib-table", "sym-lib-table"):
            t = board / tbl
            if t.exists():
                out += _check_table(root, board, t)
        pcb = board / f"{name}.kicad_pcb"
        if pcb.exists():
            out += _check_models(root, board, pcb)
        for sch in sorted(board.glob("*.kicad_sch")):
            names = set(re.findall(r'\(instances\s*\(project\s+"([^"]*)"', sch.read_text(errors="replace")))
            if names and name not in names:
                out.append(_finding("error", str(sch.relative_to(root)),
                                    f"symbol annotations belong to project {', '.join(sorted(names))!s}, not {name}",
                                    "the project was renamed or copied: KiCad can show the references as R?, U? until "
                                    "re-annotated. Open it in KiCad and check the references; re-annotate if they're lost"))
    return out


def _is_subsheet(board: Path, filename: str) -> bool:
    return any(f'"{filename}"' in s.read_text(errors="replace") for s in board.glob("*.kicad_sch") if s.name != filename)


def _check_table(root: Path, board: Path, table: Path) -> list[dict]:
    out = []
    for uri in re.findall(r'\(uri\s+"([^"]+)"\)', table.read_text(errors="replace")):
        rel = str(table.relative_to(root))
        if KIPRJMOD.search(uri):
            target = (board / KIPRJMOD.sub("", uri).lstrip("/")).resolve()
            if not target.is_relative_to(root):
                out.append(_finding("error", rel, f"{uri} points outside the project",
                                    "copy the library into kicad/lib/ and point at ${KIPRJMOD}/../lib/<name>"))
            elif not target.exists():
                out.append(_finding("error", rel, f"{uri} doesn't exist", "fix the path or put the library back"))
        elif uri.startswith("/") or re.match(r"[A-Za-z]:[\\/]", uri) or uri.startswith("~"):
            out.append(_finding("error", rel, f"absolute library path {uri}",
                                "use ${KIPRJMOD}-relative paths, so the project still opens when moved or snapshotted"))
    return out


def _check_models(root: Path, board: Path, pcb: Path) -> list[dict]:
    out, seen = [], set()
    for m in re.findall(r'\(model\s+"([^"]+)"', pcb.read_text(errors="replace")):
        if m in seen:
            continue
        seen.add(m)
        rel = str(pcb.relative_to(root))
        if KIPRJMOD.search(m):
            target = (board / KIPRJMOD.sub("", m).lstrip("/")).resolve()
            if not target.is_relative_to(root):
                out.append(_finding("warning", rel, f"3D model {m} is outside the project",
                                    "copy it into kicad/lib/<project>.3dshapes/ and relink"))
            elif not target.exists():
                out.append(_finding("warning", rel, f"3D model {m} is missing", "fix the link or put the model back"))
        elif m.startswith("/") or re.match(r"[A-Za-z]:[\\/]", m) or m.startswith("~"):
            out.append(_finding("warning", rel, f"absolute 3D model path {m}",
                                "use ${KIPRJMOD}/../lib/<project>.3dshapes/... so it survives a move or snapshot"))
    return out


def _check_fab(root: Path) -> list[dict]:
    out = []
    fab = root / "fab"
    if not fab.exists():
        return out
    for d in sorted(x for x in fab.iterdir() if x.is_dir()):
        pcb = root / "kicad" / d.name / f"{d.name}.kicad_pcb"
        rel = str(d.relative_to(root))
        if not pcb.exists():
            out.append(_finding("warning", rel, f"fab files for a board that isn't in kicad/{d.name}/",
                                "delete them (history keeps the old ones if a snapshot was taken)"))
            continue
        files = [f for f in d.rglob("*") if f.is_file()]
        if files and min(f.stat().st_mtime for f in files) < pcb.stat().st_mtime:
            out.append(_finding("warning", rel, "older than the board", "re-export: export_fab on the board"))
    return out


def work_path(board: Path, step: str) -> Path | None:
    """Inside a project: where a routing step's output goes when the caller didn't name one and in_place is off."""
    root = find_root(board)
    if root is None:
        return None
    return root / WORK / f"{board.stem}-{step}{board.suffix}"


def board_files(board: Path) -> list[Path]:
    """A board's own design files, the set a snapshot before an in-place tool run takes."""
    return [board.with_suffix(ext) for ext in KICAD_DESIGN if board.with_suffix(ext).exists()]


def keep(files: list[Path], why: str) -> str | None:
    """Keep the current version of files that are about to be replaced. Inside a project: one snapshot of them into
    history/; outside one: a .bak beside each (the old behaviour). Returns where they went, or None if none existed."""
    files = [Path(f).expanduser() for f in files if f and Path(f).expanduser().exists()]
    if not files:
        return None
    root = find_root(files[0])
    if root is not None and all(find_root(f) == root for f in files):
        label = re.sub(r"[^a-z0-9]+", "-", f"before-{why}-{files[0].stem}".lower())[:40].strip("-")
        return snapshot(root, label, [str(f) for f in files], note=f"automatic, before {why}")["snapshot"]
    for f in files:
        shutil.copy2(f, f.with_suffix(f.suffix + ".bak"))
    return ", ".join(str(f.with_suffix(f.suffix + ".bak")) for f in files)

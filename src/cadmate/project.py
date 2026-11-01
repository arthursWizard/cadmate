"""One active project, a fixed layout, and a single history folder of snapshots.

The rules (skills/cadmate-project/SKILL.md has the why):

- Every change goes into the active project, in place. There are no side-by-side versions (-v2, -routed, variant-x).
- Before a change worth going back to, snapshot what's there into history/<YYYYMMDD-HHMM>_<label>/, same relative paths.
- The layout is fixed, so every file has one obvious place and depth.
- A project made with one tool (KiCad only, FreeCAD only) is flat: that tool's files sit at the project root. A project
  with both keeps each tool's files in its own folder, kicad/ and case/. cadmate-project.json says which (its "tools").
  add_tool turns a flat project into a two-tool one, after a snapshot.

A project is a folder with a cadmate-project.json marker. Nothing here needs KiCad or FreeCAD.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MARKER = "cadmate-project.json"
LAYOUT_VERSION = 2  # 1: no "tools" in the marker, always kicad/ + case/; 2: "tools" says which, one tool is flat
HISTORY = "history"
INDEX = "HISTORY.md"
WORK = ".cadmate/work"

TOOLS = ("kicad", "freecad")
AREA = {"kicad": "kicad", "freecad": "case"}  # each tool's folder when a project has both; at the root when it's alone
KICAD_LIB = "lib"
CASE_DIRS = ("renders", "pcb-models")
# top-level entries every shape has, whatever the tools
COMMON = {
    MARKER: "marks the project root; name, layout version and tools",
    "README.md": "what the project is, the boards, what to order where, open items, decisions",
    "CLAUDE.md": "points every Claude session at these rules",
    "fab": "fab/<board>/: generated fab files (Gerbers, drill, BOM, CPL); regenerate, don't edit",
    "tools": "generator scripts and their inputs: source code of the design, never archived",
    "docs": "datasheets, drawings, photos and notes from the user",
    HISTORY: "history/<YYYYMMDD-HHMM>_<label>/: snapshots taken before a change, and HISTORY.md, the index",
    ".cadmate": ".cadmate/work/: scratch (comparisons, routing temp files); safe to delete at any time",
}
SKIP_TOP = {HISTORY, ".cadmate", ".git", ".DS_Store"}
# how deep files may sit in each area (1 = directly inside it; the root, for a tool that's alone)
DEPTH = {"kicad": 3, "case": 2, "fab": 3, "docs": 2}  # tools/ is free-form
KICAD_FILE = (".kicad_pro", ".kicad_sch", ".kicad_pcb")
FREECAD_FILE = (".fcstd", ".step", ".stp")
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

Follow cadmate's project rules (the cadmate-project skill). In short:

- This folder is the one active project. Apply every change here, in place. Never make side-by-side copies such as
  main-v2/, main-routed.kicad_pcb, case/variant-x/ or *.bak.
- Before a change worth going back to (the user saw or approved the current state, or the change is big or risky),
  run cadmate's `project_snapshot` with a short label. It copies the current state into history/<YYYYMMDD-HHMM>_<label>/.
- Files go where the layout says: {layout}.
  Scratch goes in .cadmate/work/ and is deleted when done. cadmate-project.json lists the tools: one tool is flat (its
  files at the root), both use kicad/ and case/. Adding the second tool: `project_add_tool`, never by hand.
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

def readme_md(name: str, sh: Shape) -> str:
    out = f"# {name}\n\nWhat this is, in two lines.\n\n"
    if "kicad" in sh.tools:
        out += "## Boards\n\n| Board | Folder | Fab files | Notes |\n| --- | --- | --- | --- |\n\n"
    if "freecad" in sh.tools:
        out += "## Case\n\n"
    return out + "## Ordering\n\n## Open items\n\n## Decisions\n"


class ProjectError(RuntimeError):
    pass


@dataclass(frozen=True)
class Shape:
    """Which tools a project uses, and so where each tool's files sit: alone at the root, together in kicad/ and case/."""
    tools: tuple[str, ...]

    @property
    def nested(self) -> bool:
        return len(self.tools) > 1

    def dir(self, tool: str) -> str:
        """The folder holding a tool's files, relative to the root ("" for the root itself)."""
        return AREA[tool] if self.nested else ""

    def show(self, tool: str, *parts: str) -> str:
        """A path in a tool's area as the user should read it: kicad/main/ or main/."""
        return "/".join(x for x in (self.dir(tool), *parts) if x)

    def area_of(self, rel: Path) -> tuple[str, tuple[str, ...]]:
        """The area a project path belongs to ("kicad", "case", "fab", ...) and its parts below that area."""
        top = rel.parts[0]
        if self.nested or top in COMMON:
            return top, rel.parts[1:]
        return AREA[self.tools[0]], rel.parts

    def layout(self) -> dict[str, str]:
        """The top-level entries and what each is for."""
        k, c = (lambda *p: self.show("kicad", *p)), (lambda *p: self.show("freecad", *p))
        out = dict(COMMON)
        if "kicad" in self.tools:
            out[k("<board>") + "/"] = (f"{k('<board>', '<board>')}.kicad_pro|.kicad_sch|.kicad_pcb|.kicad_dru and its "
                                       "lib tables, one board per folder")
            out[k(KICAD_LIB) + "/"] = "project footprints, symbols and 3D models, at ${KIPRJMOD}/../lib/..."
        else:
            out.pop("fab")
        if "freecad" in self.tools:
            out[c("*.FCStd") if not self.nested else "case/"] = (
                f"FreeCAD sources (.FCStd) and exports: {c('<part>.step')}, {c('assembly.step')}, {c('enclosure.json')}, "
                f"{c('renders')}/, {c('pcb-models')}/")
        return out

    def summary(self) -> str:
        """The layout in one line, for CLAUDE.md, the server's instructions and next steps."""
        parts = []
        if "kicad" in self.tools:
            parts += [self.show("kicad", "<board>", "<board>") + ".*", self.show("kicad", KICAD_LIB) + "/"]
        if "freecad" in self.tools:
            parts += ["case/" if self.nested else "*.FCStd, <part>.step, assembly.step, renders/ at the root"]
        if "kicad" in self.tools:
            parts.append("fab/<board>/")
        return ", ".join(parts + ["tools/", "docs/"])


BOTH = Shape(TOOLS)


def _tools(tools) -> tuple[str, ...]:
    tools = [t.lower() for t in ([tools] if isinstance(tools, str) else tools or [])]
    bad = [t for t in tools if t not in TOOLS]
    if bad or not tools:
        raise ProjectError(f"tools {bad or tools!r}: one or both of {', '.join(TOOLS)}")
    return tuple(t for t in TOOLS if t in tools)


def _marker(root: Path) -> dict:
    try:
        return json.loads((root / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def shape(root: str | Path) -> Shape:
    """The project's shape from its marker. A marker without "tools" (layout 1) is the two-tool layout it was made with."""
    tools = _marker(Path(root)).get("tools")
    try:
        return Shape(_tools(tools)) if tools else BOTH
    except ProjectError:
        return BOTH


def _detect(root: Path) -> tuple[str, ...]:
    """The tools a folder's files say it uses (library 3D models aren't a case)."""
    found = set()
    for f in root.rglob("*"):
        rel = f.relative_to(root)
        if rel.parts[0] in SKIP_TOP or not f.is_file():
            continue
        suf = f.suffix.lower()
        if suf in KICAD_FILE:
            found.add("kicad")
        elif suf in FREECAD_FILE and not any(p.endswith(".3dshapes") for p in rel.parts):
            found.add("freecad")
    return tuple(t for t in TOOLS if t in found)


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


def init(root: str | Path, name: str | None = None, boards: list[str] | None = None,
         tools: list[str] | str | None = None) -> dict:
    """Make the layout in root (new or existing). Creates folders and files that are missing; never moves or overwrites.

    tools: "kicad", "freecad" or both. One tool makes a flat project (its files at the root); both make kicad/ and case/.
    Left out: what the folder's files show, else both. On an existing project, tools must match its marker: add_tool
    adds the second one."""
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / MARKER).exists():
        have = shape(root)
        if tools is not None and _tools(tools) != have.tools:
            want = [t for t in _tools(tools) if t not in have.tools]
            raise ProjectError(f"this project is {' + '.join(have.tools)}; " + (
                f"project_add_tool {want[0]} adds {want[0]} (it moves the files into subfolders, after a snapshot)" if want
                else "cadmate doesn't drop a tool from a project"))
        sh = have
    else:
        sh = Shape(_tools(tools) if tools is not None else _detect(root) or TOOLS)
    if boards and "kicad" not in sh.tools:
        raise ProjectError("boards need KiCad: pass tools including kicad")
    name = name or _marker(root).get("name") or root.name
    made = []

    def mk(rel: str, text: str | None = None):
        p = root / rel
        if p.exists():
            return
        if text is None:
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8", newline="\n")
        made.append(rel)

    for b in boards or []:
        _check_label(b, "board name")
        if b in COMMON or b in (KICAD_LIB, *AREA.values(), *CASE_DIRS):
            raise ProjectError(f"board name {b!r} is a layout folder; pick another")
    mk(MARKER, json.dumps({"name": name, "layout": LAYOUT_VERSION, "tools": list(sh.tools),
                           "created": datetime.now().isoformat(timespec="seconds")}, indent=2) + "\n")
    mk("README.md", readme_md(name, sh))
    mk("CLAUDE.md", CLAUDE_MD.format(name=name, layout=sh.summary()))
    mk(BRIEF, BRIEF_MD.format(name=name))
    dirs = ["tools", "docs", HISTORY, WORK]
    if "kicad" in sh.tools:
        dirs += [x for x in (sh.dir("kicad"), sh.show("kicad", KICAD_LIB), "fab") if x]
    if sh.nested:
        dirs.append(sh.dir("freecad"))
    for d in dirs:
        mk(d)
    mk(f"{HISTORY}/{INDEX}", _index_header(name))
    for b in boards or []:
        mk(sh.show("kicad", b))
    nxt = []
    if "kicad" in sh.tools:
        nxt.append(f"put each board in {sh.show('kicad', '<board>')}/ named <board>.kicad_pro/.kicad_sch/.kicad_pcb")
    if "freecad" in sh.tools:
        nxt.append("put the FreeCAD files " + ("in case/" if sh.nested else "at the project root"))
    return {"ok": True, "root": str(root), "name": name, "tools": list(sh.tools), "flat": not sh.nested, "made": made,
            "layout": sh.layout(), "next": "; ".join(nxt) + "; then project_check"}


def add_tool(path: str | Path, tool: str, now: datetime | None = None) -> dict:
    """Add the second tool to a flat, one-tool project: snapshot the whole project, then move the first tool's files
    from the root into their folder (kicad/ or case/) and make the other's. A KiCad board and its lib/ move together,
    so ${KIPRJMOD}/../lib/ paths still resolve; nothing inside a file is rewritten."""
    root = _root(path)
    (tool,) = _tools(tool)
    sh = shape(root)
    if tool in sh.tools:
        return {"ok": True, "root": str(root), "tools": list(sh.tools), "moved": [], "snapshot": None,
                "next": f"the project already has {tool}"}
    (have,) = sh.tools
    new = Shape(tuple(t for t in TOOLS if t in (have, tool)))
    dest = root / new.dir(have)
    if dest.exists() and any(dest.iterdir()):
        raise ProjectError(f"{dest.name}/ already has files; move them out (or into their place at the root) first")
    move = []
    for p in sorted(root.iterdir()):
        if p.name in COMMON or p.name in SKIP_TOP or p.name == dest.name:
            continue
        if have == "kicad":
            ok = p.is_dir() and (p.name == KICAD_LIB or any(f.suffix in KICAD_DESIGN for f in p.iterdir() if f.is_file())
                                 or not any(p.iterdir()))
        else:  # everything at the root that isn't shared is the case's
            ok = p.is_file() or p.name in CASE_DIRS
        if ok:
            move.append(p)
    snap = snapshot(root, f"before-add-{tool}", note=f"automatic, before adding {tool}: the {have} files move into "
                                                     f"{dest.name}/", now=now)
    dest.mkdir(exist_ok=True)
    for p in move:
        shutil.move(str(p), str(dest / p.name))
    m = _marker(root) | {"layout": LAYOUT_VERSION, "tools": list(new.tools)}
    (root / MARKER).write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    made = init(root)["made"]  # the other tool's folders; the marker now says both
    return {"ok": True, "root": str(root), "tools": list(new.tools), "snapshot": snap["name"],
            "moved": [f"{p.name} -> {dest.name}/{p.name}" for p in move], "made": made,
            "next": (f"files moved: if KiCad or FreeCAD has any of them open, close and reopen them from {dest.name}/. "
                     "CLAUDE.md and README.md still describe the flat layout: update their layout lines. Then project_check")}


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
    texts = [f.read_text(encoding="utf-8", errors="replace") for f in folder.iterdir()
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
    meta = {"label": label, "taken": now.isoformat(timespec="seconds"), "note": note, "paths": [r.as_posix() for r in rels]}
    (dest / "SNAPSHOT.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    index = hist / INDEX
    if not index.exists():
        index.write_text(_index_header(json.loads((root / MARKER).read_text(encoding="utf-8")).get("name", root.name)), encoding="utf-8")
    shown = ", ".join(r.as_posix() for r in rels) if paths else "whole project"
    cell = (note or "").replace("|", "/").replace("\n", " ")
    with index.open("a") as f:
        f.write(f"| [{dest.name}]({dest.name}/) | {now.strftime('%Y-%m-%d %H:%M')} | {cell} | {shown} |\n")
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    return {"ok": True, "snapshot": str(dest), "name": dest.name, "paths": [r.as_posix() for r in rels], "files": files,
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
            meta = json.loads((d / "SNAPSHOT.json").read_text(encoding="utf-8"))
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
    meta = json.loads((src / "SNAPSHOT.json").read_text(encoding="utf-8")) if (src / "SNAPSHOT.json").exists() else {}
    rels = [_rel(root, p) for p in paths] if paths else [Path(p) for p in meta.get("paths", [])] or \
        [Path(p.name) for p in src.iterdir() if p.name != "SNAPSHOT.json"]
    missing = [r.as_posix() for r in rels if not (src / r).exists()]
    if missing:
        raise ProjectError(f"not in {name}: {', '.join(missing)}")
    present = [r.as_posix() for r in rels if (root / r).exists()]
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
    return {"ok": True, "restored": [r.as_posix() for r in rels], "from": name,
            "previous_state": before["name"] if before else None,
            "next": "regenerate fab/ for any board restored; fab files aren't restored unless the snapshot had them"}


# ---------------------------------------------------------------------------------------------------------------------
# layout check


def _finding(sev: str, path: str, what: str, fix: str) -> dict:
    # paths are reported with / on every OS, as KiCad writes them and as the layout rules name them
    return {"severity": sev, "path": path.replace(os.sep, "/"), "problem": what, "fix": fix}


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
        marker = json.loads((root / MARKER).read_text(encoding="utf-8"))
    except ValueError:
        marker = {}
        found.append(_finding("error", MARKER, "not valid JSON", "fix or delete it and run project_init again"))
    sh = shape(root)
    found += _check_top(root, sh)
    for p, rel in _files(root):
        top, within = sh.area_of(rel)
        stem = p.name.split(".")[0] if p.is_file() else p.name
        in_lib = top == "kicad" and len(within) > 0 and within[0] == KICAD_LIB
        if p.is_file() and (p.suffix in (".bak", ".orig") or p.name.endswith("~")):
            found.append(_finding("warning", rel.as_posix(), "a backup copy beside the original",
                                  "delete it; history/ holds earlier states (project_snapshot before changing)"))
        elif top in NAME_CHECKED and VERSIONED.search(stem) and p.parent.name != "renders" and not in_lib:
            found.append(_finding("warning", rel.as_posix(), "the name marks a version or variant of something else",
                                  "apply the change to the original in place (project_snapshot first if the old state matters), "
                                  "then delete this one"))
        limit = DEPTH.get(top)
        if p.is_file() and limit and len(within) > limit and not in_lib:
            where = f"under {top}/" if sh.nested or top in COMMON else "for this project"
            lay = sh.layout()
            rule = COMMON.get(top) or next(f"{k} {v}" for k, v in lay.items() if k not in COMMON and
                                           ("<board>" in k if top == "kicad" else "FCStd" in k or k == "case/"))
            found.append(_finding("warning", rel.as_posix(), f"deeper than the layout allows {where} ({limit} levels)",
                                  f"flatten it: {rule}"))
    if "kicad" in sh.tools:
        found += _check_kicad(root, sh)
        found += _check_fab(root, sh)
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
           "tools": list(sh.tools), "flat": not sh.nested, "counts": counts, "findings": found}
    if brief is not None:
        out["brief"] = brief | {"say": "before calling a stage done, check the result against every open item and tell the "
                                       "user which are met, which aren't yet and why"}
    return out


def _check_top(root: Path, sh: Shape) -> list[dict]:
    """Top-level entries outside the layout, and the files of a tool the project doesn't list (it needs add_tool)."""
    out: list[dict] = []
    allowed = set(COMMON) if "kicad" in sh.tools or sh.nested else set(COMMON) - {"fab"}
    if sh.nested:
        allowed |= set(AREA.values())
    elif "kicad" in sh.tools:
        allowed.add(KICAD_LIB)
    else:
        allowed |= set(CASE_DIRS)
    hint = "move it to where it belongs (" + sh.summary() + ") or into .cadmate/work/ if scratch"
    missing = [t for t in TOOLS if t not in sh.tools]
    for p in sorted(root.iterdir()):
        if p.name in SKIP_TOP or p.name in allowed:
            continue
        suf = p.suffix.lower()
        if not sh.nested and p.is_dir() and p.name == AREA[sh.tools[0]]:
            out.append(_finding("warning", p.name, f"{p.name}/ in a {sh.tools[0]}-only project, which is flat",
                                "move its contents to the project root"))
        elif missing and (p.is_dir() and p.name == AREA[missing[0]] or
                          (suf in KICAD_FILE if missing[0] == "kicad" else (p.is_file() and suf in FREECAD_FILE))):
            out.append(_finding("warning", p.name, f"{missing[0]} files in a {sh.tools[0]}-only project",
                                f"project_add_tool {missing[0]}: it snapshots the project, moves the {sh.tools[0]} files "
                                f"into {AREA[sh.tools[0]]}/ and makes {AREA[missing[0]]}/ for these"))
        elif "kicad" in sh.tools and not sh.nested and p.is_dir():
            if not any(p.iterdir()):
                continue  # a board folder project_init made, not filled yet
            if not any(f.suffix in KICAD_DESIGN for f in p.iterdir() if f.is_file()):
                out.append(_finding("warning", p.name, "a folder at the top level that isn't a board", hint))
        elif "kicad" in sh.tools and not sh.nested and suf in KICAD_DESIGN:
            continue  # _check_kicad says where it goes
        elif "freecad" in sh.tools and not sh.nested and p.is_file():
            continue  # the case's files sit at the root
        else:
            out.append(_finding("warning", p.name, "not part of the layout at the top level", hint))
    return out


def read_brief(root: str | Path) -> dict | None:
    """docs/brief.md's checklist: requirements still open and those met. None without a brief."""
    f = Path(root) / BRIEF
    if not f.exists():
        return None
    items = re.findall(r"^\s*[-*] \[([ xX])\] (.+)$", f.read_text(encoding="utf-8", errors="replace"), re.M)
    items = [(m, t.strip()) for m, t in items if not t.lower().startswith("(example)")]
    return {"open": [t for m, t in items if m == " "], "met": [t for m, t in items if m != " "]}


def _check_kicad(root: Path, sh: Shape) -> list[dict]:
    out: list[dict] = []
    kicad = root / sh.dir("kicad")
    if not kicad.exists():
        return out
    lib = sh.show("kicad", KICAD_LIB) + "/"
    for p in sorted(kicad.glob("*.kicad_*")):
        if p.suffix in KICAD_DESIGN:
            out.append(_finding("warning", p.relative_to(root).as_posix(),
                                f"KiCad file directly in {sh.dir('kicad') + '/' if sh.nested else 'the project root'}",
                                f"each board gets its own folder: {sh.show('kicad', '<board>', '<board>')}.kicad_pro"))
    libs = (".pretty", ".3dshapes", ".kicad_symdir", ".kicad_sym")
    for p in sorted(kicad.iterdir()):
        if p.suffix in libs:
            out.append(_finding("warning", p.relative_to(root).as_posix(), f"project library outside {lib}",
                                f"move it into {lib} and point the lib tables and model links at ${{KIPRJMOD}}/../lib/"))
    skip = set() if sh.nested else set(COMMON) | SKIP_TOP
    boards = [d for d in kicad.iterdir() if d.is_dir() and d.name != KICAD_LIB and d.suffix not in libs and d.name not in skip]
    for board in sorted(boards):
        name = board.name
        rel = board.relative_to(root)
        pros = sorted(board.glob("*.kicad_pro"))
        if not pros:
            if any(board.glob("*.kicad_pcb")) or any(board.glob("*.kicad_sch")):
                out.append(_finding("error", rel.as_posix(), "board files with no .kicad_pro",
                                    f"KiCad needs {name}.kicad_pro beside the board for its rules and net classes"))
            continue
        for f in sorted(board.iterdir()):
            if f.suffix in KICAD_DESIGN and f.stem != name and not (f.suffix == ".kicad_sch" and _is_subsheet(board, f.name)):
                out.append(_finding("warning", f.relative_to(root).as_posix(),
                                    f"a second {f.suffix} in the {name} board folder",
                                    f"one board per folder, named {name}{f.suffix}: apply its changes to that one and delete this"))
        if not (board / f"{name}.kicad_pro").exists():
            out.append(_finding("error", rel.as_posix(), f"the project isn't named after its folder ({pros[0].name})",
                                f"name the folder after the project ({pros[0].stem}) rather than renaming the KiCad project"))
        for tbl in ("fp-lib-table", "sym-lib-table"):
            t = board / tbl
            if t.exists():
                out += _check_table(root, board, t)
        pcb = board / f"{name}.kicad_pcb"
        if pcb.exists():
            out += _check_models(root, board, pcb)
        for sch in sorted(board.glob("*.kicad_sch")):
            names = set(re.findall(r'\(instances\s*\(project\s+"([^"]*)"', sch.read_text(encoding="utf-8", errors="replace")))
            if names and name not in names:
                out.append(_finding("error", sch.relative_to(root).as_posix(),
                                    f"symbol annotations belong to project {', '.join(sorted(names))!s}, not {name}",
                                    "the project was renamed or copied: KiCad can show the references as R?, U? until "
                                    "re-annotated. Open it in KiCad and check the references; re-annotate if they're lost"))
    return out


def _is_subsheet(board: Path, filename: str) -> bool:
    return any(f'"{filename}"' in s.read_text(encoding="utf-8", errors="replace") for s in board.glob("*.kicad_sch") if s.name != filename)


def _check_table(root: Path, board: Path, table: Path) -> list[dict]:
    out = []
    for uri in re.findall(r'\(uri\s+"([^"]+)"\)', table.read_text(encoding="utf-8", errors="replace")):
        rel = table.relative_to(root).as_posix()
        if KIPRJMOD.search(uri):
            target = (board / KIPRJMOD.sub("", uri).lstrip("/")).resolve()
            if not target.is_relative_to(root):
                out.append(_finding("error", rel, f"{uri} points outside the project",
                                    "copy the library into the project's lib/ and point at ${KIPRJMOD}/../lib/<name>"))
            elif not target.exists():
                out.append(_finding("error", rel, f"{uri} doesn't exist", "fix the path or put the library back"))
        elif uri.startswith("/") or re.match(r"[A-Za-z]:[\\/]", uri) or uri.startswith("~"):
            out.append(_finding("error", rel, f"absolute library path {uri}",
                                "use ${KIPRJMOD}-relative paths, so the project still opens when moved or snapshotted"))
    return out


def _check_models(root: Path, board: Path, pcb: Path) -> list[dict]:
    out, seen = [], set()
    for m in re.findall(r'\(model\s+"([^"]+)"', pcb.read_text(encoding="utf-8", errors="replace")):
        if m in seen:
            continue
        seen.add(m)
        rel = pcb.relative_to(root).as_posix()
        if KIPRJMOD.search(m):
            target = (board / KIPRJMOD.sub("", m).lstrip("/")).resolve()
            if not target.is_relative_to(root):
                out.append(_finding("warning", rel, f"3D model {m} is outside the project",
                                    "copy it into the project's lib/<project>.3dshapes/ and relink"))
            elif not target.exists():
                out.append(_finding("warning", rel, f"3D model {m} is missing", "fix the link or put the model back"))
        elif m.startswith("/") or re.match(r"[A-Za-z]:[\\/]", m) or m.startswith("~"):
            out.append(_finding("warning", rel, f"absolute 3D model path {m}",
                                "use ${KIPRJMOD}/../lib/<project>.3dshapes/... so it survives a move or snapshot"))
    return out


def _check_fab(root: Path, sh: Shape) -> list[dict]:
    out = []
    fab = root / "fab"
    if not fab.exists():
        return out
    for d in sorted(x for x in fab.iterdir() if x.is_dir()):
        pcb = root / sh.dir("kicad") / d.name / f"{d.name}.kicad_pcb"
        rel = d.relative_to(root).as_posix()
        if not pcb.exists():
            out.append(_finding("warning", rel, f"fab files for a board that isn't in {sh.show('kicad', d.name)}/",
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

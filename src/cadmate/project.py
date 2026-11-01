"""One active project, a fixed layout, and a single history folder of snapshots.

The rules (skills/cadmate-project/SKILL.md has the why):

- Every change goes into the active project, in place. There are no side-by-side versions (-v2, -routed, variant-x).
- Before a change worth going back to, snapshot what's there into history/<YYYYMMDD-HHMM>_<label>/, same relative paths.
- The layout is fixed, so every file has one obvious place and depth. No folder holds just one thing:
  - A project with one tool (KiCad only, FreeCAD only) keeps that tool's files at the root. With both, each tool has
    its folder, kicad/ and freecad/. cadmate-project.json says which ("tools"); add_tool adds the second, after a snapshot.
  - In a tool's area (the root, or its folder), one design sits directly there: one board's <board>.kicad_pro, one
    FreeCAD part's <part>.FCStd and STEPs. Two or more each get a folder named after what they are: <board>/, <part>/
    (enclosure/, bracket/, knob/). add_unit moves the first one into its folder, after a snapshot.
  - KiCad's project libraries sit in lib/ in the KiCad area, shared by its boards.

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
# 1: no "tools" in the marker, always kicad/ + case/; 2: "tools" says which, one tool is flat, both use kicad/ + freecad/
LAYOUT_VERSION = 2
HISTORY = "history"
INDEX = "HISTORY.md"
WORK = ".cadmate/work"

TOOLS = ("kicad", "freecad")
UNIT = {"kicad": "board", "freecad": "part"}  # what one design of each tool is called
KICAD_LIB = "lib"
PART_DIRS = ("renders", "pcb-models")  # inside a FreeCAD part, beside its files
# top-level entries every shape has, whatever the tools
COMMON = {
    MARKER: "marks the project root; name, layout version and tools",
    "README.md": "what the project is, the boards and parts, what to order where, open items, decisions",
    "CLAUDE.md": "points every Claude session at these rules",
    "fab": "fab/<board>/: generated fab files (Gerbers, drill, BOM, CPL); regenerate, don't edit",
    "tools": "generator scripts and their inputs: source code of the design, never archived",
    "docs": "datasheets, drawings, photos and notes from the user",
    HISTORY: "history/<YYYYMMDD-HHMM>_<label>/: snapshots taken before a change, and HISTORY.md, the index",
    ".cadmate": ".cadmate/work/: scratch (comparisons, routing temp files); safe to delete at any time",
}
SKIP_TOP = {HISTORY, ".cadmate", ".git", ".DS_Store"}
# how deep files may sit in each area (1 = directly inside it): a lib part, a part's renders/ in its folder
DEPTH = {"kicad": 3, "freecad": 3, "fab": 3, "docs": 2}  # tools/ is free-form
KICAD_FILE = (".kicad_pro", ".kicad_sch", ".kicad_pcb")
KICAD_DESIGN = (".kicad_pro", ".kicad_sch", ".kicad_pcb", ".kicad_dru", ".kicad_prl")
BOARD_LOCAL = ("fp-lib-table", "sym-lib-table", "fp-info-cache")  # files KiCad keeps beside a board's .kicad_pro
LIBS = (".pretty", ".3dshapes", ".kicad_symdir", ".kicad_sym")
FREECAD_FILE = (".fcstd", ".step", ".stp")
# names that mean "another version of the same thing": the sprawl this layout replaces
VERSIONED = re.compile(
    r"(?:[-_ ](?:v\d+|rev\d*[a-z]?|old|new|final|copy|backup|bak|orig|prev|tmp|temp|test|draft|wip|"
    r"routed|unrouted|finished\d*|critical|pass\d*|hand\d*|fixed)|\s\(\d+\))$|^variants?\b",
    re.I)
NAME_CHECKED = ("kicad", "freecad", "fab")  # tools/ and docs/ are free-form; lib/ holds library part names
KIPRJMOD = re.compile(r"\$\{KIPRJMOD\}")
KIPRJMOD_PATH = re.compile(r"(\$\{KIPRJMOD\}|\$\(KIPRJMOD\))/([^/\"\)\s]*)")
SNAP_NAME = re.compile(r"^(\d{8}-\d{4}(?:\d{2})?)_([a-z0-9][a-z0-9-]*)$")

CLAUDE_MD = """# {name}: a cadmate project

Follow cadmate's project rules (the cadmate-project skill). In short:

- This folder is the one active project. Apply every change here, in place. Never make side-by-side copies such as
  main-v2/, main-routed.kicad_pcb, enclosure-variant-b/ or *.bak.
- Before a change worth going back to (the user saw or approved the current state, or the change is big or risky),
  run cadmate's `project_snapshot` with a short label. It copies the current state into history/<YYYYMMDD-HHMM>_<label>/.
- No folder holds just one thing. One tool: its files at the root; both: kicad/ and freecad/. One board or FreeCAD
  part: its files directly in its tool's place; two or more: a folder each, named for what it is (main/, enclosure/,
  bracket/). Adding the second tool, board or part: `project_add_tool`, `project_add_board`, `project_add_part`.
  Never move them by hand. This project: {layout}.
- Scratch goes in .cadmate/work/ and is deleted when done.
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


class ProjectError(RuntimeError):
    pass


@dataclass(frozen=True)
class Shape:
    """Which tools a project uses, and so where each tool's files sit: alone at the root, together in kicad/ and
    freecad/ (case/ in a layout-1 project)."""
    tools: tuple[str, ...]
    freecad_dir: str = "freecad"

    @property
    def nested(self) -> bool:
        return len(self.tools) > 1

    def dir(self, tool: str) -> str:
        """The folder holding a tool's files, relative to the root ("" for the root itself)."""
        if not self.nested:
            return ""
        return "kicad" if tool == "kicad" else self.freecad_dir

    def area_dirs(self) -> set[str]:
        return {"kicad", "freecad", "case", self.freecad_dir}

    def show(self, tool: str, *parts: str) -> str:
        """A path in a tool's area as the user should read it: kicad/main or main."""
        return "/".join(x for x in (self.dir(tool), *parts) if x)

    def here(self, tool: str) -> str:
        return self.dir(tool) + "/" if self.nested else "the project root"

    def area_of(self, rel: Path) -> tuple[str, tuple[str, ...]]:
        """The area a project path belongs to ("kicad", "freecad", "fab", "docs", ...) and its parts below that area."""
        top = rel.parts[0]
        if top in COMMON:
            return top, rel.parts[1:]
        if self.nested:
            area = next((t for t in self.tools if self.dir(t) == top), top)
            return area, rel.parts[1:]
        return self.tools[0], rel.parts

    def layout(self) -> dict[str, str]:
        """Where things go, by tool, for the result of project_init."""
        out = {k: v for k, v in COMMON.items() if k != "fab" or "kicad" in self.tools}
        if "kicad" in self.tools:
            k = lambda *p: self.show("kicad", *p)  # noqa: E731
            out[k("<board>.kicad_pro")] = (f"one board: its .kicad_pro, .kicad_sch, .kicad_pcb, .kicad_dru and lib tables "
                                           f"directly in {self.here('kicad')}; two or more: {k('<board>', '<board>')}.* each")
            out[k(KICAD_LIB) + "/"] = ("project footprints, symbols and 3D models, at ${KIPRJMOD}/lib/... (one board) "
                                       "or ${KIPRJMOD}/../lib/... (board folders)")
        if "freecad" in self.tools:
            c = lambda *p: self.show("freecad", *p)  # noqa: E731
            out[c("<part>.FCStd")] = (f"one FreeCAD part (what's being made: enclosure, bracket, knob): its .FCStd, "
                                      f"<piece>.step, assembly.step, enclosure.json, renders/, pcb-models/ directly in "
                                      f"{self.here('freecad')}; two or more: {c('<part>')}/ each")
        return out

    def summary(self) -> str:
        """The layout in one line, for CLAUDE.md and next steps."""
        parts = []
        if "kicad" in self.tools:
            parts.append(f"KiCad in {self.here('kicad')} with lib/, fab/<board>/")
        if "freecad" in self.tools:
            parts.append(f"FreeCAD in {self.here('freecad')}")
        return "; ".join(parts) + "; tools/, docs/"


BOTH = Shape(TOOLS)


def readme_md(name: str, sh: Shape) -> str:
    out = f"# {name}\n\nWhat this is, in two lines.\n\n"
    if "kicad" in sh.tools:
        out += "## Boards\n\n| Board | Folder | Fab files | Notes |\n| --- | --- | --- | --- |\n\n"
    if "freecad" in sh.tools:
        out += "## Parts\n\n| Part | Folder | Made how | Notes |\n| --- | --- | --- | --- |\n\n"
    return out + "## Ordering\n\n## Open items\n\n## Decisions\n"


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
    """The project's shape from its marker. A marker without "tools" (layout 1) is kicad/ + case/, as it was made."""
    tools = _marker(Path(root)).get("tools")
    if not tools:
        return Shape(TOOLS, "case")
    try:
        return Shape(_tools(tools))
    except ProjectError:
        return BOTH


def _detect(root: Path) -> tuple[str, ...]:
    """The tools a folder's files say it uses (library 3D models aren't a FreeCAD part)."""
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


def _check_name(name: str, what: str) -> str:
    _check_label(name, f"{what} name")
    if name in COMMON or name in (KICAD_LIB, *PART_DIRS, "kicad", "freecad", "case"):
        raise ProjectError(f"{what} name {name!r} is a layout folder; pick another")
    return name


# ---------------------------------------------------------------------------------------------------------------------
# boards and parts: one sits directly in its tool's area, two or more get a folder each


def _unit_file(tool: str, p: Path) -> bool:
    """Part of the one board or part that sits directly in the area (not a folder of its own)."""
    if p.name.startswith("."):
        return False
    if tool == "kicad":
        if p.is_dir():
            return p.name.endswith("-backups")
        return p.suffix in KICAD_DESIGN or p.name in BOARD_LOCAL or p.name.startswith("_autosave-") or p.name.endswith(".lck")
    if p.is_dir():
        return p.name in PART_DIRS
    return p.name not in COMMON


def _has_design(tool: str, d: Path) -> bool:
    suffixes = KICAD_DESIGN if tool == "kicad" else FREECAD_FILE
    return any(f.suffix.lower() in suffixes for f in d.iterdir() if f.is_file())


def units(root: Path, sh: Shape, tool: str) -> tuple[list[Path], list[Path]]:
    """(the files of the one design sitting directly in the tool's area, the design folders in it). For KiCad the first
    list is the .kicad_pro files there; for FreeCAD, the .FCStd and STEP files."""
    area = root / sh.dir(tool)
    if not area.is_dir():
        return [], []
    skip = {KICAD_LIB} if tool == "kicad" else set(PART_DIRS)
    if not sh.nested:
        skip |= set(COMMON) | SKIP_TOP | sh.area_dirs() | ({KICAD_LIB} if "kicad" in sh.tools else set())
    folders = [d for d in area.iterdir() if d.is_dir() and d.name not in skip and d.suffix not in LIBS
               and not d.name.endswith("-backups") and (not any(d.iterdir()) or _has_design(tool, d))]
    if tool == "kicad":
        here = sorted(area.glob("*.kicad_pro"))
    else:
        here = sorted(f for f in area.iterdir() if f.is_file() and f.suffix.lower() in FREECAD_FILE)
    return here, sorted(folders)


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
         tools: list[str] | str | None = None, parts: list[str] | None = None) -> dict:
    """Make the layout in root (new or existing). Creates folders and files that are missing; never moves or overwrites.

    tools: "kicad", "freecad" or both. One tool keeps its files at the root; both make kicad/ and freecad/. Left out:
    what the folder's files show, else both. On an existing project, tools must match its marker: add_tool adds one.
    boards, parts: their names. One goes directly in its tool's area, so no folder is made; two or more get a folder each.
    A part is named for what it is (enclosure, bracket, knob), from the conversation or the brief."""
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
    for tool, names in (("kicad", boards), ("freecad", parts)):
        if names and tool not in sh.tools:
            raise ProjectError(f"{UNIT[tool]}s need {tool}: pass tools including {tool}")
        for n in names or []:
            _check_name(n, UNIT[tool])
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

    marker = {"name": name, "layout": LAYOUT_VERSION, "tools": list(sh.tools),
              "created": datetime.now().isoformat(timespec="seconds")}
    mk(MARKER, json.dumps(marker, indent=2) + "\n")
    mk("README.md", readme_md(name, sh))
    mk("CLAUDE.md", CLAUDE_MD.format(name=name, layout=sh.summary()))
    mk(BRIEF, BRIEF_MD.format(name=name))
    dirs = ["tools", "docs", HISTORY, WORK]
    if "kicad" in sh.tools:
        dirs += [sh.show("kicad", KICAD_LIB), "fab"]
    if sh.nested:
        dirs += [sh.dir(t) for t in sh.tools]
    for d in dirs:
        mk(d)
    mk(f"{HISTORY}/{INDEX}", _index_header(name))
    nxt = []
    for tool, names in (("kicad", boards), ("freecad", parts)):
        if tool not in sh.tools:
            continue
        if len(names or []) > 1:
            for n in names:
                mk(sh.show(tool, n))
        u = UNIT[tool]
        one = names[0] if len(names or []) == 1 else f"<{u}>"
        files = f"{one}.kicad_pro/.kicad_sch/.kicad_pcb" if tool == "kicad" else f"{one}.FCStd and its STEPs"
        nxt.append(f"one {u}: {files} directly in {sh.here(tool)}" if len(names or []) < 2 else
                   f"each {u} in {sh.show(tool, f'<{u}>')}/")
    return {"ok": True, "root": str(root), "name": name, "tools": list(sh.tools), "flat": not sh.nested, "made": made,
            "layout": sh.layout(), "next": "; ".join(nxt) + "; then project_check"}


def add_tool(path: str | Path, tool: str, now: datetime | None = None) -> dict:
    """Add the second tool to a one-tool project: snapshot the whole project, then move the first tool's files from the
    root into its folder (kicad/ or freecad/) and make the other's. A board moves with lib/, so ${KIPRJMOD} paths still
    resolve, and a part's files move together; nothing inside a file is rewritten."""
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
    here, folders = units(root, sh, have)
    move = []
    for p in sorted(root.iterdir()):
        if p.name in COMMON or p.name in SKIP_TOP or p.name == dest.name or p.name.startswith("."):
            continue
        if have == "kicad":
            ok = _unit_file("kicad", p) or p.name == KICAD_LIB or p in folders
        else:  # everything at the root that isn't shared is FreeCAD's
            ok = True
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
                     "Update the layout line in CLAUDE.md and README.md. Then project_check")}


def add_unit(path: str | Path, tool: str, name: str | None = None, current: str | None = None,
             now: datetime | None = None) -> dict:
    """Go from one board (or FreeCAD part) to a folder each. The one sitting directly in the tool's area moves into
    <area>/<current>/ after a snapshot; name, if given, is the new one, and gets its folder too.

    current: the name of the one already there. A board's is its .kicad_pro's; a part's is taken from its only .FCStd,
    else pass it (what the part is: enclosure, bracket). A moved board's ${KIPRJMOD} paths (lib tables, 3D models) get
    the extra ../ so they still reach lib/; a part's files move together, so links between them still hold."""
    root = _root(path)
    (tool,) = _tools(tool)
    sh = shape(root)
    u = UNIT[tool]
    if tool not in sh.tools:
        raise ProjectError(f"the project has no {tool}: project_add_tool {tool} first")
    if name is not None:
        _check_name(name, u)
    area = root / sh.dir(tool)
    here, folders = units(root, sh, tool)
    if tool == "kicad" and len(here) > 1:
        raise ProjectError(f"{len(here)} boards sit directly in {sh.here(tool)}; move all but one into folders by hand "
                           "(project_snapshot first)")
    if not here:
        if name is None:
            return {"ok": True, "moved": [], "snapshot": None, "next": f"no {u} sits directly in {sh.here(tool)}"}
        if not folders:
            return {"ok": True, "moved": [], "snapshot": None, "made": [],
                    "next": f"this is the first {u}: put its files directly in {sh.here(tool)}, no folder"}
        made = [] if (area / name).exists() else [sh.show(tool, name)]
        (area / name).mkdir(exist_ok=True)
        return {"ok": True, "moved": [], "snapshot": None, "made": made,
                "next": f"put the {u} in {sh.show(tool, name)}/"}
    if tool == "kicad":
        current = current or here[0].stem
    elif current is None:
        fc = [f for f in here if f.suffix.lower() == ".fcstd"]
        guess = re.sub(r"[^a-z0-9]+", "-", fc[0].stem.lower()).strip("-") if len(fc) == 1 else ""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", guess):
            raise ProjectError(f"which part is in {sh.here(tool)}? pass current, named for what it is (enclosure, bracket)")
        current = guess
    _check_name(current, u)
    if name == current:
        raise ProjectError(f"{current} is the {u} already there; name the new one")
    dest = area / current
    if dest.exists() and any(dest.iterdir()):
        raise ProjectError(f"{sh.show(tool, current)}/ already has files; move them out first")
    skip = set(COMMON) | SKIP_TOP | sh.area_dirs() | {KICAD_LIB} | {d.name for d in folders} if not sh.nested else \
        {KICAD_LIB} | {d.name for d in folders}
    move = [p for p in sorted(area.iterdir()) if p.name not in skip and _unit_file(tool, p)]
    snap = snapshot(root, f"before-{u}-folders-{current}"[:40].rstrip("-"), [sh.dir(tool)] if sh.nested else None,
                    now=now, note=f"automatic, before moving {current} into its own folder for a second {u}")
    dest.mkdir(exist_ok=True)
    moved_names = {p.name for p in move}
    rewritten = []
    for p in move:
        shutil.move(str(p), str(dest / p.name))
        f = dest / p.name
        if tool == "kicad" and f.is_file() and (f.suffix in KICAD_DESIGN or f.name in BOARD_LOCAL):
            text = f.read_text(encoding="utf-8", errors="surrogateescape")
            new = KIPRJMOD_PATH.sub(lambda m: m[0] if m[2] in moved_names else f"{m[1]}/../{m[2]}", text)
            if new != text:
                st = f.stat()
                f.write_text(new, encoding="utf-8", errors="surrogateescape", newline="")
                os.utime(f, (st.st_atime, st.st_mtime))  # only paths changed: fab/ isn't stale because of it
                rewritten.append(sh.show(tool, current, f.name))
    made = []
    if name is not None and not (area / name).exists():
        (area / name).mkdir()
        made.append(sh.show(tool, name))
    app = "KiCad" if tool == "kicad" else "FreeCAD"
    return {"ok": True, "root": str(root), "snapshot": snap["name"], u: current,
            "moved": [f"{p.name} -> {sh.show(tool, current, p.name)}" for p in move], "rewritten": rewritten,
            "made": made,
            "next": (f"{app}: close {current} and reopen it from {sh.show(tool, current)}/. "
                     + (f"Put the new {u} in {sh.show(tool, name)}/. " if name else "") + "Then project_check")}


def add_board(path: str | Path, board: str | None = None, now: datetime | None = None) -> dict:
    return add_unit(path, "kicad", board, now=now)


def add_part(path: str | Path, part: str | None = None, current: str | None = None, now: datetime | None = None) -> dict:
    return add_unit(path, "freecad", part, current, now=now)


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
        elif top in NAME_CHECKED and VERSIONED.search(stem) and "renders" not in rel.parts and not in_lib:
            found.append(_finding("warning", rel.as_posix(), "the name marks a version or variant of something else",
                                  "apply the change to the original in place (project_snapshot first if the old state matters), "
                                  "then delete this one"))
        limit = DEPTH.get(top)
        if p.is_file() and limit and len(within) > limit and not in_lib:
            where = f"under {sh.dir(top) or top}/" if sh.nested or top in COMMON else "for this project"
            rule = COMMON.get(top) or next((f"{k}: {v}" for k, v in sh.layout().items()
                                            if k.endswith(".kicad_pro" if top == "kicad" else ".FCStd")), "")
            found.append(_finding("warning", rel.as_posix(), f"deeper than the layout allows {where} ({limit} levels)",
                                  f"flatten it: {rule}"))
    if "kicad" in sh.tools:
        found += _check_kicad(root, sh)
        found += _check_fab(root, sh)
    if "freecad" in sh.tools:
        found += _check_parts(root, sh)
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
    allowed = {k for k in COMMON if k != "fab" or "kicad" in sh.tools or sh.nested}
    if sh.nested:
        allowed |= {sh.dir(t) for t in sh.tools}
    elif "kicad" in sh.tools:
        allowed.add(KICAD_LIB)
    (tool,) = sh.tools if not sh.nested else (None,)
    folders = set(units(root, sh, tool)[1]) if tool else set()
    hint = "move it to where it belongs (" + sh.summary() + ") or into .cadmate/work/ if scratch"
    missing = [t for t in TOOLS if t not in sh.tools]
    for p in sorted(root.iterdir()):
        if p.name in SKIP_TOP or p.name in allowed or p.name.startswith("."):
            continue
        suf = p.suffix.lower()
        if tool and p.is_dir() and p.name in sh.area_dirs() and not (missing and p.name == missing[0]):
            out.append(_finding("warning", p.name, f"{p.name}/ in a {tool}-only project, whose files sit at the root",
                                "move its contents to the project root"))
        elif missing and (p.is_dir() and p.name == missing[0] or
                          (suf in KICAD_FILE if missing[0] == "kicad" else (p.is_file() and suf in FREECAD_FILE))):
            out.append(_finding("warning", p.name, f"{missing[0]} files in a {tool}-only project",
                                f"project_add_tool {missing[0]}: it snapshots the project, moves the {tool} files into "
                                f"{tool}/ and makes {missing[0]}/ for these"))
        elif tool and (p in folders or _unit_file(tool, p) or tool == "kicad" and p.suffix in LIBS):
            continue  # a board or part at the root, or its folder; _check_kicad and _check_parts say whether it fits
        elif tool and p.is_dir():
            out.append(_finding("warning", p.name, f"a folder at the top level that isn't a {UNIT[tool]}", hint))
        else:
            out.append(_finding("warning", p.name, "not part of the layout at the top level", hint))
    return out


def _check_units(root: Path, sh: Shape, tool: str) -> list[dict]:
    """One board or part directly in the area, or a folder each: never both, never two loose ones."""
    out: list[dict] = []
    u, here_ = UNIT[tool], sh.here(tool)
    here, folders = units(root, sh, tool)
    full = [d for d in folders if any(d.iterdir())]
    add = f"project_add_{u}"
    if tool == "kicad":
        for extra in here[1:]:
            out.append(_finding("warning", extra.relative_to(root).as_posix(), f"a second board directly in {here_}",
                                f"two or more boards get a folder each: move this one into {sh.show(tool, extra.stem)}/, "
                                f"then {add} moves {here[0].stem} into its own"))
    if here and full:
        out.append(_finding("warning", here[0].relative_to(root).as_posix(),
                            f"a {u} directly in {here_} beside {u} folders ({', '.join(d.name for d in full[:3])})",
                            f"{add} moves it into a folder of its own (snapshot first"
                            + (", ${KIPRJMOD} paths fixed)" if tool == "kicad" else "; pass current, what the part is)")))
    if len(full) == 1 and not here:
        out.append(_finding("info", full[0].relative_to(root).as_posix(), f"the only {u}, in a folder of its own",
                            f"one {u} can sit directly in {here_}; leave it if a second {u} is coming"))
    return out


def _check_kicad(root: Path, sh: Shape) -> list[dict]:
    out: list[dict] = []
    kicad = root / sh.dir("kicad")
    if not kicad.exists():
        return out
    lib = sh.show("kicad", KICAD_LIB) + "/"
    for p in sorted(kicad.iterdir()):
        if p.suffix in LIBS:
            out.append(_finding("warning", p.relative_to(root).as_posix(), f"project library outside {lib}",
                                f"move it into {lib} and point the lib tables and model links at it through ${{KIPRJMOD}}"))
    pros, folders = units(root, sh, "kicad")
    loose = sorted(f for f in kicad.iterdir() if f.is_file() and f.suffix in (".kicad_pcb", ".kicad_sch"))
    if not pros and loose:
        out.append(_finding("error", loose[0].relative_to(root).as_posix(), f"board files in {sh.here('kicad')} with no .kicad_pro",
                            "KiCad needs <board>.kicad_pro beside the board for its rules and net classes"))
    out += _check_units(root, sh, "kicad")
    if pros:
        out += _check_board(root, kicad, pros[0].stem, in_folder=False)
    for board in folders:
        if any(board.iterdir()):
            out += _check_board(root, board, board.name, in_folder=True)
    return out


def _check_parts(root: Path, sh: Shape) -> list[dict]:
    if not (root / sh.dir("freecad")).exists():
        return []
    return _check_units(root, sh, "freecad")


def read_brief(root: str | Path) -> dict | None:
    """docs/brief.md's checklist: requirements still open and those met. None without a brief."""
    f = Path(root) / BRIEF
    if not f.exists():
        return None
    items = re.findall(r"^\s*[-*] \[([ xX])\] (.+)$", f.read_text(encoding="utf-8", errors="replace"), re.M)
    items = [(m, t.strip()) for m, t in items if not t.lower().startswith("(example)")]
    return {"open": [t for m, t in items if m == " "], "met": [t for m, t in items if m != " "]}




def _check_board(root: Path, board: Path, name: str, in_folder: bool) -> list[dict]:
    """A board's own files: one board per place, named alike, lib tables, 3D models, schematic annotations."""
    out: list[dict] = []
    rel = board.relative_to(root)
    pros = sorted(board.glob("*.kicad_pro"))
    if in_folder and not pros:
        if any(board.glob("*.kicad_pcb")) or any(board.glob("*.kicad_sch")):
            out.append(_finding("error", rel.as_posix(), "board files with no .kicad_pro",
                                f"KiCad needs {name}.kicad_pro beside the board for its rules and net classes"))
        return out
    where = f"in the {name} board folder" if in_folder else f"beside the {name} board"
    for f in sorted(board.iterdir()):
        if (f.suffix in KICAD_DESIGN and f.stem != name and not (f.suffix == ".kicad_sch" and _is_subsheet(board, f.name))
                and not (f.suffix == ".kicad_pro" and not in_folder)):
            out.append(_finding("warning", f.relative_to(root).as_posix(), f"a second {f.suffix} {where}",
                                f"one board per place, named {name}{f.suffix}: apply its changes to that one and delete this"))
    if in_folder and not (board / f"{name}.kicad_pro").exists():
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
                                    "copy the library into the project's lib/ and point at it through ${KIPRJMOD}"))
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
                                "use a ${KIPRJMOD} path into lib/<project>.3dshapes/ so it survives a move or snapshot"))
    return out


def _check_fab(root: Path, sh: Shape) -> list[dict]:
    out = []
    fab = root / "fab"
    if not fab.exists():
        return out
    for d in sorted(x for x in fab.iterdir() if x.is_dir()):
        area = root / sh.dir("kicad")
        pcb = next((f for f in (area / d.name / f"{d.name}.kicad_pcb", area / f"{d.name}.kicad_pcb") if f.exists()),
                   area / d.name / f"{d.name}.kicad_pcb")
        rel = d.relative_to(root).as_posix()
        if not pcb.exists():
            out.append(_finding("warning", rel, f"fab files for a board that isn't in the project ({d.name})",
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

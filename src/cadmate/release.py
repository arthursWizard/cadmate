"""Board releases: a revision of a board, frozen as it was ordered.

A release is two things, both kept for good:

- a whole-project snapshot in history/ (label release-<board>-<rev>), so the design as ordered can be opened or brought
  back with project_restore, and project_prune never removes it;
- releases/<board>/<rev>/: the fab files as they were sent (Gerbers zip, BOM, placement, the board house's own files),
  renders, and RELEASE.md with what changed since the previous revision. release.json records a hash of each file, so
  project_check notices a frozen file that was changed afterwards.

It doesn't make the fab files: export_fab does, and the release refuses fab files older than the board. Nothing here
needs KiCad, except the optional board renders.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import project, sexpr, versions

RELEASES = "releases"
RECORD = "release.json"
EXPORT_RECORD = "export.json"  # written by export_fab beside the fab files
NOTES = "RELEASE.md"
INDEX = "RELEASES.md"
REV = re.compile(r"^[A-Z]{1,3}$|^[0-9]+(?:\.[0-9]+){0,2}$")
SKIP = set("IOQSXZ")  # letters ASME Y14.35 leaves out of revisions: read as digits or each other
PICTURES = (".png", ".jpg", ".jpeg", ".svg", ".pdf")


class ReleaseError(project.ProjectError):
    pass


def _sha(f: Path) -> str:
    return hashlib.sha256(f.read_bytes()).hexdigest()


def _board(root: Path, path: Path, board: str | None) -> Path:
    """The .kicad_pcb to release: path itself, the one board in the project, or the one named board."""
    if path.suffix == ".kicad_pcb":
        if not path.exists():
            raise ReleaseError(f"no board at {path}")
        return path
    sh = project.shape(root)
    if "kicad" not in sh.tools:
        raise ReleaseError("this project has no KiCad boards to release")
    here, folders = project.units(root, sh, "kicad")
    found = [p.with_suffix(".kicad_pcb") for p in here] + [d / f"{d.name}.kicad_pcb" for d in folders]
    found = [p for p in found if p.exists()]
    if board:
        match = [p for p in found if p.stem == board]
        if not match:
            raise ReleaseError(f"no board {board!r} in the project; boards: {', '.join(p.stem for p in found) or 'none'}")
        return match[0]
    if len(found) != 1:
        raise ReleaseError(f"which board? pass board, one of: {', '.join(p.stem for p in found)}" if found
                           else "no .kicad_pcb in the project yet")
    return found[0]


def releases(root: Path, board: str) -> list[dict]:
    """A board's releases, oldest first, each its release.json plus "folder"."""
    out = []
    for rec in (root / RELEASES / board).glob(f"*/{RECORD}"):
        try:
            d = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(d | {"folder": rec.parent.relative_to(root).as_posix()})
    return sorted(out, key=lambda d: d.get("released", ""))


def _next_rev(previous: list[str]) -> str:
    """The revision after the last lettered one: A, B, ... Y, AA, AB, ..., skipping I, O, Q, S, X and Z."""
    letters = [r for r in previous if re.fullmatch(r"[A-Z]{1,3}", r)]
    if not letters:
        if previous:
            raise ReleaseError(f"earlier revisions are {', '.join(previous)}: pass revision, the next one in that scheme")
        return "A"
    last = max(letters, key=lambda r: (len(r), r))
    n = 0
    for c in last:
        n = n * 26 + ord(c) - 64
    while True:
        n += 1
        out, m = "", n
        while m:
            m, r = divmod(m - 1, 26)
            out = chr(65 + r) + out
        if not SKIP & set(out):
            return out


def _title_rev(text: str) -> str | None:
    tb = sexpr.parse(text).child("title_block")
    rev = tb.child("rev") if tb is not None else None
    return (rev.atoms() or [""])[0] if rev is not None else None


# ---------------------------------------------------------------------------------------------------------------------
# what changed between two revisions of a board


def _outline(root: sexpr.Node) -> tuple[float, float] | None:
    xs, ys = [], []
    for n in root.items:
        if not isinstance(n, sexpr.Node) or not (n.head or "").startswith("gr_"):
            continue
        layer = n.child("layer")
        if layer is None or "Edge.Cuts" not in layer.atoms():
            continue
        for k in ("start", "end", "mid", "center"):
            c = n.child(k)
            if c is not None and len(c.floats()) >= 2:
                xs.append(c.floats()[0])
                ys.append(c.floats()[1])
        if n.head == "gr_circle" and n.child("center") is not None and n.child("end") is not None:
            (cx, cy), (ex, ey) = n.child("center").floats()[:2], n.child("end").floats()[:2]
            r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
        pts = n.child("pts")
        for xy in pts.children("xy") if pts is not None else []:
            xs.append(xy.floats()[0])
            ys.append(xy.floats()[1])
    if not xs:
        return None
    return round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2)


def summary(text: str) -> dict:
    """What a release note compares: parts by reference, nets, copper layers, outline size, track and via counts."""
    from . import fab_export  # it imports project, which this module imports

    root = sexpr.parse(text)
    parts = {p["ref"]: p for p in fab_export.footprints(text) if p["ref"] and not p["ref"].startswith("#")}
    nets = sorted({(n.atoms() + ["", ""])[1] for n in root.children("net")} - {""})
    return {"parts": parts, "nets": nets, "layers": fab_export.copper_layers(text), "outline_mm": _outline(root),
            "tracks": len(root.children("segment")) + len(root.children("arc")), "vias": len(root.children("via"))}


PART_FIELDS = ("value", "footprint", "manufacturer", "mpn", "side", "bom")


def changes(old_text: str, new_text: str) -> dict:
    a, b = summary(old_text), summary(new_text)
    key = _refkey
    added = sorted(set(b["parts"]) - set(a["parts"]), key=key)
    removed = sorted(set(a["parts"]) - set(b["parts"]), key=key)
    changed = []
    for ref in sorted(set(a["parts"]) & set(b["parts"]), key=key):
        diff = {f: [a["parts"][ref][f], b["parts"][ref][f]] for f in PART_FIELDS
                if a["parts"][ref][f] != b["parts"][ref][f]}
        if diff:
            changed.append({"ref": ref, **diff})
    out = {"parts_added": [f"{r} {b['parts'][r]['value']}".strip() for r in added],
           "parts_removed": [f"{r} {a['parts'][r]['value']}".strip() for r in removed],
           "parts_changed": changed,
           "nets_added": sorted(set(b["nets"]) - set(a["nets"])),
           "nets_removed": sorted(set(a["nets"]) - set(b["nets"]))}
    for k in ("layers", "outline_mm", "tracks", "vias"):
        if a[k] != b[k]:
            out[k] = [a[k], b[k]]
    return {k: v for k, v in out.items() if v}


def _refkey(r: str):
    head = r.rstrip("0123456789")
    tail = r[len(head):]
    return (head, int(tail) if tail else 0)


def _changes_md(ch: dict | None, previous: str | None) -> str:
    if previous is None:
        return "First release of this board.\n"
    if not ch:
        return f"No change to parts, nets, layers, outline or routing since {previous}.\n"
    lines = []
    if ch.get("parts_added"):
        lines.append(f"- Parts added: {', '.join(ch['parts_added'])}")
    if ch.get("parts_removed"):
        lines.append(f"- Parts removed: {', '.join(ch['parts_removed'])}")
    for c in ch.get("parts_changed", []):
        what = "; ".join(f"{f} {old or '(none)'} -> {new or '(none)'}" for f, (old, new) in
                         ((f, v) for f, v in c.items() if f != "ref"))
        lines.append(f"- {c['ref']}: {what}")
    if ch.get("nets_added"):
        lines.append(f"- Nets added: {', '.join(ch['nets_added'])}")
    if ch.get("nets_removed"):
        lines.append(f"- Nets removed: {', '.join(ch['nets_removed'])}")
    if "layers" in ch:
        lines.append(f"- Copper layers: {len(ch['layers'][0])} -> {len(ch['layers'][1])}")
    if "outline_mm" in ch:
        fmt = lambda o: f"{o[0]} x {o[1]} mm" if o else "none"  # noqa: E731
        lines.append(f"- Board outline: {fmt(ch['outline_mm'][0])} -> {fmt(ch['outline_mm'][1])}")
    for k, what in (("tracks", "Track segments"), ("vias", "Vias")):
        if k in ch:
            lines.append(f"- {what}: {ch[k][0]} -> {ch[k][1]}")
    return f"Since {previous}:\n\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------------------------
# releasing


def _fab_problem(board: Path, fab_dir: Path) -> str | None:
    files = [f for f in fab_dir.iterdir() if f.is_file()] if fab_dir.is_dir() else []
    if not any(f.name.endswith("-gerbers.zip") for f in files):
        return f"no fab files in {fab_dir.name}/: run export_fab on the board first"
    try:  # export_fab records which board it exported: that holds even when a copy or restore kept an old file time
        made_from = json.loads((fab_dir / EXPORT_RECORD).read_text(encoding="utf-8")).get("board_sha256")
    except (OSError, ValueError, AttributeError):
        made_from = None
    if made_from:
        if made_from != _sha(board):
            return "the fab files were exported from another state of the board: run export_fab again"
        return None
    if min(f.stat().st_mtime for f in files) < board.stat().st_mtime:
        return "the fab files are older than the board: run export_fab again, so the release is what the board is now"
    return None


def _drop_snapshot(root: Path, snap: str) -> None:
    """Undo a failed release's snapshot, and its HISTORY.md row, so a retry doesn't leave unprunable release snapshots."""
    shutil.rmtree(root / project.HISTORY / snap, ignore_errors=True)
    index = root / project.HISTORY / project.INDEX
    if index.exists():
        lines = index.read_text(encoding="utf-8").splitlines(keepends=True)
        index.write_text("".join(x for x in lines if not x.startswith(f"| [{snap}]")), encoding="utf-8", newline="")


def _render(board: Path, dest: Path) -> tuple[list[str], str | None]:
    """Top and bottom pictures of the board through kicad-cli, when it's there. A failure is a note, not an error."""
    from . import fab_export

    made = []
    try:
        for side in ("top", "bottom"):
            out = dest / f"{board.stem}-{side}.png"
            fab_export._cli(*versions.kicad_cli_args("pcb_render"), "--side", side, "--output", str(out), str(board))
            made.append(out.name)
    except fab_export.ExportError as e:
        return made, f"no board renders: {e}"
    return made, None


def release(path: str | Path, revision: str | None = None, notes: str = "", board: str | None = None,
            renders: list[str] | None = None, render: bool = True, now: datetime | None = None) -> dict:
    """Release a revision of a board: snapshot the whole project, freeze the fab files and renders into
    releases/<board>/<rev>/ with RELEASE.md (what changed since the previous revision, and notes), and log it."""
    path = Path(path).expanduser()
    root = project._root(path)
    pcb = _board(root, path.resolve(), board)
    name = pcb.stem
    fab_dir = root / "fab" / name
    problem = _fab_problem(pcb, fab_dir)
    if problem:
        raise ReleaseError(problem)
    earlier = releases(root, name)
    if revision is None:
        revision = _next_rev([r["revision"] for r in earlier])
    revision = revision.strip()
    revision = revision.upper() if revision.isalpha() else revision
    if not REV.match(revision):
        raise ReleaseError(f"revision {revision!r}: up to three letters (A, B, ... AA) or a number (1, 2.1)")
    dest = root / RELEASES / name / revision
    if dest.exists():
        raise ReleaseError(f"{name} revision {revision} is already released ({dest.relative_to(root).as_posix()}); "
                           "a release is never changed: release the next revision instead")
    rel_renders = [project._rel(root, r) for r in renders or []]
    for r in rel_renders:
        if not (root / r).exists():
            raise ReleaseError(f"no render at {r.as_posix()}")
    text = pcb.read_text(encoding="utf-8", errors="replace")
    warnings = []
    tb = _title_rev(text)
    if tb and tb != revision:
        warnings.append(f"the board's title block says rev {tb}, not {revision}: if the silkscreen or fab drawing shows "
                        "${REVISION}, the boards will carry the wrong one. Set it in Board Setup, re-export, and release "
                        "again")
    now = now or datetime.now()
    label = re.sub(r"[^a-z0-9-]+", "-", f"release-{name}-{revision.lower()}")[:40].strip("-")
    snap = project.snapshot(root, label, note=f"release {revision} of {name}", now=now,
                            extra={"release": {"board": name, "revision": revision}})
    board_rel = pcb.relative_to(root).as_posix()
    try:
        dest.mkdir(parents=True)
    except FileExistsError:
        _drop_snapshot(root, snap["name"])
        raise ReleaseError(f"{name} revision {revision} was released meanwhile; a release is never changed") from None
    try:
        frozen = []
        for f in sorted(fab_dir.iterdir()):  # gerbers/ is in the zip
            if f.is_file():
                shutil.copy2(f, dest / f.name)
                frozen.append(f.name)
        pictures = []
        for r in rel_renders:
            src = root / r
            for f in sorted([src] if src.is_file() else src.rglob("*")):
                if f.is_file() and f.suffix.lower() in PICTURES:
                    if (dest / f.name).exists():
                        raise ReleaseError(f"two files named {f.name} in the release; pass renders that don't clash")
                    shutil.copy2(f, dest / f.name)
                    pictures.append(f.name)
        note = None
        if render:
            made, note = _render(pcb, dest)
            pictures += made
        prev = earlier[-1] if earlier else None
        ch = None
        if prev:
            old = root / project.HISTORY / prev.get("snapshot", "") / prev.get("board_file", "")
            if old.is_file():
                ch = changes(old.read_text(encoding="utf-8", errors="replace"), text)
            else:
                warnings.append(f"revision {prev['revision']}'s snapshot isn't in history/ any more, so the changes since "
                                "it can't be listed")
        fab = project.fab_settings(root)
        record = {"board": name, "revision": revision, "released": now.isoformat(timespec="seconds"),
                  "board_file": board_rel, "snapshot": snap["name"], "fab": fab,
                  "previous": prev["revision"] if prev else None, "changes": ch, "notes": notes.strip(),
                  "files": {f.name: _sha(f) for f in sorted(dest.iterdir()) if f.is_file()}}
        md = [f"# {name} revision {revision}", "",
              f"Released {now.strftime('%Y-%m-%d %H:%M')}. The design as released is the snapshot "
              f"history/{snap['name']}/ (project_restore brings it back).", ""]
        if fab:
            shown = {k: v for k, v in fab.items() if v is not None}
            if shown:
                md += ["Made at: " + ", ".join(f"{project.FAB_FIELDS[k].lower()} {'yes' if v is True else 'no' if v is False else v}"
                                              for k, v in shown.items()), ""]
        if notes.strip():
            md += ["## Notes", "", notes.strip(), ""]
        md += ["## Changes", "", _changes_md(ch, prev["revision"] if prev else None).rstrip(), "",
               "## Files", ""] + [f"- {f}" for f in sorted(record["files"])] + [""]
        (dest / NOTES).write_text("\n".join(md), encoding="utf-8", newline="\n")
        (dest / RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        _drop_snapshot(root, snap["name"])
        raise
    index = root / RELEASES / INDEX
    if not index.exists():
        index.write_text("# Releases\n\nEach board revision as released, oldest first. The folders are frozen: don't "
                         "edit them.\n\n| Board | Revision | Released | Snapshot | Notes |\n| --- | --- | --- | --- | --- |\n",
                         encoding="utf-8", newline="\n")
    cell = (notes.strip().splitlines() or [""])[0].replace("|", "/")
    with index.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"| {name} | [{revision}]({name}/{revision}/{NOTES}) | {now.strftime('%Y-%m-%d %H:%M')} | "
                f"{snap['name']} | {cell} |\n")
    out = {"ok": True, "board": name, "revision": revision, "folder": dest.relative_to(root).as_posix(),
           "snapshot": snap["name"], "files": sorted(record["files"]), "renders": pictures,
           "previous": record["previous"], "changes": ch, "notes_file": f"{dest.relative_to(root).as_posix()}/{NOTES}",
           "next": ("tell the user the revision and what changed since the last one; ask what to add to the notes if "
                    "they're empty. If the project is a git repository, offer to commit and tag it "
                    f"({name}-rev-{revision})")}
    if warnings:
        out["warnings"] = warnings
    if note:
        out["note"] = note
    return out


def project_findings(root: Path) -> list[dict]:
    """For project_check: release folders with a changed or missing file, no record, or a snapshot that's gone."""
    out: list[dict] = []
    base = root / RELEASES
    if not base.is_dir():
        return out
    for d in sorted(p for p in base.glob("*/*") if p.is_dir()):
        rel = d.relative_to(root).as_posix()
        rec = d / RECORD
        try:
            data = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append(project._finding("warning", rel, "a release folder without a readable release.json",
                                        "release revisions with project_release; don't make these folders by hand"))
            continue
        files = data.get("files", {})
        bad = sorted(n for n, h in files.items() if not (d / n).is_file() or _sha(d / n) != h)
        extra = sorted(f.name for f in d.iterdir() if f.is_file() and f.name not in files and f.name not in (RECORD, NOTES))
        if bad or extra:
            what = ", ".join(([f"changed or missing: {', '.join(bad)}"] if bad else []) +
                             ([f"added: {', '.join(extra)}"] if extra else []))
            out.append(project._finding("warning", rel, f"a released revision was changed after release ({what})",
                                        "a release is what was ordered: put the files back from its snapshot "
                                        f"(history/{data.get('snapshot')}/fab/) and release changes as the next revision"))
        snap = data.get("snapshot")
        if snap and not (root / project.HISTORY / snap).is_dir():
            out.append(project._finding("warning", rel, f"its snapshot history/{snap}/ is gone",
                                        "the design as released can't be restored; if it's in the Trash, put it back"))
    return out

"""The routing flow behind cadmate's tools: classify the critical connections, route and lock them, autoroute the rest.

Each step works on the board open in KiCad's PCB editor (unsaved edits included, results as one undo step) or on a
.kicad_pcb file. Inside a cadmate project (project.py) a file is changed in place, after a snapshot of the board into
history/; outside one, or with in_place=False, the result goes to a new file and the original is kept.
Review the result with review_pcb_layout.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from . import fab, freerouting, pcb_critical, pcb_finish, pcb_review, pcb_route, project
from .pcb_board import Board, load_project, load_schematic, parse


CAREFUL = ("crystal", "diff_pair", "rf")  # nets finish_routing leaves to route_critical_nets or the user


class FlowError(RuntimeError):
    pass


def load(path: str | None) -> tuple[Board, object | None]:
    """The board with its project and schematic around it, and the live kipy board when it came from the editor."""
    if path:
        f = Path(path).expanduser()
        if not f.exists():
            raise FlowError(f"no board at {f}")
        b = parse(f.read_text(errors="replace"), f.name)
        load_project(b, f.with_suffix(".kicad_pro"))
        load_schematic(b, f.parent, f.stem)
        return b, None
    from . import kicad_pcb_live

    _, live = kicad_pcb_live.connect()
    b = parse(live.get_as_string(), live.name)
    project = getattr(getattr(live.document, "project", None), "path", "") or ""
    folder = Path(project) if project else None
    if folder and folder.is_file():
        folder = folder.parent
    if folder:
        pro = folder / Path(live.name).with_suffix(".kicad_pro").name
        load_project(b, pro if pro.exists() else next(iter(folder.glob("*.kicad_pro")), None))
        load_schematic(b, folder, Path(live.name).stem)
    return b, live


def _rules_file(dst: Path | None, live) -> Path | None:
    """Where the board's custom rules live: next to the written board, or in the open board's project."""
    if dst is not None:
        return dst.with_suffix(".kicad_dru")
    project_ = getattr(getattr(live.document, "project", None), "path", "") or ""
    folder = Path(project_) if project_ else None
    if folder and folder.is_file():
        folder = folder.parent
    return folder / Path(live.name).with_suffix(".kicad_dru").name if folder else None


def target(path: str, out_path: str | None, step: str, in_place: bool | None) -> tuple[Path, dict | None]:
    """Where a step writes a saved board, and the snapshot taken first when that is the board itself.

    out_path given: there (the input itself only with in_place). Otherwise, in a cadmate project the board is changed in
    place (in_place defaults to True there) after a snapshot of its files into history/, or with in_place=False the
    result goes to the project's scratch folder, never beside the board. Outside a project: <name>-<step>.kicad_pcb."""
    src = Path(path).expanduser()
    root = project.find_root(src)
    if in_place is None:
        in_place = root is not None and out_path is None
    dst = Path(out_path).expanduser() if out_path else src if in_place else (
        project.work_path(src, step) or src.with_name(f"{src.stem}-{step}{src.suffix}"))
    if dst.resolve() != src.resolve():
        return dst, None
    if not in_place:
        raise FlowError("out_path is the input board; pass in_place=True to change it (inside a cadmate project the "
                        "board is snapshotted first), or give a new file")
    if root is None:
        raise FlowError("in_place needs the board inside a cadmate project, so its current state can be snapshotted to "
                        "history/ first (project_init); outside one, give out_path")
    label = re.sub(r"[^a-z0-9]+", "-", f"before-{step}-{src.stem}".lower())[:40].strip("-")
    snap = project.snapshot(src, label,
                            [str(f) for f in project.board_files(src)], note=f"automatic, before {step} on {src.name}")
    return dst, {"name": snap["name"], "restore": f"project_restore with name {snap['name']!r} undoes this step"}


def classify(path: str | None = None, roles: dict | None = None, currents: dict | None = None, overrides: dict | None = None,
             limits: dict | None = None, classes: list[str] | None = None) -> dict:
    b, live = load(path)
    out = pcb_critical.classify(b, roles, currents, limits, overrides, classes)
    out["source"] = "the open PCB editor, unsaved edits included" if live is not None else str(Path(path).expanduser())
    out["next"] = ("Show the user the table, grouped by class, and ask them to confirm or override before routing: overrides "
                   "{item id: 'route' | 'hand' | 'skip' | ...}, roles {net: role}, currents {net: amps}. Items marked 'place' "
                   "need a part moved first; 'ask' needs a current. Then route_critical_nets with the same arguments.")
    return out


def route_critical(path: str | None = None, out_path: str | None = None, roles: dict | None = None, currents: dict | None = None,
                   overrides: dict | None = None, limits: dict | None = None, items: list[str] | None = None, dry_run: bool = False,
                   in_place: bool | None = None) -> dict:
    """Route the plan's 'route' items (or just items), lock them, and put them on the live board or into out_path."""
    b, live = load(path)
    plan = pcb_critical.classify(b, roles, currents, limits, overrides)
    known = {it["id"] for it in plan["items"]}
    unknown = [i for i in (items or []) if i not in known]
    if unknown:
        raise FlowError(f"no item {', '.join(unknown)} on this board; classify_critical_nets lists the ids")
    already = len(b.tracks)
    res = pcb_route.route_plan(b, plan, ids=items)
    tracks, vias = res.pop("tracks"), res.pop("vias")
    out = {"ok": True, "routed": res["routed"], "failed": res["failed"], "added": {"tracks": len(tracks), "vias": len(vias)},
           "connections": res["connections"], "pairs": res["pairs"],
           "by_hand": [c for c in res["connections"] if not c["ok"]],
           "over_rule": res["over_rule"],
           "routed_first": res["routed_first"],
           "boxed_in": res["boxed_in"],
           "not_routed_here": [{"id": it["id"], "action": it["action"], "title": it["title"]} for it in plan["items"]
                               if it["action"] != "route" and not (items and it["id"] in items)],
           "locked": "every segment and via added here is locked, so Freerouting routes around it",
           "existing_tracks": already}
    if dry_run or not tracks and not vias:
        out["written"] = "nothing (dry run)" if dry_run else "nothing to add"
        return out
    if live is not None:
        from . import kicad_pcb_live

        r = kicad_pcb_live.commit(f"route {res['routed']} critical connections", lambda kicad, board: pcb_route.apply_live(board, tracks, vias))
        if not r.get("ok"):
            raise FlowError(r.get("error", "the edit failed"))
        out["written"] = "the open board, as one undo step ('cadmate: route … critical connections'); not saved yet"
    else:
        src = Path(path).expanduser()
        dst, snap = target(path, out_path, "critical", in_place)
        pcb_route.write_file(src, dst, tracks, vias)
        out["written"] = str(dst)
        if snap:
            out["snapshot"] = snap
    if res["boxed_in"]:
        out["boxed_in_note"] = ("these pads had a way out before this routing and have none now: their nets will stay open. "
                                "Route them first (or move a part), then route the critical items again")
    out["next"] = ("tell the user what's in by_hand, boxed_in and over_rule (routed but longer than the rule, or a pair out of "
                   "skew: KiCad's skew tuner, or better placement), then autoroute_rest and review_pcb_layout")
    return out


def autoroute(path: str | None = None, out_path: str | None = None, currents: dict | None = None, roles: dict | None = None,
              passes: int = 30, timeout: int = 1200, fab_name: str | None = "jlcpcb", margin: float = fab.MARGIN,
              neck_rules: bool = True, in_place: bool | None = None) -> dict:
    """Freerouting on everything not routed yet, with the widths the high-current items ask for. Its narrow tracks are
    checked against the fab's minimums (fab_name, None to skip) plus the margin. With neck_rules, the necks into pads that
    the fab can make get a KiCad custom rule allowing them there, so DRC flags only what needs fixing."""
    b, live = load(path)
    try:
        lim = fab.limits(fab_name, len(b.copper_layers), margin) if fab_name else None
    except ValueError as e:
        raise FlowError(str(e)) from None
    plan = pcb_critical.classify(b, roles, currents, None, None, ["high_current"])
    widths, capped = {}, []
    for it in plan["items"]:
        if it["action"] != "netclass":
            continue
        net, want = it["nets"][0], it["rule"]["width_outer_mm"]
        # Freerouting uses one width per net, pins included: a wider track than the smallest pad can't reach that pad
        pad = min((p for p in b.pads if p.net == net and p.kind == "smd"), key=lambda p: min(p.w, p.h), default=None)
        width = min(want, min(pad.w, pad.h)) if pad else want
        widths[net] = round(width, 3)
        if width < want - 1e-3:
            capped.append({"net": net, "ipc2221_mm": want, "given_mm": round(width, 3), "by": pad.name,
                           "what_to_do": "the autorouter can't neck down at one pad, so the main path needs widening by hand or a "
                                         "pour on this net; review_pcb_layout with the same currents checks it"})
    dst = snap = None
    if live is not None:
        out = freerouting.autoroute_live(widths, passes, timeout, lim)
    else:
        src = Path(path).expanduser()
        dst, snap = target(path, out_path, "routed", in_place)
        out = freerouting.autoroute_file(src, dst, widths, passes, timeout, lim)
        if snap:
            out["snapshot"] = snap
    out["widths_from_currents"] = widths
    narrow = [w for w in out.get("widened", []) if "wanted_mm" in w]
    if narrow:
        out["still_narrow"] = narrow  # Freerouting necked these down where there's no room for the full width
    if lim:
        out["fab"] = {k: lim[k] for k in ("fab", "margin", "rules", "source")}
        if neck_rules:
            dru = _rules_file(dst, live)
            warns = out.get("fab_check", {}).get("warnings", [])
            # the rule matches by courtyard: a part without one would leave its neck a DRC error
            bare = sorted({w["ref"] for w in warns if w.get("ref") and not getattr(b.part(w["ref"]), "courtyard", None)})
            block = fab.neck_rules([w for w in warns if w.get("ref") not in bare], lim["rules"]["track"]["hard"])
            if dru is not None and (block or dru.exists()):
                src = Path(path).expanduser().with_suffix(".kicad_dru") if path else dru
                before = src.read_text() if src.exists() else ""
                dru.write_text(fab.merge_rules(before, block))
                out["neck_rules"] = {"file": str(dru), "rules": block.count("(rule "),
                                     "what": "KiCad custom rules allowing these nets down to the fab's minimum inside the "
                                             "courtyard of the part they neck into; tracks squeezed elsewhere stay DRC errors",
                                     "check": "kicad-cli doesn't report a broken rules file, it falls back to the board rules: "
                                              "after DRC, the necks in fab_check.warnings should not be track_width errors"}
                if bare:
                    out["neck_rules"]["no_courtyard"] = bare
    if capped:
        out["widths_capped"] = capped
    out["next"] = "review_pcb_layout on the result; fix what it finds (by hand, or route_critical_nets for an item), then review again"
    return out


def finish_routing(path: str, out_path: str | None = None, outer_only: bool = True, in_place: bool | None = None) -> dict:
    """After the autorouter: take out the tracks that break clearance or the minimum width, route what is still open, and
    refill the zones. Saved boards only; where the result goes is target()'s rule (outside a project,
    <name>-finished.kicad_pcb)."""
    if not path:
        raise FlowError("finish_routing works on a saved .kicad_pcb: save the board and pass its path")
    if not Path(path).expanduser().with_suffix(".kicad_pro").exists():
        raise FlowError("no .kicad_pro next to the board, so no design rules: copy the project's .kicad_pro (and .kicad_dru) "
                        "beside it under the board's name, or the new tracks would follow KiCad's defaults, not the board's")
    b, _ = load(path)
    dru = Path(path).expanduser().with_suffix(".kicad_dru")
    necks = fab.read_neck_rules(dru.read_text()) if dru.exists() else {}
    planes = pcb_route.plane_nets(b)
    roles = pcb_review.infer_roles(b)
    plan = pcb_critical.classify(b, only=list(CAREFUL))
    quiet = {n for n in b.nets if n not in planes and roles.get(n, {}).get("role") not in ("ground", "power")}
    skip = {n for it in plan["items"] for n in it["nets"] if n in quiet}
    for it in plan["items"]:  # a crystal's drive often goes through a series resistor: the net behind it too
        if it["class"] != "crystal":
            continue
        own = {p.net for p in b.pads if p.ref == it["id"].split(":")[-1]} | set(it["nets"])
        own &= quiet  # not ground: every pull-down would pull its net in
        skip |= own & quiet
        for part in b.parts:
            pads = [p for p in b.pads if p.ref == part.ref]
            if part.ref[:1] in "RC" and len(pads) == 2 and any(p.net in own for p in pads):
                skip |= {p.net for p in pads if p.net in quiet}
    res = pcb_finish.finish(b, outer_only, necks=necks, skip=skip)
    src = Path(path).expanduser()
    dst, snap = target(path, out_path, "finished", in_place)
    cut = pcb_finish.write(src, dst, res)
    for ext in (".kicad_pro", ".kicad_dru"):  # the rules (and any neck rules) go with it
        if src.with_suffix(ext).exists() and dst.with_suffix(ext) != src.with_suffix(ext):
            dst.with_suffix(ext).write_bytes(src.with_suffix(ext).read_bytes())
    out = {"ok": True, "written": str(dst), "ripped": res["ripped"], "segments_removed": cut,
           "added": {"tracks": len(res["tracks"]), "vias": len(res["vias"])},
           "open_before": res["open_before"], "open_after": res["open_after"], "still_open": res["open"],
           "left_alone": sorted(skip), "by_hand": res["by_hand"]}
    if snap:
        out["snapshot"] = snap
    if res["open_after"] > res["open_before"]:
        out["worse"] = ("more connections open than before: the bad tracks it took out couldn't all be replaced. "
                        + (f"project_restore {snap['name']!r} puts the board back; " if snap else "Keep the input board and ")
                        + "fix the ripped list by hand")
    try:
        freerouting.refill(dst)
        out["zones"] = "refilled"
    except Exception as e:  # noqa: BLE001 - the routing is written either way
        out["zones"] = f"not refilled ({e.__class__.__name__}: {e}); refill in KiCad (B) before DRC"
    out["next"] = "run KiCad's DRC on the result; route still_open by hand, then review_pcb_layout"
    return out

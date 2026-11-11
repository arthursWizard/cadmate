"""Ready to order: every check a board needs before it's sent to be made, run together and returned as one list.

Nothing here is a new check. It gathers the ones cadmate already has (KiCad's DRC with schematic parity, KiCad's ERC,
the layout review, part numbers in the BOM, fab files against the board, the brief's open items, the project layout)
and sorts what they find into what blocks the order, what to look at, and what passed. A board is ready only when DRC
actually ran and nothing blocks.

No board house is assumed: the fab comes from the argument, else the project's choice (project_fab), else none, and
the house-specific checks run only when there is one.
"""

from __future__ import annotations

from pathlib import Path

from . import apps, enclosure, fab_export, package_match, pcb_review, project, schematic_review
from . import fab as fabmod

DRC_RULES = ("drc", "unconnected", "parity")  # pcb_review's findings that came from KiCad's DRC, counted from its summary


class ReadyError(RuntimeError):
    pass


def _item(check: str, what: str, fix: str, **extra) -> dict:
    return {"check": check, "what": what, "fix": fix, **extra}


def erc_summary(rep: dict) -> dict:
    """ERC errors and warnings, by type."""
    out: dict[str, dict[str, int]] = {"error": {}, "warning": {}}
    for sheet in rep.get("sheets", []):
        for v in sheet.get("violations", []):
            sev = v.get("severity", "?")
            if sev in out:
                out[sev][v.get("type", "?")] = out[sev].get(v.get("type", "?"), 0) + 1
    return {"errors": sum(out["error"].values()), "warnings": sum(out["warning"].values()),
            "errors_by_type": out["error"], "warnings_by_type": out["warning"]}


def _types(by_type: dict) -> str:
    return ", ".join(f"{n} × {k}" for k, n in list(by_type.items())[:6])


def check(path: str, fab: str | None = None, currents: dict | None = None, roles: dict | None = None,
          fab_dir: str | None = None, packages: dict[str, str] | None = None) -> dict:
    board = Path(path).expanduser()
    if not board.exists() or board.suffix != ".kicad_pcb":
        raise ReadyError(f"no .kicad_pcb at {board}")
    root = project.find_root(board)
    chosen = fabmod.resolve(fab, board)
    blocking: list[dict] = []
    look: list[dict] = []
    passed: list[str] = []
    not_checked: list[dict] = []

    # the layout review, with KiCad's DRC (and schematic parity when the schematic is beside the board)
    review = pcb_review.review_file(str(board), currents=currents, roles=roles, fab=fab, drc=True)
    drc = review.get("drc")
    if drc is None:
        why = next((s["why"] for s in review["not_checked"] if s["rule"] == "kicad_drc"), "kicad-cli not found")
        not_checked.append({"check": "drc", "why": why})
    else:
        if drc["errors"]:
            blocking.append(_item("drc", f"DRC: {drc['errors']} error(s): {_types(drc['errors_by_type'])}",
                                  "fix them in the board; review_pcb_layout gives the locations"))
        if drc["unconnected"]:
            blocking.append(_item("drc", f"DRC: {drc['unconnected']} unconnected item(s)", "route them; the board isn't finished"))
        if drc["parity"]:
            blocking.append(_item("parity", f"schematic and board disagree in {drc['parity']} place(s)",
                                  "update the PCB from the schematic in KiCad (or fix the schematic), then check again"))
        if drc["warnings"]:
            look.append(_item("drc", f"DRC: {drc['warnings']} warning(s): {_types(drc['warnings_by_type'])}",
                              "decide each with the user: fix it, or keep it on purpose and say why"))
        if not (drc["errors"] or drc["unconnected"] or drc["parity"] or drc["warnings"]):
            passed.append("DRC: 0 errors, 0 warnings, 0 unconnected, 0 parity")
    own = [f for f in review["findings"] if f["rule"] not in DRC_RULES]
    problems = [f for f in own if f["severity"] == "problem"]
    warnings = [f for f in own if f["severity"] == "warning"]
    for f in problems:
        blocking.append(_item(f["rule"], f["what"], "see review_pcb_layout for the reasoning and location", source=f["source"]))
    if warnings:
        look.append(_item("layout", f"{len(warnings)} layout warning(s): " + "; ".join(f["what"] for f in warnings[:5]),
                          "review_pcb_layout has each one with its reasoning; fix or keep on purpose"))
    if not problems and not warnings:
        passed.append("layout review: no problems or warnings" + (f" ({review['counts']['note']} notes)" if review["counts"]["note"] else ""))
    not_checked += [{"check": s["rule"], "why": s["why"]} for s in review["not_checked"] if s["rule"] != "kicad_drc"]

    # ERC
    sch = board.with_suffix(".kicad_sch")
    if not sch.exists():
        look.append(_item("erc", "no schematic beside the board, so ERC and schematic parity weren't checked",
                          "keep the .kicad_sch next to the .kicad_pcb with the same name"))
    elif not apps.find_kicad_cli():
        not_checked.append({"check": "erc", "why": f"kicad-cli not found; set {apps.KICAD_CLI_ENV}"})
    else:
        rep = schematic_review.run_erc(sch)
        if rep is None or "error" in rep:
            not_checked.append({"check": "erc", "why": (rep or {}).get("error", "kicad-cli not found")})
        else:
            erc = erc_summary(rep)
            if erc["errors"]:
                blocking.append(_item("erc", f"ERC: {erc['errors']} error(s): {_types(erc['errors_by_type'])}",
                                      "fix them in the schematic; review_schematic gives the locations"))
            if erc["warnings"]:
                look.append(_item("erc", f"ERC: {erc['warnings']} warning(s): {_types(erc['warnings_by_type'])}",
                                  "decide each with the user"))
            if not erc["errors"] and not erc["warnings"]:
                passed.append("ERC: 0 errors, 0 warnings")

    # part numbers, layers
    text = board.read_text(encoding="utf-8", errors="replace")
    parts = [p for p in fab_export.footprints(text) if fab_export._in_bom(p)]
    bare = sorted((p["ref"] for p in parts if not p["mpn"].strip()
                   and all(fab_export.placeholder_any(p[d]) for d in fab_export.DISTRIBUTORS)), key=fab_export._refkey)
    if bare:
        (blocking if chosen["assembly"] else look).append(_item(
            "bom", f"{len(bare)} part(s) with no part number: {', '.join(bare[:20])}",
            "add the manufacturer part number (MPN field) or a distributor number in the schematic"
            + ("; the assembler can't place a part it can't buy" if chosen["assembly"] else "; needed to buy them")))
    elif parts:
        passed.append(f"every BOM part ({len(parts)}) has a part number")
    if chosen["fab"] == "jlcpcb" and chosen["assembly"]:
        no_lcsc = sorted((p["ref"] for p in parts if fab_export.placeholder(p["lcsc"])), key=fab_export._refkey)
        if no_lcsc:
            blocking.append(_item("bom", f"{len(no_lcsc)} part(s) without an LCSC number for JLCPCB assembly: {', '.join(no_lcsc[:20])}",
                                  "add the LCSC field, or mark parts you'll solder yourself as excluded from the BOM"))
    dist = fab_export._norm(chosen.get("distributor") or "")
    dist = next((d for d, names in fab_export.FIELDS.items() if d in fab_export.DISTRIBUTORS and dist in names), None)
    if dist:
        missing = sorted((p["ref"] for p in parts if fab_export.placeholder_any(p[dist])), key=fab_export._refkey)
        if missing:
            look.append(_item("bom", f"{len(missing)} part(s) without a {chosen['distributor']} number: {', '.join(missing[:20])}",
                              f"add the {chosen['distributor']} number, or buy those elsewhere"))
    # the package being bought against the footprint on the board (from a Package field, or packages {ref: text})
    packages = packages or {}
    given = {p["ref"]: packages[p["ref"]] if p["ref"] in packages else p["package"] for p in parts}  # "" skips one
    unknown_refs = sorted(set(packages) - set(given), key=fab_export._refkey)
    if unknown_refs:
        look.append(_item("package", f"packages given for parts not on the board: {', '.join(unknown_refs[:20])}",
                          "check the references"))
    compared, unread = 0, []
    for p in sorted(parts, key=lambda p: fab_export._refkey(p["ref"])):
        if not (given.get(p["ref"]) or "").strip():
            continue
        r = package_match.compare(given[p["ref"]], p["footprint"])
        if r["result"] == "mismatch":
            blocking.append(_item("package", f"{p['ref']} ({given[p['ref']]}) on footprint {p['footprint']}: " + "; ".join(r["why"]),
                                  "the part won't fit its pads: change the footprint, or pick the part in this package"))
        elif r["result"] == "unknown":
            unread.append(p["ref"])
        compared += r["result"] == "match"
    if unread:
        look.append(_item("package", f"couldn't compare the package with the footprint for {', '.join(unread[:20])}",
                          "check those by eye against the datasheet's package drawing"))
    if compared and not any(i["check"] == "package" for i in blocking):
        passed.append(f"packages match their footprints ({compared} part(s) with a package to compare)")
    layers = len(fab_export.copper_layers(text))
    if chosen.get("layers") and layers and layers != chosen["layers"]:
        blocking.append(_item("layers", f"the board has {layers} copper layers; the project says {chosen['layers']}",
                              "change the board's stackup, or the choice with project_fab if it changed"))

    # where it gets made
    ask = not chosen["house"] and chosen["from"] in ("project", "not asked")  # "none" as the argument is an answer
    if ask:
        look.append(_item("fab", "no board house chosen" + (" yet: the user was never asked" if chosen["from"] == "not asked" else ""),
                          "ask the user now (ask_user): the generic fab files suit any house, but the house's own limits "
                          "and file format are only checked once it's chosen; record it with project_fab"))
    elif not chosen["house"] and chosen["from"] == "not in a project":
        look.append(_item("fab", "no board house given, so its limits weren't checked",
                          "pass fab if the user has one in mind; the generic fab files suit any house"))
    elif chosen["house"] and not chosen["fab"]:
        look.append(_item("fab", f"cadmate has no rule profile for {chosen['house']}",
                          f"check the board's rules against {chosen['house']}'s capability page by hand"))

    # fab files: the ones this house needs, all newer than the board
    out_dir = Path(fab_dir).expanduser() if fab_dir else (root / "fab" / board.stem) if root else board.parent / "fab" / board.stem
    want = [f"{board.stem}-gerbers.zip", f"{board.stem}-bom.csv", f"{board.stem}-pos.csv"]
    if chosen["fab"] == "jlcpcb":
        want += [f"{board.stem}-bom-jlc.csv", f"{board.stem}-cpl-jlc.csv"]
    have = [out_dir / n for n in want if (out_dir / n).is_file()]
    if not have:
        look.append(_item("fab_files", f"no fab files in {out_dir}", "export_fab once everything above is clear (pass "
                                                                       "fab_dir here if they were exported elsewhere)"))
    elif len(have) < len(want):
        blocking.append(_item("fab_files", "fab files missing: " + ", ".join(n for n in want if not (out_dir / n).is_file()),
                              "export_fab again" + (" now that the house is chosen" if chosen["house"] else "")))
    elif min(f.stat().st_mtime for f in have) < board.stat().st_mtime:
        blocking.append(_item("fab_files", f"the fab files in {out_dir.name}/ are older than the board",
                              "export_fab again: ordering them would send an earlier board"))
    else:
        passed.append("fab files complete and newer than the board")

    # the project: open requirements, layout errors, the enclosure check
    if root:
        brief = project.read_brief(root)
        if brief and brief["open"]:
            blocking.append(_item("brief", f"{len(brief['open'])} requirement(s) in the brief not ticked: " + "; ".join(brief["open"][:8]),
                                  "check each against the design and tick it, or ask the user whether it still applies"))
        elif brief and brief["met"]:
            passed.append(f"brief: all {len(brief['met'])} requirement(s) met")
        else:
            look.append(_item("brief", "the brief lists no requirements, so nothing was checked against them",
                              "ask the user what the board must do and fit, and write it into docs/brief.md"))
        pc = project.check(root)
        for f in pc["findings"]:
            if f["severity"] == "error":
                blocking.append(_item("project", f"{f['path']}: {f['problem']}", f["fix"]))
            elif f["path"].endswith(enclosure.REPORT):  # a missing, stale or failing enclosure check
                look.append(_item("enclosure", f"{f['path']}: {f['problem']}", f["fix"]))

    ready = drc is not None and not blocking
    out = {"ok": True, "ready": ready, "board": str(board), "fab": chosen, "blocking": blocking, "to_look_at": look,
           "passed": passed, "not_checked": not_checked, **({"drc": drc} if drc else {}),
           "say": "lead with ready or not; list every blocking item, then what to look at; give DRC as all four numbers"}
    if not ready and drc is None:
        out["why_not_ready"] = "DRC didn't run, so cadmate can't say the board is ready"
    if ask:
        out["ask_user"] = project.FAB_QUESTION
    return out

"""Enclosure checks on a case assembly: parts that collide, shells with openings nobody asked for, and joints between
case parts that don't sit flush (a gap, or one part standing proud of the other).

FreeCAD does the geometry (freecad_scripts/check_enclosure.py); this module works out what to check, turns the
measurements into findings, and keeps the report beside the case so project_check can tell when it's missing or stale.

Inputs, in a cadmate project: case/assembly.step (every part placed, each a named child), case/<part>.step for each
printed part (their names say which assembly children are the case), and optionally case/enclosure.json:

    {
      "enclosures": {"main": ["main-top", "main-base"], "sensor": ["sensor-cap"]},  # the closed shells; default below
      "openings": [                                                      # the openings meant to be there
        {"name": "buttons", "enclosure": "main", "face": "top", "near": "buttons", "count": 4},
        {"name": "USB-C", "enclosure": "main", "near": "board", "face": "side"},
        {"name": "display window", "box": [-25, -40, 10, 25, 30, 13]}
      ],
      "joints": {"main-top/sensor-cap": {"max_step": 1.0}, "main-top/main-base": {"skip": true}},
      "heights": {"main-top": 24.0},                                     # top of the part above z = 0
      "allow_overlap": [["light-pipe", "main-top"]],
      "max_gap": 0.3, "max_step": 0.2, "min_clearance": 0.3, "resolution": 0.5, "max_opening": 20
    }

Shells, by default: parts whose names agree up to the last dash form one (main-top + main-base). If no two names
agree that way, all the case parts form one shell. Say "enclosures" whenever that guess is wrong. Faces are classed
with +z up (top, bottom, side), as the assembly sits on the desk. A designed step or gap at a joint (a rim, a shadow
line) is a per-project limit under "joints", never a change to the defaults.

An opening matches a declared one when everything the declaration gives holds: its centre inside "box" (1 mm margin),
"near" among the parts beside it, the same "face" (top, bottom, side), the same "enclosure".
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import apps, project

SPEC = "enclosure.json"
REPORT = "enclosure-check.json"
ASSEMBLY = "assembly.step"
DEFAULTS = {"resolution": 0.5, "max_opening": 20.0, "joint_search": 2.0, "joint_spacing": 3.0,
            "max_joint_samples": 120, "overlap_tol": 0.01, "max_gap": 0.3, "max_step": 0.2,
            "height_tol": 0.05, "min_clearance": None}
SCRIPT = Path(__file__).parent / "freecad_scripts" / "check_enclosure.py"


class EnclosureError(RuntimeError):
    pass


def locate(path: str | Path) -> tuple[Path, Path]:
    """(assembly STEP, case folder) from a STEP, a case folder or a project."""
    p = Path(path).expanduser().resolve()
    if p.is_file():
        return p, p.parent
    for d in (p / "case", p):
        if (d / ASSEMBLY).exists():
            return d / ASSEMBLY, d
    raise EnclosureError(f"no {ASSEMBLY} in {p} or {p / 'case'}: export the case with every part placed, each a named child")


def case_part_names(case_dir: Path) -> list[str]:
    """The printed parts: case/<part>.step, except the assembly."""
    return sorted(f.stem for f in case_dir.glob("*.step") if f.name != ASSEMBLY)


def group_enclosures(case_parts: list[str]) -> dict[str, list[str]]:
    """Parts whose names agree up to the last dash (main-top, main-base) make one shell, and a part alone is its own;
    when no two names agree that way (top, base), all the parts make one shell."""
    groups: dict[str, list[str]] = {}
    for n in case_parts:
        groups.setdefault(n.rsplit("-", 1)[0] if "-" in n else n, []).append(n)
    if all(len(v) == 1 for v in groups.values()):
        return {"case": list(case_parts)}
    return groups


def load_spec(case_dir: Path, spec: dict | str | None) -> tuple[dict, str | None]:
    """The spec as given, else case/enclosure.json, else {}; and where it came from."""
    if isinstance(spec, dict):
        return spec, "argument"
    f = Path(spec).expanduser() if spec else case_dir / SPEC
    if not f.exists():
        if spec:
            raise EnclosureError(f"no spec at {f}")
        return {}, None
    try:
        return json.loads(f.read_text(encoding="utf-8")), str(f)
    except ValueError as e:
        raise EnclosureError(f"{f} isn't valid JSON: {e}") from None


def resolve(case_dir: Path, spec: dict) -> dict:
    """Everything the FreeCAD script needs, with the defaults filled in."""
    s = DEFAULTS | {k: v for k, v in spec.items() if k in DEFAULTS}
    s["case_parts"] = spec.get("case_parts") or case_part_names(case_dir)
    if not s["case_parts"]:
        raise EnclosureError(f"no case parts: put each printed part in {case_dir}/<part>.step or list them as case_parts")
    s["enclosures"] = spec.get("enclosures") or group_enclosures(s["case_parts"])
    plugs: dict[str, list] = {}
    for o in spec.get("openings", []):
        if "box" in o and o.get("enclosure"):
            plugs.setdefault(o["enclosure"], []).append(o["box"])
    s["plugs"] = plugs
    return s


def measure(assembly: Path, resolved: dict, timeout: int = 1800) -> dict:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "spec.json"
        f.write_text(json.dumps(resolved), encoding="utf-8")
        return apps.run_freecad_script(SCRIPT, env={"CADMATE_ASSEMBLY": str(assembly), "CADMATE_SPEC": str(f)}, timeout=timeout)


# ---------------------------------------------------------------------------------------------------------------------
# findings


def _f(sev: str, check: str, parts: list[str], problem: str, fix: str, at=None) -> dict:
    d = {"severity": sev, "check": check, "parts": parts, "problem": problem, "fix": fix}
    if at is not None:
        d["at"] = at
    return d


def _inside(c: list[float], box: list[float], margin: float = 1.0) -> bool:
    return all(box[i] - margin <= c[i] <= box[i + 3] + margin for i in range(3))


def _matches(decl: dict, enc: str, o: dict) -> bool:
    if decl.get("enclosure") and decl["enclosure"] != enc:
        return False
    if "box" in decl and not _inside(o["centre"], decl["box"]):
        return False
    if decl.get("near") and decl["near"] not in o.get("near", []):
        return False
    if decl.get("face") and decl["face"] != o.get("face"):
        return False
    return any(k in decl for k in ("box", "near", "face"))  # a name alone matches nothing


def _size(box: list[float]) -> str:
    return " x ".join(f"{box[i + 3] - box[i]:.1f}" for i in range(3))


def evaluate(raw: dict, spec: dict, resolved: dict) -> dict:
    """Findings from the FreeCAD measurements. errors: something collides, a shell is open where nobody said it should
    be, a joint has a gap or a step, a part's height is off. warnings: a declared opening wasn't found, a check couldn't
    run."""
    out: list[dict] = []
    case = set(resolved["case_parts"])
    for n in raw.get("missing_case_parts", []):
        out.append(_f("error", "parts", [n], f"case part {n} isn't in the assembly",
                      "export the assembly with every printed part as a child named after its STEP"))
    allowed = {frozenset(p) for p in spec.get("allow_overlap", [])}
    tol = resolved["overlap_tol"]
    for r in raw.get("interference", []):
        pair = [r["a"], r["b"]]
        if "error" in r:
            out.append(_f("warning", "collision", pair, f"FreeCAD couldn't intersect them: {r['error']}",
                          "check both shapes are valid solids (Part > Check geometry)"))
        elif r.get("volume", 0) > tol and frozenset(pair) not in allowed:
            region = f" (region {_size(r['at'])} mm)" if r.get("at") else ""
            out.append(_f("error", "collision", pair, f"{r['a']} and {r['b']} overlap by {r['volume']:.3g} mm³{region}",
                          "move or cut one of them; if the overlap is "
                          "meant (a press fit, a clip), add the pair to allow_overlap in case/enclosure.json", r.get("at")))
        elif (mc := resolved.get("min_clearance")) and 0 < r.get("gap", mc) < mc and bool(case & set(pair)) != (set(pair) <= case):
            out.append(_f("warning", "clearance", pair, f"only {r['gap']} mm between {r['a']} and {r['b']}",
                          f"give it at least {mc} mm", r.get("gap_at")))

    declared = spec.get("openings", [])
    found_for = {i: 0 for i in range(len(declared))}
    for ename, e in raw.get("enclosures", {}).items():
        for o in e["openings"]:
            hit = [i for i, d in enumerate(declared) if _matches(d, ename, o)]
            if hit:
                o["intended"] = declared[hit[0]].get("name", f"opening {hit[0] + 1}")
                found_for[hit[0]] += 1
                continue
            o["intended"] = None
            near = f", beside {', '.join(o['near'])}" if o.get("near") else ""
            out.append(_f("error", "closed shell", e["parts"],
                          f"{ename}: opening in the {o['face']} ({_size(o['outer_box'])} mm, about {o['outer_area']} mm² "
                          f"on the outside) at {o['centre']}{near}",
                          "close it; if it's meant to be there, add it to openings in case/enclosure.json (name plus "
                          "box, near or face)", o["outer_box"]))
        ex = e.get("exact", {})
        if "error" in ex:
            out.append(_f("warning", "closed shell", e["parts"], f"{ename}: the exact seal test failed: {ex['error']}",
                          "check the parts are valid solids"))
        elif "sealed" in ex and not ex["sealed"]:
            out.append(_f("error", "closed shell", e["parts"],
                          f"{ename}: still open with every opening found plugged: a leak narrower than the "
                          f"{resolved['resolution']} mm voxels, or the shell has no inside",
                          "rerun with a smaller resolution to find it"))
    for i, d in enumerate(declared):
        n = d.get("name", f"opening {i + 1}")
        if found_for[i] == 0:
            out.append(_f("warning", "closed shell", [], f"declared opening '{n}' wasn't found",
                          "it may have been closed by mistake, or its box/near/face doesn't match: look at the openings list"))
        elif d.get("count") and found_for[i] != d["count"]:
            out.append(_f("warning", "closed shell", [], f"'{n}': {found_for[i]} openings found, {d['count']} expected",
                          "look at the openings list: two may have merged, or one is missing or extra"))

    jspec = spec.get("joints", {})
    for j in raw.get("joints", []):
        key = next((k for k in (f"{j['a']}/{j['b']}", f"{j['b']}/{j['a']}") if k in jspec), None)
        js = resolved | (jspec.get(key, {}) if key else {})
        if js.get("skip"):
            continue
        gaps = [s for s in j["samples"] if s["gap"] is not None and s["gap"] > js["max_gap"]]
        steps = [s for s in j["samples"] if s["step"] is not None and abs(s["step"]) > js["max_step"]]
        n = len(j["samples"])
        if gaps:
            w = max(gaps, key=lambda s: s["gap"])
            out.append(_f("error", "flush joint", [j["a"], j["b"]],
                          f"gap up to {w['gap']:.2f} mm between {j['a']} and {j['b']} at {len(gaps)} of {n} points along "
                          f"the seam (limit {js['max_gap']})", "close the gap: extend one part to meet the other", w["at"]))
        if steps:
            w = max(steps, key=lambda s: abs(s["step"]))
            proud = j["a"] if w["step"] > 0 else j["b"]
            out.append(_f("error", "flush joint", [j["a"], j["b"]],
                          f"{proud} stands {abs(w['step']):.2f} mm proud of the other at the seam, {len(steps)} of {n} "
                          f"points over {js['max_step']} mm", "bring the two surfaces level at the joint; if the step is "
                          f"meant (a rim), set max_step for '{j['a']}/{j['b']}' in case/enclosure.json", w["at"]))
    tops = {n: raw["parts"][n]["bbox"][5] for n in resolved["case_parts"] if n in raw.get("parts", {})}
    for n, want in spec.get("heights", {}).items():
        if n not in tops:
            out.append(_f("warning", "height", [n], f"heights names {n}, which isn't a case part", "fix the name"))
        elif abs(tops[n] - want) > resolved["height_tol"]:
            out.append(_f("error", "height", [n], f"{n} tops out at {tops[n]:.2f} mm, the brief says {want}",
                          "change the part's height, or the brief if it changed"))
    order = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda f: (order[f["severity"]], f["check"]))
    return {"findings": out, "tops": tops}


def summary(raw: dict, ev: dict) -> dict:
    joints = {}
    for j in raw.get("joints", []):
        g = [s["gap"] for s in j["samples"] if s["gap"] is not None]
        st = [abs(s["step"]) for s in j["samples"] if s["step"] is not None]
        joints[f"{j['a']}/{j['b']}"] = {"points": len(j["samples"]), "max_gap": max(g) if g else None,
                                        "max_step": max(st) if st else None}
    return {
        "collisions": [[r["a"], r["b"], r["volume"]] for r in raw.get("interference", []) if r.get("volume", 0) > 0],
        "enclosures": {k: {"sealed": e.get("exact", {}).get("sealed", not any(o.get("intended") is None for o in e["openings"])),
                           "interior_mm3": e["interior_volume"],
                           "openings": [{"intended": o.get("intended"), "face": o["face"], "size": _size(o["outer_box"]),
                                         "centre": o["centre"], "near": o["near"]} for o in e["openings"]]}
                       for k, e in raw.get("enclosures", {}).items()},
        "joints": joints,
        "tops": ev["tops"],
    }


def check(path: str | Path, spec: dict | str | None = None, save: bool = True) -> dict:
    """Run the enclosure checks on a case assembly and keep the report beside it."""
    assembly, case_dir = locate(path)
    spec_d, spec_from = load_spec(case_dir, spec)
    resolved = resolve(case_dir, spec_d)
    raw = measure(assembly, resolved)
    ev = evaluate(raw, spec_d, resolved)
    counts = {s: sum(1 for f in ev["findings"] if f["severity"] == s) for s in ("error", "warning", "info")}
    result = {"ok": counts["error"] == 0, "assembly": str(assembly), "spec": spec_from, "counts": counts,
              "findings": ev["findings"], "summary": summary(raw, ev), "checked": datetime.now().isoformat(timespec="seconds"),
              "settings": {k: resolved[k] for k in DEFAULTS} | {"enclosures": resolved["enclosures"]}}
    if spec_from is None:
        result["next"] = (f"no {SPEC}: every opening found is reported as unintended. Write case/{SPEC} with the "
                          "openings that are meant to be there (and heights from the brief), then run this again")
    if save:
        f = case_dir / REPORT
        f.write_text(json.dumps(result | {"raw": raw}, indent=1) + "\n", encoding="utf-8")
        result["report"] = str(f)
    return result


# ---------------------------------------------------------------------------------------------------------------------
# project_check's view: is there a report, is it current, is it clean


def project_findings(root: Path) -> list[dict]:
    case = root / "case"
    steps = [f for f in case.glob("*.step")] if case.exists() else []
    if not steps:
        return []
    rep = case / REPORT
    rel = str(rep.relative_to(root))
    if not rep.exists():
        return [project._finding("info", rel, "the case has never been checked",
                                 "run check_enclosure on the project (collisions, openings, flush joints)")]
    newer = [f.name for f in steps + [case / SPEC] if f.exists() and f.stat().st_mtime > rep.stat().st_mtime]
    if newer:
        return [project._finding("warning", rel, f"older than {', '.join(sorted(newer)[:3])}",
                                 "run check_enclosure again")]
    try:
        r = json.loads(rep.read_text(encoding="utf-8"))
    except ValueError:
        return [project._finding("warning", rel, "not valid JSON", "run check_enclosure again")]
    n = r.get("counts", {}).get("error", 0)
    if n:
        first = next((f["problem"] for f in r.get("findings", []) if f["severity"] == "error"), "")
        first = re.sub(r"\s+", " ", first)[:160]
        return [project._finding("warning", rel, f"the last enclosure check found {n} error(s), e.g. {first}",
                                 "fix the case, or declare what's intended in case/enclosure.json, and run check_enclosure again")]
    return []

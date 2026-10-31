# Runs inside FreeCADCmd. Reads the assembly STEP in $CADMATE_ASSEMBLY and the settings in the JSON file
# $CADMATE_SPEC (resolved by cadmate/enclosure.py), measures the enclosure, and writes the raw measurements as JSON to
# $CADMATE_OUT. cadmate/enclosure.py turns them into findings.
#
# Spec: {"case_parts": [names], "enclosures": {name: [case part names]}, "plugs": {enclosure: [[x0,y0,z0,x1,y1,z1]]},
#        "resolution", "max_opening", "joint_search", "joint_spacing", "max_joint_samples",
#        "overlap_tol", "min_clearance"}
#
# - parts: every direct child of the assembly (a compound of its solids), with its box, volume and solid count.
# - interference: the exact common volume of every pair whose boxes overlap, and for pairs that only touch or come
#   close, the gap between them (with min_clearance only: distances to big solids such as a board take FreeCAD
#   half a minute each).
# - enclosures: each group of case parts is voxelised (sliced at each voxel layer, exact outlines), the fit seams
#   between its parts filled, and closed with a ball of radius max_opening/2 to get its envelope. Where the envelope's
#   outer layer is air joined to the inside, that's an opening's mouth. A shell of one part also gets an exact test:
#   the part plus a plug over every opening found (and the declared plugs) is cut out of a box, and the air must split
#   into outside and inside.
# - joints: on the outer surface, every place where two case parts come within joint_search of each other is a seam.
#   At sample points along it, rays against each part's mesh measure the step across the seam (one part's surface
#   standing proud of the other) and the gap between them.
import json
import math
import os
import time

import FreeCAD as App
import Import
import numpy as np
import Part
from matplotlib.path import Path as MPath
from scipy import ndimage

V = App.Vector
T0 = time.time()
spec = json.load(open(os.environ["CADMATE_SPEC"]))
R = float(spec.get("resolution", 0.5))
timing = {}


def lap(name):
    timing[name] = round(time.time() - T0, 1)
    print(f"cadmate: {name} done at {timing[name]} s", flush=True)


# ---------------------------------------------------------------------------------------------------------------------
# load

doc = App.newDocument("cadmate_enclosure")
Import.insert(os.environ["CADMATE_ASSEMBLY"], doc.Name)


def global_shape(o):
    s = o.Shape.copy()
    s.Placement = o.getGlobalPlacement()
    return s


def leaves(o):
    if o.TypeId == "App::Part" or (hasattr(o, "Group") and not hasattr(o, "Shape")):
        out = []
        for c in o.Group:
            out += leaves(c)
        return out
    return [o] if hasattr(o, "Shape") and not o.Shape.isNull() and o.Shape.Solids else []


roots = [o for o in doc.Objects if not o.InList]
children = []
for r in roots:
    if r.TypeId == "App::Part" and len(roots) == 1:
        children += list(r.Group)  # the usual export: one App::Part holding the parts
    else:
        children.append(r)
parts = {}
for c in children:
    shapes = [global_shape(x) for x in leaves(c)]
    solids = [s for sh in shapes for s in sh.Solids]
    if solids:
        name = c.Label
        while name in parts:
            name += "_"
        parts[name] = Part.makeCompound(solids)
lap("load")


def bb(s):
    b = s.BoundBox
    return [b.XMin, b.YMin, b.ZMin, b.XMax, b.YMax, b.ZMax]


out = {"parts": {n: {"bbox": [round(v, 3) for v in bb(s)], "volume": round(s.Volume, 3), "solids": len(s.Solids)}
                 for n, s in parts.items()},
       "warnings": []}
case_names = [n for n in spec["case_parts"] if n in parts]
out["missing_case_parts"] = [n for n in spec["case_parts"] if n not in parts]


# ---------------------------------------------------------------------------------------------------------------------


def boxes_near(a, b, d):
    return all(a[i] - d <= b[i + 3] and b[i] - d <= a[i + 3] for i in range(3))


# ---------------------------------------------------------------------------------------------------------------------
# voxels: one grid over every case part, padded for the closing


case_bb = None
for n in case_names:
    b = bb(parts[n])
    case_bb = b if case_bb is None else [min(case_bb[i], b[i]) for i in range(3)] + [max(case_bb[i + 3], b[i + 3]) for i in range(3)]
max_open = float(spec.get("max_opening", 20.0))
pad = int(math.ceil(max_open / 2 / R)) + 3
origin = np.array(case_bb[:3]) - pad * R
shape3 = tuple(int(math.ceil((case_bb[i + 3] - case_bb[i]) / R)) + 2 * pad for i in range(3))
xs = origin[0] + (np.arange(shape3[0]) + 0.5) * R
ys = origin[1] + (np.arange(shape3[1]) + 0.5) * R
zs = origin[2] + (np.arange(shape3[2]) + 0.5) * R + 1e-3  # off any face that sits on a round number


def rasterise(shape):
    m = np.zeros(shape3, bool)
    sb = bb(shape)
    for k, z in enumerate(zs):
        if z < sb[2] or z > sb[5]:
            continue
        for solid in shape.Solids:
            sl = bb(solid)
            if not sl[2] < z < sl[5]:
                continue
            layer = np.zeros(shape3[:2], bool)
            for w in solid.slice(V(0, 0, 1), z):
                if not w.isClosed():
                    continue
                pts = np.array([[p.x, p.y] for p in w.discretize(Deflection=R / 8)])
                i0 = max(int((pts[:, 0].min() - origin[0]) / R) - 1, 0)
                i1 = min(int((pts[:, 0].max() - origin[0]) / R) + 2, shape3[0])
                j0 = max(int((pts[:, 1].min() - origin[1]) / R) - 1, 0)
                j1 = min(int((pts[:, 1].max() - origin[1]) / R) + 2, shape3[1])
                gx, gy = np.meshgrid(xs[i0:i1], ys[j0:j1], indexing="ij")
                inside = MPath(pts).contains_points(np.column_stack([gx.ravel(), gy.ravel()]))
                layer[i0:i1, j0:j1] ^= inside.reshape(gx.shape)  # even-odd: holes come out
            m[:, :, k] |= layer
    return m


masks = {n: rasterise(parts[n]) for n in case_names}
lap("voxels")


def world(idx):
    idx = np.asarray(idx, float)
    return origin + (idx + 0.5) * R


def vbox(sel):
    """World box of a set of voxel indices (N x 3), to the voxel edges."""
    lo, hi = sel.min(0), sel.max(0)
    return [round(float(v), 2) for v in list(origin + lo * R) + list(origin + (hi + 1) * R)]


def edt(m):
    return ndimage.distance_transform_edt(m) * R


# ---------------------------------------------------------------------------------------------------------------------
# interference: exact booleans, but only where the voxels say a case part is close


def voxels_near(case_part, solid):
    """Whether the case part has a voxel within the solid's box grown by min_clearance and a voxel: the board under a
    lid fills the lid's box, but only the parts of it near the lid's material are worth an exact boolean."""
    b = bb(solid)
    lo = np.floor((np.array(b[:3]) - near_d - R - origin) / R).astype(int).clip(0, np.array(shape3) - 1)
    hi = np.ceil((np.array(b[3:]) + near_d + R - origin) / R).astype(int).clip(1, np.array(shape3))
    return bool(masks[case_part][lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]].any())


names = list(parts)
clearance = spec.get("min_clearance")
near_d = float(clearance or 0)
inter = []
for i, a in enumerate(names):
    for b in names[i + 1:]:
        ba, bb_ = bb(parts[a]), bb(parts[b])
        if not boxes_near(ba, bb_, near_d):
            continue
        rec = {"a": a, "b": b}
        try:
            # solid by solid: one bad solid in a board model shouldn't hide the rest
            vol, where, close = 0.0, None, []
            case_one = a if a in case_names else b if b in case_names else None
            for sa in parts[a].Solids:
                for sb in parts[b].Solids:
                    if not boxes_near(bb(sa), bb(sb), near_d):
                        continue
                    if case_one and len(case_names) and not voxels_near(case_one, sb if case_one == a else sa):
                        continue  # nothing of the case part anywhere near this solid
                    close.append((sa, sb))
                    if not boxes_near(bb(sa), bb(sb), 0):
                        continue
                    c = sa.common(sb)
                    if c.Volume > 1e-6:
                        vol += c.Volume
                        if where is None or c.Volume > where[0]:
                            where = (c.Volume, bb(c))
            rec["volume"] = round(vol, 4)
            if where:
                rec["at"] = [round(v, 2) for v in where[1]]
            if clearance and vol <= spec.get("overlap_tol", 0.01) and (a in case_names or b in case_names):
                d, pts = near_d + 1, None
                for sa, sb in close:
                    dd, pp, _ = sa.distToShape(sb)
                    if dd < d:
                        d, pts = dd, pp
                if d <= near_d:
                    rec["gap"] = round(d, 3)
                    rec["gap_at"] = [round(v, 2) for v in pts[0][0]]
        except Exception as e:  # noqa: BLE001 - OCC raises plain exceptions
            rec["error"] = str(e)[:200]
        inter.append(rec)
out["interference"] = inter
lap("interference")


# ---------------------------------------------------------------------------------------------------------------------
# enclosures: envelope, openings, exact seal


ball = max_open / 2
envelopes = {}
enc_out = {}
for ename, members in spec["enclosures"].items():
    members = [m for m in members if m in masks]
    if not members:
        continue
    S = np.zeros(shape3, bool)
    for m in members:
        S |= masks[m]
    # fit seams: air a voxel wide between two parts of the shell is the clearance between them, not an opening (the
    # joint check measures it)
    cube = np.ones((3, 3, 3), bool)
    seam = np.zeros(shape3, bool)
    for i, m in enumerate(members):
        grown = ndimage.binary_dilation(masks[m], structure=cube)
        for m2 in members[i + 1:]:
            seam |= grown & ndimage.binary_dilation(masks[m2], structure=cube) & ~S
    S |= seam
    D = edt(~S) <= ball  # dilate by the ball
    E = (edt(D) > ball) | S  # then erode: the closing fills every opening narrower than the ball
    envelopes[ename] = E
    depth = edt(E)
    air = E & ~S
    lab, n = ndimage.label(air)
    sizes = ndimage.sum(air, lab, range(1, n + 1)) * R ** 3 if n else np.array([])
    big = sizes.max() if n else 0
    keep = [i + 1 for i, v in enumerate(sizes) if v >= max(50.0, 0.05 * big)]
    interior = np.isin(lab, keep)  # the shell's inside, and every opening's mouth with it; corner fills stay apart
    # an opening's mouth: the outermost layer of the envelope where it is air, and joined to the inside
    mouth = interior & (depth <= R * 1.01)
    mlab, mn = ndimage.label(mouth, structure=cube)
    openings = []
    grad = np.gradient(depth)
    for k in range(1, mn + 1):
        outer = np.argwhere(mlab == k)
        g = np.array([-grad[a][tuple(outer.T)].mean() for a in range(3)])  # out of the envelope
        g = g / (np.linalg.norm(g) or 1)
        side = "top" if g[2] > 0.7 else "bottom" if g[2] < -0.7 else "side"
        ob = vbox(outer)
        near = [p for p in parts if p not in case_names and boxes_near(bb(parts[p]), ob, 1.0)]
        openings.append({"outer_box": ob, "centre": [round(float(v), 2) for v in world(outer.mean(0))],
                         "face": side, "normal": [round(float(v), 2) for v in g],
                         "outer_area": round(len(outer) * R * R, 1), "near": near})
    interior &= depth > R * 1.01
    rec = {"parts": members, "seam_voxels": int(seam.sum()), "interior_volume": round(float(interior.sum()) * R ** 3, 1),
           "envelope_box": vbox(np.argwhere(E)), "openings": openings}
    # exact: plug every opening found and every declared plug; the air must split into outside and inside. A shell
    # of several parts has fit seams the voxels closed, and those leak here, so only one-part shells get this test.
    try:
        if len(members) > 1:
            raise StopIteration

        plugs = []
        for o in openings:  # the hole where it meets the outside, a voxel bigger all round
            b = o["outer_box"]
            plugs.append(Part.makeBox(b[3] - b[0] + 2 * R, b[4] - b[1] + 2 * R, b[5] - b[2] + 2 * R, V(b[0] - R, b[1] - R, b[2] - R)))
        for b in spec.get("plugs", {}).get(ename, []):
            plugs.append(Part.makeBox(b[3] - b[0], b[4] - b[1], b[5] - b[2], V(*b[:3])))
        solid = Part.makeCompound([parts[m] for m in members] + plugs)
        eb = bb(solid)
        box = Part.makeBox(eb[3] - eb[0] + 4, eb[4] - eb[1] + 4, eb[5] - eb[2] + 4, V(eb[0] - 2, eb[1] - 2, eb[2] - 2))
        airx = box.cut(solid)
        inside = [s.Volume for s in airx.Solids if bb(s)[0] > eb[0] - 1.9 or bb(s)[3] < eb[3] + 1.9 or
                  bb(s)[2] > eb[2] - 1.9 or bb(s)[5] < eb[5] + 1.9]
        rec["exact"] = {"air_bodies": len(airx.Solids), "inside_volumes": sorted([round(v, 1) for v in inside], reverse=True)[:5],
                        "sealed": bool(inside) and max(inside) >= 0.5 * rec["interior_volume"]}
    except StopIteration:
        rec["exact"] = {"skipped": "several parts: their fit seams leak"}
    except Exception as e:  # noqa: BLE001
        rec["exact"] = {"error": str(e)[:200]}
    enc_out[ename] = rec
out["enclosures"] = enc_out
lap("enclosures")


# ---------------------------------------------------------------------------------------------------------------------
# joints: seams on the outer surface between case parts


E_all = np.zeros(shape3, bool)
for E in envelopes.values():
    E_all |= E
S_all = np.zeros(shape3, bool)
for m in masks.values():
    S_all |= m
E_all |= S_all
outside = ~E_all
outer_surf = S_all & ndimage.binary_dilation(outside)
smooth = ndimage.gaussian_filter(E_all.astype(float), 1.5)
egrad = np.gradient(smooth)

meshes = {}


def mesh(n):
    if n not in meshes:
        pts, tris = parts[n].tessellate(0.02)
        P = np.array([[p.x, p.y, p.z] for p in pts])
        T = np.array(tris, int)
        A, B, C = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
        meshes[n] = (A, B - A, C - A, np.minimum(np.minimum(A, B), C), np.maximum(np.maximum(A, B), C))
    return meshes[n]


def hits(n, o, d, length, normal=False):
    """Distances along the ray o + t d (0 <= t <= length) where it crosses part n's surface; with normal, also the unit
    normal of the first surface hit, facing back along the ray."""
    A, e1, e2, lo, hi = mesh(n)
    end = o + d * length
    rlo, rhi = np.minimum(o, end) - 1e-6, np.maximum(o, end) + 1e-6
    sel = np.all((lo <= rhi) & (hi >= rlo), axis=1)
    if not sel.any():
        return (np.array([]), None) if normal else np.array([])
    A, e1, e2 = A[sel], e1[sel], e2[sel]
    p = np.cross(d, e2)
    det = np.einsum("ij,ij->i", e1, p)
    ok = np.abs(det) > 1e-12
    inv = np.where(ok, 1 / np.where(ok, det, 1), 0)
    s = o - A
    u = np.einsum("ij,ij->i", s, p) * inv
    q = np.cross(s, e1)
    v = (q @ d) * inv
    t = np.einsum("ij,ij->i", e2, q) * inv
    good = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t >= 0) & (t <= length)
    if not normal:
        return np.sort(t[good])
    if not good.any():
        return np.array([]), None
    i = np.flatnonzero(good)[np.argmin(t[good])]
    nv = np.cross(e1[i], e2[i])
    nv /= np.linalg.norm(nv) or 1
    return np.sort(t[good]), (nv if nv @ d < 0 else -nv)


search = float(spec.get("joint_search", 2.0))
spacing = float(spec.get("joint_spacing", 3.0))
max_samples = int(spec.get("max_joint_samples", 120))
dist_to = {n: edt(~masks[n]) for n in case_names}
surf_dist = {n: edt(~(masks[n] & outer_surf)) for n in case_names}  # to the part's outside face


def seam_samples(a, b):
    """Points along the seam, seen from a's side: (seam voxels, samples). step > 0 where a stands proud of b."""
    # a's outside face near b's, and of that only the edge nearest b: the seam line
    a_out = outer_surf & masks[a] & (surf_dist[b] <= search)
    arr = np.where(a_out, dist_to[b], np.inf)
    seam = a_out & (arr <= ndimage.minimum_filter(arr, size=2 * int(math.ceil(search / R)) + 1) + 0.6 * R)
    cand = np.argwhere(seam)
    if not len(cand):
        return 0, []
    # spread the samples along the seam: keep a voxel only if no kept one is within `spacing`
    kept, cell = [], {}
    cs = spacing
    for v in cand[np.lexsort(cand.T[::-1])]:
        w = world(v)
        key = tuple((w // cs).astype(int))
        if any(np.linalg.norm(w - k) < cs for kk in [(key[0] + dx, key[1] + dy, key[2] + dz) for dx in (-1, 0, 1)
                                                     for dy in (-1, 0, 1) for dz in (-1, 0, 1)] for k in cell.get(kk, [])):
            continue
        cell.setdefault(key, []).append(w)
        kept.append(v)
    stride = max(1, int(math.ceil(len(kept) / max_samples)))
    samples = []
    db_grad = np.gradient(dist_to[b])
    for v in kept[::stride]:
        v = tuple(v)
        p = world(v)
        nrm = -np.array([egrad[k][v] for k in range(3)])
        if np.linalg.norm(nrm) < 1e-6:
            continue
        nrm /= np.linalg.norm(nrm)
        _, fn = hits(a, p + nrm * 2, -nrm, 4, normal=True)  # the voxels' normal is rough; take the face's
        if fn is not None and fn @ nrm > 0.5:
            nrm = fn
        t = -np.array([db_grad[k][v] for k in range(3)])
        t -= nrm * (t @ nrm)
        if np.linalg.norm(t) < 1e-6:
            continue
        t /= np.linalg.norm(t)
        c = p + t * dist_to[b][v] / 2  # roughly on the seam line
        # gap: a ray across the seam just under the surface, from inside a to inside b
        o = c - nrm * 0.25 - t * 3
        ha, hb = hits(a, o, t, 6), hits(b, o, t, 6)
        gap = None
        if len(ha) and len(hb):
            ea = ha[ha <= 3 + 1.5]
            eb = hb[hb >= (ea.max() if len(ea) else 0) - 1.5]
            if len(ea) and len(eb):
                gap = float(eb.min() - ea.max())
                lo, hi = sorted((ea.max(), eb.min()))
                if any(((h > lo + 1e-3) & (h < hi - 1e-3)).any() for h in (hits(x, o, t, 6) for x in case_names if x not in (a, b))):
                    gap = None  # another case part fills the space between them
        if gap is None:
            continue  # no clean crossing from one to the other: not on the seam
        # step: rays down onto each side of the seam, along the inward normal, at three distances from it. A real
        # step shows at all three; a hole or a fillet beside the seam only at some, so the smallest one counts.
        step = None
        for off in (0.5, 1.0, 1.5):
            half = off + (gap or 0) / 2
            sa = hits(a, c - t * half + nrm * 3, -nrm, 6)
            sb = hits(b, c + t * half + nrm * 3, -nrm, 6)
            if len(sa) and len(sb):
                st = float(sb[0] - sa[0])  # > 0: a stands proud of b
                step = st if step is None or abs(st) < abs(step) else step
        samples.append({"at": [round(float(x), 2) for x in c], "normal": [round(float(x), 2) for x in nrm],
                        "gap": round(gap, 3), "step": None if step is None else round(step, 3)})
    return int(len(cand)), samples


joints = []
for i, a in enumerate(case_names):
    for b in case_names[i + 1:]:
        if not boxes_near(bb(parts[a]), bb(parts[b]), search):
            continue
        # from both sides: where one part steps up, only the other one's face meets the seam edge-on
        na, sa = seam_samples(a, b)
        nb, sb = seam_samples(b, a)
        samples = sa + [s | {"step": None if s["step"] is None else -s["step"]} for s in sb]
        if samples:
            joints.append({"a": a, "b": b, "seam_voxels": na + nb, "samples": samples})
out["joints"] = joints
lap("joints")
out["timing"] = timing
out["grid"] = {"resolution": R, "shape": list(shape3), "origin": [round(float(v), 3) for v in origin]}

with open(os.environ["CADMATE_OUT"], "w") as f:
    json.dump(out, f)

"""Finds a clean primary and secondary route automatically (no U-turn, no tunnel), and explains every deviation."""
from concurrent.futures import ThreadPoolExecutor

from . import geo, tomtom

RING_RADII = (400, 900, 1700)
BEARINGS = range(0, 360, 45)


def n_uturns(p) -> int:
    return sum(1 for s in p["steps"] if s["uturn"])


def clean(p) -> bool:
    return n_uturns(p) == 0 and not p["tunnel"]


def score(p):
    return (n_uturns(p) + (5 if p["tunnel"] else 0), p["total_s"])


def _try(points, rt="fastest", alts=0, strict=False):
    try:
        data = tomtom.calculate(points, alternatives=alts, route_type=rt)
        return [tomtom.parse_route(r) for r in data.get("routes", [])]
    except (tomtom.TomTomError, KeyError, IndexError):
        if strict:
            raise
        return []


def _dedupe(routes):
    seen, out = set(), []
    for r in routes:
        k = (r["total_m"], r["total_s"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def similarity(p, c) -> float:
    """Share of p's distance that runs on roads also used by c."""
    roads = {r["road"] for r in c["rows"]}
    shared = sum(r["dist_m"] for r in p["rows"] if r["road"] in roads)
    return shared / max(p["total_m"], 1)


def auto_pool(o, d):
    first = _try([o, d], "fastest", 4, strict=True)
    return _dedupe(first + _try([o, d], "short", 2))


def _order(vias, geom):
    """Sort via points in travel order by their closest position on the route."""
    return sorted(vias, key=lambda v: geo.nearest(v["lat"], v["lon"], geom)[1])


def _label(parsed, v) -> str:
    _, i = geo.nearest(v["lat"], v["lon"], parsed["geom"])
    off = parsed["geom"][i][2]
    row = 0
    for s in parsed["steps"]:
        if s["off"] <= off:
            row = s["row"]
    return "near " + parsed["rows"][row]["road"]


def repair(o, d, user_vias, base):
    """Add via points on rings around each U-turn until the route has none. Returns (parsed, auto_vias, repairs)."""
    cur, auto, repairs = base, [], []
    for _ in range(3):
        bad = [s for s in cur["steps"] if s["uturn"]]
        if not bad:
            break
        spot = bad[0]
        trials = []
        for r in RING_RADII:
            for b in BEARINGS:
                la, lo = geo.destination(spot["lat"], spot["lon"], b, r)
                v = {"lat": la, "lon": lo}
                vias = _order(user_vias + auto + [v], cur["geom"])
                trials.append((v, [o] + vias + [d]))
        with ThreadPoolExecutor(6) as ex:
            results = list(ex.map(lambda t: _try(t[1]), trials))
        best = None
        for (v, _pts), res in zip(trials, results):
            if res and (best is None or score(res[0]) < score(best[1])):
                best = (v, res[0])
        if best is None or score(best[1]) >= score(cur):
            break
        v, new = best
        v["label"] = _label(new, v)
        repairs.append({"reason": f"U-turn at {spot['road']} ({spot['msg']})", "via": v["label"],
                        "extra_s": new["total_s"] - cur["total_s"], "extra_m": new["total_m"] - cur["total_m"]})
        auto.append(v)
        cur = new
    return cur, auto, repairs


def diversify(o, d, primary):
    """Force a different route by routing through points off to the side of the primary."""
    g = primary["geom"]
    total = g[-1][2] or 1
    trials = []
    for frac, radii in ((0.5, (1500, 3000)), (0.3, (2000,)), (0.7, (2000,))):
        i = min(range(len(g)), key=lambda k: abs(g[k][2] - total * frac))
        for r in radii:
            for b in BEARINGS:
                la, lo = geo.destination(g[i][0], g[i][1], b, r)
                trials.append(({"lat": la, "lon": lo}, [o, {"lat": la, "lon": lo}, d]))
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda t: _try(t[1]), trials))
    best = None
    for (v, _pts), res in zip(trials, results):
        if res and clean(res[0]) and similarity(primary, res[0]) < 0.9:
            cost = res[0]["total_s"] * (1 + similarity(primary, res[0]))
            if best is None or cost < best[0]:
                best = (cost, v, res[0])
    if not best:
        return None
    _, v, new = best
    v["label"] = _label(new, v)
    return new, v


def _route(source, user_vias, parsed, auto=None, repairs=None):
    return {"source": source, "vias": user_vias, "auto_vias": auto or [], "repairs": repairs or [],
            "data": parsed, "clean": clean(parsed)}


def plan_two(o, d, user_pv, user_sv):
    pool = None
    # primary
    if user_pv:
        base = _try([o] + user_pv + [d], strict=True)[0]
        cands = [base]
    else:
        pool = auto_pool(o, d)
        cands = pool
        base = None
    cl = sorted([c for c in cands if clean(c)], key=lambda c: c["total_s"])
    if cl:
        primary = _route("via" if user_pv else "auto", user_pv, cl[0])
    else:
        base = base or min(cands, key=score)
        fixed, auto, reps = repair(o, d, user_pv, base)
        primary = _route("repaired", user_pv, fixed, auto, reps)
    p = primary["data"]
    # secondary
    if user_sv:
        s_base = _try([o] + user_sv + [d], strict=True)[0]
        if clean(s_base):
            secondary = _route("via", user_sv, s_base)
        else:
            fixed, auto, reps = repair(o, d, user_sv, s_base)
            secondary = _route("repaired", user_sv, fixed, auto, reps)
        return primary, secondary
    pool = pool or auto_pool(o, d)
    others = [c for c in pool if (c["total_m"], c["total_s"]) != (p["total_m"], p["total_s"])]
    cl = [c for c in others if clean(c) and similarity(p, c) < 0.98]
    capped = [c for c in cl if c["total_s"] <= 1.6 * p["total_s"]] or cl
    if capped:
        best = min(capped, key=lambda c: (round(similarity(p, c), 1), c["total_s"]))
        return primary, _route("auto", [], best)
    div = diversify(o, d, p)
    if div:
        new, v = div
        return primary, _route("diversified", [], new, [v], [{"reason": "TomTom offered no clean alternative, so a different route was forced",
                                                              "via": v["label"], "extra_s": new["total_s"] - p["total_s"],
                                                              "extra_m": new["total_m"] - p["total_m"]}])
    if others:
        fixed, auto, reps = repair(o, d, [], min(others, key=score))
        return primary, _route("repaired", [], fixed, auto, reps)
    return primary, None

"""Stage 3: revised journey times using the 50 km/h cap formula, reconciled and auditable."""
import math

CAP = 50.0  # km/h planning speed
REST_AFTER_S = 3600  # driving beyond 60 minutes needs a rest stop


def items_of(d) -> tuple:
    """Finest available segments: TomTom steps, or merged road rows for runs made by older versions."""
    steps = d.get("steps") or []
    if steps and "secs" in steps[0]:
        return [{"row": s["row"], "road": s["road"], "dist_m": s["dist_m"], "secs": s["secs"], "n": s["n"]} for s in steps], "steps"
    return [{"row": i, "road": r["road"], "dist_m": r["dist_m"], "secs": r["secs"], "n": i} for i, r in enumerate(d["rows"])], "rows"


def calc(d, contingency_min: float = 0.0) -> dict:
    items, level = items_of(d)
    for it in items:
        dkm = it["dist_m"] / 1000.0
        it["dkm"] = dkm
        it["v"] = dkm / (it["secs"] / 3600.0) if it["secs"] > 0 and dkm > 0 else None
        it["note"] = ""
        if dkm == 0:
            it["delta_min"], it["rev"] = 0.0, float(it["secs"])
        elif it["secs"] <= 0:
            it["delta_min"] = 60 * dkm / CAP
            it["rev"] = dkm / CAP * 3600
            it["note"] = "TomTom gave 0 s, so 50 km/h assumed"
        elif it["v"] > CAP:
            it["delta_min"] = 60 * dkm * (1 / CAP - 1 / it["v"])  # the formula in the spec, minutes
            it["rev"] = it["secs"] + it["delta_min"] * 60
        else:
            it["delta_min"], it["rev"] = 0.0, float(it["secs"])
    rows = []
    for r_i, row in enumerate(d["rows"]):
        mine = [x for x in items if x["row"] == r_i]
        base = sum(x["secs"] for x in mine)
        rev = sum(x["rev"] for x in mine)
        dist = sum(x["dist_m"] for x in mine)
        v = (dist / 1000) / (base / 3600) if base > 0 and dist > 0 else None
        rows.append({"base": base, "rev": round(rev), "dist_m": dist, "v": v, "adj": round(rev) - base,
                     "adjusted": any(x["delta_min"] > 0 for x in mine)})
    tot_base, tot_rev, tot_dist = sum(r["base"] for r in rows), sum(r["rev"] for r in rows), sum(r["dist_m"] for r in rows)
    float_rev = sum(x["rev"] for x in items)
    checks = {
        "Distance: rows add up to TomTom's route total": abs(tot_dist - d["total_m"]) <= 5,
        "Baseline time: rows add up to TomTom's route total": abs(tot_base - d["total_s"]) <= 5,
        "Revised time: rows add up to the unrounded segment total (rounding only)": abs(tot_rev - float_rev) <= len(rows) / 2 + 1,
    }
    stops = max(0, math.ceil(tot_rev / REST_AFTER_S) - 1)
    cont_s = round(max(contingency_min, 0) * 60)
    return {"items": items, "level": level, "rows": rows, "tot_base": tot_base, "tot_rev": tot_rev, "tot_dist": tot_dist,
            "checks": checks, "ok": all(checks.values()), "stops": stops, "contingency_s": cont_s,
            "adjusted_rows": sum(1 for r in rows if r["adjusted"])}

"""Stage 4: rest stops for journeys over 60 minutes (revised driving time), with a seven point acceptance check."""
import bisect
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import geo, places, planner, stage2, stage3, store, streetview, tomtom, vision

QUERIES = ("heavy vehicle park", "lorry park", "truck parking", "commercial vehicle car park", "bus park", "open car park")
ALLOW_S = 20 * 60  # 15 min rest + 5 min entry and exit
MAX_OFF = 2500  # metres from the route
MAX_CANDS = 8
CRIT = {1: "Vehicle access", 2: "Legal access", 3: "Approach without U-turn", 4: "Space", 5: "Exit without U-turn",
        6: "Location verified", 7: "Availability"}


def ends(run):
    o, d = run["origin"], run["dest"]
    return (d, o) if run["direction"] == "return" else (o, d)


def needing(run) -> dict:
    out = {}
    for rn in stage2.routes_of(run):
        c = stage3.calc(run["routes"][rn]["data"])
        if c["stops"]:
            out[rn] = (c["stops"], c["tot_rev"])
    return out


def point_at_time(data, t_s):
    c = stage3.calc(data)
    cum, steps = 0.0, data["steps"]
    off = steps[-1]["off"]
    for it, st in zip(c["items"], steps):
        if cum + it["rev"] >= t_s:
            frac = (t_s - cum) / it["rev"] if it["rev"] else 0
            off = st["off"] + frac * st["dist_m"]
            break
        cum += it["rev"]
    g = data["geom"]
    i = min(range(len(g)), key=lambda k: abs(g[k][2] - off))
    return g[i][0], g[i][1], off


def time_at_offset(data, off) -> float:
    c = stage3.calc(data)
    cum = 0.0
    for it, st in zip(c["items"], data["steps"]):
        if st["off"] + st["dist_m"] >= off:
            frac = (off - st["off"]) / st["dist_m"] if st["dist_m"] else 0
            return cum + it["rev"] * max(min(frac, 1), 0)
        cum += it["rev"]
    return cum


def detour_rows(base, new, thresh=30):
    cell = 0.0004
    grid = {}
    for la, lo, _ in base["geom"]:
        grid.setdefault((int(la / cell), int(lo / cell)), []).append((la, lo))

    def near(la, lo):
        ci, cj = int(la / cell), int(lo / cell)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for a, b in grid.get((ci + di, cj + dj), ()):
                    if geo.hav(la, lo, a, b) <= thresh:
                        return True
        return False

    flagged = [g[2] for g in new["geom"] if not near(g[0], g[1])]
    rows = []
    for r_i, row in enumerate(new["rows"]):
        st = new["steps"][row["step"]]["off"]
        en = st + row["dist_m"]
        hits = sum(1 for o in flagged if st <= o <= en)
        if hits >= 2 and hits / max(row["dist_m"] / 20.0, 1) >= 0.3:
            rows.append(r_i)
    return rows


# ---------- candidate assessment ----------
def criteria(c) -> list:
    rt, sv = c["route"], c["sv"]
    out = []

    def add(n, status, why):
        out.append({"n": n, "name": CRIT[n], "status": status, "why": why})
    add(1, "NA", "Vehicle size is out of scope for this exercise")
    if sv.get("restriction_text"):
        add(2, "FAIL", f"Sign seen: {sv['restriction_text']}")
    else:
        add(2, "NA", "Legal access is out of scope; no restriction sign seen in the images")
    if rt.get("error"):
        add(3, "UNVERIFIED", rt["error"])
    elif rt["uturn_before"] or rt["tunnel"]:
        add(3, "FAIL", "A U-turn or tunnel is needed to reach it")
    else:
        add(3, "PASS", "TomTom reaches the stop point without a U-turn (the exact gate is not modelled)")
    sp = sv.get("space")
    if not sv.get("usable"):
        add(4, "UNVERIFIED", "No Street View coverage" if sv.get("nocov") else "No usable Street View image")
    elif sp == "small" or not sv.get("open_area"):
        add(4, "FAIL", f"Space {sp}; open area: {sv.get('open_area')}")
    elif sp == "large":
        add(4, "PASS", "Large open area seen in imagery")
    else:
        add(4, "UNVERIFIED", f"Space looks {sp}, not clearly large")
    if rt.get("error"):
        add(5, "UNVERIFIED", rt["error"])
    elif rt["uturn_after"]:
        add(5, "FAIL", "A U-turn is needed to rejoin the route")
    else:
        add(5, "PASS", "TomTom rejoins the route without a U-turn")
    if not sv.get("usable"):
        add(6, "UNVERIFIED", "Entrance and parking area not seen")
    elif not sv.get("is_parking"):
        add(6, "FAIL", "No parking area visible")
    elif not sv.get("entrance_seen"):
        add(6, "UNVERIFIED", "Parking seen but the entrance was not")
    else:
        add(6, "PASS", "Entrance and parking area seen in Street View")
    st = {x["n"]: x["status"] for x in out}
    if st[4] == "PASS" and st[6] == "PASS":
        add(7, "PASS", "Usable space seen in Street View; no live occupancy data")
    else:
        add(7, "UNVERIFIED", "Availability not established")
    return out


def verdict(crit) -> tuple:
    bad = [f"{x['n']} {x['name']}: {x['status']} ({x['why']})" for x in crit if x["status"] in ("FAIL", "UNVERIFIED")]
    return ("ACCEPTABLE" if not bad else "PROVISIONAL: SUITABILITY NOT FULLY VERIFIED"), bad


def _route_check(run, rn, p):
    base = run["routes"][rn]["data"]
    o, d = ends(run)
    vias = planner._order(run["routes"][rn].get("vias", []) + [{"lat": p["lat"], "lon": p["lon"]}], base["geom"])
    try:
        res = planner._try([o] + vias + [d], hd=run.get("start_heading"), strict=True)[0]
    except (tomtom.TomTomError, IndexError, KeyError) as e:
        return {"error": f"Could not route via this place ({str(e)[:80]})"}
    dist, gi = geo.nearest(p["lat"], p["lon"], res["geom"])
    off = res["geom"][gi][2]
    ut = [s["off"] for s in res["steps"] if s["uturn"]]
    return {"extra_s": res["total_s"] - base["total_s"], "extra_m": res["total_m"] - base["total_m"], "tunnel": res["tunnel"],
            "uturn_before": sum(1 for u in ut if u < off - 20), "uturn_after": sum(1 for u in ut if u >= off - 20), "snap_m": round(dist)}


def _sv(p):
    try:
        m = streetview.metadata(p["lat"], p["lon"])
        st = m.get("status", "")
        if st in stage2.FATAL:
            raise RuntimeError(f"Street View says {st}: {m.get('error_message', '')}")
        if st != "OK":
            return None, {"nocov": True, "usable": False}
        jp = [streetview.image(m["pano_id"], h, 100) for h in (0, 120, 240)]
        return jp, {"date": m.get("date", ""), "pano": m["pano_id"]}
    except (streetview.StreetViewError, httpx.HTTPError) as e:
        return None, {"usable": False, "error": str(e)[:100]}


def _save(run):
    with stage2.LOCK:
        run["stage4"]["beat"] = time.time()
        store.save_run(run)


def find_core(run):
    s4 = run["stage4"]
    for rn, (n, total) in needing(run).items():
        data = run["routes"][rn]["data"]
        slots = []
        for k in range(1, n + 1):
            lat, lon, _ = point_at_time(data, total * k / (n + 1))
            s4["phase"] = f"{rn} route, rest stop {k} of {n}: searching Google Places"
            _save(run)
            errs, found = [], {}
            with ThreadPoolExecutor(4) as ex:
                futs = [ex.submit(places.search, q, lat, lon) for q in QUERIES]
                for f in futs:
                    try:
                        for p in f.result():
                            found.setdefault(p["place_id"], p)
                    except places.PlacesError as e:
                        errs.append(str(e))
            if errs and not found:
                raise RuntimeError(errs[0])
            cands = []
            for p in found.values():
                dist, gi = geo.nearest(p["lat"], p["lon"], data["geom"])
                if dist <= MAX_OFF:
                    p["dist_route_m"], p["route_off"] = round(dist), data["geom"][gi][2]
                    cands.append(p)
            cands = sorted(cands, key=lambda p: p["dist_route_m"])[:MAX_CANDS]
            for i, p in enumerate(cands):
                p["id"] = f"{rn[0]}{k}_{i + 1}"
            s4["phase"] = f"{rn} route, rest stop {k}: checking routes to {len(cands)} candidates"
            _save(run)
            with ThreadPoolExecutor(6) as ex:
                checks = list(ex.map(lambda p: _route_check(run, rn, p), cands))
                svs = list(ex.map(_sv, cands))
            s4["phase"] = f"{rn} route, rest stop {k}: reading Street View"
            _save(run)
            items = [{"id": p["id"], "context": f"Candidate rest stop: {p['name']}, {p['address']}", "jpegs": jp}
                     for p, (jp, _) in zip(cands, svs) if jp]
            reads = {}
            for j in range(0, len(items), 4):
                reads.update(vision.screen_parking(items[j:j + 4]))
            for p, chk, (jp, meta) in zip(cands, checks, svs):
                p["at_s"] = round(time_at_offset(data, p["route_off"]))
                p["route"] = chk
                p["sv"] = {**meta, **reads.get(p["id"], {})} if jp else {**meta, "usable": False}
                p["criteria"] = criteria(p)
                p["verdict"], p["unresolved"] = verdict(p["criteria"])
            tgt = total * k / (n + 1)
            cands.sort(key=lambda p: (p["verdict"] != "ACCEPTABLE", sum(1 for x in p["criteria"] if x["status"] == "FAIL"),
                                      round(abs(p["at_s"] - tgt) / 300), p["route"].get("extra_s", 10 ** 6)))
            slots.append({"slot": k, "target_min": round(total * k / (n + 1) / 60), "at": [lat, lon], "cands": cands})
        s4.setdefault("slots", {})[rn] = slots
        _save(run)
    s4.update({"status": "done", "phase": "", "finished": stage2.now()})
    _save(run)


def find_job(run_id):
    run = store.load_run(run_id)
    run["stage4"] = run.get("stage4") or {"status": "running", "phase": "starting", "error": "", "beat": time.time(), "slots": {}, "choices": {}}
    try:
        find_core(run)
    except Exception as e:
        run["stage4"].update({"status": "error", "error": f"{type(e).__name__}: {e}"[:300]})
        _save(run)


def start_find(run_id) -> bool:
    run = store.load_run(run_id)
    s4 = run.get("stage4") or {}
    if s4.get("status") in ("running", "queued") and time.time() - s4.get("beat", 0) < 180:
        return False
    run["stage4"] = {"status": "queued", "phase": "starting", "error": "", "beat": time.time(), "slots": {}, "choices": {}}
    run["final"] = None
    for rn in stage2.routes_of(run):
        run["routes"][rn].pop("stopped", None)
    store.save_run(run)
    threading.Thread(target=find_job, args=(run_id,), daemon=True).start()
    return True


# ---------- choosing stops and building the route through them ----------
def cand_of(run, rn, slot, cid):
    for sl in run["stage4"]["slots"].get(rn, []):
        if sl["slot"] == slot:
            return next((c for c in sl["cands"] if c["id"] == cid), None)
    return None


def signature(run) -> str:
    ch = (run.get("stage4") or {}).get("choices", {})
    return "|".join(f"{rn}:{k}={v}" for rn in sorted(ch) for k, v in sorted(ch[rn].items()))


def apply_choices(run, rn):
    s4 = run["stage4"]
    slots = s4["slots"].get(rn, [])
    ch = s4["choices"].get(rn, {})
    run["routes"][rn].pop("stopped", None)
    if not slots or len(ch) < len(slots):
        return
    stops = [cand_of(run, rn, int(k), cid) for k, cid in sorted(ch.items(), key=lambda kv: int(kv[0]))]
    base = run["routes"][rn]["data"]
    o, d = ends(run)
    hd = run.get("start_heading")
    vias = planner._order(run["routes"][rn].get("vias", []) + [{"lat": s["lat"], "lon": s["lon"]} for s in stops], base["geom"])
    res = planner._try([o] + vias + [d], hd=hd, strict=True)[0]
    reps = []
    if not planner.clean(res):
        res, _auto, reps = planner.repair(o, d, vias, res, hd)
    rows = {}
    for s in stops:
        _, gi = geo.nearest(s["lat"], s["lon"], res["geom"])
        rows[s["id"]] = stage2.row_at_offset(res, res["geom"][gi][2])
    offs = sorted(geo.nearest(s["lat"], s["lon"], res["geom"])[1] for s in stops)
    marks = [0] + [res["geom"][i][2] for i in offs] + [res["total_m"]]
    times = [time_at_offset(res, m) for m in marks]
    longest = max(b - a for a, b in zip(times, times[1:]))
    run["routes"][rn]["stopped"] = {
        "stops": [{"cand_id": s["id"], "name": s["name"], "address": s["address"], "lat": s["lat"], "lon": s["lon"], "url": s.get("url", ""),
                   "verdict": s["verdict"], "unresolved": s["unresolved"], "criteria": s["criteria"], "sv": s["sv"], "row": rows[s["id"]]} for s in stops],
        "data": res, "repairs": reps, "extra_s": res["total_s"] - base["total_s"], "extra_m": res["total_m"] - base["total_m"],
        "detour_rows": detour_rows(base, res), "longest_s": round(longest), "clean": planner.clean(res)}


def assign_ids(run):
    n = 1
    for rn in ("primary", "secondary"):
        st = (run["routes"].get(rn) or {}).get("stopped")
        if st:
            for s in st["stops"]:
                s["rid"] = f"R{n}"
                n += 1


def choose(run, rn, slot, cid):
    s4 = run["stage4"]
    if not cand_of(run, rn, slot, cid):
        raise KeyError("candidate not found")
    s4.setdefault("choices", {}).setdefault(rn, {})[str(slot)] = cid
    run["final"] = None
    run["gates"]["4"] = "pending"
    apply_choices(run, rn)
    assign_ids(run)


def unchoose(run, rn, slot):
    run["stage4"].get("choices", {}).get(rn, {}).pop(str(slot), None)
    run["final"] = None
    run["gates"]["4"] = "pending"
    apply_choices(run, rn)
    assign_ids(run)


def all_chosen(run) -> bool:
    return all((run["routes"][rn].get("stopped") is not None) for rn in needing(run))


# ---------- final route set + re-screening of the changed route ----------
def final_view(run):
    """Returns ({rn: route data}, hazards or None if the re-screen of the stopped route is not finished)."""
    routes, stopped = {}, False
    for rn in stage2.routes_of(run):
        r = run["routes"][rn]
        routes[rn] = r["stopped"]["data"] if r.get("stopped") else r["data"]
        stopped = stopped or bool(r.get("stopped"))
    if not stopped:
        return routes, run.get("hazards", [])
    f = run.get("final")
    ok = f and f.get("sig") == signature(run) and f.get("version") == run["version"]
    return routes, (f["hazards"] if ok else None)


def finalize_job(run_id):
    run = store.load_run(run_id)
    run.setdefault("vision", {})
    vrun = dict(run)
    vrun["routes"] = {rn: {"data": d, "vias": run["routes"][rn].get("vias", [])} for rn, d in final_view(run)[0].items()}
    vrun["hazards"] = list(run.get("hazards", []))
    vrun["next_id"] = run.get("next_id", 3)
    sig = signature(run)

    def persist():
        with stage2.LOCK:
            vrun.get("screen", {})["beat"] = time.time()
            run["final_screen"] = dict(vrun.get("screen", {}))
            store.save_run(run)
    try:
        stage2.screen_core(vrun, persist)
        run["final"] = {"version": run["version"], "sig": sig, "hazards": vrun["hazards"], "built": stage2.now()}
        run["next_id"] = max(run.get("next_id", 3), vrun["next_id"])
        run["final_screen"] = dict(vrun["screen"])
    except Exception as e:
        run["final_screen"] = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300], "beat": time.time()}
    store.save_run(run)


def start_final(run_id) -> bool:
    run = store.load_run(run_id)
    fs = run.get("final_screen") or {}
    if fs.get("status") in ("running", "queued") and time.time() - fs.get("beat", 0) < 180:
        return False
    run["final_screen"] = {"status": "queued", "done": 0, "total": 0, "beat": time.time()}
    store.save_run(run)
    threading.Thread(target=finalize_job, args=(run_id,), daemon=True).start()
    return True


def gate4_ok(run) -> bool:
    return (not needing(run)) or run["gates"].get("4") == "approved"

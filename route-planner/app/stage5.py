"""Stage 5: driver reference on a frozen route version.
For each road: the lane to be in until the next manoeuvre, where to start moving over, and what tells you to turn."""
import bisect
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import geo, stage2, stage3, stage4, store, streetview, vision

POINTS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
EXPRESS = ("AYE", "PIE", "CTE", "TPE", "SLE", "BKE", "KJE", "KPE", "MCE", "ECP", "NSC")
BAD_CUE = re.compile(r"\b(trees?|plants?|grass|lamp ?posts?|poles?|fences?|kerbs?|curbs?|vehicles?|cars?|trucks?|lorry|lorries|people|pedestrians?|shadows?|sky|road markings?)\b", re.I)


def compass(h) -> str:
    return POINTS[int(((h or 0) + 22.5) // 45) % 8]


def is_expressway(road: str) -> bool:
    return "expressway" in road.lower() or any(f"({c})" in road for c in EXPRESS)


def clean_cue(text: str) -> str:
    t = re.sub(r"bus (stop|interchange|terminal)", "bus_\\1", text or "", flags=re.I)
    return "" if BAD_CUE.search(t) or re.search(r"\bbus\b", t, re.I) else (text or "").strip()


def ready_state(run) -> list:
    rid, g = run["id"], run["gates"]
    final = stage4.final_view(run)[1] is not None
    return [("Gate 1: routes chosen", g.get("1") == "approved", f"/run/{rid}"),
            ("Gate 2: danger areas reviewed", g.get("2") == "approved", f"/run/{rid}/stage2"),
            ("Gate 3: times reconciled", g.get("3") == "approved", f"/run/{rid}/stage3"),
            ("Gate 4: rest stops" + (" (not required)" if not stage4.needing(run) else ""), stage4.gate4_ok(run), f"/run/{rid}/stage4"),
            ("Danger areas re-screened for the final route", final, f"/run/{rid}/stage4")]


# ---------- the views Street View has to give us ----------
def segment_views(data, i):
    """Images looking forward along road i towards the next manoeuvre: one far, one near."""
    steps, g = data["steps"], data["geom"]
    st = steps[i]
    nxt = steps[i + 1] if i + 1 < len(steps) else None
    if not nxt or nxt["lat"] is None:
        return []
    seg = nxt["off"] - st["off"]
    views = []
    if seg >= 150:
        offs = [p[2] for p in g]
        d = min(220, seg * 0.75)
        gi = min(bisect.bisect_left(offs, nxt["off"] - d), len(g) - 2)
        j = min(gi + 3, len(g) - 1)
        views.append({"label": f"far view, about {round(d)} m before the next manoeuvre", "lat": g[gi][0], "lon": g[gi][1],
                      "heading": round(geo.bearing(g[gi][0], g[gi][1], g[j][0], g[j][1])), "kind": "far"})
    views.append({"label": "near view, about 35 m before the next manoeuvre", "lat": nxt["cam_lat"] or nxt["lat"], "lon": nxt["cam_lon"] or nxt["lon"],
                  "heading": nxt["heading"], "kind": "near"})
    return views


def _fetch_view(v):
    try:
        m = streetview.metadata(v["lat"], v["lon"])
        st = m.get("status", "")
        if st in stage2.FATAL:
            raise RuntimeError(f"Street View says {st}: {m.get('error_message', '')}")
        if st != "OK":
            return {"ok": False}
        return {"ok": True, "pano": m["pano_id"], "jpeg": streetview.image(m["pano_id"], v["heading"], 90)}
    except (streetview.StreetViewError, httpx.HTTPError):
        return {"ok": False}


# ---------- wording ----------
def lane_words(k, n) -> str:
    if n == 1:
        return "the only lane"
    pos = "leftmost" if k == 1 else ("rightmost" if k == n else "a middle lane")
    return f"lane {k} of {n} from the left ({pos})"


def write_segment(road, express, nxt, vis):
    """Plain wording from structured facts. Nothing here is invented: no evidence means 'not established'."""
    if not nxt:
        return {"lane": "n/a", "lane_status": "na", "filter_cue": "", "turn_cue": "", "instruction": "Continue to the destination.", "note": ""}
    usable = bool(vis and vis.get("usable"))
    k, n = (vis or {}).get("lane_for_next"), (vis or {}).get("lane_count")
    filt = clean_cue((vis or {}).get("filter_cue", "")) if usable else ""
    turn = clean_cue((vis or {}).get("turn_cue", "")) if usable else ""
    known = usable and k and n and 1 <= k <= n
    lane = lane_words(k, n) if known else "Not established"
    if express:
        lane += (". " if known else ". ") + "Keep to the left lane on the expressway (heavy vehicle rule) until you need to move over."
    parts = []
    if known:
        parts.append(f"Be in {lane_words(k, n)}.")
        if filt:
            parts.append(f"Start moving into it at {filt}.")
    elif filt:
        parts.append(f"Watch for {filt}.")
    parts.append(f"{nxt['msg']} at {turn}." if turn else f"{nxt['msg']}.")
    return {"lane": lane, "lane_status": "evidence" if known else "missing", "filter_cue": filt, "turn_cue": turn,
            "instruction": " ".join(parts), "note": (vis or {}).get("note", "") if usable else ""}


def build_route(run, rn, data, hazards, lab, progress):
    steps = data["steps"]
    calc = stage3.calc(data, run.get("contingency_min", 0))
    per = calc["items"] if calc["level"] == "steps" and len(calc["items"]) == len(steps) else None
    segs = {i: segment_views(data, i) for i in range(len(steps) - 1)}
    flat = [(i, v) for i, vs in segs.items() for v in vs]
    progress(f"{rn} route: fetching {len(flat)} Street View images")
    with ThreadPoolExecutor(6) as ex:
        got = list(ex.map(lambda t: _fetch_view(t[1]), flat))
    for (i, v), r in zip(flat, got):
        v.update(r)
    items = []
    for i, vs in segs.items():
        views = [{"label": v["label"], "jpeg": v["jpeg"]} for v in vs if v.get("ok")]
        if views:
            st, nxt = steps[i], steps[i + 1]
            ctx = (f"The truck drives along {st['road']} ({st.get('dist_m', 0)} m) heading {compass(geo.bearing(st['lat'], st['lon'], nxt['lat'], nxt['lon']) if st['lat'] and nxt['lat'] else 0)}. "
                   f"At the end it must: {nxt['msg']} (action: {stage2.classify(nxt)}).")
            items.append({"id": f"{rn}{i}", "context": ctx, "views": views})
    reads = {}
    batches = [items[j:j + 3] for j in range(0, len(items), 3)]
    progress(f"{rn} route: reading lanes and landmarks ({len(batches)} batches)")
    with ThreadPoolExecutor(3) as ex:
        for res in ex.map(vision.nav_screen, batches):
            reads.update(res)
    hz_by_step = {}
    for h in hazards:
        if h["id"] not in lab or rn not in h["routes"]:
            continue
        cand = [i for i, s in enumerate(steps) if s["row"] == h["routes"][rn] and s["lat"] is not None]
        if cand and h.get("lat") is not None:
            best = min(cand, key=lambda i: geo.hav(h["lat"], h["lon"], steps[i]["lat"], steps[i]["lon"]))
            d0 = stage2.best_details(h, 1)[0] if h.get("details") else {}
            hz_by_step.setdefault(best, []).append({"label": lab[h["id"]], "what": d0.get("what", "")[:120], "precaution": d0.get("precaution", "")[:120]})
    out = []
    for i, st in enumerate(steps):
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        head = geo.bearing(st["lat"], st["lon"], nxt["lat"], nxt["lon"]) if nxt and st["lat"] is not None and nxt["lat"] is not None else None
        w = write_segment(st["road"], is_expressway(st["road"]), nxt, reads.get(f"{rn}{i}"))
        links = [{"label": v["kind"] + " view", "url": geo.sv_link(v["lat"], v["lon"], v["heading"], v.get("pano"))} for v in segs.get(i, [])]
        out.append({"n": i + 1, "road": st["road"], "direction": compass(head) if head is not None else "", "distance_m": st.get("dist_m"),
                    "revised_s": round(per[i]["rev"]) if per else None, "join": st["msg"], "next_action": nxt["msg"] if nxt else "",
                    "hazards": hz_by_step.get(i, []), "links": links, "row": st["row"], "uturn": st["uturn"], "express": is_expressway(st["road"]), **w})
    stp = run["routes"][rn].get("stopped")
    stops = []
    if stp:
        for s in stp["stops"]:
            sv = s["sv"]
            stops.append({"rid": s.get("rid", "R?"), "name": s["name"], "address": s["address"], "row": s["row"], "verdict": s["verdict"], "unresolved": s["unresolved"],
                          "approach": [data["rows"][i]["road"] for i in stp["detour_rows"] if i <= s["row"]],
                          "exit": [data["rows"][i]["road"] for i in stp["detour_rows"] if i > s["row"]],
                          "parking": f"{sv.get('kind', '?')}, space {sv.get('space', '?')}; {sv.get('notes', '')}".strip(),
                          "link": geo.sv_link(s["lat"], s["lon"], 0, sv.get("pano"))})
    return {"steps": out, "stops": stops, "tot_m": calc["tot_dist"], "tot_rev": calc["tot_rev"], "tot_base": calc["tot_base"],
            "rest_s": len(stops) * stage4.ALLOW_S, "contingency_s": calc["contingency_s"]}


def unresolved(run, routes, pend) -> list:
    out = []
    for rn, r in routes.items():
        segs = [s for s in r["steps"] if s["lane_status"] != "na"]
        miss = sum(1 for s in segs if s["lane_status"] == "missing")
        if miss:
            out.append(f"{rn}: lane not established on {miss} of {len(segs)} roads. Use the view links to check, or confirm on site.")
        nocue = sum(1 for s in segs if not s["turn_cue"])
        if nocue:
            out.append(f"{rn}: no clear landmark found for the turn on {nocue} of {len(segs)} roads.")
        for s in r["stops"]:
            if s["verdict"] != "ACCEPTABLE":
                out.append(f"{rn}: rest stop {s['rid']} is PROVISIONAL: " + "; ".join(s["unresolved"])[:300])
    if pend:
        out.append("Danger areas still pending your review: " + ", ".join(pend))
    return out


def job(run_id):
    run = store.load_run(run_id)
    ds = run["driver_status"]

    def progress(text):
        ds["phase"], ds["beat"] = text, time.time()
        with stage2.LOCK:
            store.save_run(run)
    try:
        routes_data, hazards = stage4.final_view(run)
        lab = stage2.labels(hazards, routes_data)
        out = {rn: build_route(run, rn, data, hazards, lab, progress) for rn, data in routes_data.items()}
        pend = [lab[h["id"]] for h in hazards if h["id"] in lab and h.get("review") == "pending"]
        run["frozen"] = {"version": run["version"], "at": stage2.now(), "routes": out, "unresolved": unresolved(run, out, pend)}
        ds.update({"status": "done", "phase": ""})
    except Exception as e:
        ds.update({"status": "error", "error": f"{type(e).__name__}: {e}"[:300]})
    ds["beat"] = time.time()
    store.save_run(run)


def start(run_id) -> bool:
    run = store.load_run(run_id)
    ds = run.get("driver_status") or {}
    if ds.get("status") in ("running", "queued") and time.time() - ds.get("beat", 0) < 240:
        return False
    run["driver_status"] = {"status": "queued", "phase": "starting", "error": "", "beat": time.time()}
    run["frozen"] = None
    run["gates"]["5"] = "pending"
    store.save_run(run)
    threading.Thread(target=job, args=(run_id,), daemon=True).start()
    return True

"""Stage 5: driver reference on a frozen route version.
Lane numbers count from the RIGHT: lane 1 is the rightmost lane and the highest number is the leftmost lane.
The truck keeps to the leftmost lane unless a turn needs another lane or the leftmost lane turns off ahead (then: SEEK DEVIATION)."""
import bisect
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import geo, overlay, stage2, stage3, stage4, store, streetview, vision

POINTS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
EXPRESS = ("AYE", "PIE", "CTE", "TPE", "SLE", "BKE", "KJE", "KPE", "MCE", "ECP", "NSC")
SPACINGS = (200, 300, 500)
SEEK = "SEEK DEVIATION"
BAD_CUE = re.compile(r"\b(trees?|plants?|grass|lamp ?posts?|poles?|fences?|kerbs?|curbs?|vehicles?|cars?|trucks?|lorry|lorries|people|pedestrians?|shadows?|sky|road markings?)\b", re.I)


def compass(h) -> str:
    return POINTS[int(((h or 0) + 22.5) // 45) % 8]


def is_expressway(road: str) -> bool:
    return "expressway" in road.lower() or any(f"({c})" in road for c in EXPRESS)


def clean_cue(text: str) -> str:
    t = re.sub(r"bus (stop|interchange|terminal)", "bus_\\1", text or "", flags=re.I)
    return "" if BAD_CUE.search(t) or re.search(r"\bbus\b", t, re.I) or vision.is_live_condition(text) else (text or "").strip()


def ready_state(run) -> list:
    rid, g = run["id"], run["gates"]
    final = stage4.final_view(run)[1] is not None
    return [("Gate 1: routes chosen", g.get("1") == "approved", f"/run/{rid}"),
            ("Gate 2: danger areas reviewed", g.get("2") == "approved", f"/run/{rid}/stage2"),
            ("Gate 3: times reconciled", g.get("3") == "approved", f"/run/{rid}/stage3"),
            ("Gate 4: rest stops" + (" (not required)" if not stage4.needing(run) else ""), stage4.gate4_ok(run), f"/run/{rid}/stage4"),
            ("Danger areas re-screened for the final route", final, f"/run/{rid}/stage4")]


# ---------- which photos to take ----------
cam_at = overlay.cam_at


def views_for_segment(data, i, spacing):
    """Photos for road i: one just after joining, reference photos along it, then far and near photos before the next manoeuvre."""
    steps = data["steps"]
    st = steps[i]
    nxt = steps[i + 1] if i + 1 < len(steps) else None
    s0 = st["off"]
    e = nxt["off"] if nxt else data["total_m"]
    seg = e - s0
    out = []

    def add(kind, off, label):
        la, lo, h = cam_at(data, off)
        out.append({"kind": kind, "off": off, "at_m": max(round(off - s0), 0), "to_next": max(round(e - off), 0), "label": label, "lat": la, "lon": lo, "heading": h})
    if seg >= 120 and i > 0:
        add("start", s0 + 40, "start view, just after joining this road")
    if nxt and seg > 700:
        t = s0 + 40 + spacing
        while t < e - 300:
            add("ref", t, "reference view along the road")
            t += spacing
    if nxt:
        if seg >= 150:
            add("far", e - min(220, seg * 0.75), "far view, before the next manoeuvre")
        out.append({"kind": "near", "off": e - 35, "at_m": max(round(e - 35 - s0), 0), "to_next": 35, "label": "near view, about 35 m before the next manoeuvre",
                    "lat": nxt["cam_lat"] or nxt["lat"], "lon": nxt["cam_lon"] or nxt["lon"], "heading": nxt["heading"], "target": (nxt["lat"], nxt["lon"])})
    return out


def _fetch_view(v, data):
    """Try the exact spot, then nearby spots along the route. Records why a photo is missing instead of hiding it."""
    tries = [(v["lat"], v["lon"], v["heading"])]
    for d in ((-30, 30, -60, 60) if v["kind"] != "near" else (-25, -50, 15)):
        tries.append(cam_at(data, v["off"] + d))
    last = "nocov"
    for la, lo, h in tries:
        try:
            m = streetview.metadata(la, lo)
        except (streetview.StreetViewError, httpx.HTTPError):
            last = "error"
            continue
        st = m.get("status", "")
        if st in stage2.FATAL:
            raise RuntimeError(f"Street View says {st}: {m.get('error_message', '')}")
        if st != "OK":
            continue
        loc = m.get("location") or {}
        cam = (loc.get("lat", la), loc.get("lng", lo))
        head = h
        if v.get("target") and geo.hav(cam[0], cam[1], v["target"][0], v["target"][1]) >= 8:
            head = round(geo.bearing(cam[0], cam[1], v["target"][0], v["target"][1]))
        try:
            jpg = streetview.image(m["pano_id"], head, 90)
        except (streetview.StreetViewError, httpx.HTTPError):
            last = "error"
            continue
        jpg = overlay.draw_path(jpg, cam, head, overlay.route_ahead(data, cam[0], cam[1], 230))
        return {"status": "ok", "pano": m["pano_id"], "jpeg": jpg, "cam": cam, "heading": head}
    return {"status": last}


# ---------- turning readings into lane advice ----------
def side_needed(nxt) -> str:
    """Which way the truck must move for the next manoeuvre: left, right or none."""
    if not nxt:
        return ""
    man, cls = nxt["maneuver"].upper(), stage2.classify(nxt)
    if cls == "u-turn":
        return "right"
    if "RIGHT" in man and (cls != "continue straight" or man.startswith(("KEEP", "BEAR"))):
        return "right"
    if "LEFT" in man and (cls != "continue straight" or man.startswith(("KEEP", "BEAR"))):
        return "left"
    return ""


def summarise(st, nxt, express, pairs, seg_len):
    usable = [(v, r) for v, r in pairs if r and r["usable"]]
    cnt = Counter(r["lane_count"] for _, r in usable if r["lane_count"]).most_common(1)
    M = cnt[0][0] if cnt else None
    side, cls = side_needed(nxt), stage2.classify(nxt) if nxt else ""
    k_left = None
    for kind in ("near", "far"):
        for v, r in usable:
            if v["kind"] == kind and r["lane_for_next"] and not k_left:
                k_left = r["lane_for_next"]
    taken = side == "left" and cls == "expressway exit or entry"
    dev = [(v, r) for v, r in usable if r["leftmost_lane_exits"] and not (taken and v["to_next"] <= 450)]
    seek, status = False, "rule"
    if not nxt:
        lane, status = "n/a", "na"
    elif side == "right":
        if k_left and M:
            lane, status = f"Move to lane {M - k_left + 1} of {M} before the turn (lane {M} is the leftmost; counted from the right).", "evidence"
        else:
            lane = "Move to the rightmost lane, or the right turn lane, before the turn."
    else:
        base = f"Stay in the leftmost lane (lane {M} of {M})." if M else "Stay in the leftmost lane."
        status = "evidence" if M else "rule"
        if dev:
            seek = True
            where = dev[0][1]["exit_ahead"]
            devlane = f"lane {M - 1} of {M}" if M and M >= 2 else "the second lane from the left"
            lane = f"{SEEK}: the leftmost lane turns off ahead{(' (' + where + ')') if where else ''}. Be in {devlane} (second from the left) to avoid turning off."
        else:
            lane = base
    if express and nxt and not seek and side != "right":
        lane += " Heavy vehicles keep left on expressways."
    filt = next((clean_cue(r["filter_cue"]) for v, r in usable if v["kind"] == "far" and clean_cue(r["filter_cue"])), "")
    turn = next((clean_cue(r["turn_cue"]) for v, r in usable if v["kind"] == "near" and clean_cue(r["turn_cue"])), "")
    over = next((r["over"] for _, r in usable if r["over"]), "")
    note = next((r["note"] for _, r in usable if r["note"]), "")
    parts = []
    if over:
        parts.append(over[0].upper() + over[1:] + ".")
    if nxt:
        if filt:
            parts.append(f"Start moving over at {filt}." if side == "right" else f"Watch for {filt}.")
        parts.append(f"{nxt['msg']} at {turn}." if turn else f"{nxt['msg']}.")
    else:
        parts.append("Continue to the destination.")
    # reference points along the road: exits to ignore, and landmarks to keep your place
    refs, last_at, seen = [], -10 ** 9, []
    for v, r in sorted(usable, key=lambda t: t[0]["at_m"]):
        if v["kind"] == "near":
            continue
        if r["exit_ahead"] and not any(r["exit_ahead"][:18].lower() == s[0][:18].lower() and abs(v["at_m"] - s[1]) < 450 for s in seen):
            seen.append((r["exit_ahead"], v["at_m"]))
            flag = bool(r["leftmost_lane_exits"]) and not (taken and v["to_next"] <= 450)
            devlane = f"lane {M - 1} of {M}" if M and M >= 2 else "the second lane from the left"
            text = (f"{r['exit_ahead']}. {SEEK}: the leftmost lane turns off here. Be in {devlane} so you do not turn off." if flag else
                    f"{r['exit_ahead']}. Do not take it: stay in the leftmost lane{(' (lane ' + str(M) + ' of ' + str(M) + ')') if M else ''}.")
            refs.append({"at_m": v["at_m"], "text": text, "seek": flag, "kind": "exit"})
            last_at = v["at_m"]
        elif seg_len >= 700 and r["landmark"] and clean_cue(r["landmark"]) and v["at_m"] - last_at >= 800:
            refs.append({"at_m": v["at_m"], "text": f"Checkpoint: {clean_cue(r['landmark'])}. Stay in the leftmost lane.", "seek": False, "kind": "checkpoint"})
            last_at = v["at_m"]
    return {"lane": lane, "lane_status": status, "seek": seek or any(x["seek"] for x in refs), "filter_cue": filt, "turn_cue": turn,
            "instruction": " ".join(parts), "note": note, "refs": refs}


def build_route(run, rn, data, hazards, lab, progress, spacing):
    steps = data["steps"]
    calc = stage3.calc(data, run.get("contingency_min", 0))
    per = calc["items"] if calc["level"] == "steps" and len(calc["items"]) == len(steps) else None
    segs = {i: views_for_segment(data, i, spacing) for i in range(len(steps))}
    flat = [(i, k, v) for i, vs in segs.items() for k, v in enumerate(vs)]
    progress(f"{rn} route: fetching {len(flat)} Street View images")
    with ThreadPoolExecutor(6) as ex:
        got = list(ex.map(lambda t: _fetch_view(t[2], data), flat))
    stats = Counter(g["status"] for g in got)
    items = []
    for (i, k, v), g in zip(flat, got):
        v.update(g)
        if g["status"] == "ok":
            st, nxt = steps[i], steps[i + 1] if i + 1 < len(steps) else None
            ctx = (f"The truck is on {st['road']}. This is the {v['label']}, {v['at_m']} m after the start of this road and {v['to_next']} m before the next manoeuvre. " +
                   (f"Next manoeuvre: {nxt['msg']} (action: {stage2.classify(nxt)}). " if nxt else "This is the last road. ") + "The yellow line is the truck's path.")
            items.append({"id": f"{i}|{k}", "context": ctx, "views": [{"label": v["kind"] + " view", "jpeg": g["jpeg"]}]})
    batches = [items[j:j + 6] for j in range(0, len(items), 6)]
    progress(f"{rn} route: reading {len(items)} photos ({len(batches)} batches)")
    reads = {}
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
    out, no_photo = [], []
    for i, st in enumerate(steps):
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        head = geo.bearing(st["lat"], st["lon"], nxt["lat"], nxt["lon"]) if nxt and st["lat"] is not None and nxt["lat"] is not None else None
        pairs = [(v, reads.get(f"{i}|{k}")) for k, v in enumerate(segs[i])]
        seg_len = (nxt["off"] if nxt else data["total_m"]) - st["off"]
        w = summarise(st, nxt, is_expressway(st["road"]), pairs, seg_len)
        n_ok = sum(1 for v, _ in pairs if v.get("status") == "ok")
        n_use = sum(1 for _, r in pairs if r and r["usable"])
        if pairs and n_use == 0:
            no_photo.append(st["road"])
        links = [{"label": f"{v['kind']} view", "url": geo.sv_link(v["cam"][0], v["cam"][1], v["heading"], v.get("pano"))} for v in segs[i] if v.get("status") == "ok"]
        for ref in w["refs"]:
            vv = min((v for v in segs[i] if v.get("status") == "ok"), key=lambda v: abs(v["at_m"] - ref["at_m"]), default=None)
            ref["link"] = geo.sv_link(vv["cam"][0], vv["cam"][1], vv["heading"], vv.get("pano")) if vv else ""
        out.append({"n": i + 1, "road": st["road"], "direction": compass(head) if head is not None else "", "distance_m": st.get("dist_m"),
                    "revised_s": round(per[i]["rev"]) if per else None, "join": st["msg"], "next_action": nxt["msg"] if nxt else "",
                    "hazards": hz_by_step.get(i, []), "links": links, "row": st["row"], "uturn": st["uturn"], "express": is_expressway(st["road"]),
                    "photos": {"asked": len(pairs), "found": n_ok, "usable": n_use}, **w})
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
    diag = {"asked": len(flat), "ok": stats.get("ok", 0), "nocov": stats.get("nocov", 0), "error": stats.get("error", 0),
            "usable": sum(1 for r in reads.values() if r["usable"]), "no_photo_roads": no_photo[:10]}
    return {"steps": out, "stops": stops, "tot_m": calc["tot_dist"], "tot_rev": calc["tot_rev"], "tot_base": calc["tot_base"],
            "rest_s": len(stops) * stage4.ALLOW_S, "contingency_s": calc["contingency_s"], "diag": diag}


def unresolved(run, routes, pend) -> list:
    out = []
    for rn, r in routes.items():
        d = r["diag"]
        out.append(f"{rn}: {d['asked']} photos asked for, {d['ok']} found ({d['nocov']} places have no Street View, {d['error']} failed to load), {d['usable']} readable.")
        segs = [s for s in r["steps"] if s["lane_status"] != "na"]
        rule = sum(1 for s in segs if s["lane_status"] == "rule")
        if rule:
            out.append(f"{rn}: on {rule} of {len(segs)} roads the lane comes from the leftmost lane rule only, because no readable photo showed the lanes.")
        seek = sum(1 for s in r["steps"] if s["seek"])
        if seek:
            out.append(f"{rn}: {seek} road(s) are flagged {SEEK}. Check each one.")
        for s in r["stops"]:
            if s["verdict"] != "ACCEPTABLE":
                out.append(f"{rn}: rest stop {s['rid']} is PROVISIONAL: " + "; ".join(s["unresolved"])[:300])
    if pend:
        out.append("Danger areas still pending your review: " + ", ".join(pend))
    return out


def job(run_id):
    run = store.load_run(run_id)
    ds = run["driver_status"]
    spacing = ds.get("spacing", 300)

    def progress(text):
        ds["phase"], ds["beat"] = text, time.time()
        with stage2.LOCK:
            store.save_run(run)
    try:
        routes_data, hazards = stage4.final_view(run)
        lab = stage2.labels(hazards, routes_data)
        out = {rn: build_route(run, rn, data, hazards, lab, progress, spacing) for rn, data in routes_data.items()}
        pend = [lab[h["id"]] for h in hazards if h["id"] in lab and h.get("review") == "pending"]
        run["frozen"] = {"version": run["version"], "at": stage2.now(), "spacing": spacing, "routes": out, "unresolved": unresolved(run, out, pend)}
        ds.update({"status": "done", "phase": ""})
    except Exception as e:
        ds.update({"status": "error", "error": f"{type(e).__name__}: {e}"[:300]})
    ds["beat"] = time.time()
    store.save_run(run)


def start(run_id, spacing=300) -> bool:
    run = store.load_run(run_id)
    ds = run.get("driver_status") or {}
    if ds.get("status") in ("running", "queued") and time.time() - ds.get("beat", 0) < 240:
        return False
    run["driver_status"] = {"status": "queued", "phase": "starting", "error": "", "beat": time.time(), "spacing": spacing if spacing in SPACINGS else 300}
    run["frozen"] = None
    run["gates"]["5"] = "pending"
    store.save_run(run)
    threading.Thread(target=job, args=(run_id,), daemon=True).start()
    return True

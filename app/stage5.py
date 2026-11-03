"""Stage 5: exhaustive driver reference built on a frozen route version. Lane advice only where evidence exists."""
import json
import re
import threading
import time

from . import geo, stage2, stage3, stage4, store, vision

POINTS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]

SYSTEM = """You write driver instructions for a route in Singapore, one per step. Use ONLY the facts supplied for each step.
Never invent landmarks, lane numbers, road names, distances, speed limits, clearances, signs or restrictions.
Return ONLY a JSON array with one object per step, in the same order, keys:
n (the step number given), instruction (max 40 words: if landmarks are supplied start with one, e.g. "Past the grey building on the left,", then the action using the supplied road names, and the direction of travel; do not mention lanes),
lane (max 30 words, or "" if no lane evidence is supplied: say which lane to be in ONLY using the supplied lane evidence and name its source, copying any lane numbers or positions exactly from it),
uncertainty (max 20 words: what is not verified, e.g. imagery date; "" if nothing)."""

FORBID = re.compile(r"\b(lane|leftmost|rightmost|middle lane|centre lane|center lane)\b", re.I)


def compass(h) -> str:
    return POINTS[int(((h or 0) + 22.5) // 45) % 8]


def tomtom_lane_text(l) -> str:
    if not l:
        return ""
    dirs = "; ".join(f"lane {k} {','.join(l['dirs'][k - 1]).lower()}" for k in l["follow"] if 0 < k <= len(l["dirs"]) and l["dirs"][k - 1])
    return f"TomTom lane data: {l['count']} lanes, lanes to follow: {l['follow'] or 'not given'} ({dirs or 'directions not given'}); order as listed by TomTom, unverified"


def ready_state(run) -> list:
    """List of (label, ok, link) for the preconditions."""
    rid = run["id"]
    g = run["gates"]
    final = stage4.final_view(run)[1] is not None
    return [("Gate 1: routes chosen", g.get("1") == "approved", f"/run/{rid}"),
            ("Gate 2: danger areas reviewed", g.get("2") == "approved", f"/run/{rid}/stage2"),
            ("Gate 3: times reconciled", g.get("3") == "approved", f"/run/{rid}/stage3"),
            ("Gate 4: rest stops" + (" (not required)" if not stage4.needing(run) else ""), stage4.gate4_ok(run), f"/run/{rid}/stage4"),
            ("Danger areas re-screened for the final route", final, f"/run/{rid}/stage4")]


def evidence(run, rn, data, hazards):
    c = stage3.calc(data, run.get("contingency_min", 0))
    steps = data["steps"]
    per_step = c["items"] if c["level"] == "steps" and len(c["items"]) == len(steps) else None
    vis = run.get("vision", {})
    hz_by_step = {}
    for h in hazards:
        if h.get("review") == "rejected" or not h.get("applicable", True) or rn not in h["routes"]:
            continue
        row = h["routes"][rn]
        cand = [i for i, s in enumerate(steps) if s["row"] == row and s["lat"] is not None]
        if cand and h.get("lat") is not None:
            best = min(cand, key=lambda i: geo.hav(h["lat"], h["lon"], steps[i]["lat"], steps[i]["lon"]))
            d0 = h["details"][0] if h.get("details") else {}
            hz_by_step.setdefault(best, []).append({"id": h["id"], "what": d0.get("what", "")[:120], "precaution": d0.get("precaution", "")[:120]})
    ev = []
    for i, st in enumerate(steps):
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        head = geo.bearing(st["lat"], st["lon"], nxt["lat"], nxt["lon"]) if nxt and st["lat"] is not None and nxt["lat"] is not None else None
        v = vis.get(stage2.key_of(st["cam_lat"] or st["lat"], st["cam_lon"] or st["lon"], st["heading"])) or {} if st["lat"] is not None else {}
        usable = bool(v.get("usable"))
        oh = [f"{o['what']}: {o['clearance_m']} m posted" if o.get("clearance_m") is not None else f"{o['what']}: clearance not visible" for o in v.get("overhead", [])] if usable else []
        ev.append({"n": i + 1, "road": st["road"], "direction": compass(head) if head is not None else "", "distance_m": st["dist_m"] if "dist_m" in st else None,
                   "revised_s": round(per_step[i]["rev"]) if per_step else None, "action": st["msg"], "maneuver": st["maneuver"],
                   "landmarks": v.get("landmarks", []) if usable else [], "imagery_date": v.get("date", "") if usable else "",
                   "lane_evidence_streetview": v.get("lanes", "") if usable and v.get("lanes", "").lower() not in ("", "not visible") else "",
                   "lane_evidence_tomtom": tomtom_lane_text(st.get("lanes")), "overhead": oh,
                   "hazards": hz_by_step.get(i, []), "lat": st["lat"], "lon": st["lon"], "cam_lat": st["cam_lat"], "cam_lon": st["cam_lon"], "heading": st["heading"],
                   "row": st["row"], "uturn": st["uturn"]})
    return ev, c


def fallback_instruction(e) -> str:
    d = f", heading {e['direction']}" if e["direction"] else ""
    return f"{e['action']}{d}."


def finish_step(e, out):
    lane_src = "; ".join(x for x in (e["lane_evidence_streetview"], e["lane_evidence_tomtom"]) if x)
    inst = str(out.get("instruction") or "").strip() or fallback_instruction(e)
    own = (e["action"] + " " + e["road"]).lower()
    if not lane_src and FORBID.search(inst) and not any(w.lower() in own for w in FORBID.findall(inst)):
        inst = fallback_instruction(e)  # the model mentioned lanes without evidence
    lane = str(out.get("lane") or "").strip()
    if not lane_src:
        lane, status = "Not established", "missing"
    else:
        toks = re.findall(r"\d+|first|second|third|fourth|fifth|left|right|middle|centre|center", lane.lower())
        if not lane or any(t not in lane_src.lower() for t in toks):
            lane = "Evidence only: " + lane_src
        status = "evidence"
    link = geo.sv_link(e["cam_lat"] or e["lat"], e["cam_lon"] or e["lon"], e["heading"]) if e["lat"] is not None else ""
    return {**{k: e[k] for k in ("n", "road", "direction", "distance_m", "revised_s", "action", "landmarks", "imagery_date", "overhead", "hazards", "row", "uturn")},
            "instruction": inst, "lane": lane, "lane_status": status, "view": link, "uncertainty": str(out.get("uncertainty") or "").strip()}


def build_route(run, rn, data, hazards):
    ev, calc = evidence(run, rn, data, hazards)
    payload = [{k: e[k] for k in ("n", "road", "direction", "distance_m", "action", "landmarks", "imagery_date", "lane_evidence_streetview", "lane_evidence_tomtom", "hazards")} for e in ev]
    got = {}
    for o in vision.compose(SYSTEM, "Steps (JSON):\n" + json.dumps(payload)):
        if isinstance(o, dict) and isinstance(o.get("n"), int):
            got[o["n"]] = o
    steps = [finish_step(e, got.get(e["n"], {})) for e in ev]
    st = run["routes"][rn].get("stopped")
    stops = []
    if st:
        for s in st["stops"]:
            ing = [data["rows"][i]["road"] for i in st["detour_rows"] if i <= s["row"]]
            egr = [data["rows"][i]["road"] for i in st["detour_rows"] if i > s["row"]]
            sv = s["sv"]
            stops.append({"rid": s.get("rid", "R?"), "name": s["name"], "address": s["address"], "lat": s["lat"], "lon": s["lon"], "row": s["row"],
                          "verdict": s["verdict"], "unresolved": s["unresolved"], "approach": ing, "exit": egr,
                          "parking": f"{sv.get('kind', '?')}, space {sv.get('space', '?')}; {sv.get('notes', '')}".strip(), "imagery": sv.get("date", "")})
    return {"steps": steps, "stops": stops, "tot_m": calc["tot_dist"], "tot_rev": calc["tot_rev"], "tot_base": calc["tot_base"],
            "rest_s": len(stops) * stage4.ALLOW_S, "contingency_s": calc["contingency_s"]}


def unresolved(run, routes) -> list:
    out = []
    for rn, r in routes.items():
        miss = sum(1 for s in r["steps"] if s["lane_status"] == "missing")
        if miss:
            out.append(f"{rn}: lane position not established at {miss} of {len(r['steps'])} steps (open the view link and send the screenshot, or confirm on site).")
        for s in r["stops"]:
            if s["verdict"] != "ACCEPTABLE":
                out.append(f"{rn}: rest stop {s['rid']} is PROVISIONAL: " + "; ".join(s["unresolved"])[:300])
    pend = [h["id"] for h in (stage4.final_view(run)[1] or []) if h.get("review") == "pending" and h.get("applicable", True)]
    if pend:
        out.append("Hazards still pending your review: " + ", ".join(pend))
    return out


def job(run_id):
    run = store.load_run(run_id)
    ds = run["driver_status"]
    try:
        routes_data, hazards = stage4.final_view(run)
        out = {}
        for rn, data in routes_data.items():
            ds["phase"] = f"writing the {rn} route"
            with stage2.LOCK:
                store.save_run(run)
            out[rn] = build_route(run, rn, data, hazards)
        run["frozen"] = {"version": run["version"], "at": stage2.now(), "routes": out, "unresolved": unresolved(run, out)}
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

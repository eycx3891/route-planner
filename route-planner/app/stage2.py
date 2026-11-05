"""Stage 2: screen every segment of both routes for danger areas (hazard IDs D1, D2, ...)."""
import bisect
import datetime
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import geo, incidents, store, streetview, tomtom, vision

LOCK = threading.Lock()
RADIUS = 100  # metres: a route "passes" a designated location if within this distance
TRUCK_HEIGHT_M = 4.0  # overhead bridges with posted clearance at or below height + margin are flagged
MARGIN_M = 0.3
FATAL = {"REQUEST_DENIED", "OVER_QUERY_LIMIT", "INVALID_REQUEST", "UNKNOWN_ERROR"}
MID_EVERY = 3000  # screen long segments every 3 km
CLUSTER = 40  # metres: observations closer than this become one hazard

SETTINGS = "settings/operator.json"  # designated positions you confirmed once, reused by every run
STRICT = ("continue straight", "set off", "arrive")  # actions where side features rarely matter

MANDATORY = [
    {"id": "D1", "name": "Sembawang Road and Yishun Avenue 1 junction",
     "query": "Sembawang Road and Yishun Avenue 1 junction, Singapore", "cats": [11, 4],
     "what": "Two traffic lights close together. The driver may turn off into Sembawang Air Base instead of continuing towards Mandai.",
     "risk": "Wrong turn into Sembawang Air Base",
     "precaution": "Do not turn into the air base. Stay on the road towards Mandai."},
    {"id": "D2", "name": "Outside Chong Pang Camp", "query": "Chong Pang Camp, Singapore", "cats": [11, 10],
     "what": "Garbage vehicles may come out of the camp entrance in front of you.",
     "risk": "Vehicle emerging from the camp entrance",
     "precaution": "Slow down and watch the entrance as you pass."},
]


def classify(s) -> str:
    """What the truck does at a TomTom step, in words the image reader understands."""
    man = str(s.get("maneuver") or "").upper()
    if s.get("uturn"):
        return "u-turn"
    if "ROUNDABOUT" in man or str(s.get("junction", "")).upper().startswith("ROUNDABOUT"):
        return "roundabout"
    if any(w in man for w in ("EXIT", "RAMP", "ENTER", "MERGE", "MOTORWAY", "HIGHWAY", "FREEWAY")):
        return "expressway exit or entry"
    if "LEFT" in man and ("TURN" in man or "SHARP" in man):
        return "turn left"
    if "RIGHT" in man and ("TURN" in man or "SHARP" in man):
        return "turn right"
    if "DEPART" in man:
        return "set off"
    if "ARRIVE" in man:
        return "arrive"
    return "continue straight"


def truck_action(data, lat, lon):
    """(text, class) saying what the truck is doing at a point on a route."""
    g = data["geom"]
    _, gi = geo.nearest(lat, lon, g)
    off = g[gi][2]
    cur = nxt = None
    for st in data["steps"]:
        if st["off"] <= off:
            cur = st
        elif nxt is None:
            nxt = st
    if nxt and nxt["off"] - off <= 120 and classify(nxt) != "arrive":
        return f"Approaching: {nxt['msg']}", classify(nxt)
    if cur and off - cur["off"] <= 60:
        return cur["msg"], classify(cur)
    return f"Continuing straight along {cur['road'] if cur else 'the road'}", "continue straight"


def labels(hazards, routes) -> dict:
    """Display numbers D1, D2... for the hazards you have not rejected, in the order the truck meets them."""
    live = [h for h in hazards if h.get("review") != "rejected" and h.get("applicable", True) and h.get("lat") is not None and h.get("routes")]

    def key(h):
        rn = "primary" if "primary" in h["routes"] and "primary" in routes else next(iter(h["routes"]))
        d = routes.get(rn)
        return (0 if rn == "primary" else 1, geo.nearest(h["lat"], h["lon"], d["geom"])[1] if d else 0)
    return {h["id"]: f"D{i}" for i, h in enumerate(sorted(live, key=key), 1)}


def best_details(h, n=2) -> list:
    ranked = sorted(h["details"], key=lambda d: (d.get("src") == "operator", d.get("sev", 2), d.get("src") == "vision"), reverse=True)
    out, seen = [], set()
    for d in ranked:
        if d.get("cat") in seen:
            continue
        seen.add(d.get("cat"))
        out.append(d)
        if len(out) >= n:
            break
    return out


def now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def routes_of(run) -> list:
    return [n for n in ("primary", "secondary") if run["routes"].get(n)]


def ready(run) -> bool:
    return all("steps" in run["routes"][n]["data"] for n in routes_of(run))


def row_at_offset(d, off) -> int:
    row = 0
    for s in d["steps"]:
        if s["off"] <= off:
            row = s["row"]
    return row


# ---------- operator designated locations ----------
def remember_mandatory(mid, loc):
    """Keep a confirmed position so later runs do not ask again."""
    saved = store.load_json(SETTINGS) or {}
    saved.setdefault("mandatory", {})[mid] = {k: loc.get(k) for k in ("lat", "lon", "label", "source")} | {"confirmed": True}
    store.save_json(SETTINGS, saved)


def locate_mandatory(run):
    mand = run.setdefault("mandatory", {})
    saved = (store.load_json(SETTINGS) or {}).get("mandatory", {})
    for m in MANDATORY:
        cur = mand.get(m["id"])
        if cur and cur.get("lat") is not None:
            continue
        s = saved.get(m["id"])
        if s and s.get("lat") is not None:
            mand[m["id"]] = {**s, "source": "your saved position", "confirmed": True}
            continue
        try:  # reuse a position you confirmed in an earlier run
            for old in store.list_runs(40):
                o = (old.get("mandatory") or {}).get(m["id"])
                if o and o.get("confirmed") and o.get("lat") is not None:
                    mand[m["id"]] = {**o, "source": "from an earlier run you confirmed", "confirmed": True}
                    remember_mandatory(m["id"], mand[m["id"]])
                    break
        except Exception:
            pass
        if mand.get(m["id"], {}).get("lat") is not None:
            continue
        try:
            g = tomtom.geocode(m["query"])
            mand[m["id"]] = {"lat": g["lat"], "lon": g["lon"], "label": g["label"], "source": "TomTom search (check it)"}
        except tomtom.TomTomError as e:
            mand[m["id"]] = {"lat": None, "lon": None, "label": "", "source": "not found", "error": str(e)[:150]}


def passes(run, mid) -> dict:
    """For each route: nearest approach to the designated location, if within RADIUS."""
    loc = run.get("mandatory", {}).get(mid)
    out = {}
    if not loc or loc.get("lat") is None:
        return out
    for rn in routes_of(run):
        d = run["routes"][rn]["data"]
        dist, i = geo.nearest(loc["lat"], loc["lon"], d["geom"])
        if dist <= RADIUS:
            g = d["geom"]
            cam = g[max(i - 2, 0)]
            nxt = g[min(i + 1, len(g) - 1)]
            out[rn] = {"dist": round(dist), "row": row_at_offset(d, g[i][2]), "lat": g[i][0], "lon": g[i][1],
                       "cam_lat": cam[0], "cam_lon": cam[1],
                       "heading": round(geo.bearing(cam[0], cam[1], nxt[0], nxt[1])) if nxt != cam else 0}
    return out


def fetch_incidents(run):
    lats, lons = [], []
    for rn in routes_of(run):
        for g in run["routes"][rn]["data"]["geom"]:
            lats.append(g[0])
            lons.append(g[1])
    if not lats:
        return
    items, err = incidents.fetch((min(lons) - 0.01, min(lats) - 0.01, max(lons) + 0.01, max(lats) + 0.01))
    run["incidents"] = {"fetched": now(), "items": items, "error": err}


def mandatory_status(run, rn) -> str:
    """One line per designated location saying whether this route passes it (always shown)."""
    out = []
    for m in MANDATORY:
        loc = run.get("mandatory", {}).get(m["id"], {})
        if loc.get("lat") is None:
            out.append(f"<b>{m['name']}</b>: <span class=hz>position not found yet, set it in Stage 2</span>")
            continue
        ps = passes(run, m["id"]).get(rn)
        conf = "" if loc.get("confirmed") else " <span class=muted>(position not yet confirmed by you)</span>"
        if ps:
            out.append(f"<b class=hz>{m['name']}</b>: <b class=hz>ROUTE PASSES HERE</b> (row {ps['row'] + 1}, {ps['dist']} m away){conf}")
        else:
            out.append(f"<b>{m['name']}</b>: checked, route does not pass{conf}")
    return "<p class=muted>" + "<br>".join(out) + "</p>"


def clearances_for(run, rn) -> dict:
    """Overhead bridges seen in the images of each row, with posted clearance where readable."""
    d = run["routes"][rn]["data"]
    seen = {}
    for c in candidates(run):
        if c["route"] != rn:
            continue
        for o in (run.get("vision", {}).get(c["key"]) or {}).get("overhead", []):
            cl = o.get("clearance_m")
            low = cl is not None and cl <= TRUCK_HEIGHT_M + MARGIN_M
            txt = f"{o.get('what', 'overpass')}: " + (f"{cl} m posted" if cl is not None else "clearance not visible, check")
            seen.setdefault(c["row"], {})[txt] = low
    return {r: "<br>".join((f"<b class=hz>{t}</b>" if low else t) for t, low in items.items()) for r, items in seen.items()}


def diagnostics(run) -> dict:
    cands = candidates(run)
    vis = run.get("vision", {})
    res = [vis.get(c["key"]) for c in cands]
    mans = {}
    for rn in routes_of(run):
        for s in run["routes"][rn]["data"]["steps"]:
            mans[s["maneuver"]] = mans.get(s["maneuver"], 0) + 1
    dates = sorted({v.get("date") for v in res if v and v.get("date")})
    sample = []
    for c, v in zip(cands, res):
        if v and v.get("usable") and len(sample) < 3:
            sample.append(f"{c['route']} row {c['row'] + 1} ({c['kind']}): lanes={v.get('lanes')!r}; junction={v.get('junction')}; "
                          f"findings={len(v.get('findings', []))}; landmarks={v.get('landmarks')}")
    return {"points": len(cands), "usable": sum(1 for v in res if v and v.get("usable")),
            "nocov": sum(1 for v in res if v and v.get("nocov")), "unusable": sum(1 for v in res if v and not v.get("usable") and not v.get("nocov")),
            "missing": sum(1 for v in res if v is None), "maneuvers": mans, "dates": dates[:1] + dates[-1:], "sample": sample,
            "sv": (run.get("screen") or {}).get("sv", {})}


def landmarks_for(run, rn) -> dict:
    """Landmark text at each row's first turning point, taken from the Street View reading."""
    d = run["routes"][rn]["data"]
    out = {}
    for r_i, row in enumerate(d["rows"]):
        st = d["steps"][row["step"]]
        if st["lat"] is None:
            continue
        key = key_of(st["cam_lat"] or st["lat"], st["cam_lon"] or st["lon"], st["heading"], classify(st))
        res = run.get("vision", {}).get(key) or {}
        lm = res.get("landmarks") or []
        out[r_i] = "; ".join(lm) if lm else ("(no usable image)" if not res.get("usable") else "(none clear)")
    return out


# ---------- candidate screening points ----------
def key_of(lat, lon, heading, cls="") -> str:
    """Cache key: the same spot is read again if the truck does something different there, or if the prompt changes (v2)."""
    return f"{lat:.5f},{lon:.5f},{int(round(heading / 10.0) * 10) % 360}|{cls}|v2"


def candidates(run) -> list:
    out = []
    for rn in routes_of(run):
        d = run["routes"][rn]["data"]
        for s in d["steps"]:
            if s["lat"] is None:
                continue
            clat, clon = s["cam_lat"] or s["lat"], s["cam_lon"] or s["lon"]
            cls = classify(s)
            out.append({"route": rn, "row": s["row"], "kind": "step", "lat": clat, "lon": clon, "plat": s["lat"], "plon": s["lon"], "cls": cls,
                        "heading": s["heading"], "key": key_of(clat, clon, s["heading"], cls), "step": s,
                        "ctx": f"THE TRUCK IS: {s['msg']} (action: {cls}) on {s['road']}. The camera looks towards that point from about 35 m before it."})
        g = d["geom"]
        offs = [p[2] for p in g]
        for r_i, row in enumerate(d["rows"]):
            if row["dist_m"] <= MID_EVERY:
                continue
            start = d["steps"][row["step"]]["off"]
            t = start + MID_EVERY
            while t < start + row["dist_m"] - 800:
                i = min(bisect.bisect_left(offs, t), len(g) - 2)
                h = round(geo.bearing(g[max(i - 1, 0)][0], g[max(i - 1, 0)][1], g[i + 1][0], g[i + 1][1]))
                out.append({"route": rn, "row": r_i, "kind": "mid", "lat": g[i][0], "lon": g[i][1], "plat": g[i][0], "plon": g[i][1], "cls": "continue straight",
                            "heading": h, "key": key_of(g[i][0], g[i][1], h, "continue straight"), "step": None,
                            "ctx": f"THE TRUCK IS: continuing straight along {row['road']} (action: continue straight). No turn here."})
                t += MID_EVERY
    for m in MANDATORY:
        for rn, p in passes(run, m["id"]).items():
            text, cls = truck_action(run["routes"][rn]["data"], p["lat"], p["lon"])
            out.append({"route": rn, "row": p["row"], "kind": "mandatory", "lat": p["cam_lat"], "lon": p["cam_lon"],
                        "plat": p["lat"], "plon": p["lon"], "heading": p["heading"], "cls": cls, "key": key_of(p["cam_lat"], p["cam_lon"], p["heading"], cls),
                        "step": None, "ctx": f"THE TRUCK IS: {text} (action: {cls}). Operator-designated location: {m['name']}."})
    return out


# ---------- screening job ----------
def _save(run):
    with LOCK:
        run["screen"]["beat"] = time.time()
        store.save_run(run)


def _fetch(c):
    try:
        m = streetview.metadata(c["lat"], c["lon"])
        st = m.get("status", "")
        if st in FATAL:
            return c["key"], {"error": f"Street View says {st}: {m.get('error_message', 'check the Google key and that Street View Static API is enabled')}", "fatal": True, "status": st}, None
        if st != "OK":
            return c["key"], {"nocov": True, "usable": False, "findings": [], "note": st, "status": st}, None
        return c["key"], {"date": m.get("date", ""), "pano": m["pano_id"]}, streetview.image(m["pano_id"], c["heading"])
    except (streetview.StreetViewError, httpx.HTTPError) as e:
        return c["key"], {"error": str(e)[:150]}, None


def screen_core(run, persist):
    """Screen every point of the routes in `run` (cached points are skipped) and rebuild the hazard list."""
    locate_mandatory(run)
    fetch_incidents(run)
    vis = run.setdefault("vision", {})
    seen, need = set(), []
    for c in candidates(run):
        if "usable" not in vis.get(c["key"], {}) and c["key"] not in seen:
            seen.add(c["key"])
            need.append(c)
    run["screen"] = {"status": "running", "done": 0, "total": len(need), "error": "", "started": now()}
    persist()
    for i in range(0, len(need), 15):
        chunk = need[i:i + 15]
        with ThreadPoolExecutor(6) as ex:
            fetched = list(ex.map(_fetch, chunk))
        fatal = [m for _, m, _ in fetched if m.get("fatal")]
        if fatal:
            raise RuntimeError(fatal[0]["error"])
        sv = run["screen"].setdefault("sv", {})
        for _, m, _ in fetched:
            k = m.get("status") or ("ERROR" if "error" in m else "OK")
            sv[k] = sv.get(k, 0) + 1
        byctx = {c["key"]: c["ctx"] for c in chunk}
        items = []
        for key, meta, img in fetched:
            if img is None:
                if "error" not in meta:
                    vis[key] = meta  # no coverage: remembered
                continue
            vis[key] = meta
            items.append({"id": key, "context": byctx[key], "jpeg": img})
        batches = [items[j:j + 5] for j in range(0, len(items), 5)]
        with ThreadPoolExecutor(3) as ex:
            for res in ex.map(vision.screen, batches):
                for key, r in res.items():
                    vis[key].update(r)
        run["screen"]["done"] += len(chunk)
        persist()
    build_hazards(run)
    run["screen"].update({"status": "done", "finished": now()})
    persist()


def run_screen(run_id: str):
    run = store.load_run(run_id)
    try:
        screen_core(run, lambda: _save(run))
    except Exception as e:  # keep progress; user can press the button again to resume
        run["screen"] = {**run.get("screen", {}), "status": "error", "error": f"{type(e).__name__}: {e}"[:300]}
        _save(run)


def start(run_id: str) -> bool:
    run = store.load_run(run_id)
    sc = run.get("screen") or {}
    if sc.get("status") in ("running", "queued") and time.time() - sc.get("beat", 0) < 180:
        return False
    run["screen"] = {"status": "queued", "done": 0, "total": 0, "error": "", "beat": time.time()}
    store.save_run(run)
    threading.Thread(target=run_screen, args=(run_id,), daemon=True).start()
    return True


# ---------- hazards ----------
SEV = {"high": 3, "medium": 2, "low": 1}


def _obs(c, cat, what, risk, precaution, src, sev=2, evidence=""):
    return {"c": c, "cat": cat, "what": what, "risk": risk, "precaution": precaution, "src": src, "sev": sev, "evidence": evidence}


def observations(run) -> list:
    obs = []
    for c in candidates(run):
        s, res, cls = c["step"], run.get("vision", {}).get(c["key"]) or {}, c.get("cls", "")
        if s:
            man = s["maneuver"].upper()
            if cls == "u-turn":
                obs.append(_obs(c, 6, "The route needs a U-turn here, which is not allowed.", "Not allowed", "Re-route with a via point.", "rule", 3, "TomTom maneuver"))
            if cls == "expressway exit or entry":
                obs.append(_obs(c, 5, "Leaving or joining the expressway or a slip road here.", "Short exit or merge: the lane must be right in time.",
                                "Get into the correct lane early and check mirrors.", "rule", 2, "TomTom maneuver"))
            if cls == "roundabout":
                obs.append(_obs(c, 4, "Roundabout ahead.", "Multi-lane entry; easy to pick the wrong lane.", "Slow down and choose the lane before entry.", "rule", 2, "TomTom maneuver"))
            ang = s.get("angle")
            if cls in ("turn left", "turn right") and ("SHARP" in man or (ang is not None and abs(ang) >= 100)):
                obs.append(_obs(c, 4, "Sharp turn.", "Tight turning angle.", "Approach wide and slow.", "rule", 2, "TomTom turn angle"))
        if res.get("usable"):
            ev = f"Street View {res.get('date', '?')}"
            need = 3 if cls in STRICT else 2
            for f in res.get("findings", [])[:2]:
                if SEV.get(f.get("severity", "low"), 1) >= need:
                    obs.append(_obs(c, f["cat"], f["what"], f["risk"], f["precaution"], "vision", SEV[f["severity"]], ev))
            for o in res.get("overhead", []):
                cl = o.get("clearance_m")
                if cl is not None and cl <= TRUCK_HEIGHT_M + MARGIN_M:
                    obs.append(_obs(c, 7, f"Overhead bridge ({o.get('what', '')}) with posted clearance {cl} m.", f"Too low for a {TRUCK_HEIGHT_M} m truck.",
                                    "Do not pass under it; add a via point to avoid it.", "vision", 3, ev))
            if cls not in STRICT and res.get("junction") == "large":
                obs.append(_obs(c, 4, "Large multi-lane junction.", "The correct lane is easy to miss.", "Pick the lane early.", "vision", 2, ev))
            if cls in ("turn left", "turn right", "expressway exit or entry") and res.get("slip"):
                obs.append(_obs(c, 5, "Small slip road beside this manoeuvre.", "The truck may have to swing across it.", "Slow down and check the slip road.", "vision", 2, ev))
    inc = (run.get("incidents") or {}).get("items", [])
    for rn in routes_of(run):
        d = run["routes"][rn]["data"]
        for it, la, lo, gi in incidents.near(inc, d["geom"]):
            g = d["geom"]
            nxt = g[min(gi + 1, len(g) - 1)]
            head = round(geo.bearing(g[gi][0], g[gi][1], nxt[0], nxt[1])) if nxt != g[gi] else 0
            c = {"route": rn, "row": row_at_offset(d, g[gi][2]), "plat": la, "plon": lo, "heading": head, "key": "", "cls": "continue straight", "step": None}
            span = " to ".join(x for x in (it["from"], it["to"]) if x)
            ev = f"TomTom incident feed ({it['when']}), fetched {run['incidents']['fetched']}; starts {it['start'] or '?'}, ends {it['end'] or '?'}"
            obs.append(_obs(c, it["cat"], f"{it['text']}{(' (' + span + ')') if span else ''}.", "Roadworks or a closure on the route.",
                            "Check the diversion and lanes before the run.", "tomtom-live", 2, ev))
    return obs


def build_hazards(run):
    old = run.get("hazards", [])
    next_id = run.get("next_id", 3)
    vis = run.get("vision", {})
    hz = []
    for m in MANDATORY:  # operator designated first
        ps = passes(run, m["id"])
        loc = run.get("mandatory", {}).get(m["id"], {})
        first = next(iter(ps)) if ps else None
        lat = ps[first]["lat"] if ps else loc.get("lat")
        lon = ps[first]["lon"] if ps else loc.get("lon")
        act, acls = truck_action(run["routes"][first]["data"], lat, lon) if ps else ("", "")
        h = {"id": m["id"], "src": "mandatory", "name": m["name"], "cats": list(m["cats"]), "routes": {rn: p["row"] for rn, p in ps.items()},
             "lat": lat, "lon": lon, "heading": ps[first]["heading"] if ps else 0, "applicable": bool(ps), "action": act, "action_cls": acls,
             "landmark": "", "pano": "",
             "details": [{"cat": 11, "what": m["what"], "risk": m["risk"], "precaution": m["precaution"], "src": "operator", "sev": 3,
                          "evidence": f"Operator designated; location: {loc.get('source', 'unknown')}"}],
             "review": "pending", "note": "", "checked": now()}
        prev = next((o for o in old if o["id"] == m["id"]), None)
        if prev:
            h["review"], h["note"] = prev.get("review", "pending"), prev.get("note", "")
        hz.append(h)
    auto = []
    for o in observations(run):
        c = o["c"]
        tgt = next((h for h in hz + auto if h["lat"] is not None and geo.hav(c["plat"], c["plon"], h["lat"], h["lon"]) <= (60 if h["src"] == "mandatory" else CLUSTER)), None)
        if tgt is None:
            act, acls = truck_action(run["routes"][c["route"]]["data"], c["plat"], c["plon"])
            tgt = {"id": None, "src": "auto", "cats": [], "routes": {}, "lat": c["plat"], "lon": c["plon"], "heading": c["heading"], "details": [],
                   "action": act, "action_cls": acls, "landmark": "", "pano": "", "review": "pending", "note": "", "checked": now()}
            auto.append(tgt)
        r = vis.get(c.get("key", "")) or {}
        if not tgt["landmark"] and r.get("usable") and r.get("landmarks"):
            tgt["landmark"] = "; ".join(r["landmarks"][:2])
        if not tgt["pano"] and r.get("pano"):
            tgt["pano"] = r["pano"]
        if o["cat"] not in tgt["cats"]:
            tgt["cats"].append(o["cat"])
        tgt["routes"].setdefault(c["route"], c["row"])
        if not any(d["what"] == o["what"] for d in tgt["details"]):
            tgt["details"].append({"cat": o["cat"], "what": o["what"], "risk": o["risk"], "precaution": o["precaution"], "src": o["src"], "sev": o["sev"], "evidence": o["evidence"]})
    auto.sort(key=lambda h: (0, h["routes"]["primary"]) if "primary" in h["routes"] else (1, h["routes"].get("secondary", 0)))
    for h in auto:
        prev = next((o for o in old if o["src"] == "auto" and geo.hav(h["lat"], h["lon"], o["lat"], o["lon"]) <= CLUSTER and o["id"] not in {x["id"] for x in hz}), None)
        if prev:
            h["id"], h["review"], h["note"] = prev["id"], prev.get("review", "pending"), prev.get("note", "")
        else:
            h["id"] = f"D{next_id}"
            next_id += 1
        hz.append(h)
    for o in old:  # manual hazards survive if still on a route
        if o["src"] == "manual":
            keep = {}
            for rn in routes_of(run):
                dist, _ = geo.nearest(o["lat"], o["lon"], run["routes"][rn]["data"]["geom"])
                if dist <= 60:
                    keep[rn] = o["routes"].get(rn, 0)
            if keep:
                o["routes"] = keep
                hz.append(o)
    run["hazards"], run["next_id"] = hz, next_id


def add_manual(run, route, row_no, cat, text):
    d = run["routes"][route]["data"]
    row = d["rows"][row_no - 1]
    s = d["steps"][row["step"]]
    hid = f"D{run.get('next_id', 3)}"
    run["next_id"] = run.get("next_id", 3) + 1
    act, acls = truck_action(d, s["lat"], s["lon"])
    run.setdefault("hazards", []).append({
        "id": hid, "src": "manual", "cats": [cat], "routes": {route: row_no - 1}, "lat": s["lat"], "lon": s["lon"], "heading": s["heading"],
        "action": act, "action_cls": acls, "landmark": "", "pano": "",
        "details": [{"cat": cat, "what": text, "risk": "Added by you", "precaution": "", "src": "manual", "sev": 3, "evidence": "Added manually"}],
        "review": "confirmed", "note": "", "checked": now()})


def checklist(run) -> dict:
    out = {}
    hz = [h for h in run.get("hazards", []) if h.get("review") != "rejected" and h.get("applicable", True)]
    for rn in routes_of(run):
        d = run["routes"][rn]["data"]
        cands = [c for c in candidates(run) if c["route"] == rn]
        grid = []
        for r_i in range(len(d["rows"])):
            usable = any((run.get("vision", {}).get(c["key"]) or {}).get("usable") for c in cands if c["row"] == r_i)
            cats = {c for h in hz if h["routes"].get(rn) == r_i for c in h["cats"]}
            row = []
            for n in range(1, 13):
                if n in cats:
                    row.append("H")
                elif n in (8, 12):
                    row.append("N")
                elif n == 11:
                    row.append("N")
                elif n in (4, 5, 6):
                    row.append("S")
                else:
                    row.append("S" if usable else "U")
            grid.append(row)
        out[rn] = grid
    return out

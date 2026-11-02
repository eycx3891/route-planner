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

MANDATORY = [
    {"id": "D1", "name": "Sembawang Road / Yishun Road 1 junction",
     "query": "Sembawang Road and Yishun Road junction, Singapore", "cats": [1, 2, 3, 11],
     "what": "Operator-designated: construction, reduced speeds and lane closures. Check current works and affected approaches.",
     "risk": "Lane closures and reduced speeds near the junction",
     "precaution": "Slow down, expect lane shifts, check which approaches are affected."},
    {"id": "D2", "name": "In front of Chong Pang Camp", "query": "Chong Pang Camp, Singapore", "cats": [10, 9, 11],
     "what": "Operator-designated: garbage vehicles may emerge from the camp entrance. Check visibility, traffic interaction and turning movements.",
     "risk": "Vehicles emerging unexpectedly from the camp entrance",
     "precaution": "Reduce speed, watch the entrance, keep clear of the entrance lane where possible."},
]


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
def locate_mandatory(run):
    mand = run.setdefault("mandatory", {})
    for m in MANDATORY:
        cur = mand.get(m["id"])
        if cur and cur.get("lat") is not None:
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
            out.append(f"<b>{m['id']}</b> {m['name']}: <span class=hz>position not found yet, set it in Stage 2</span>")
            continue
        ps = passes(run, m["id"]).get(rn)
        conf = "" if loc.get("confirmed") else " <span class=muted>(position not yet confirmed by you)</span>"
        if ps:
            out.append(f"<b class=hz>{m['id']}</b> {m['name']}: <b class=hz>ROUTE PASSES HERE</b> (row {ps['row'] + 1}, {ps['dist']} m away){conf}")
        else:
            out.append(f"<b>{m['id']}</b> {m['name']}: checked, route does not pass{conf}")
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
        key = key_of(st["cam_lat"] or st["lat"], st["cam_lon"] or st["lon"], st["heading"])
        res = run.get("vision", {}).get(key) or {}
        lm = res.get("landmarks") or []
        out[r_i] = "; ".join(lm) if lm else ("(no usable image)" if not res.get("usable") else "(none clear)")
    return out


# ---------- candidate screening points ----------
def key_of(lat, lon, heading) -> str:
    return f"{lat:.5f},{lon:.5f},{int(round(heading / 10.0) * 10) % 360}"


def candidates(run) -> list:
    out = []
    for rn in routes_of(run):
        d = run["routes"][rn]["data"]
        for s in d["steps"]:
            if s["lat"] is None:
                continue
            clat, clon = s["cam_lat"] or s["lat"], s["cam_lon"] or s["lon"]
            out.append({"route": rn, "row": s["row"], "kind": "step", "lat": clat, "lon": clon, "plat": s["lat"], "plon": s["lon"],
                        "heading": s["heading"], "key": key_of(clat, clon, s["heading"]), "step": s,
                        "ctx": f"{s['msg']} [{s['maneuver']}] on {s['road']}"})
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
                out.append({"route": rn, "row": r_i, "kind": "mid", "lat": g[i][0], "lon": g[i][1], "plat": g[i][0], "plon": g[i][1],
                            "heading": h, "key": key_of(g[i][0], g[i][1], h), "step": None,
                            "ctx": f"Mid-section of {row['road']}, travelling along the road"})
                t += MID_EVERY
    for m in MANDATORY:
        for rn, p in passes(run, m["id"]).items():
            out.append({"route": rn, "row": p["row"], "kind": "mandatory", "lat": p["cam_lat"], "lon": p["cam_lon"],
                        "plat": p["lat"], "plon": p["lon"], "heading": p["heading"], "key": key_of(p["cam_lat"], p["cam_lon"], p["heading"]),
                        "step": None, "ctx": f"Operator-designated location: {m['name']}"})
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


def run_screen(run_id: str):
    run = store.load_run(run_id)
    try:
        locate_mandatory(run)
        fetch_incidents(run)
        vis = run.setdefault("vision", {})
        seen, need = set(), []
        for c in candidates(run):
            if "usable" not in vis.get(c["key"], {}) and c["key"] not in seen:
                seen.add(c["key"])
                need.append(c)
        run["screen"] = {"status": "running", "done": 0, "total": len(need), "error": "", "started": now()}
        _save(run)
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
            _save(run)
        build_hazards(run)
        run["screen"].update({"status": "done", "finished": now()})
        _save(run)
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
def _obs(c, cat, what, risk, precaution, src, conf="medium", evidence=""):
    return {"c": c, "cat": cat, "what": what, "risk": risk, "precaution": precaution, "src": src, "conf": conf, "evidence": evidence}


def observations(run) -> list:
    obs = []
    for c in candidates(run):
        s, res = c["step"], run.get("vision", {}).get(c["key"]) or {}
        if s:
            man = s["maneuver"].upper()
            if s["uturn"]:
                obs.append(_obs(c, 6, "Router instructs a U-turn here", "Prohibited manoeuvre", "Re-route with a via point", "rule", "high", "TomTom maneuver"))
            if any(w in man for w in ("EXIT", "RAMP", "ENTER", "MERGE", "MOTORWAY", "HIGHWAY", "FREEWAY")):
                obs.append(_obs(c, 5, f"Expressway or slip road entry, exit or merge ({s['msg']})", "Short merge or difficult exit positioning",
                                "Get into the correct lane early and check mirrors", "rule", "medium", "TomTom maneuver"))
            if "ROUNDABOUT" in man or s["junction"].upper().startswith("ROUNDABOUT"):
                obs.append(_obs(c, 4, f"Roundabout ({s['msg']})", "Constrained junction with multi-lane entry", "Slow, choose lane before entry", "rule", "medium", "TomTom maneuver"))
            ang = s.get("angle")
            if ("SHARP" in man or (ang is not None and abs(ang) >= 100)) and not s["uturn"]:
                obs.append(_obs(c, 4, f"Sharp turn ({s['msg']})", "Tight turn", "Approach wide and slow", "rule", "medium", "TomTom turn angle"))
        if res.get("usable"):
            ev = f"Street View {res.get('date', '?')}"
            for f in res.get("findings", []):
                obs.append(_obs(c, f["cat"], f["what"], f["risk"], f["precaution"], "vision", f["conf"], ev))
            for o in res.get("overhead", []):
                cl = o.get("clearance_m")
                if cl is not None and cl <= TRUCK_HEIGHT_M + MARGIN_M:
                    obs.append(_obs(c, 7, f"Overhead bridge ({o.get('what', '')}) posted clearance {cl} m", f"Too low for a {TRUCK_HEIGHT_M} m truck",
                                    "Do not pass under it; add a via point to avoid it", "vision", "high", ev))
            if res.get("junction") == "large":
                obs.append(_obs(c, 4, "Large junction visible", "Large multi-lane junction", "Pick lane early, watch for cross traffic", "vision", "medium", ev))
            if res.get("slip"):
                obs.append(_obs(c, 5, "Slip road visible", "Small slip road entry or exit", "Slow, check merge and give way", "vision", "medium", ev))
    inc = (run.get("incidents") or {}).get("items", [])
    for rn in routes_of(run):
        d = run["routes"][rn]["data"]
        for it, la, lo, gi in incidents.near(inc, d["geom"]):
            g = d["geom"]
            nxt = g[min(gi + 1, len(g) - 1)]
            head = round(geo.bearing(g[gi][0], g[gi][1], nxt[0], nxt[1])) if nxt != g[gi] else 0
            c = {"route": rn, "row": row_at_offset(d, g[gi][2]), "plat": la, "plon": lo, "heading": head, "key": ""}
            span = " to ".join(x for x in (it["from"], it["to"]) if x)
            ev = f"TomTom incident feed ({it['when']}), fetched {run['incidents']['fetched']}; starts {it['start'] or '?'}, ends {it['end'] or '?'}"
            obs.append(_obs(c, it["cat"], f"{it['text']}{(' (' + span + ')') if span else ''}",
                            "Roadworks or closure on the route", "Check the diversion and lane layout before the run",
                            "tomtom-live", "medium", ev))
    return obs


def build_hazards(run):
    old = run.get("hazards", [])
    next_id = run.get("next_id", 3)
    hz = []
    # operator designated first (IDs fixed)
    for m in MANDATORY:
        ps = passes(run, m["id"])
        loc = run.get("mandatory", {}).get(m["id"], {})
        h = {"id": m["id"], "src": "mandatory", "name": m["name"], "cats": list(m["cats"]), "routes": {rn: p["row"] for rn, p in ps.items()},
             "lat": ps[next(iter(ps))]["lat"] if ps else loc.get("lat"), "lon": ps[next(iter(ps))]["lon"] if ps else loc.get("lon"),
             "heading": ps[next(iter(ps))]["heading"] if ps else 0,
             "applicable": bool(ps), "details": [{"what": m["what"], "risk": m["risk"], "precaution": m["precaution"], "src": "operator", "conf": "high",
                                                 "evidence": f"Operator designated; location: {loc.get('source', 'unknown')}"}],
             "review": "pending", "note": "", "checked": now()}
        prev = next((o for o in old if o["id"] == m["id"]), None)
        if prev:
            h["review"], h["note"] = prev.get("review", "pending"), prev.get("note", "")
        hz.append(h)
    # clustered observations
    auto = []
    for o in observations(run):
        c = o["c"]
        tgt = next((h for h in hz + auto if h["lat"] is not None and geo.hav(c["plat"], c["plon"], h["lat"], h["lon"]) <= (60 if h["src"] == "mandatory" else CLUSTER)), None)
        if tgt is None:
            tgt = {"id": None, "src": "auto", "cats": [], "routes": {}, "lat": c["plat"], "lon": c["plon"], "heading": c["heading"], "details": [],
                   "review": "pending", "note": "", "checked": now()}
            auto.append(tgt)
        if o["cat"] not in tgt["cats"]:
            tgt["cats"].append(o["cat"])
        tgt["routes"].setdefault(c["route"], c["row"])
        if not any(d["what"] == o["what"] for d in tgt["details"]):
            tgt["details"].append({"what": o["what"], "risk": o["risk"], "precaution": o["precaution"], "src": o["src"], "conf": o["conf"], "evidence": o["evidence"]})
    auto.sort(key=lambda h: (0, h["routes"]["primary"]) if "primary" in h["routes"] else (1, h["routes"].get("secondary", 0)))
    for h in auto:
        prev = next((o for o in old if o["src"] == "auto" and geo.hav(h["lat"], h["lon"], o["lat"], o["lon"]) <= CLUSTER and o["id"] not in {x["id"] for x in hz}), None)
        if prev:
            h["id"], h["review"], h["note"] = prev["id"], prev.get("review", "pending"), prev.get("note", "")
        else:
            h["id"] = f"D{next_id}"
            next_id += 1
        hz.append(h)
    # manual hazards survive if still on a route
    for o in old:
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
    run.setdefault("hazards", []).append({
        "id": hid, "src": "manual", "cats": [cat], "routes": {route: row_no - 1}, "lat": s["lat"], "lon": s["lon"], "heading": s["heading"],
        "details": [{"what": text, "risk": "Added by you", "precaution": "", "src": "manual", "conf": "high", "evidence": "Added manually"}],
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

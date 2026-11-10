"""Stage 6: a series of maps that follows the whole route in order.
Each map is as zoomed out as the road allows and zoomed in only where a turn or danger area needs a close view."""
import math

from . import geo, mapdraw, stage2, stage4, store

COL = {"primary": mapdraw.BLUE, "secondary": mapdraw.RED}
GREEN, GREY = (20, 130, 60), (90, 90, 90)
# zoom each kind of danger area needs (17 = about 0.7 km across, 16 = 1.5 km, 15 = 3 km)
ZREQ = {1: 15, 2: 15, 3: 15, 4: 17, 5: 16, 6: 17, 7: 16, 9: 16, 10: 17, 11: 17}
TRICKY = {"turn left": 16, "turn right": 16, "roundabout": 16, "expressway exit or entry": 16, "u-turn": 17}
MARGIN = 25  # a danger area belongs to the map whose stretch contains it (give or take this many metres)
MAXLAB = 7  # danger area boxes per map, so every map stays tidy
ZMIN = 12
PREFIX = {"primary": "P", "secondary": "Q"}
DETAIL = {-1: "Fewer maps", 0: "Standard", 1: "More detail"}


def ready_state(run) -> list:
    rid, g, fr = run["id"], run["gates"], run.get("frozen")
    return [("Gates 1 to 4 approved", all(g.get(k) == "approved" for k in "123") and stage4.gate4_ok(run), f"/run/{rid}/stage4"),
            ("Driver reference frozen and approved (Gate 5)", bool(fr) and fr["version"] == run["version"] and g.get("5") == "approved", f"/run/{rid}/stage5")]


def data(run):
    routes, hz_all = stage4.final_view(run)
    lm = stage2.labels(hz_all or [], routes)  # D1, D2... in the order the truck meets them
    hz = [dict(h, label=lm[h["id"]]) for h in (hz_all or []) if h["id"] in lm]
    stops = []
    for rn in ("primary", "secondary"):
        st = (run["routes"].get(rn) or {}).get("stopped")
        for s in (st["stops"] if st else []):
            stops.append({"rid": s.get("rid", "R?"), "name": s["name"], "address": s["address"], "lat": s["lat"], "lon": s["lon"], "route": rn})
    return routes, hz, stops


# ---------- planning the series ----------
def profile(rn, routes, hz):
    d = routes[rn]
    raw = mapdraw.route_pts(d)
    # the drawing line has long straight segments on open roads, so add points every 25 m to measure and place things along it
    pts, cum = [tuple(raw[0])], [0.0]
    for a, b in zip(raw, raw[1:]):
        seg = geo.hav(a[0], a[1], b[0], b[1])
        n = max(int(seg // 25), 1)
        for k in range(1, n + 1):
            pts.append((a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))
            cum.append(cum[-1] + seg / n)
    thin = list(zip(pts, cum))

    def off(lat, lon):
        kx = math.cos(math.radians(lat))
        i = min(range(len(thin)), key=lambda k: (thin[k][0][0] - lat) ** 2 + ((thin[k][0][1] - lon) * kx) ** 2)
        return thin[i][1]
    feats = []
    for h in hz:
        if rn in h["routes"]:
            feats.append({"type": "hazard", "off": off(h["lat"], h["lon"]), "zr": max([ZREQ.get(c, 15) for c in h["cats"]] or [15]), "label": h["label"], "lat": h["lat"], "lon": h["lon"]})
    for st in d["steps"]:
        cls = stage2.classify(st)
        if cls in TRICKY and st["lat"] is not None:
            feats.append({"type": "turn", "off": off(st["lat"], st["lon"]), "zr": TRICKY[cls], "text": st["msg"], "lat": st["lat"], "lon": st["lon"]})
    return thin, cum[-1], sorted(feats, key=lambda f: f["off"])


def hz_req(h, bias) -> int:
    return min(max([ZREQ.get(c, 15) for c in h['cats']] or [15]) + bias, 17)


def window(thin, a, b, feats, bias, skip=-1):
    sp = [p for p, c in thin if a <= c <= b]
    if len(sp) < 2:
        near = lambda x: thin[min(range(len(thin)), key=lambda k: abs(thin[k][1] - x))][0]
        sp = [near(a), near(b)]
    inn = [f for f in feats if a <= f["off"] <= b and f["off"] > skip]
    pts = sp + [(f["lat"], f["lon"]) for f in inn if f["type"] == "hazard"]
    center, z = mapdraw.fit(pts, pad_css=80, zmax=17, zmin=ZMIN)
    zr = max([min(f["zr"] + bias, 17) for f in inn], default=0)
    labels = sum(1 for f in inn if f["type"] == "hazard")
    return (mapdraw.fits_at(pts, z, 80) and z >= zr and labels <= MAXLAB), center, z, zr, labels


def series(thin, total, feats, bias):
    """Frames that run from the start to the end of the route. Each is as long as it can be while staying readable."""
    frames, pos, skip = [], 0.0, -1
    while True:
        e = min(pos + 300, total)
        ok, c, z, zr, lab = window(thin, pos, e, feats, bias, skip)
        best = (e, c, z, zr, lab)
        if ok:
            good, step, bad = e, 300, None
            while good < total:
                nxt = min(good + step, total)
                r = window(thin, pos, nxt, feats, bias, skip)
                if r[0]:
                    good, best, step = nxt, (nxt,) + r[1:], step * 2
                else:
                    bad = nxt
                    break
            if bad is not None:
                lo, hi = good, bad
                while hi - lo > 100:
                    m = (lo + hi) / 2
                    r = window(thin, pos, m, feats, bias, skip)
                    if r[0]:
                        lo, best = m, (m,) + r[1:]
                    else:
                        hi = m
        else:
            e = min(pos + 200, total)
            best = (e,) + window(thin, pos, e, feats, bias, skip)[1:]
        near = [f for f in feats if f["zr"] > 0 and abs(f["off"] - best[0]) <= 150 and f["off"] > skip]
        if best[0] < total - 1 and any(f["off"] <= best[0] for f in near) and any(f["off"] > best[0] for f in near):
            cut = min(f["off"] for f in near) - 30  # do not split a cluster across two maps: end this one before it
            if cut > pos + 150:
                best = (cut,) + window(thin, pos, cut, feats, bias, skip)[1:]
        frames.append({"a": pos, "b": best[0], "center": best[1], "zoom": best[2], "zr": best[3], "labels": best[4]})
        if best[0] >= total - 1:
            break
        length = best[0] - pos
        shown = [f["off"] for f in feats if f["zr"] > 0 and pos <= f["off"] <= best[0] and f["off"] > skip]
        skip = max([skip] + shown)
        pos = max(best[0] - max(250, min(600, 0.12 * length)), pos + 100)
        if shown:
            pos = max(pos, min(max(shown) + 30, best[0] - 40))
    return frames


def _in(vp, lat, lon) -> bool:
    return vp[0] <= lat <= vp[2] and vp[1] <= lon <= vp[3]


def view_link(center, z) -> str:
    return f"https://www.google.com/maps/@{center[0]:.6f},{center[1]:.6f},{z}z"


def why_text(zr) -> str:
    if zr >= 17:
        return "A tight junction or danger area needs a close view."
    if zr == 16:
        return "A turn, exit or merge needs a closer view."
    if zr == 15:
        return "Roadworks or a lane closure on this stretch."
    return "Straight road with nothing tricky: zoomed out to keep the number of maps down."


def plan(run, detail=0) -> list:
    routes, hz, stops = data(run)
    names = [n for n in ("primary", "secondary") if n in routes]
    specs = []
    if len(names) == 2:
        pts = [(p[0], p[1]) for n in names for p in mapdraw.route_pts(routes[n])]
        c, z = mapdraw.fit(pts)
        specs.append({"id": "M1", "title": "Both routes overview", "kind": "overview", "routes": names, "purpose": "One picture of both routes together, with no danger areas, to show the whole trip at a glance.",
                      "coverage": f"Whole trip, both routes (zoom {z})", "why": "A single first slide before the detailed maps.", "compulsory": False, "default": True,
                      "center": list(c), "zoom": z, "viewport": list(mapdraw.viewport(c, z))})
    for rn in names:
        thin, total, feats = profile(rn, routes, hz)
        frames = series(thin, total, feats, detail)
        for k, f in enumerate(frames, 1):
            vp = mapdraw.viewport(f["center"], f["zoom"])
            labs = [x["label"] for x in feats if x["type"] == "hazard" and f["a"] - MARGIN <= x["off"] <= f["b"] + MARGIN and _in(vp, x["lat"], x["lon"])
                    and f["zoom"] >= min(x["zr"] + detail, 17)]
            specs.append({"id": f"{PREFIX[rn]}{k}", "title": f"{rn.capitalize()} route: map {k} of {len(frames)}", "kind": "series", "routes": [rn], "k": k, "n": len(frames),
                          "a": f["a"], "b": f["b"], "total": total, "zr": f["zr"], "labels": labs,
                          "bias": detail, "purpose": f"{f['a'] / 1000:.1f} to {f['b'] / 1000:.1f} km of {total / 1000:.1f} km" + (f". Danger areas: {', '.join(labs)}" if labs else ". No danger areas."),
                          "coverage": f"km {f['a'] / 1000:.1f} to {f['b'] / 1000:.1f} (zoom {f['zoom']})", "why": why_text(f["zr"]), "compulsory": True, "default": True,
                          "center": list(f["center"]), "zoom": f["zoom"], "viewport": list(mapdraw.viewport(f["center"], f["zoom"]))})
    for s in stops:
        near = [(g[0], g[1]) for g in mapdraw.route_pts(routes[s["route"]]) if geo.hav(s["lat"], s["lon"], g[0], g[1]) <= 900]
        c, z = mapdraw.fit(near + [(s["lat"], s["lon"])], pad_css=90, zmax=17)
        specs.append({"id": f"S_{s['rid']}", "title": f"Rest stop {s['rid']}: access and exit", "kind": "stop", "routes": [s["route"]], "stop": s["rid"],
                      "purpose": f"How to get into and out of {s['name']} and back onto the route.", "coverage": f"The stop and about 900 m of road (zoom {z})",
                      "why": "Compulsory whenever a rest stop is planned.", "compulsory": True, "default": True, "center": list(c), "zoom": z, "viewport": list(mapdraw.viewport(c, z))})
    return specs


# ---------- drawing ----------
def point_at(thin, off):
    return thin[min(range(len(thin)), key=lambda k: abs(thin[k][1] - off))][0]


def render(run, s, routes, hz, stops, profiles):
    base = mapdraw.fetch_base(s["center"], s["zoom"])
    cv = mapdraw.Canvas(base, tuple(s["center"]), s["zoom"])
    names = s["routes"]
    for rn in [n for n in ("secondary", "primary") if n in names]:
        pts = cv.line(mapdraw.route_pts(routes[rn]), COL[rn], 8 if len(names) == 2 else 9)
        cv.drawn["routes"].append(rn)
    cv.flush_lines()
    mine = [h for h in hz if set(h["routes"]) & set(names)] if s["kind"] != "overview" else []
    if s["kind"] == "series":
        th = profiles[names[0]][0]
        mine = [h for h in mine if _in(s["viewport"], h["lat"], h["lon"]) and s["zoom"] >= hz_req(h, s["bias"]) and s["a"] - MARGIN <= stage2_off(th, h) <= s["b"] + MARGIN]
        rn = names[0]
        thin = profiles[rn][0]
        for h in sorted(mine, key=lambda h: stage2_off(thin, h)):
            cv.add_marker(h["lat"], h["lon"], h["label"], mapdraw.RED, "hazards")
    if s["kind"] == "stop":
        mine = [h for h in mine if _in(s["viewport"], h["lat"], h["lon"])]
        for h in mine:
            cv.add_marker(h["lat"], h["lon"], h["label"], mapdraw.RED, "hazards")
    for st in stops:
        if st["route"] in names and s["kind"] in ("series", "stop"):
            cv.add_marker(st["lat"], st["lon"], st["rid"], mapdraw.ORANGE, "stops", 38)
    first = mapdraw.route_pts(routes[names[0]])
    cv.add_marker(first[0][0], first[0][1], "START", GREEN, "ends", 30)
    cv.add_marker(first[-1][0], first[-1][1], "END", mapdraw.DARK, "ends", 30)
    if s["kind"] == "series":
        thin = profiles[names[0]][0]
        if s["k"] > 1:
            p = point_at(thin, s["a"])
            cv.add_marker(p[0], p[1], f"from map {s['k'] - 1}", GREY, "tag", 24)
        if s["k"] < s["n"]:
            p = point_at(thin, s["b"])
            cv.add_marker(p[0], p[1], f"continues on map {s['k'] + 1}", GREY, "tag", 24)
    cv.draw_markers()
    if s["kind"] in ("overview", "series") and s["zoom"] <= 15:
        for rn in names:
            r = routes[rn]
            cv.road_labels(r["geom"], r["rows"], r["steps"], COL[rn])
    sub = s["purpose"] if s["kind"] == "series" else (s["coverage"] if s["kind"] != "stop" else next((x["name"] + ", " + x["address"] for x in stops if x["rid"] == s["stop"]), ""))
    return cv.finish(s["title"], sub), cv, mine


def stage2_off(thin, h) -> float:
    kx = math.cos(math.radians(h["lat"]))
    return thin[min(range(len(thin)), key=lambda k: (thin[k][0][0] - h["lat"]) ** 2 + ((thin[k][0][1] - h["lon"]) * kx) ** 2)][1]


def verify(s, cv, mine, stops) -> list:
    vp, res = s["viewport"], []
    exp_h = sorted(h["label"] for h in mine if _in(vp, h["lat"], h["lon"]))  # `mine` is already limited to hazards this zoom can show
    got_h = sorted(cv.drawn["hazards"])
    res.append({"what": "Danger areas drawn match this map's area", "ok": exp_h == got_h, "detail": f"expected {exp_h or 'none'}, drawn {got_h or 'none'}"})
    exp_s = sorted(x["rid"] for x in stops if x["route"] in s["routes"] and _in(vp, x["lat"], x["lon"]) and s["kind"] in ("series", "stop"))
    got_s = sorted(cv.drawn["stops"])
    res.append({"what": "Rest stops drawn match this map's area", "ok": exp_s == got_s, "detail": f"expected {exp_s or 'none'}, drawn {got_s or 'none'}"})
    res.append({"what": "Routes drawn match this map's routes", "ok": sorted(cv.drawn["routes"]) == sorted(s["routes"]), "detail": ", ".join(cv.drawn["routes"])})
    bad = []
    for h in mine:
        if _in(vp, h["lat"], h["lon"]):
            cx, cy = mapdraw.proj(s["center"][0], s["center"][1], s["zoom"])
            x, y = mapdraw.proj(h["lat"], h["lon"], s["zoom"])
            want = ((x - cx) * mapdraw.SC + mapdraw.W / 2, (y - cy) * mapdraw.SC + mapdraw.W / 2)
            got = cv.drawn["circles"].get(h["label"])
            if not got or abs(got[0] - want[0]) > 2 or abs(got[1] - want[1]) > 2:
                bad.append(h["label"])
    res.append({"what": "Each marker sits on its recorded location", "ok": not bad, "detail": "all within 2 px" if not bad else "misplaced: " + ", ".join(bad)})
    res.append({"what": "No boxes overlap", "ok": cv.drawn["overlaps"] == 0, "detail": "none overlap" if not cv.drawn["overlaps"] else f"{cv.drawn['overlaps']} box(es) had no free space"})
    return res


def series_checks(specs, hz, profiles) -> list:
    out = []
    for rn, (thin, total, feats) in profiles.items():
        fr = sorted([s for s in specs if s["kind"] == "series" and s["routes"] == [rn]], key=lambda s: s["k"])
        if not fr:
            continue
        gap = [f"{a['id']}-{b['id']}" for a, b in zip(fr, fr[1:]) if b["a"] > a["b"] + 1]
        out.append({"what": f"{rn.capitalize()}: the maps cover the whole route from start to end in order", "ok": fr[0]["a"] <= 1 and fr[-1]["b"] >= total - 1 and not gap,
                    "detail": f"{len(fr)} maps, km 0 to {total / 1000:.1f}" + (f"; gaps between {', '.join(gap)}" if gap else "")})
        missing = [f["label"] for f in feats if f["type"] == "hazard" and not any(s["a"] - MARGIN <= f["off"] <= s["b"] + MARGIN and _in(s["viewport"], f["lat"], f["lon"])
                                                                               and s["zoom"] >= min(f["zr"] + fr[0].get("bias", 0), 17) for s in fr)]
        out.append({"what": f"{rn.capitalize()}: every danger area appears on at least one map", "ok": not missing, "detail": "all shown" if not missing else "missing: " + ", ".join(missing)})
        bias = fr[0].get("bias", 0)
        weak = [f.get("label") or f.get("text", "turn") for f in feats if f["zr"] > 0 and not any(_in(s["viewport"], f["lat"], f["lon"]) and s["zoom"] >= min(f["zr"] + bias, 17) for s in fr)]
        out.append({"what": f"{rn.capitalize()}: tricky turns and danger areas are shown close enough", "ok": not weak, "detail": "all at the zoom they need" if not weak else f"too far out: {', '.join(str(w)[:30] for w in weak[:6])}"})
    return out


def generate(run, selected, detail=0) -> None:
    routes, hz, stops = data(run)
    specs = plan(run, detail)
    profiles = {rn: profile(rn, routes, hz) for rn in ("primary", "secondary") if rn in routes}
    chosen = [s for s in specs if s["compulsory"] or s["id"] in selected]
    items = []
    for s in chosen:
        png, cv, mine = render(run, s, routes, hz, stops, profiles)
        checks = verify(s, cv, mine, stops)
        fname = f"{s['id']}.png"
        store.save_blob(f"maps/{run['id']}/{fname}", png)
        inside = [h for h in mine if _in(s["viewport"], h["lat"], h["lon"])]
        info = [{"label": h["label"], "cats": [stage2.best_details(h, 3)[i].get("cat") for i in range(len(stage2.best_details(h, 3)))], "action": h.get("action", ""),
                 "coords": geo.coords(h["lat"], h["lon"])} for h in sorted(inside, key=lambda h: int(h["label"][1:]))]
        items.append({"id": s["id"], "title": s["title"], "kind": s["kind"], "file": fname, "checks": checks, "all_ok": all(c["ok"] for c in checks),
                      "coverage": s["coverage"], "why": s["why"], "hazards": info, "link": view_link(s["center"], s["zoom"])})
    run["maps"] = {"version": run["version"], "at": stage2.now(), "detail": detail, "items": items, "series_checks": series_checks(chosen, hz, profiles),
                   "selected": [s["id"] for s in chosen]}
    run["gates"]["6"] = "pending"


def make_preview(run) -> None:
    """Stage 1 picture of both routes so you can see what is going on from the start. No danger areas."""
    names = [n for n in ("primary", "secondary") if run["routes"].get(n)]
    datas = {n: run["routes"][n]["data"] for n in names}
    pts = [(p[0], p[1]) for n in names for p in mapdraw.route_pts(datas[n])]
    center, z = mapdraw.fit(pts)
    cv = mapdraw.Canvas(mapdraw.fetch_base(center, z), center, z)
    for rn in [n for n in ("secondary", "primary") if n in names]:
        cv.line(mapdraw.route_pts(datas[rn]), COL[rn], 8 if len(names) == 2 else 9)
    cv.flush_lines()
    first = mapdraw.route_pts(datas[names[0]])
    cv.add_marker(first[0][0], first[0][1], "START", GREEN, "ends", 30)
    cv.add_marker(first[-1][0], first[-1][1], "END", mapdraw.DARK, "ends", 30)
    cv.draw_markers()
    store.save_blob(f"maps/{run['id']}/preview.png", cv.finish("Route preview"))
    run["preview"] = True


def gmaps_link(run, rn="primary") -> str:
    """Opens one of the routes in Google Maps, held to the route with waypoints. Google may still pick a slightly different road."""
    d = run["routes"][rn]["data"]["geom"]
    o, dest = (run["dest"], run["origin"]) if run["direction"] == "return" else (run["origin"], run["dest"])
    idx = sorted({int(len(d) * k / 10) for k in range(1, 10)})
    wp = "%7C".join(f"{d[i][0]:.5f},{d[i][1]:.5f}" for i in idx if 0 < i < len(d) - 1)
    return (f"https://www.google.com/maps/dir/?api=1&origin={o['lat']:.6f},{o['lon']:.6f}&destination={dest['lat']:.6f},{dest['lon']:.6f}"
            f"&travelmode=driving&waypoints={wp}")


def gmaps_links(run) -> str:
    out = [f"<a href='{gmaps_link(run, 'primary')}' target=_blank>Open the primary route in Google Maps</a>"]
    if run["routes"].get("secondary"):
        out.append(f"<a href='{gmaps_link(run, 'secondary')}' target=_blank>Open the secondary route in Google Maps</a>")
    return " | ".join(out) + " <span class=muted>(approximate: Google may pick a slightly different road)</span>"

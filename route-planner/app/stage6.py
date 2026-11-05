"""Stage 6: propose a map plan, draw the approved maps, and check them against the frozen route record."""
from . import geo, mapdraw, stage2, stage4, store

COL = {"primary": mapdraw.BLUE, "secondary": mapdraw.RED}
GREEN = (20, 130, 60)
CLUSTER_PX = 120


def ready_state(run) -> list:
    rid, g, fr = run["id"], run["gates"], run.get("frozen")
    return [("Gates 1 to 4 approved", all(g.get(k) == "approved" for k in "123") and stage4.gate4_ok(run), f"/run/{rid}/stage4"),
            ("Driver reference frozen and approved (Gate 5)", bool(fr) and fr["version"] == run["version"] and g.get("5") == "approved", f"/run/{rid}/stage5")]


def data(run):
    routes, hz_all = stage4.final_view(run)
    lm = stage2.labels(hz_all or [], routes)  # numbered D1, D2... in the order the truck meets them
    hz = [dict(h, label=lm[h["id"]]) for h in (hz_all or []) if h["id"] in lm]
    stops = []
    for rn in ("primary", "secondary"):
        st = (run["routes"].get(rn) or {}).get("stopped")
        for s in (st["stops"] if st else []):
            stops.append({"rid": s.get("rid", "R?"), "name": s["name"], "lat": s["lat"], "lon": s["lon"], "route": rn})
    return routes, hz, stops


def _pts(names, routes, hz, stops):
    p = [(g[0], g[1]) for rn in names for g in mapdraw.route_pts(routes[rn])]
    p += [(h["lat"], h["lon"]) for h in hz if set(h["routes"]) & set(names)]
    p += [(s["lat"], s["lon"]) for s in stops if s["route"] in names]
    return p


def _spec(id_, title, kind, names, pts, purpose, coverage, why, compulsory, default, zmax=18, pad=70, **extra):
    center, z = mapdraw.fit(pts, pad_css=pad, zmax=zmax)
    return {"id": id_, "title": title, "kind": kind, "routes": names, "purpose": purpose, "coverage": f"{coverage} (zoom {z})", "why": why,
            "compulsory": compulsory, "default": default, "center": list(center), "zoom": z, "viewport": list(mapdraw.viewport(center, z)), **extra}


def _px(center, z, lat, lon):
    cx, cy = mapdraw.proj(center[0], center[1], z)
    x, y = mapdraw.proj(lat, lon, z)
    return (x - cx) * mapdraw.SC + mapdraw.W / 2, (y - cy) * mapdraw.SC + mapdraw.W / 2


def _offset(lat, dist_m):
    return dist_m / 111320.0, dist_m / (111320.0 * max(abs(__import__("math").cos(__import__("math").radians(lat))), 0.2))


def plan(run) -> list:
    routes, hz, stops = data(run)
    names = [n for n in ("primary", "secondary") if n in routes]
    specs = []
    both = _pts(names, routes, hz, stops)
    if len(names) == 2:
        specs.append(_spec("M1", "Combined overview", "overview", names, both, "One picture of both routes together with every danger area and rest stop.",
                           "Whole trip, both routes", "Gives the overall picture and shows where the routes share roads.", True, True))
    for i, rn in enumerate(names):
        specs.append(_spec(f"M{i + 2}", f"{rn.capitalize()} route overview", "overview", [rn], _pts([rn], routes, hz, stops),
                           f"The {rn} route only, with its danger areas and rest stops.", f"Whole {rn} route",
                           "Separate from the combined map so each route can be read on its own." if len(names) == 2 else "The only route, shown on its own.", True, True))
    ov = specs[0]
    items = [{"id": h["label"], "lat": h["lat"], "lon": h["lon"]} for h in hz] + [{"id": s["rid"], "lat": s["lat"], "lon": s["lon"]} for s in stops]
    clusters = []
    for it in items:
        p = _px(ov["center"], ov["zoom"], it["lat"], it["lon"])
        tgt = next((c for c in clusters if any(((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5 < CLUSTER_PX for q in c["px"])), None)
        if tgt:
            tgt["items"].append(it)
            tgt["px"].append(p)
        else:
            clusters.append({"items": [it], "px": [p]})
    z_n = 0
    for c in clusters:
        if len(c["items"]) < 2:
            continue
        z_n += 1
        pts = []
        for it in c["items"]:
            dla, dlo = _offset(it["lat"], 300)
            pts += [(it["lat"] + dla, it["lon"] + dlo), (it["lat"] - dla, it["lon"] - dlo)]
        ids = ", ".join(i["id"] for i in c["items"])
        specs.append(_spec(f"Z{z_n}", f"Zoom {z_n}: {ids}", "zoom", names, pts, f"Close up of {ids}, which sit too close together to read on the overview.",
                           "A small area around these labels", "Optional: these labels overlap on the overview. A zoom map is its own separate map.", False, False, zmax=17, pad=60))
    if hz:
        specs.append(_spec("DA", "Danger area map", "danger", names, [(h["lat"], h["lon"]) for h in hz] + [(g[0], g[1]) for rn in names for g in routes[rn]["geom"][:1]],
                           "Every danger area on one map with the routes thinned out.", "Area covering all danger areas", "Optional: the overviews already show these, so only add it if you want one dedicated danger map.", False, False))
    for s in stops:
        near = [(g[0], g[1]) for g in routes[s["route"]]["geom"] if geo.hav(s["lat"], s["lon"], g[0], g[1]) <= 900]
        specs.append(_spec(f"S_{s['rid']}", f"Rest stop {s['rid']}: access and exit", "stop", [s["route"]], near + [(s["lat"], s["lon"])],
                           f"How to get into and out of {s['name']} and back onto the route.", "The stop and about 900 m of road around it",
                           "Compulsory whenever a rest stop is planned.", True, True, zmax=17, pad=90, stop=s["rid"]))
    return specs


def make_preview(run) -> None:
    """Stage 1 picture of both routes so you can see what is going on from the start. Not approved, no danger areas."""
    names = [n for n in ("primary", "secondary") if run["routes"].get(n)]
    datas = {n: run["routes"][n]["data"] for n in names}
    pts = [(p[0], p[1]) for n in names for p in mapdraw.route_pts(datas[n])]
    center, z = mapdraw.fit(pts)
    cv = mapdraw.Canvas(mapdraw.fetch_base(center, z), center, z)
    for rn in [n for n in ("secondary", "primary") if n in names]:
        cv.chevrons(cv.line(mapdraw.route_pts(datas[rn]), COL[rn], 13 if len(names) == 1 else 11))
    cv.flush_lines()
    first = mapdraw.route_pts(datas[names[0]])
    cv.dot(first[0][0], first[0][1], GREEN, "START")
    cv.dot(first[-1][0], first[-1][1], mapdraw.DARK, "END")
    store.save_blob(f"maps/{run['id']}/preview.png", cv.finish("Route preview (blue primary, red secondary)"))
    run["preview"] = True


def gmaps_link(run) -> str:
    """Opens the trip in Google Maps, nudged along the primary route with a few waypoints. Google may still choose a slightly different road."""
    d = run["routes"]["primary"]["data"]["geom"]
    o, dest = (run["dest"], run["origin"]) if run["direction"] == "return" else (run["origin"], run["dest"])
    idx = [int(len(d) * f) for f in (0.14, 0.28, 0.42, 0.56, 0.70, 0.84)]
    wp = "%7C".join(f"{d[i][0]:.5f},{d[i][1]:.5f}" for i in idx if 0 < i < len(d) - 1)
    return (f"https://www.google.com/maps/dir/?api=1&origin={o['lat']:.6f},{o['lon']:.6f}&destination={dest['lat']:.6f},{dest['lon']:.6f}"
            f"&travelmode=driving&waypoints={wp}")


def _in(vp, lat, lon) -> bool:
    return vp[0] <= lat <= vp[2] and vp[1] <= lon <= vp[3]


def render(run, s, routes, hz, stops):
    base = mapdraw.fetch_base(s["center"], s["zoom"])
    cv = mapdraw.Canvas(base, tuple(s["center"]), s["zoom"])
    names = s["routes"]
    for rn in [n for n in ("secondary", "primary") if n in names]:
        pts = cv.line(mapdraw.route_pts(routes[rn]), COL[rn], 13 if len(names) == 1 else 11)
        cv.chevrons(pts)
        cv.drawn["routes"].append(rn)
    cv.flush_lines()
    first = mapdraw.route_pts(routes[names[0]])
    cv.dot(first[0][0], first[0][1], GREEN, "START")
    cv.dot(first[-1][0], first[-1][1], mapdraw.DARK, "END")
    if s["kind"] in ("overview", "danger"):
        for rn in names:
            r = routes[rn]
            cv.road_labels(r["geom"], r["rows"], r["steps"], COL[rn])
    mine = [h for h in hz if set(h["routes"]) & set(names)]
    for h in mine:
        cv.circle(h["lat"], h["lon"], mapdraw.RED, h["label"], kind="hazards")
    for st in stops:
        if st["route"] in names:
            cv.circle(st["lat"], st["lon"], mapdraw.ORANGE, st["rid"], radius=40, size=40, kind="stops")
    return cv.finish(s["title"]), cv, mine


def verify(s, cv, mine, stops) -> list:
    vp, res = s["viewport"], []
    exp_h = sorted(h["label"] for h in mine if _in(vp, h["lat"], h["lon"]))
    got_h = sorted(cv.drawn["hazards"])
    res.append({"what": "Danger areas drawn match the frozen list", "ok": exp_h == got_h, "detail": f"expected {exp_h or 'none'}, drawn {got_h or 'none'}"})
    exp_s = sorted(x["rid"] for x in stops if x["route"] in s["routes"] and _in(vp, x["lat"], x["lon"]))
    got_s = sorted(cv.drawn["stops"])
    res.append({"what": "Rest stops drawn match the frozen list", "ok": exp_s == got_s, "detail": f"expected {exp_s or 'none'}, drawn {got_s or 'none'}"})
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
    res.append({"what": "Each circle sits on its recorded location", "ok": not bad, "detail": "all within 2 px" if not bad else "misplaced: " + ", ".join(bad)})
    return res


def generate(run, selected) -> None:
    routes, hz, stops = data(run)
    specs = {s["id"]: s for s in plan(run)}
    chosen = [s for s in specs.values() if s["compulsory"] or s["id"] in selected]
    items = []
    for s in chosen:
        png, cv, mine = render(run, s, routes, hz, stops)
        checks = verify(s, cv, mine, stops)
        fname = f"{s['id']}.png"
        store.save_blob(f"maps/{run['id']}/{fname}", png)
        items.append({"id": s["id"], "title": s["title"], "kind": s["kind"], "file": fname, "checks": checks, "all_ok": all(c["ok"] for c in checks)})
    run["maps"] = {"version": run["version"], "at": stage2.now(), "items": items, "selected": [s["id"] for s in chosen]}
    run["gates"]["6"] = "pending"

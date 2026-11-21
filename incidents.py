"""TomTom live and planned roadworks / closures near the routes (needs Traffic API ticked on the key)."""
import os

import httpx

from . import geo

URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"
FIELDS = "{incidents{type,geometry{type,coordinates},properties{id,iconCategory,events{description,iconCategory},startTime,endTime,from,to,roadNumbers,timeValidity}}}"
CAT = {9: 1, 7: 2, 8: 2}  # TomTom roadworks -> checklist 1, lane closed -> 2, road closed -> 2


def fetch(bbox):
    """bbox = (min_lon, min_lat, max_lon, max_lat). Returns (incidents, error_text)."""
    key = os.environ.get("TOMTOM_KEY")
    if not key:
        return [], "TOMTOM_KEY is not set"
    try:
        r = httpx.get(URL, params={"key": key, "bbox": ",".join(f"{x:.5f}" for x in bbox), "fields": FIELDS, "language": "en-GB",
                                   "categoryFilter": "7,8,9", "timeValidityFilter": "present,future"}, timeout=60)
    except httpx.HTTPError as e:
        return [], f"Could not reach TomTom traffic: {e}"
    if r.status_code != 200:
        hint = " (tick Traffic API on your TomTom key)" if r.status_code in (401, 403) else ""
        return [], f"TomTom traffic error {r.status_code}{hint}"
    out = []
    for inc in r.json().get("incidents", []):
        p, g = inc.get("properties", {}), inc.get("geometry", {})
        coords = g.get("coordinates") or []
        pts = [coords] if g.get("type") == "Point" else coords
        pts = [[c[1], c[0]] for c in pts if isinstance(c, list) and len(c) >= 2 and isinstance(c[0], (int, float))]
        if len(pts) > 60:
            pts = pts[::max(len(pts) // 60, 1)]
        cat = CAT.get(p.get("iconCategory"))
        if cat and pts:
            ev = (p.get("events") or [{}])[0].get("description", "")
            out.append({"cat": cat, "pts": pts, "text": ev or "Roadworks", "from": p.get("from", ""), "to": p.get("to", ""),
                        "start": p.get("startTime", ""), "end": p.get("endTime", ""), "when": p.get("timeValidity", ""),
                        "roads": p.get("roadNumbers", [])})
    return out, ""


def near(items, geom, max_m=40):
    """Incidents with a point within max_m of the route geometry -> [(incident, lat, lon, geom_index)]."""
    hits = []
    for it in items:
        best = None
        for la, lo in it["pts"]:
            dist, i = geo.nearest(la, lo, geom)
            if dist <= max_m and (best is None or dist < best[0]):
                best = (dist, la, lo, i)
        if best:
            hits.append((it, best[1], best[2], best[3]))
    return hits

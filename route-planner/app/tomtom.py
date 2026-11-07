"""TomTom geocoding and routing (REST). Keep parsing tolerant: raw JSON is also saved."""
import math
import os
from urllib.parse import quote

import httpx

from . import geo

BASE = "https://api.tomtom.com"


class TomTomError(Exception):
    pass


def _key() -> str:
    key = os.environ.get("TOMTOM_KEY")
    if not key:
        raise TomTomError("TOMTOM_KEY is not set")
    return key


def _get(path: str, params: list) -> dict:
    try:
        r = httpx.get(BASE + path, params=params, timeout=60)
    except httpx.HTTPError as e:
        raise TomTomError(f"Could not reach TomTom: {e}") from e
    if r.status_code != 200:
        raise TomTomError(f"TomTom error {r.status_code}: {r.text[:300]}")
    return r.json()


def parse_coords(text: str):
    """Accepts 'lat, lon' pasted from Google Maps. Returns (lat, lon) or None."""
    parts = text.replace(";", ",").replace("(", "").replace(")", "").split(",")
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        if 1.0 < lat < 1.6 and 103.5 < lon < 104.2:
            return lat, lon
    return None


def geocode(text: str) -> dict:
    xy = parse_coords(text)
    if xy:
        return {"text": text, "label": f"Coordinates {xy[0]:.5f}, {xy[1]:.5f} (entered by you)", "lat": xy[0], "lon": xy[1]}
    data = _get(f"/search/2/search/{quote(text)}.json",
                [("key", _key()), ("countrySet", "SG"), ("limit", 5), ("typeahead", "false")])
    results = data.get("results") or []
    if not results:
        raise TomTomError(f"No match found for: {text}")
    top = results[0]
    others, seen = [], {(round(top["position"]["lat"], 4), round(top["position"]["lon"], 4))}
    for r in results[1:]:
        k = (round(r["position"]["lat"], 4), round(r["position"]["lon"], 4))
        if k not in seen:
            seen.add(k)
            poi = (r.get("poi") or {}).get("name", "")
            lab = r.get("address", {}).get("freeformAddress", "")
            others.append({"label": f"{poi}, {lab}".strip(", ") or text, "lat": r["position"]["lat"], "lon": r["position"]["lon"]})
    return {"text": text, "label": top.get("address", {}).get("freeformAddress", text), "lat": top["position"]["lat"], "lon": top["position"]["lon"],
            "others": others[:3], "matched_name": (top.get("poi") or {}).get("name", "")}


def calculate(points: list, alternatives: int = 0, route_type: str = "fastest", heading=None) -> dict:
    try:
        return _calculate(points, alternatives, route_type, heading, True)
    except TomTomError as e:
        if "error 400" in str(e):  # lane data not offered: plan without it
            return _calculate(points, alternatives, route_type, heading, False)
        raise


def _calculate(points: list, alternatives, route_type, heading, lanes) -> dict:
    """Route through points (dicts with lat, lon). Tunnels avoided, no live traffic."""
    loc = ":".join(f"{p['lat']},{p['lon']}" for p in points)
    params = [("key", _key()), ("travelMode", "car"), ("routeType", route_type),
              ("traffic", "false"), ("instructionsType", "text"), ("language", "en-GB"),
              ("sectionType", "tunnel"), ("avoid", "tunnels")]
    if lanes:
        params.append(("sectionType", "lanes"))
    if heading is not None:
        params.append(("vehicleHeading", int(heading) % 360))
    if alternatives and len(points) == 2:
        params.append(("maxAlternatives", alternatives))
    return _get(f"/routing/1/calculateRoute/{loc}/json", params)


def _label(ins: dict) -> str:
    street = (ins.get("street") or "").strip()
    nums = ", ".join(ins.get("roadNumbers") or [])
    if street and nums:
        return f"{street} ({nums})"
    return street or nums or "(unnamed road)"


def simplify(pts, tol_m=0.8):
    """Douglas-Peucker on (lat, lon) points; keeps every real bend, drops only points within tol_m of a straight line."""
    if len(pts) < 3:
        return [list(p) for p in pts]
    lat0 = pts[0][0]
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
    xy = [((p[1] - pts[0][1]) * kx, (p[0] - lat0) * ky) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay, bx, by = xy[a][0], xy[a][1], xy[b][0], xy[b][1]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy) or 1e-9
        best, bi = -1.0, a
        for i in range(a + 1, b):
            d = abs(dy * (xy[i][0] - ax) - dx * (xy[i][1] - ay)) / norm if norm > 1e-6 else math.hypot(xy[i][0] - ax, xy[i][1] - ay)
            if d > best:
                best, bi = d, i
        if best > tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [[round(p[0], 6), round(p[1], 6)] for p, k in zip(pts, keep) if k]


def parse_route(route: dict) -> dict:
    """One TomTom route -> totals, merged road rows, raw steps (with camera view) and geometry."""
    total_m = route["summary"]["lengthInMeters"]
    total_s = route["summary"]["travelTimeInSeconds"]
    ins = route.get("guidance", {}).get("instructions", [])
    tunnel = any(str(s.get("sectionType", "")).upper() == "TUNNEL" for s in route.get("sections", []))
    pts = []
    for leg in route.get("legs", []):
        pts += [(p["latitude"], p["longitude"]) for p in leg.get("points", [])]
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + geo.hav(a[0], a[1], b[0], b[1]))
    geom, last = [], None
    for i, p in enumerate(pts):
        if last is None or cum[i] - last >= 20 or i == len(pts) - 1:
            geom.append([round(p[0], 5), round(p[1], 5), int(cum[i])])
            last = cum[i]
    lane_secs = []
    for sec in route.get("sections", []):
        if str(sec.get("sectionType", "")).upper() == "LANES" and sec.get("lanes"):
            lane_secs.append({"end": sec.get("endPointIndex", -1), "lanes": sec["lanes"]})
    rows, steps = [], []
    for i, step in enumerate(ins):
        nxt = ins[i + 1] if i + 1 < len(ins) else None
        end_m = nxt["routeOffsetInMeters"] if nxt else total_m
        end_s = nxt["travelTimeInSeconds"] if nxt else total_s
        dist = max(end_m - step["routeOffsetInMeters"], 0)
        secs = max(end_s - step.get("travelTimeInSeconds", 0), 0)
        msg = step.get("message") or step.get("instructionType", "")
        man = str(step.get("maneuver", ""))
        uturn = "UTURN" in man.upper() or "u-turn" in msg.lower()
        label = _label(step)
        if rows and rows[-1]["road"] == label and not uturn:
            rows[-1]["dist_m"] += dist
            rows[-1]["secs"] += secs
            rows[-1]["steps"] += 1
        else:
            rows.append({"road": label, "action": msg, "dist_m": dist, "secs": secs, "steps": 1,
                         "uturn": uturn, "step": i})
        pt = step.get("point", {})
        lat, lon = pt.get("latitude"), pt.get("longitude")
        cam_lat, cam_lon, head = lat, lon, None
        pi = step.get("pointIndex")
        if pts and isinstance(pi, int) and 0 <= pi < len(pts):
            lat, lon = pts[pi]
            j = pi
            while j > 0 and cum[pi] - cum[j] < 35:
                j -= 1
            cam_lat, cam_lon = pts[j]
            if geo.hav(cam_lat, cam_lon, lat, lon) >= 10:
                head = geo.bearing(cam_lat, cam_lon, lat, lon)
            else:
                k = min(pi + 5, len(pts) - 1)
                head = geo.bearing(lat, lon, pts[k][0], pts[k][1]) if k > pi else None
        lanes_info = None
        if lane_secs and isinstance(pi, int):
            near = min(lane_secs, key=lambda x: abs(x["end"] - pi))
            if abs(near["end"] - pi) <= 25:
                ln = near["lanes"]
                lanes_info = {"count": len(ln), "follow": [k + 1 for k, l in enumerate(ln) if l.get("follow")],
                              "dirs": [l.get("directions", []) for l in ln]}
        steps.append({"lanes": lanes_info, "n": i, "off": step["routeOffsetInMeters"], "road": label, "msg": msg, "maneuver": man,
                      "junction": str(step.get("junctionType", "")), "angle": step.get("turnAngleInDecimalDegrees"),
                      "lat": lat, "lon": lon, "cam_lat": cam_lat, "cam_lon": cam_lon,
                      "heading": round(head) if head is not None else 0, "row": len(rows) - 1, "uturn": uturn,
                      "dist_m": dist, "secs": secs})
    return {"total_m": total_m, "total_s": total_s, "tunnel": tunnel, "rows": rows, "steps": steps, "geom": geom, "draw": simplify(pts)}

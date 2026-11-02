"""TomTom geocoding and routing (REST). Keep parsing tolerant: raw JSON is also saved."""
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
                [("key", _key()), ("countrySet", "SG"), ("limit", 1), ("typeahead", "false")])
    results = data.get("results") or []
    if not results:
        raise TomTomError(f"No match found for: {text}")
    top = results[0]
    return {"text": text, "label": top.get("address", {}).get("freeformAddress", text),
            "lat": top["position"]["lat"], "lon": top["position"]["lon"]}


def calculate(points: list, alternatives: int = 0, route_type: str = "fastest", heading=None) -> dict:
    """Route through points (dicts with lat, lon). Tunnels avoided, no live traffic."""
    loc = ":".join(f"{p['lat']},{p['lon']}" for p in points)
    params = [("key", _key()), ("travelMode", "car"), ("routeType", route_type),
              ("traffic", "false"), ("instructionsType", "text"), ("language", "en-GB"),
              ("sectionType", "tunnel"), ("avoid", "tunnels")]
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
        steps.append({"n": i, "off": step["routeOffsetInMeters"], "road": label, "msg": msg, "maneuver": man,
                      "junction": str(step.get("junctionType", "")), "angle": step.get("turnAngleInDecimalDegrees"),
                      "lat": lat, "lon": lon, "cam_lat": cam_lat, "cam_lon": cam_lon,
                      "heading": round(head) if head is not None else 0, "row": len(rows) - 1, "uturn": uturn})
    return {"total_m": total_m, "total_s": total_s, "tunnel": tunnel, "rows": rows, "steps": steps, "geom": geom}

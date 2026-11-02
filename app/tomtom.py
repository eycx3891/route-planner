"""TomTom geocoding and routing (REST). Keep parsing tolerant: raw JSON is also saved."""
import os
from urllib.parse import quote

import httpx

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


def geocode(text: str) -> dict:
    data = _get(f"/search/2/search/{quote(text)}.json",
                [("key", _key()), ("countrySet", "SG"), ("limit", 1), ("typeahead", "false")])
    results = data.get("results") or []
    if not results:
        raise TomTomError(f"No match found for: {text}")
    top = results[0]
    return {"text": text, "label": top.get("address", {}).get("freeformAddress", text),
            "lat": top["position"]["lat"], "lon": top["position"]["lon"]}


def calculate(points: list, alternatives: int = 0) -> dict:
    """Route through points (dicts with lat, lon). Tunnels avoided, no live traffic."""
    loc = ":".join(f"{p['lat']},{p['lon']}" for p in points)
    params = [("key", _key()), ("travelMode", "car"), ("routeType", "fastest"),
              ("traffic", "false"), ("instructionsType", "text"), ("language", "en-GB"),
              ("sectionType", "tunnel"), ("avoid", "tunnels")]
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
    """Turn one TomTom route into totals plus road by road rows (merged by road)."""
    total_m = route["summary"]["lengthInMeters"]
    total_s = route["summary"]["travelTimeInSeconds"]
    ins = route.get("guidance", {}).get("instructions", [])
    tunnel = any(str(s.get("sectionType", "")).upper() == "TUNNEL" for s in route.get("sections", []))
    rows = []
    for i, step in enumerate(ins):
        nxt = ins[i + 1] if i + 1 < len(ins) else None
        end_m = nxt["routeOffsetInMeters"] if nxt else total_m
        end_s = nxt["travelTimeInSeconds"] if nxt else total_s
        dist = max(end_m - step["routeOffsetInMeters"], 0)
        secs = max(end_s - step.get("travelTimeInSeconds", 0), 0)
        msg = step.get("message") or step.get("instructionType", "")
        uturn = "UTURN" in str(step.get("maneuver", "")).upper() or "u-turn" in msg.lower()
        label = _label(step)
        if rows and rows[-1]["road"] == label and not uturn:
            rows[-1]["dist_m"] += dist
            rows[-1]["secs"] += secs
            rows[-1]["steps"] += 1
        else:
            rows.append({"road": label, "action": msg, "dist_m": dist, "secs": secs,
                         "steps": 1, "uturn": uturn,
                         "lat": step.get("point", {}).get("latitude"),
                         "lon": step.get("point", {}).get("longitude")})
    return {"total_m": total_m, "total_s": total_s, "tunnel": tunnel, "rows": rows}

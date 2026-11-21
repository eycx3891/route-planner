"""Google Places API (New): find candidate rest stops near a point."""
import os

import httpx

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = "places.id,places.displayName,places.formattedAddress,places.location,places.types,places.googleMapsUri"


class PlacesError(Exception):
    pass


def search(query: str, lat: float, lon: float, radius: int = 3000, n: int = 10) -> list:
    key = os.environ.get("GOOGLE_KEY")
    if not key:
        raise PlacesError("GOOGLE_KEY is not set")
    body = {"textQuery": query, "maxResultCount": n,
            "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": float(radius)}}}
    try:
        r = httpx.post(URL, json=body, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS}, timeout=30)
    except httpx.HTTPError as e:
        raise PlacesError(f"Could not reach Google Places: {e}") from e
    if r.status_code != 200:
        msg = ""
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text[:200]
        raise PlacesError(f"Places API error {r.status_code}: {msg} (enable Places API (New) and allow it on your key)")
    out = []
    for p in r.json().get("places", []):
        loc = p.get("location") or {}
        if "latitude" in loc:
            out.append({"place_id": p.get("id", ""), "name": (p.get("displayName") or {}).get("text", "Unnamed place"),
                        "address": p.get("formattedAddress", ""), "lat": loc["latitude"], "lon": loc["longitude"],
                        "types": p.get("types", []), "url": p.get("googleMapsUri", "")})
    return out

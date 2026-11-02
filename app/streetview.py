"""Google Street View Static API: metadata (free, gives capture date) and images."""
import os

import httpx

META = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMG = "https://maps.googleapis.com/maps/api/streetview"


class StreetViewError(Exception):
    pass


def _key() -> str:
    k = os.environ.get("GOOGLE_KEY")
    if not k:
        raise StreetViewError("GOOGLE_KEY is not set")
    return k


def metadata(lat: float, lon: float) -> dict:
    r = httpx.get(META, params={"location": f"{lat},{lon}", "source": "outdoor", "radius": 40, "key": _key()}, timeout=30)
    if r.status_code != 200:
        raise StreetViewError(f"Street View metadata error {r.status_code}: {r.text[:200]}")
    return r.json()  # status, date (YYYY-MM), pano_id, location


def image(pano_id: str, heading: float) -> bytes:
    r = httpx.get(IMG, params={"size": "640x400", "pano": pano_id, "heading": int(heading), "fov": 90,
                               "pitch": 0, "key": _key()}, timeout=30)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        raise StreetViewError(f"Street View image error {r.status_code}: {r.text[:200]}")
    return r.content

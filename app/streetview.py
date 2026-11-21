"""Google Street View Static API: metadata (free, gives capture date) and images."""
import os
import time

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


RETRY_STATUS = {"UNKNOWN_ERROR"}  # Google's own temporary fault. Not a key problem (a bad key gives REQUEST_DENIED).


def metadata(lat: float, lon: float) -> dict:
    """Street View metadata. Retries temporary faults (UNKNOWN_ERROR, 429, 5xx, network) with a growing pause."""
    last = ""
    for attempt in range(4):
        if attempt:
            time.sleep(1.5 * 2 ** (attempt - 1))  # 1.5 s, 3 s, 6 s
        try:
            r = httpx.get(META, params={"location": f"{lat},{lon}", "source": "outdoor", "radius": 60, "key": _key()}, timeout=30)
        except httpx.HTTPError as e:
            last = f"network: {e}"
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {r.status_code}"
            continue
        if r.status_code != 200:
            raise StreetViewError(f"Street View metadata error {r.status_code}: {r.text[:200]}")
        m = r.json()  # status, date (YYYY-MM), pano_id, location
        if m.get("status") in RETRY_STATUS:
            last = m.get("status", "")
            continue
        return m
    return {"status": "UNKNOWN_ERROR", "error_message": f"Google kept failing after 4 tries ({last})"}


def image(pano_id: str, heading: float, fov: int = 90) -> bytes:
    last = ""
    for attempt in range(3):
        if attempt:
            time.sleep(1.5 * 2 ** (attempt - 1))
        try:
            r = httpx.get(IMG, params={"size": "640x400", "pano": pano_id, "heading": int(heading), "fov": fov,
                                       "pitch": 0, "key": _key()}, timeout=30)
        except httpx.HTTPError as e:
            last = f"network: {e}"
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {r.status_code}"
            continue
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            raise StreetViewError(f"Street View image error {r.status_code}: {r.text[:200]}")
        return r.content
    raise StreetViewError(f"Street View image kept failing ({last})")

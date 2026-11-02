"""Small geometry helpers and Google Maps links."""
import math

R = 6371000.0


def hav(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def nearest(lat, lon, geom):
    """geom: list of [lat, lon, offset_m]. Returns (distance_m, index)."""
    best, bi = 1e18, -1
    for i, p in enumerate(geom):
        d = hav(lat, lon, p[0], p[1])
        if d < best:
            best, bi = d, i
    return best, bi


def sv_link(lat, lon, heading=0) -> str:
    return (f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat:.6f},{lon:.6f}"
            f"&heading={int(heading)}&pitch=0&fov=90")


def map_link(lat, lon) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def destination(lat, lon, bearing_deg, dist_m):
    """Point reached from (lat, lon) after dist_m metres on the given bearing."""
    br, d = math.radians(bearing_deg), dist_m / R
    la, lo = math.radians(lat), math.radians(lon)
    la2 = math.asin(math.sin(la) * math.cos(d) + math.cos(la) * math.sin(d) * math.cos(br))
    lo2 = lo + math.atan2(math.sin(br) * math.sin(d) * math.cos(la), math.cos(d) - math.sin(la) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


def traffic_link(lat, lon) -> str:
    return f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},17z/data=!5m1!1e1"

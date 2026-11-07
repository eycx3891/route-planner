"""Draws the truck's path onto a Street View image, so the model can see where the truck is and where it goes next."""
import io
import math

from PIL import Image, ImageDraw

from . import geo

CAM_H = 2.6  # metres: typical Street View camera height


def route_ahead(data, lat, lon, meters=170):
    """The next `meters` of the route from the point closest to (lat, lon)."""
    pts = data.get("draw") or [[g[0], g[1]] for g in data["geom"]]
    kx = math.cos(math.radians(lat))
    bi = min(range(len(pts)), key=lambda i: (pts[i][0] - lat) ** 2 + ((pts[i][1] - lon) * kx) ** 2)
    out, acc = [pts[bi]], 0.0
    for a, b in zip(pts[bi:], pts[bi + 1:]):
        acc += geo.hav(a[0], a[1], b[0], b[1])
        out.append(b)
        if acc >= meters:
            break
    return out


def _densify(pts, step=2.0):
    out = [tuple(pts[0])]
    for a, b in zip(pts, pts[1:]):
        d = geo.hav(a[0], a[1], b[0], b[1])
        n = max(int(d / step), 1)
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(1, n + 1)]
    return out


def draw_path(jpeg: bytes, cam, heading, ahead, width_m=2.2, fov=90) -> bytes:
    """Thin yellow line with a black edge along the route. Approximate (flat road, fixed camera height) but it shows which road, exit or turn the truck takes."""
    if not ahead or len(ahead) < 2:
        return jpeg
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    W, H = im.size
    f = (W / 2) / math.tan(math.radians(fov / 2))
    lat0, lon0 = cam
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
    h = math.radians(heading)

    def local(p):
        e, n = (p[1] - lon0) * kx, (p[0] - lat0) * ky
        return e * math.sin(h) + n * math.cos(h), e * math.cos(h) - n * math.sin(h)  # forward, right

    def proj(fwd, right):
        return W / 2 + f * right / fwd, H / 2 + f * CAM_H / fwd

    pts = [local(p) for p in _densify(ahead)]
    xy = [proj(a, r) for a, r in pts if a >= 2.0]
    if len(xy) < 2:
        return jpeg
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    d.line(xy, fill=(0, 0, 0, 230), width=11, joint="curve")        # black edge so it shows on any road colour
    d.line(xy, fill=(255, 214, 0, 255), width=6, joint="curve")     # thin yellow line: lane arrows underneath stay readable
    x2, y2 = xy[-1]
    x1, y1 = xy[max(len(xy) - 6, 0)]
    ang = math.atan2(y2 - y1, x2 - x1)
    tip = (x2 + 16 * math.cos(ang), y2 + 16 * math.sin(ang))
    left = (x2 + 11 * math.cos(ang + 2.4), y2 + 11 * math.sin(ang + 2.4))
    right = (x2 + 11 * math.cos(ang - 2.4), y2 + 11 * math.sin(ang - 2.4))
    d.polygon([tip, left, right], fill=(255, 140, 0, 255), outline=(0, 0, 0, 255))
    out = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=88)
    return buf.getvalue()

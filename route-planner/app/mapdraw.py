"""Draws routes, danger circles, rest stops and labels over a Google Static Map image."""
import hashlib
import io
import math
import os

import httpx
from PIL import Image, ImageDraw, ImageFont

from . import geo, store

CSS, SC = 640, 2
W = CSS * SC
HDR = 92
SS = 2  # lines are drawn at twice the size and shrunk, which removes the jagged edges
BLUE, RED, ORANGE, DARK = (26, 86, 219), (214, 42, 42), (245, 130, 13), (40, 40, 40)


class MapError(Exception):
    pass


def proj(lat, lon, z):
    s = min(max(math.sin(math.radians(lat)), -0.9999), 0.9999)
    n = 256 * 2 ** z
    return n * (0.5 + lon / 360.0), n * (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi))


def unproj(x, y, z):
    n = 256 * 2 ** z
    lon = (x / n - 0.5) * 360.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def fit(points, pad_css=70, zmax=18, zmin=8):
    """Centre and largest zoom at which all points fit inside the 640 css px map with a margin."""
    for z in range(zmax, zmin - 1, -1):
        xs, ys = zip(*[proj(la, lo, z) for la, lo in points])
        if max(xs) - min(xs) <= CSS - 2 * pad_css and max(ys) - min(ys) <= CSS - 2 * pad_css:
            break
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return unproj(cx, cy, z), z


def viewport(center, z):
    cx, cy = proj(center[0], center[1], z)
    a = unproj(cx - CSS / 2, cy + CSS / 2, z)
    b = unproj(cx + CSS / 2, cy - CSS / 2, z)
    return (a[0], a[1], b[0], b[1])  # min lat, min lon, max lat, max lon


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def fetch_base(center, z):
    key = os.environ.get("GOOGLE_KEY")
    if not key:
        raise MapError("GOOGLE_KEY is not set")
    cache = "maps/base/" + hashlib.md5(f"{center[0]:.5f},{center[1]:.5f},{z}".encode()).hexdigest() + ".png"
    hit = store.load_blob(cache)
    if hit:
        return Image.open(io.BytesIO(hit)).convert("RGB")
    params = [("center", f"{center[0]:.6f},{center[1]:.6f}"), ("zoom", z), ("size", f"{CSS}x{CSS}"), ("scale", SC), ("maptype", "roadmap"),
              ("style", "feature:poi|visibility:off"), ("style", "feature:transit|visibility:off"), ("key", key)]
    try:
        r = httpx.get("https://maps.googleapis.com/maps/api/staticmap", params=params, timeout=40)
    except httpx.HTTPError as e:
        raise MapError(f"Could not reach Google Maps: {e}") from e
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        raise MapError(f"Static Maps error {r.status_code}: {r.text[:200]} (enable Maps Static API and allow it on your key)")
    store.save_blob(cache, r.content)
    return Image.open(io.BytesIO(r.content)).convert("RGB")


class Canvas:
    def __init__(self, base, center, z):
        self.im = base.convert("RGBA")
        self.center, self.z = center, z
        self.cx, self.cy = proj(center[0], center[1], z)
        self.d = ImageDraw.Draw(self.im, "RGBA")
        self.layer = Image.new("RGBA", (W * SS, W * SS), (0, 0, 0, 0))
        self.ld = ImageDraw.Draw(self.layer)
        self.placed = []
        self.drawn = {"hazards": [], "stops": [], "insets": [], "routes": [], "circles": {}, "ends": []}

    def px(self, lat, lon):
        x, y = proj(lat, lon, self.z)
        return (x - self.cx) * SC + W / 2, (y - self.cy) * SC + W / 2

    def inside(self, p, m=0):
        return -m <= p[0] <= W + m and -m <= p[1] <= W + m

    def box_free(self, b):
        return all(b[2] < o[0] or b[0] > o[2] or b[3] < o[1] or b[1] > o[3] for o in self.placed)

    def text(self, xy, s, size, fg, bold=False):
        f = font(size)
        for dx in (-3, 0, 3):
            for dy in (-3, 0, 3):
                self.d.text((xy[0] + dx, xy[1] + dy), s, font=f, fill=(255, 255, 255, 255))
        self.d.text(xy, s, font=f, fill=fg + (255,))
        if bold:
            self.d.text((xy[0] + 1, xy[1]), s, font=f, fill=fg + (255,))

    def label(self, anchor, s, size, fg, offsets, bold=True):
        f = font(size)
        bb = self.d.textbbox((0, 0), s, font=f)
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        for dx, dy in offsets:
            x, y = anchor[0] + dx, anchor[1] + dy
            box = (x - 4, y - 4, x + w + 4, y + h + 8)
            if box[0] >= 0 and box[1] >= 0 and box[2] <= W and box[3] <= W and self.box_free(box):
                self.placed.append(box)
                self.text((x, y), s, size, fg, bold)
                return True
        x, y = anchor[0] + offsets[0][0], anchor[1] + offsets[0][1]
        self.placed.append((x - 4, y - 4, x + w + 4, y + h + 8))
        self.text((x, y), s, size, fg, bold)
        return False

    def line(self, pts_latlon, colour, width):
        """Route line with a white edge and round ends, drawn on the large layer for clean anti-aliased edges."""
        pts = [self.px(p[0], p[1]) for p in pts_latlon]
        big = [(x * SS, y * SS) for x, y in pts]
        if len(big) > 1:
            for w, c in ((width + 9, (255, 255, 255, 255)), (width, colour + (255,))):
                self.ld.line(big, fill=c, width=int(w * SS), joint="curve")
                for e in (big[0], big[-1]):
                    r = w * SS / 2
                    self.ld.ellipse([e[0] - r, e[1] - r, e[0] + r, e[1] + r], fill=c)
        return pts

    def chevrons(self, pts, every=420):
        """Small white direction marks on the line."""
        acc = 0.0
        for a, b in zip(pts, pts[1:]):
            seg = math.hypot(b[0] - a[0], b[1] - a[1])
            acc += seg
            if acc >= every and seg > 4 and self.inside(b, -30):
                acc = 0.0
                ang = math.atan2(b[1] - a[1], b[0] - a[0])
                for sgn in (1, -1):
                    tip = (b[0] * SS, b[1] * SS)
                    tail = ((b[0] - 11 * math.cos(ang + sgn * 0.7)) * SS, (b[1] - 11 * math.sin(ang + sgn * 0.7)) * SS)
                    self.ld.line([tail, tip], fill=(255, 255, 255, 255), width=4 * SS)

    def flush_lines(self):
        small = self.layer.resize((W, W), Image.LANCZOS)
        self.im = Image.alpha_composite(self.im, small)
        self.d = ImageDraw.Draw(self.im, "RGBA")
        self.layer = Image.new("RGBA", (W * SS, W * SS), (0, 0, 0, 0))
        self.ld = ImageDraw.Draw(self.layer)

    def circle(self, lat, lon, colour, text, radius=34, size=34, kind="hazards"):
        p = self.px(lat, lon)
        if not self.inside(p):
            return False
        self.d.ellipse([p[0] - radius, p[1] - radius, p[0] + radius, p[1] + radius], outline=colour + (255,), width=7)
        self.placed.append((p[0] - radius, p[1] - radius, p[0] + radius, p[1] + radius))
        self.label(p, text, size, colour, [(radius - 4, -radius - 30), (radius - 4, radius - 6), (-radius - 70, -radius - 30), (-radius - 70, radius - 6), (radius + 10, -10)])
        self.drawn[kind].append(text)
        self.drawn["circles"][text] = p
        return True

    def dot(self, lat, lon, colour, text):
        p = self.px(lat, lon)
        if not self.inside(p):
            return
        self.d.ellipse([p[0] - 16, p[1] - 16, p[0] + 16, p[1] + 16], fill=colour + (255,), outline=(255, 255, 255, 255), width=4)
        self.label(p, text, 30, colour, [(22, -44), (22, 8), (-90, -44)])
        self.drawn["ends"].append(text)

    def rect(self, bbox, text):
        a = self.px(bbox[2], bbox[1])
        b = self.px(bbox[0], bbox[3])
        self.d.rectangle([a[0], a[1], b[0], b[1]], outline=DARK + (255,), width=5)
        self.label((b[0], b[1]), text, 32, DARK, [(-60, 6), (6, 6), (-60, -48)])
        self.drawn["insets"].append(text)

    def road_labels(self, geom, rows, steps, colour, min_m=1500):
        """Label long roads with their name beside the line."""
        offs = [g[2] for g in geom]
        for r in rows:
            if r["dist_m"] < min_m:
                continue
            start = steps[r["step"]]["off"]
            mid = start + r["dist_m"] / 2
            i = min(range(len(geom)), key=lambda k: abs(offs[k] - mid))
            p = self.px(geom[i][0], geom[i][1])
            q = self.px(geom[min(i + 3, len(geom) - 1)][0], geom[min(i + 3, len(geom) - 1)][1])
            ang = math.atan2(q[1] - p[1], q[0] - p[0]) + math.pi / 2
            if not self.inside(p, -40):
                continue
            nx, ny = math.cos(ang), math.sin(ang)
            text = r["road"][:34]
            bb = self.d.textbbox((0, 0), text, font=font(28))
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            ext = abs(nx) * w / 2 + abs(ny) * h / 2 + 26
            offsets = []
            for extra in (0, 60):
                for sg in (1, -1):
                    offsets.append((p[0] + sg * nx * (ext + extra) - w / 2 - p[0], p[1] + sg * ny * (ext + extra) - h / 2 - p[1]))
            self.label(p, text, 28, colour, offsets, bold=False)

    def finish(self, title):
        out = Image.new("RGB", (W, W + HDR), (255, 255, 255))
        out.paste(self.im.convert("RGB"), (0, HDR))
        ImageDraw.Draw(out).text((24, 18), title[:70], font=font(46), fill=(20, 20, 20))
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()


def route_pts(data):
    """The precise drawing line if the run has one, otherwise the coarser geometry."""
    return data.get("draw") or [[g[0], g[1]] for g in data["geom"]]

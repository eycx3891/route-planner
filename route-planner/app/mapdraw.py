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
HDR = 112
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


def fits_at(points, z, pad_css=70) -> bool:
    xs, ys = zip(*[proj(la, lo, z) for la, lo in points])
    return max(xs) - min(xs) <= CSS - 2 * pad_css and max(ys) - min(ys) <= CSS - 2 * pad_css


def fit(points, pad_css=70, zmax=18, zmin=8):
    """Centre and largest zoom at which all points fit inside the 640 css px map with a margin."""
    for z in range(zmax, zmin - 1, -1):
        if fits_at(points, z, pad_css):
            break
    xs, ys = zip(*[proj(la, lo, z) for la, lo in points])
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
    cache = "maps/base/" + hashlib.md5(f"{center[0]:.5f},{center[1]:.5f},{z},v2".encode()).hexdigest() + ".png"
    hit = store.load_blob(cache)
    if hit:
        return Image.open(io.BytesIO(hit)).convert("RGB")
    params = [("center", f"{center[0]:.6f},{center[1]:.6f}"), ("zoom", z), ("size", f"{CSS}x{CSS}"), ("scale", SC), ("maptype", "roadmap"),
              ("style", "feature:poi|visibility:off"), ("style", "feature:transit|visibility:off"), ("style", "feature:road|element:labels.icon|visibility:off"), ("key", key)]
    try:
        r = httpx.get("https://maps.googleapis.com/maps/api/staticmap", params=params, timeout=40)
    except httpx.HTTPError as e:
        raise MapError(f"Could not reach Google Maps: {e}") from e
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        raise MapError(f"Static Maps error {r.status_code}: {r.text[:200]} (enable Maps Static API and allow it on your key)")
    store.save_blob(cache, r.content)
    return Image.open(io.BytesIO(r.content)).convert("RGB")


class Canvas:
    """Draws over one Google map picture. Danger areas, rest stops and tags are neat boxes with thin leader lines that never overlap."""

    def __init__(self, base, center, z):
        self.im = base.convert("RGBA")
        self.center, self.z = center, z
        self.cx, self.cy = proj(center[0], center[1], z)
        self.d = ImageDraw.Draw(self.im, "RGBA")
        self.layer = Image.new("RGBA", (W * SS, W * SS), (0, 0, 0, 0))
        self.ld = ImageDraw.Draw(self.layer)
        self.placed = []
        self.route_segs = []  # route line segments in picture pixels: labels must keep clear of them
        self.pending = []
        self.drawn = {"hazards": [], "stops": [], "routes": [], "circles": {}, "ends": [], "overlaps": 0, "route_overlaps": 0, "end_px": {}, "line_end_px": {}}

    def px(self, lat, lon):
        x, y = proj(lat, lon, self.z)
        return (x - self.cx) * SC + W / 2, (y - self.cy) * SC + W / 2

    def inside(self, p, m=0):
        return -m <= p[0] <= W + m and -m <= p[1] <= W + m

    # ---- the route line: solid, thin, no border ----
    def line(self, pts_latlon, colour, width):
        pts = [self.px(p[0], p[1]) for p in pts_latlon]
        self.route_segs += [(a, b) for a, b in zip(pts, pts[1:])
                            if max(a[0], b[0]) > -60 and min(a[0], b[0]) < W + 60 and max(a[1], b[1]) > -60 and min(a[1], b[1]) < W + 60]
        self.drawn["line_end_px"][len(self.drawn["line_end_px"])] = (pts[0], pts[-1])
        big = [(x * SS, y * SS) for x, y in pts]
        if len(big) > 1:
            c = colour + (255,)
            self.ld.line(big, fill=c, width=int(width * SS), joint="curve")
            for e in (big[0], big[-1]):
                r = width * SS / 2
                self.ld.ellipse([e[0] - r, e[1] - r, e[0] + r, e[1] + r], fill=c)
        return pts

    def chevrons(self, pts, every=640):
        acc = 0.0
        for a, b in zip(pts, pts[1:]):
            seg = math.hypot(b[0] - a[0], b[1] - a[1])
            acc += seg
            if acc >= every and seg > 4 and self.inside(b, -30):
                acc = 0.0
                ang = math.atan2(b[1] - a[1], b[0] - a[0])
                for sgn in (1, -1):
                    tip = (b[0] * SS, b[1] * SS)
                    tail = ((b[0] - 10 * math.cos(ang + sgn * 0.7)) * SS, (b[1] - 10 * math.sin(ang + sgn * 0.7)) * SS)
                    self.ld.line([tail, tip], fill=(255, 255, 255, 255), width=3 * SS)

    def flush_lines(self):
        small = self.layer.resize((W, W), Image.LANCZOS)
        self.im = Image.alpha_composite(self.im, small)
        self.d = ImageDraw.Draw(self.im, "RGBA")
        self.layer = Image.new("RGBA", (W * SS, W * SS), (0, 0, 0, 0))
        self.ld = ImageDraw.Draw(self.layer)

    # ---- neat boxes ----
    ROUTE_CLEAR = 14  # pixels of clear space kept between any label and the route line (the line itself is about 8 px wide)

    @staticmethod
    def _seg_hits_box(a, b, box) -> bool:
        """Does the segment a-b touch the rectangle box (x0, y0, x1, y1)? Liang-Barsky clipping."""
        x0, y0, x1, y1 = box
        dx, dy = b[0] - a[0], b[1] - a[1]
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
            if p == 0:
                if q < 0:
                    return False
            else:
                t = q / p
                if p < 0:
                    if t > t1:
                        return False
                    t0 = max(t0, t)
                else:
                    if t < t0:
                        return False
                    t1 = min(t1, t)
        return True

    def hits_route(self, box, clear=None) -> bool:
        c = self.ROUTE_CLEAR if clear is None else clear
        fat = (box[0] - c, box[1] - c, box[2] + c, box[3] + c)
        for a, b in self.route_segs:
            if max(a[0], b[0]) < fat[0] or min(a[0], b[0]) > fat[2] or max(a[1], b[1]) < fat[1] or min(a[1], b[1]) > fat[3]:
                continue
            if self._seg_hits_box(a, b, fat):
                return True
        return False

    def leader_crosses_route(self, anchor, box, skip=22) -> bool:
        """True if the thin line from a marker to its label crosses the route anywhere except right at the marker."""
        px_, py_ = min(max(anchor[0], box[0]), box[2]), min(max(anchor[1], box[1]), box[3])
        n = max(int(math.hypot(px_ - anchor[0], py_ - anchor[1]) / 6), 1)
        for k in range(1, n + 1):
            x, y = anchor[0] + (px_ - anchor[0]) * k / n, anchor[1] + (py_ - anchor[1]) * k / n
            if math.hypot(x - anchor[0], y - anchor[1]) < skip:
                continue
            if self.hits_route((x - 2, y - 2, x + 2, y + 2), 6):
                return True
        return False

    def _free(self, box, pad=10, route=True):
        if box[0] < 8 or box[1] < 8 or box[2] > W - 8 or box[3] > W - 8:
            return False
        if route and self.hits_route(box):
            return False
        return all(box[2] + pad < o[0] or box[0] - pad > o[2] or box[3] + pad < o[1] or box[1] - pad > o[3] for o in self.placed)

    def ring(self, p, colour, radius=15):
        self.d.ellipse([p[0] - radius, p[1] - radius, p[0] + radius, p[1] + radius], outline=colour + (255,), width=5)
        self.placed.append((p[0] - radius - 4, p[1] - radius - 4, p[0] + radius + 4, p[1] + radius + 4))

    def box(self, anchor, text, fill, size=34, leader=True, ring_r=15):
        """Place a rounded box near `anchor` where nothing else is, then join it to the spot with a thin line."""
        f = font(size)
        bb = self.d.textbbox((0, 0), text, font=f)
        w, h = bb[2] - bb[0] + 30, size + 24
        chosen = None
        strict = True
        prefer = (-50, -130, 40, 140, -90, 90, 0, 180, -20, -160, 20, 160, -70, -110, 70, 110, -30, -150, 30, 150, -10, -170, 10, 170, -60, -120, 60, 120, -80, -100, 80, 100)
        angles = [a for k in range(0, 360, 20) for a in (k,)]
        for strict in (True, False):  # first insist the leader line also stays off the route, then relax that
            for r in (ring_r + 60, ring_r + 100, ring_r + 145, ring_r + 195, ring_r + 255, ring_r + 330, ring_r + 420, ring_r + 520, ring_r + 640):
                for ang in prefer + tuple(angles):
                    a = math.radians(ang)
                    x, y = anchor[0] + r * math.cos(a) - w / 2, anchor[1] + r * math.sin(a) - h / 2
                    bx = (x, y, x + w, y + h)
                    if self._free(bx) and not (strict and leader and self.leader_crosses_route(anchor, bx)):
                        chosen = (x, y)
                        break
                if chosen:
                    break
            if chosen:
                break
        if not chosen:  # no place clear of the route: use a spot clear of other boxes, and say so in the checks
            for r in (ring_r + 60, ring_r + 100, ring_r + 145, ring_r + 195, ring_r + 255, ring_r + 330, ring_r + 420):
                for ang in prefer:
                    a = math.radians(ang)
                    x, y = anchor[0] + r * math.cos(a) - w / 2, anchor[1] + r * math.sin(a) - h / 2
                    if self._free((x, y, x + w, y + h), route=False):
                        chosen = (x, y)
                        self.drawn["route_overlaps"] += 1
                        break
                if chosen:
                    break
        if not chosen:
            a = math.radians(-50)
            x, y = anchor[0] + (ring_r + 60) * math.cos(a) - w / 2, anchor[1] + (ring_r + 60) * math.sin(a) - h / 2
            chosen = (min(max(x, 8), W - w - 8), min(max(y, 8), W - h - 8))  # never off the picture
            self.drawn["overlaps"] += 1
            self.drawn["route_overlaps"] += 1
        x, y = chosen
        if leader:
            px_, py_ = min(max(anchor[0], x), x + w), min(max(anchor[1], y), y + h)
            self.d.line([(anchor[0], anchor[1]), (px_, py_)], fill=fill + (255,), width=3)
        self.d.rounded_rectangle([x, y, x + w, y + h], radius=9, fill=fill + (255,), outline=(255, 255, 255, 255), width=2)
        self.d.text((x + 15, y + 9), text, font=f, fill=(255, 255, 255, 255))
        self.placed.append((x, y, x + w, y + h))

    def add_marker(self, lat, lon, text, colour, kind, size=34):
        """Queue a marker. Drawn later in route order so the first ones get the best spots."""
        p = self.px(lat, lon)
        if not self.inside(p):
            return False
        self.pending.append((p, text, colour, kind, size))
        return True

    def draw_markers(self):
        for p, text, colour, kind, size in self.pending:
            if kind in ("hazards", "stops"):
                self.ring(p, colour)
        for p, text, colour, kind, size in self.pending:
            if kind in ("hazards", "stops"):
                self.box(p, text, colour, size)
                self.drawn[kind].append(text)
                self.drawn["circles"][text] = p
            else:
                self.d.ellipse([p[0] - 11, p[1] - 11, p[0] + 11, p[1] + 11], fill=colour + (255,), outline=(255, 255, 255, 255), width=3)
                self.placed.append((p[0] - 13, p[1] - 13, p[0] + 13, p[1] + 13))
                self.box(p, text, colour, size, ring_r=11)
                if kind == "ends":
                    self.drawn["ends"].append(text)
                    self.drawn["end_px"][text] = p
        self.pending = []

    def request_ring(self, requested, routed, min_gap_m=12) -> bool:
        """Hollow ring where you typed the point, with a thin line to the road point the route really uses. Only drawn when they are apart."""
        if geo.hav(requested[0], requested[1], routed[0], routed[1]) < min_gap_m:
            return False
        a, b = self.px(requested[0], requested[1]), self.px(routed[0], routed[1])
        if not (self.inside(a) and self.inside(b)):
            return False
        self.d.line([a, b], fill=(90, 90, 90, 255), width=3)
        self.d.ellipse([a[0] - 10, a[1] - 10, a[0] + 10, a[1] + 10], outline=(90, 90, 90, 255), width=4)
        self.placed.append((a[0] - 14, a[1] - 14, a[0] + 14, a[1] + 14))
        return True

    def road_labels(self, geom, rows, steps, colour, min_m=1500):
        """Name long roads beside the line, only where there is room."""
        offs = [g[2] for g in geom]
        f = font(26)
        for r in rows:
            if r["dist_m"] < min_m:
                continue
            mid = steps[r["step"]]["off"] + r["dist_m"] / 2
            i = min(range(len(geom)), key=lambda k: abs(offs[k] - mid))
            p = self.px(geom[i][0], geom[i][1])
            q = self.px(geom[min(i + 3, len(geom) - 1)][0], geom[min(i + 3, len(geom) - 1)][1])
            if not self.inside(p, -60):
                continue
            ang = math.atan2(q[1] - p[1], q[0] - p[0]) + math.pi / 2
            nx, ny = math.cos(ang), math.sin(ang)
            text = r["road"][:34]
            bb = self.d.textbbox((0, 0), text, font=f)
            w, h = bb[2] - bb[0] + 20, 38
            ext = abs(nx) * w / 2 + abs(ny) * h / 2 + 22
            for extra in (0, 60):
                done = False
                for sg in (1, -1):
                    cx, cy = p[0] + sg * nx * (ext + extra), p[1] + sg * ny * (ext + extra)
                    box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                    if self._free(box, 6):
                        self.d.rounded_rectangle(box, radius=8, fill=(255, 255, 255, 225), outline=colour + (255,), width=2)
                        self.d.text((box[0] + 10, box[1] + 4), text, font=f, fill=colour + (255,))
                        self.placed.append(box)
                        done = True
                        break
                if done:
                    break

    def finish(self, title, subtitle=""):
        hdr = 112 if subtitle else 92
        out = Image.new("RGB", (W, W + hdr), (255, 255, 255))
        out.paste(self.im.convert("RGB"), (0, hdr))
        d = ImageDraw.Draw(out)
        d.text((24, 14), title[:80], font=font(44), fill=(20, 20, 20))
        if subtitle:
            d.text((24, 66), subtitle[:120], font=font(28), fill=(80, 80, 80))
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()


def route_pts(data):
    """The precise drawing line if the run has one, otherwise the coarser geometry."""
    return data.get("draw") or [[g[0], g[1]] for g in data["geom"]]

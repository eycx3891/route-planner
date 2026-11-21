"""Bus lanes and trip timing.
You enter when the truck leaves. For every bus lane found (Street View reading or added by you) the app works out the
clock time the truck reaches it and says whether that is inside or outside the lane's posted hours.
USABLE here means: the truck passes outside the posted hours. Hours are only trusted once you confirm them against the sign."""
import datetime
from html import escape

from . import geo, stage2, stage3, stage4

SGT = datetime.timezone(datetime.timedelta(hours=8))
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CLUSTER_M = 80  # sightings closer than this on one route are the same bus lane


def next_id(run) -> str:
    used = [int(l["id"][1:]) for l in run.get("buslanes", []) if str(l.get("id", "")).startswith("B") and l["id"][1:].isdigit()]
    n = max(used + [run.get("next_bus", 1) - 1]) + 1
    run["next_bus"] = n + 1
    return f"B{n}"


def now_sgt() -> datetime.datetime:
    return datetime.datetime.now(SGT).replace(second=0, microsecond=0)


def parse_depart(text: str):
    """'2026-11-20T22:30' (browser date time box) or '2026-11-20 22:30' as Singapore time, or None."""
    try:
        return datetime.datetime.fromisoformat((text or "").strip().replace(" ", "T")).replace(tzinfo=SGT)
    except ValueError:
        return None


def depart_of(run):
    """(datetime, public_holiday_flag) or (None, False) when no time has been entered."""
    d = run.get("depart") or {}
    dt = parse_depart(d.get("at", ""))
    return dt, bool(d.get("ph"))


# ---------- hours ----------
def complete(l) -> bool:
    return bool(l.get("days")) and (bool(l.get("all_day")) or bool(l.get("start") and l.get("end")))


def hm(v) -> int:
    h, m = str(v).split(":")
    return int(h) * 60 + int(m)


def days_text(days) -> str:
    days = sorted(days or [])
    if days == list(range(7)):
        return "every day"
    if days == list(range(5)):
        return "Mon to Fri"
    if days == list(range(6)):
        return "Mon to Sat"
    return ", ".join(DAYS[d] for d in days) or "no days"


def window_text(l) -> str:
    if not complete(l):
        return "hours not confirmed"
    hrs = "all day" if l.get("all_day") else f"{l['start']} to {l['end']}"
    ph = "" if l.get("ph_applies", True) else ", not on public holidays"
    return f"{hrs}, {days_text(l['days'])}{ph}"


def in_force(l, dt, ph) -> bool:
    days = set(l.get("days") or [])
    if ph:
        if not l.get("ph_applies", True):
            return False
        day_ok = True
    else:
        day_ok = dt.weekday() in days
    if l.get("all_day"):
        return day_ok
    s, e, t = hm(l["start"]), hm(l["end"]), dt.hour * 60 + dt.minute
    if s <= e:
        return day_ok and s <= t < e
    if t >= s:  # window runs past midnight
        return day_ok
    return t < e and (True if ph else (dt.weekday() - 1) % 7 in days)


# ---------- clock time along a route ----------
class Timeline:
    """Seconds from departure to any distance along the route: revised driving time (50 km/h cap) plus 20 min for each rest stop passed."""

    def __init__(self, run, rn, data):
        self.items = stage3.calc(data)["items"]
        self.steps = data["steps"]
        self.total = data["total_m"]
        self.stop_offs = []
        stp = (run["routes"].get(rn) or {}).get("stopped")
        for s in (stp["stops"] if stp else []):
            _, gi = geo.nearest(s["lat"], s["lon"], data["geom"])
            self.stop_offs.append(data["geom"][gi][2])

    def drive_s(self, off) -> float:
        cum = 0.0
        for it, st in zip(self.items, self.steps):
            if st["off"] + st["dist_m"] >= off:
                frac = (off - st["off"]) / st["dist_m"] if st["dist_m"] else 0
                return cum + it["rev"] * max(min(frac, 1), 0)
            cum += it["rev"]
        return cum

    def at(self, off) -> float:
        return self.drive_s(off) + stage4.ALLOW_S * sum(1 for o in self.stop_offs if o < off)


# ---------- finding bus lanes ----------
def _vrun(run):
    routes, _ = stage4.final_view(run)
    v = dict(run)
    v["routes"] = {rn: {"data": d, "vias": []} for rn, d in routes.items()}
    return v


def sync(run) -> bool:
    """Merge Street View sightings into run['buslanes'], keeping what you reviewed. Returns True if anything changed."""
    lanes = run.setdefault("buslanes", [])
    vis = run.get("vision") or {}
    if not vis:
        return False
    before = repr(lanes)
    found = []
    for c in stage2.candidates(_vrun(run)):
        res = vis.get(c["key"]) or {}
        b = res.get("bus")
        if not (res.get("usable") and b):
            continue
        hit = next((f for f in found if c["route"] in f["routes"] and geo.hav(c["plat"], c["plon"], f["lat"], f["lon"]) <= CLUSTER_M), None)
        if not hit:
            hit = {"routes": [c["route"]], "lat": c["plat"], "lon": c["plon"], "texts": [], "in_path": False, "side": "", "reads": []}
            found.append(hit)
        hit["in_path"] = hit["in_path"] or b["in_path"]
        hit["side"] = hit["side"] or b["side"]
        if b["sign_text"] and b["sign_text"] not in hit["texts"]:
            hit["texts"].append(b["sign_text"])
        if b["days"]:
            hit["reads"].append(b)
        if res.get("view") and not hit.get("view"):
            hit["view"], hit["pano"] = res["view"], res.get("pano", "")
    for f in found:
        cur = next((l for l in lanes if l["src"] == "vision" and set(f["routes"]) & set(l["routes"]) and geo.hav(f["lat"], f["lon"], l["lat"], l["lon"]) <= CLUSTER_M), None)
        if not cur:
            cur = {"id": next_id(run), "src": "vision", "routes": [], "lat": f["lat"], "lon": f["lon"], "texts": [], "days": None, "start": None, "end": None,
                   "all_day": None, "ph_applies": True, "review": "pending", "note": "", "in_path": False, "side": ""}
            lanes.append(cur)
        for rn in f["routes"]:
            if rn not in cur["routes"]:
                cur["routes"].append(rn)
        cur["in_path"] = cur["in_path"] or f["in_path"]
        cur["side"] = cur.get("side") or f["side"]
        cur["texts"] = sorted(set(cur.get("texts", []) + f["texts"]))
        for k in ("view", "pano"):
            if f.get(k) and not cur.get(k):
                cur[k] = f[k]
        if cur["review"] == "pending" and not complete(cur) and f["reads"]:  # offer the model's reading as a starting point, never as confirmed
            r = f["reads"][0]
            cur.update({"days": r["days"], "start": r["start"], "end": r["end"], "all_day": r["all_day"]})
    return repr(lanes) != before


def add_manual(run, route, row_no, days, start, end, all_day, ph_applies, note):
    d = run["routes"][route]["data"]
    row = d["rows"][row_no - 1]
    s = d["steps"][row["step"]]
    run.setdefault("buslanes", []).append({
        "id": next_id(run), "src": "manual", "routes": [route], "lat": s["lat"], "lon": s["lon"], "texts": [], "days": days, "start": start, "end": end,
        "all_day": all_day, "ph_applies": ph_applies, "review": "confirmed", "note": note[:200], "in_path": True, "side": ""})


# ---------- verdicts ----------
def fmt(dt) -> str:
    return dt.strftime("%a %H:%M")


def assess(run) -> list:
    """One entry per bus lane per route it is on, with the clock times at the start, the sign and the end of that road, and the verdict."""
    dep, ph = depart_of(run)
    routes, _ = stage4.final_view(run)
    out, tl = [], {}
    for l in run.get("buslanes", []):
        if l.get("review") == "rejected":
            continue
        for rn in l["routes"]:
            d = routes.get(rn)
            if not d:
                continue
            tl.setdefault(rn, Timeline(run, rn, d))
            dist, gi = geo.nearest(l["lat"], l["lon"], d["geom"])
            if dist > 120:  # the final route no longer passes this lane
                out.append({"lane": l, "route": rn, "gone": True})
                continue
            off = d["geom"][gi][2]
            row_i = stage2.row_at_offset(d, off)
            row = d["rows"][row_i]
            a = d["steps"][row["step"]]["off"]
            b = a + row["dist_m"]
            e = {"lane": l, "route": rn, "gone": False, "row": row_i, "road": row["road"], "off": off, "span": (a, b)}
            if dep:
                times = [dep + datetime.timedelta(seconds=tl[rn].at(x)) for x in (a, off, b)]
                e["clocks"] = times
                if not complete(l) or l.get("review") != "confirmed":
                    e["verdict"], e["why"] = "check", "CHECK SIGN: hours not confirmed yet"
                else:
                    hit = next((t for t in times if in_force(l, t, ph)), None)
                    if hit:
                        e["verdict"], e["why"] = "no", f"NOT USABLE: in force at {fmt(hit)} ({window_text(l)})"
                    else:
                        e["verdict"], e["why"] = "yes", f"USABLE: you pass outside the posted hours ({window_text(l)})"
            else:
                e["verdict"], e["why"] = "none", "Enter a departure time to check"
            out.append(e)
    return out


def row_notes(run, rn) -> dict:
    """{road row index on the final route: short note} for the driver reference."""
    notes = {}
    for e in assess(run):
        if e["route"] != rn or e["gone"]:
            continue
        txt = {"no": "BUS LANE: closed to you at this time. Do not use it.", "yes": "Bus lane here: open to you at this time.",
               "check": "Bus lane here: hours not confirmed. Read the sign.", "none": ""}[e["verdict"]]
        if txt:
            notes[e["row"]] = txt
    return notes


# ---------- page pieces ----------
def depart_form(run) -> str:
    dep, ph = depart_of(run)
    val = (dep or now_sgt()).strftime("%Y-%m-%dT%H:%M")
    return (f"<form method=post action=/run/{run['id']}/depart><b>When the truck sets off (Singapore time)</b> "
            f"<input type=datetime-local name=depart value='{val}' required> "
            f"<label><input type=checkbox name=ph value=1{' checked' if ph else ''}> that day is a public holiday</label> <button>Set</button></form>")


def lane_forms(run) -> str:
    rid = run["id"]
    dep, ph = depart_of(run)
    if not dep:
        return depart_form(run) + "<p class=muted>Enter the departure time to see clock times and bus lane verdicts.</p>"
    res = assess(run)
    by = {}
    for e in res:
        by.setdefault(e["lane"]["id"], []).append(e)
    lanes = [l for l in run.get("buslanes", [])]
    rows = ""
    for l in lanes:
        es = by.get(l["id"], [])
        clock = "<br>".join(
            f"{e['route']}: {escape(e['road'])}<br><span class=muted>reaches the start of this road {fmt(e['clocks'][0])}, the sign {fmt(e['clocks'][1])}, the end {fmt(e['clocks'][2])}</span>"
            for e in es if not e["gone"] and e.get("clocks")) or "<span class=muted>the final route does not pass here</span>"
        verdicts = "<br>".join(
            f"<span class={'ok' if e['verdict'] == 'yes' else 'hz'}><b>{escape(e['why'])}</b></span>" for e in es if not e["gone"]) or ""
        day_boxes = "".join(f"<label><input type=checkbox name=days_{l['id']} value={i}{' checked' if i in (l.get('days') or []) else ''}>{n}</label> " for i, n in enumerate(DAYS))
        seen = f"<br><span class=muted>Street View text: {escape('; '.join(l['texts']))}</span>" if l.get("texts") else ""
        where = f"<code>{geo.coords(l['lat'], l['lon'])}</code> <a href='{geo.map_link(l['lat'], l['lon'])}' target=_blank>Map</a> | <a href='{geo.sv_link(l['lat'], l['lon'], 0, l.get('pano'))}' target=_blank>Street View</a>"
        if l.get("view"):
            where += f" | <a href='/run/{rid}/view/{l['view']}.jpg' target=_blank>What the model saw</a>"
        pathnote = "on the truck's path" if l.get("in_path") else "beside the truck's path (check which lane you are in)"
        opts = "".join(f"<option value={k}{' selected' if l.get('review', 'pending') == k else ''}>{v}</option>"
                       for k, v in (("pending", "Pending"), ("confirmed", "Hours confirmed against the sign"), ("rejected", "No bus lane here")))
        rows += (f"<tr><td><span class=pill>{l['id']}</span><br><span class=muted>{escape(l['src'])}</span></td>"
                 f"<td>{where}<br><span class=muted>{pathnote}</span>{seen}</td>"
                 f"<td>{day_boxes}<br>from <input type=time name=start_{l['id']} value='{l.get('start') or ''}'> to <input type=time name=end_{l['id']} value='{l.get('end') or ''}'> "
                 f"<label><input type=checkbox name=allday_{l['id']} value=1{' checked' if l.get('all_day') else ''}>all day</label><br>"
                 f"<label><input type=checkbox name=phapp_{l['id']} value=1{' checked' if l.get('ph_applies', True) else ''}>applies on public holidays</label></td>"
                 f"<td>{clock}</td><td>{verdicts}</td>"
                 f"<td><select name=review_{l['id']}>{opts}</select><br><input type=text name=note_{l['id']} placeholder='note' value='{escape(l.get('note', ''), quote=True)}'></td></tr>")
    table = (f"<form method=post action=/run/{rid}/buslane/save><table><tr><th>No.</th><th>Where</th><th>Posted hours</th><th>When the truck gets there</th><th>Verdict</th><th>Your review</th></tr>{rows}</table>"
             "<button>Save bus lane review</button></form>") if rows else "<p class=muted>No bus lane found on the routes by Street View. Add one below if you know of one.</p>"
    n_chk = sum(1 for e in res if not e["gone"] and e["verdict"] == "check")
    n_no = sum(1 for e in res if not e["gone"] and e["verdict"] == "no")
    summary = ""
    if n_no:
        summary += f"<p class=banner>{n_no} bus lane(s) are IN FORCE when the truck passes. The truck must stay out of them.</p>"
    if n_chk:
        summary += f"<p class=banner>{n_chk} bus lane(s) have hours you have not confirmed. They are shown as CHECK SIGN until you do.</p>"
    add = (f"<details><summary><b>Add a bus lane yourself</b></summary><form method=post action=/run/{rid}/buslane/add>"
           "<select name=route><option>primary</option><option>secondary</option></select> Road row # <input type=text name=row size=4> "
           + "".join(f"<label><input type=checkbox name=days value={i} checked>{n}</label> " for i, n in enumerate(DAYS))
           + " from <input type=time name=start> to <input type=time name=end> <label><input type=checkbox name=allday value=1>all day</label> "
             "<label><input type=checkbox name=phapp value=1 checked>applies on public holidays</label> <input type=text name=note placeholder='note'> <button>Add</button></form></details>")
    legend = ("<p class=muted>USABLE means the truck reaches the lane outside its posted hours. The app checks the start of the road, the sign and the end of the road, "
              "and uses the 50 km/h revised times plus 20 min for each rest stop. Street View hours are only a starting point: confirm them against the sign. "
              "Public holidays: tick the box above if the trip day is one, because the app does not know the holiday calendar.</p>")
    return depart_form(run) + summary + table + add + legend

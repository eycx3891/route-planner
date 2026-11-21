"""Claude reads Street View images against the danger checklist. Only reports what is visible."""
import base64
import json
import re
import os
import time

import httpx

MODEL = "claude-sonnet-5-5"
URL = "https://api.anthropic.com/v1/messages"

CATS = {1: "Roadworks", 2: "Lane closure", 3: "Temporary signals", 4: "Tight turn or junction", 5: "Merge or exit",
        6: "U-turn", 7: "Tunnel or underpass", 8: "Weight or vehicle class", 9: "Blind spot or gradient",
        10: "Entrance or emerging vehicles", 11: "Operator-designated", 12: "Current incident"}

SYSTEM = """You screen ONE Singapore Street View image for a route planning exercise. A long garbage truck will drive this route late at night, when the roads are almost empty.
Each image comes with a line "THE TRUCK IS: ..." saying exactly what the truck does at this point.
A thin YELLOW LINE with a black edge is drawn on the road: it is the exact path the truck takes, starting at the camera, with an orange arrowhead at its far end. That is how you know where the truck is and where it is going. Judge ONLY what is on or beside that line and affects the truck's action. If the line turns, the truck turns there; if it follows a ramp, the truck takes that ramp. Ignore other roads, ramps and lanes the line does not use. The line is approximate (flat road assumed).
The photo is OLD and is not live. NEVER report traffic, queues, slower or stopped or parked or moving vehicles (including other trucks and buses), pedestrians, weather, lighting, or anything that is only a condition of this photo. Do not mention vehicles or traffic at all. The one exception is the bus_lane field below, which describes road markings and signs only, never traffic.
NEVER mistake petrol stations, showrooms, shops, ordinary buildings or hoardings for roadworks. Report roadworks only if you see cones, barriers or hoarding on the carriageway, excavation, or a roadworks sign.
Rules by what the truck is doing:
- "continue straight" or "set off" or "arrive": slip roads, merges, exits, lane arrows and junctions to the side do NOT matter, even on flyovers and expressways. Report only roadworks or a closure in the truck's own lanes, a tunnel or underpass entrance ahead, or a clear hazard directly in its path. Usually report nothing.
- "turn left" or "turn right": report a tight turn or small turning radius, a short or narrow turn lane, a slip road or kerb the truck must swing across, a blind corner, or a junction so large or confusing that the correct lane is easy to miss.
- "expressway exit or entry": report a short exit or merge, a lane that must be reached quickly, or a sharp ramp.
- "roundabout": report a tight or multi-lane roundabout.
- "u-turn": always report.
Severity: high = could cause a collision or wrong turn for THIS truck doing THIS action; medium = needs care; low = minor. Do not report low. At most 2 findings per image, only the most important.
Categories: 1 roadworks; 2 lane closure or narrow lane; 3 temporary speed limit or signals; 4 tight turn or constrained or large junction; 5 short merge or exit; 6 U-turn; 7 tunnel or underpass (a road dipping below ground or enclosed; a road that merely passes UNDER a bridge or flyover is allowed and is NOT category 7); 9 blind spot or steep gradient; 10 entrance where vehicles may emerge.
Return ONLY a JSON array, one object per image in the same order, with keys:
id (string), usable (bool), findings (list of {cat:int, what:string<=14 words describing the situation FOR THIS TRUCK ACTION, risk:string<=14 words, precaution:string<=14 words, severity:"high"|"medium"|"low"}),
lanes (string<=25 words: lane arrows or markings visible ahead, or "not visible"), signals (bool), slip (bool: a slip road is visible), junction ("none"|"small"|"medium"|"large"),
bus_lane (null unless a bus lane is visible on the road the truck is on, ahead of the camera: a red or painted lane, a BUS LANE marking, or a bus lane sign. Otherwise an object {in_path:bool (the yellow line runs along or across it), side:"left"|"right"|"centre", sign_text:string<=25 words copying any readable operating hours from a sign or marking else "", days:list of ints Monday=0 to Sunday=6 or null, start:"HH:MM" or null, end:"HH:MM" or null, all_day:bool or null}. Fill days, start, end and all_day ONLY if the hours are clearly legible. NEVER guess hours: use null),\noverhead (list of {what:string<=8 words, clearance_m:number or null} for EVERY bridge, flyover or overpass the road passes under; read any posted clearance sign such as "4.5m", otherwise null),
landmarks (up to 3 permanent things a driver could recognise, each with colour, type and side, e.g. "grey 10 storey HDB block on the left", "blue overhead sign reading Mandai Rd". Never trees, plants, lamp posts, poles, fences, road markings, vehicles or people)."""


BANNED = re.compile(r"\b(vehicles?|cars?|buses|bus|lorr(?:y|ies)|vans?|motorcycles?|motorbikes?|trucks|another truck|other truck|a truck ahead|(?:heavy|slow|busy|moving|dense)\s+traffic|traffic\s+(?:jams?|flow|queues?|congestion|volume|ahead|buildup)|queues?|queuing|queued|congest\w*|"
                    r"slower|slow-moving|stationary|stopped|parked|pedestrians?|people|cyclists?|jam|jammed|crowd\w*)\b", re.I)


def is_live_condition(*texts) -> bool:
    """True if the text describes traffic, other vehicles or people. The photo is old, so none of that counts."""
    return any(BANNED.search(t or "") for t in texts)


class VisionError(Exception):
    pass


def screen(items: list) -> dict:
    """items: [{id, context, jpeg(bytes)}]. Returns {id: result}."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise VisionError("ANTHROPIC_API_KEY is not set")
    content = []
    for it in items:
        content.append({"type": "text", "text": f"Image id: {it['id']}. Route context: {it['context']}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(it["jpeg"]).decode()}})
    content.append({"type": "text", "text": "Return the JSON array now."})
    body = {"model": MODEL, "max_tokens": 4000, "system": SYSTEM, "messages": [{"role": "user", "content": content}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last = ""
    for attempt in range(4):
        try:
            r = httpx.post(URL, json=body, headers=headers, timeout=120)
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(2 + attempt * 3)
            continue
        if r.status_code == 200:
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
            return _parse(text, items)
        last = f"{r.status_code}: {r.text[:300]}"
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(3 + attempt * 5)
            continue
        break
    raise VisionError(f"Claude API failed: {last}")


def _overhead(raw) -> list:
    out = []
    for o in raw or []:
        if not isinstance(o, dict):
            continue
        try:
            cl = float(o["clearance_m"]) if o.get("clearance_m") is not None else None
        except (TypeError, ValueError):
            cl = None
        out.append({"what": str(o.get("what", "overpass"))[:80], "clearance_m": cl})
    return out[:4]


def _hhmm(v):
    m = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", str(v or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _bus(raw):
    """Bus lane reading, or None. Hours are kept only if every part is clean; otherwise the person must read the sign."""
    if not isinstance(raw, dict):
        return None
    days = raw.get("days")
    days = sorted({int(d) for d in days if str(d).isdigit() and 0 <= int(d) <= 6}) if isinstance(days, list) else None
    start, end = _hhmm(raw.get("start")), _hhmm(raw.get("end"))
    all_day = raw.get("all_day") if isinstance(raw.get("all_day"), bool) else None
    complete = bool(days) and (all_day is True or (start and end))
    return {"in_path": bool(raw.get("in_path")), "side": str(raw.get("side", ""))[:8], "sign_text": str(raw.get("sign_text", ""))[:160],
            "days": days if complete else None, "start": start if complete else None, "end": end if complete else None,
            "all_day": all_day if complete else None}


def _parse(text: str, items: list) -> dict:
    a, b = text.find("["), text.rfind("]")
    try:
        arr = json.loads(text[a:b + 1])
    except (ValueError, TypeError) as e:
        raise VisionError(f"Could not read Claude's reply as JSON: {text[:200]}") from e
    out = {}
    for x in arr:
        if isinstance(x, dict) and x.get("id") in {i["id"] for i in items}:
            fs = []
            for f in x.get("findings") or []:
                try:
                    c = int(f.get("cat"))
                except (TypeError, ValueError):
                    continue
                if c in CATS and c not in (8, 11, 12) and not is_live_condition(f.get("what"), f.get("risk"), f.get("precaution")):
                    fs.append({"cat": c, "what": str(f.get("what", ""))[:160], "risk": str(f.get("risk", ""))[:160],
                               "precaution": str(f.get("precaution", ""))[:160], "severity": str(f.get("severity", "low")).lower()})
            out[x["id"]] = {"usable": bool(x.get("usable", True)), "findings": fs, "lanes": str(x.get("lanes", ""))[:200],
                            "signals": bool(x.get("signals")), "slip": bool(x.get("slip")),
                            "junction": str(x.get("junction", "none")), "overhead": _overhead(x.get("overhead")), "landmarks": [str(l)[:90] for l in (x.get("landmarks") or [])][:3], "bus": _bus(x.get("bus_lane"))}
    for i in items:  # anything Claude skipped is "unable to verify"
        out.setdefault(i["id"], {"usable": False, "findings": [], "lanes": "", "signals": False, "slip": False,
                                 "junction": "none", "overhead": [], "landmarks": []})
    return out


PARK_SYSTEM = """You assess whether a place could serve as a rest stop for a long vehicle (9 m) from Street View images, for a route planning exercise.
Report ONLY what is visible. Never guess. If the views are blocked, dark or show no parking area, set usable=false or is_parking=false.
Return ONLY a JSON array, one object per candidate id, in order, with keys:
id (string), usable (bool), is_parking (bool: a parking area for vehicles is visible), kind ("heavy vehicle park"|"commercial carpark"|"public carpark"|"roadside"|"other"|"unclear"),
open_area (bool: open air, not multi-storey, covered or underground), space ("large"|"medium"|"small"|"unclear": room for a 9 m vehicle to enter, turn and park without blocking traffic),
entrance_seen (bool), barrier (bool: gate, gantry or height barrier visible), restriction_text (string: any readable sign restricting lorries or heavy vehicles, else ""), notes (string<=30 words)."""


def _ask(system: str, content: list, max_tokens: int = 4000) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise VisionError("ANTHROPIC_API_KEY is not set")
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": content}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last = ""
    for attempt in range(4):
        try:
            r = httpx.post(URL, json=body, headers=headers, timeout=180)
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(2 + attempt * 3)
            continue
        if r.status_code == 200:
            return "".join(b.get("text", "") for b in r.json().get("content", []))
        last = f"{r.status_code}: {r.text[:300]}"
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(3 + attempt * 5)
            continue
        break
    raise VisionError(f"Claude API failed: {last}")


def _json_array(text: str):
    a, b = text.find("["), text.rfind("]")
    try:
        return json.loads(text[a:b + 1])
    except (ValueError, TypeError) as e:
        raise VisionError(f"Could not read Claude's reply as JSON: {text[:200]}") from e


def screen_parking(items: list) -> dict:
    """items: [{id, context, jpegs:[bytes]}]. Returns {id: result}."""
    content = []
    for it in items:
        content.append({"type": "text", "text": f"Candidate id: {it['id']}. {it['context']}"})
        for jpg in it["jpegs"]:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpg).decode()}})
    content.append({"type": "text", "text": "Return the JSON array now."})
    ids = {i["id"] for i in items}
    out = {}
    for x in _json_array(_ask(PARK_SYSTEM, content)):
        if isinstance(x, dict) and x.get("id") in ids:
            out[x["id"]] = {"usable": bool(x.get("usable")), "is_parking": bool(x.get("is_parking")), "kind": str(x.get("kind", "unclear"))[:30],
                            "open_area": bool(x.get("open_area")), "space": str(x.get("space", "unclear"))[:10],
                            "entrance_seen": bool(x.get("entrance_seen")), "barrier": bool(x.get("barrier")),
                            "restriction_text": str(x.get("restriction_text", ""))[:120], "notes": str(x.get("notes", ""))[:200]}
    for i in items:
        out.setdefault(i["id"], {"usable": False, "is_parking": False, "kind": "unclear", "open_area": False, "space": "unclear",
                                 "entrance_seen": False, "barrier": False, "restriction_text": "", "notes": ""})
    return out


def compose(system: str, user_text: str) -> list:
    """Text only call returning a JSON array (used for the driver reference)."""
    return _json_array(_ask(system, [{"type": "text", "text": user_text}], 12000))


NAV_SYSTEM = """You help write a driver reference for a long garbage truck in Singapore. Each image is an OLD Street View photo (not live) looking forward along a road.
A thin YELLOW LINE with a black edge is drawn on the road: it is the exact path the truck takes from the camera, so you know where the truck is and where it goes. The line is approximate (flat road assumed).
The truck keeps to the LEFTMOST lane. Report ONLY what is visible. Never mention traffic, vehicles, queues or people.
For each image give:
- usable (bool): false if the view is blocked, dark or unclear.
- lane_count: lanes going the truck's way (null if you cannot tell).
- exit_ahead: an exit, slip road, branch or merge visible ahead that the yellow line does NOT take, with readable sign text and its side, e.g. "Exit 27 to Clementi Ave 6 on the left (green overhead sign)". "" if none.
- leftmost_lane_exits (bool): true if the LEFTMOST lane is forced off the main road ahead (exit only lane, a left turn or exit arrow in the leftmost lane, a lane drop on the left) and the yellow line does NOT follow that exit. Otherwise false.
- lane_for_next: ONLY for views marked far or near: the lane the truck needs for the next manoeuvre, counted from the LEFT (1 = leftmost), from lane arrows, markings or signs; null if they do not show it.
- lane_basis: "arrows" | "signs" | "markings" | "none".
- filter_cue: ONLY for far views: ONE landmark showing where to start moving over. "" if none good.
- turn_cue: ONLY for near views: ONE landmark showing the turn or exit is NOW, plus a count of traffic lights only if clearly visible. "" if none good.
- landmark: ONE permanent landmark in view (any view). "" if none good.
- over: if the yellow line goes up onto and over a flyover or bridge, say so in a few words, else "".
- note: one short sentence the driver must know (max 20 words), or "".
Good landmarks: overhead gantry or road signs with readable text, named or distinctive buildings (colour, type, side), petrol stations (brand), bridges or flyovers, bus interchanges, MRT stations, shelters with readable names, a counted traffic light. Always say whether it is on the left or right.
NEVER use trees, plants, lamp posts, poles, fences, road markings, kerbs, vehicles, people, shadows, sky, or vague things like "a building". If nothing good is visible give "". Do not guess.
Return ONLY a JSON array with one object per image in the same order, keys: id, usable, lane_count, exit_ahead, leftmost_lane_exits, lane_for_next, lane_basis, filter_cue, turn_cue, landmark, over, note."""


def nav_screen(items: list) -> dict:
    """items: [{id, context, views:[{label, jpeg}]}] (one image each). Returns {id: result}."""
    content = []
    for it in items:
        content.append({"type": "text", "text": f"Image id: {it['id']}. {it['context']}"})
        for vw in it["views"]:
            content.append({"type": "text", "text": f"This is the {vw['label']}."})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(vw["jpeg"]).decode()}})
    content.append({"type": "text", "text": "Return the JSON array now."})
    ids = {i["id"] for i in items}
    out = {}

    def clean(x, k, n):
        t = str(x.get(k, "") or "")[:n].strip()
        return "" if is_live_condition(t) else t

    for x in _json_array(_ask(NAV_SYSTEM, content, 6000)):
        if not isinstance(x, dict) or x.get("id") not in ids:
            continue

        def num(k, hi=12):
            try:
                n = int(x.get(k))
                return n if 1 <= n <= hi else None
            except (TypeError, ValueError):
                return None
        cnt, lane = num("lane_count"), num("lane_for_next")
        if cnt and lane and lane > cnt:
            lane = None
        basis = str(x.get("lane_basis", "none")).lower()
        out[x["id"]] = {"usable": bool(x.get("usable")), "lane_count": cnt, "lane_for_next": lane if basis != "none" else None, "lane_basis": basis,
                        "exit_ahead": clean(x, "exit_ahead", 140), "leftmost_lane_exits": bool(x.get("leftmost_lane_exits")),
                        "filter_cue": clean(x, "filter_cue", 120), "turn_cue": clean(x, "turn_cue", 140), "landmark": clean(x, "landmark", 120),
                        "over": clean(x, "over", 100), "note": clean(x, "note", 160)}
    for i in items:
        out.setdefault(i["id"], {"usable": False, "lane_count": None, "lane_for_next": None, "lane_basis": "none", "exit_ahead": "", "leftmost_lane_exits": False,
                                 "filter_cue": "", "turn_cue": "", "landmark": "", "over": "", "note": ""})
    return out

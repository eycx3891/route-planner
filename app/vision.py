"""Claude reads Street View images against the danger checklist. Only reports what is visible."""
import base64
import json
import os
import time

import httpx

MODEL = "claude-sonnet-5-5"
URL = "https://api.anthropic.com/v1/messages"

CATS = {1: "Roadworks / construction", 2: "Lane closure / narrow lane", 3: "Temporary speed limit / changing signals",
        4: "Tight turn / constrained or large junction", 5: "Short merge / filtering / exit positioning",
        6: "U-turn exposure", 7: "Tunnel / underpass / overhead obstruction", 8: "Weight / vehicle class (out of scope)",
        9: "Blind spot / gradient", 10: "Busy entrance / queue / emerging vehicles",
        11: "Operator-reported hazard", 12: "Current incident (out of scope)"}

SYSTEM = """You screen Singapore Street View images for a route planning exercise for heavy vehicles.
Rules: report ONLY what is visible in each image. Never guess. If the view is blocked, dark or unclear, set usable=false.
Categories for findings: 1 roadworks/construction visible; 2 lane closure, lane reduction or narrow lane; 3 temporary speed limit or temporary signal arrangement;
4 tight turn, constrained junction, or LARGE multi-lane junction; 5 short merge lane, multi-lane filtering, abrupt lane change, difficult exit positioning, or a small slip road;
6 U-turn exposure; 7 tunnel or underpass (a road dipping below ground level or enclosed). A road that merely passes UNDER a bridge, flyover or overpass is allowed and is NOT category 7;
9 blind spot or steep gradient; 10 busy entrance/exit where vehicles may emerge, or queuing.
Do not use categories 8, 11 or 12.
Return ONLY a JSON array, one object per image, in the same order, with keys:
id (string), usable (bool), findings (list of {cat:int, what:string<=20 words, risk:string<=20 words, precaution:string<=20 words, conf:"high"|"medium"|"low"}),
lanes (string<=30 words: lane count and arrows/markings visible ahead, or "not visible"), signals (bool, traffic lights visible), slip (bool, a slip road is visible),
junction ("none"|"small"|"medium"|"large"),
overhead (list of {what:string<=8 words, clearance_m:number or null} for EVERY bridge, flyover or overpass the road passes under in this view; read any posted height clearance sign such as "4.5m" or "H 4.5", otherwise null), landmarks (list of up to 3 phrases a driver could use to recognise this junction, each with colour, type and side relative to the direction of travel,
e.g. "grey multi-storey building on the left", "blue overhead sign reading Mandai Rd", "red-roofed petrol station on the right". Prefer large permanent things
(buildings, bridges, signs with readable text, petrol stations, shelters). Never use vehicles, people or anything temporary. Only describe what you can see)."""


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
                if c in CATS and c not in (8, 11, 12):
                    fs.append({"cat": c, "what": str(f.get("what", ""))[:160], "risk": str(f.get("risk", ""))[:160],
                               "precaution": str(f.get("precaution", ""))[:160], "conf": str(f.get("conf", "low"))})
            out[x["id"]] = {"usable": bool(x.get("usable", True)), "findings": fs, "lanes": str(x.get("lanes", ""))[:200],
                            "signals": bool(x.get("signals")), "slip": bool(x.get("slip")),
                            "junction": str(x.get("junction", "none")), "overhead": _overhead(x.get("overhead")), "landmarks": [str(l)[:90] for l in (x.get("landmarks") or [])][:3]}
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

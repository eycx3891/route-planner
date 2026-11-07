"""Stage 2 pages: run screening, review one compiled list of danger areas, Gate 2."""
import time
from html import escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import geo, stage2, store
from .ui import bubble, login_needed, nav, page, table
from .vision import CATS

router = APIRouter()
REVIEW = {"pending": "Pending", "confirmed": "Confirm", "rejected": "Reject"}
SYM = {"H": "<b class=hz>H</b>", "S": "s", "N": "n/a", "U": "<b>?</b>"}


def back(run_id, error=""):
    return RedirectResponse(f"/run/{run_id}/stage2" + (f"?error={error}" if error else ""), status_code=303)


def route_data(run) -> dict:
    return {rn: run["routes"][rn]["data"] for rn in stage2.routes_of(run)}


def dang_cells(run, rn, routes=None, hazards=None):
    """Per road row: the danger labels (D1, D2...) with their colour coded category bubbles."""
    hazards = run.get("hazards", []) if hazards is None else hazards
    routes = routes or route_data(run)
    lm = stage2.labels(hazards, routes)
    out = {}
    for h in hazards:
        if h["id"] in lm and rn in h["routes"]:
            bub = "".join(bubble(d.get("cat")) for d in stage2.best_details(h))
            out.setdefault(h["routes"][rn], []).append(f"<span class=pill>{lm[h['id']]}</span> {bub}")
    return {k: "<br>".join(v) for k, v in out.items()}


def hazard_row(h, run, label) -> str:
    routes = route_data(run)
    where = []
    road = ""
    for rn, r in h["routes"].items():
        d = routes.get(rn)
        if d and r < len(d["rows"]):
            road = road or d["rows"][r]["road"]
            where.append(f"{rn} row {r + 1}")
    loc = ""
    if h.get("lat") is not None:
        loc = (f"<code>{geo.coords(h['lat'], h['lon'])}</code><br><a href='{geo.map_link(h['lat'], h['lon'])}' target=_blank>Map</a> | "
               f"<a href='{geo.sv_link(h['lat'], h['lon'], h.get('heading', 0), h.get('pano'))}' target=_blank>Street View</a>"
               + (f" | <a href='/run/{run['id']}/view/{h['view']}.jpg' target=_blank>What the model saw</a>" if h.get("view") else ""))
    lm = f"<br><span class=muted>Landmark: {escape(h['landmark'])}</span>" if h.get("landmark") else ""
    danger = ""
    todo = []
    for d in stage2.best_details(h):
        extra = f" {escape(d['risk'])}" if d.get("src") in ("rule", "operator", "tomtom-live") and d.get("risk") and d["risk"] != "Added by you" else ""
        danger += f"<div>{bubble(d.get('cat'))} {escape(d['what'])}{extra}</div>"
        if d.get("precaution") and d["precaution"] not in todo:
            todo.append(d["precaution"])
    opts = "".join(f"<option value={k}{' selected' if h.get('review', 'pending') == k else ''}>{v}</option>" for k, v in REVIEW.items())
    na = "" if h.get("applicable", True) else "<br><b>Checked: the route does not pass here.</b>"
    name = f"<b>{escape(h['name'])}</b><br>" if h.get("name") else ""
    return (f"<tr><td><span class=pill>{label}</span><br><span class=muted>{escape(', '.join(where))}</span></td>"
            f"<td>{name}<b>{escape(road)}</b><br>{loc}{lm}</td><td>{escape(h.get('action') or 'n/a')}</td><td>{danger}{na}</td>"
            f"<td>{escape(' '.join(todo))}</td><td><select name=review_{h['id']}>{opts}</select><br>"
            f"<input type=text name=note_{h['id']} placeholder='note (kept with this hazard)' value='{escape(h.get('note', ''), quote=True)}'></td></tr>")


@router.get("/run/{run_id}/stage2", response_class=HTMLResponse)
def stage2_page(request: Request, run_id: str, error: str = ""):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    head = nav(run, 2) + "<h2>Stage 2: danger areas</h2>"
    if run["gates"]["1"] != "approved":
        return page(head + "<p class=banner>Approve Gate 1 first.</p>")
    if not stage2.ready(run):
        return page(head + "<p class=banner>This run was made with an older version. Plan it again from the home page.</p>")
    err = f"<p class=banner>{escape(error)}</p>" if error else ""
    sc = run.get("screen") or {}
    st = sc.get("status", "none")
    stalled = st in ("running", "queued") and time.time() - sc.get("beat", 0) > 180
    live = st in ("running", "queued") and not stalled
    refresh = "<meta http-equiv=refresh content=5>" if st in ("running", "queued") else ""
    if stalled:
        msg = ("<p class=banner>Screening looks stalled (the app was probably restarted while it ran). "
               "Press Restart below. Finished points are kept, so nothing is repeated.</p>")
    elif live:
        msg = f"<p class=banner style='color:#036;border-color:#036'>Screening in progress: {sc.get('done', 0)} of {sc.get('total', '?')} points. This page refreshes itself.</p>"
    elif st == "error":
        msg = f"<p class=banner>Screening stopped: {escape(sc.get('error', ''))}. Press the button to resume (finished points are kept).</p>"
    elif st == "stale":
        msg = "<p class=banner>Routes changed. Re-run screening (finished points are reused).</p>"
    elif st == "done":
        msg = "<p class=ok>Screening finished.</p>"
    else:
        msg = "<p>Screening has not been run yet.</p>"
    inc = run.get("incidents")
    if st == "done" and inc:
        msg += (f"<p class=banner>Live roadworks feed not used: {escape(inc['error'])}</p>" if inc.get("error")
                else f"<p class=muted>TomTom roadworks and closures checked {escape(inc['fetched'])}: {len(inc['items'])} found in the area.</p>")
    label = "Restart" if stalled else ("Re-run" if st == "done" else "Run")
    btn = "" if live else (f"<form method=post action=/run/{run_id}/stage2/start><button>{label} screening "
                           "(uses Street View and Claude, a few minutes)</button></form>")
    body = refresh + head + err + msg + btn
    if st != "done":
        return page(body)
    routes = route_data(run)
    hz = run.get("hazards", [])
    lm = stage2.labels(hz, routes)
    gate = run["gates"].get("2", "pending")
    pending = sum(1 for h in hz if h["id"] in lm and h.get("review") == "pending")
    unconf = [m["name"] for m in stage2.MANDATORY if not run.get("mandatory", {}).get(m["id"], {}).get("confirmed")]
    if unconf:
        body += f"<p class=banner>Confirm the position of: {escape(', '.join(unconf))} (section further down) before approving Gate 2.</p>"
    body += ("<p class=ok>GATE 2 APPROVED. Still NOT APPROVED FOR USE until all gates pass.</p>" if gate == "approved"
             else f"<p class=banner>GATE 2 PENDING: {pending} danger area(s) awaiting your review. NOT APPROVED FOR USE</p>")
    # one compiled list with everything needed to decide
    live_h = sorted([h for h in hz if h["id"] in lm], key=lambda h: int(lm[h["id"]][1:]))
    rej = [h for h in hz if h.get("review") == "rejected" and h.get("applicable", True)]
    na = [h for h in hz if not h.get("applicable", True)]
    rows = "".join(hazard_row(h, run, lm[h["id"]]) for h in live_h)
    rows_na = "".join(hazard_row(h, run, "n/a") for h in na)
    rows_rej = "".join(hazard_row(h, run, "rejected") for h in rej)
    body += ("<h3>Danger areas to review</h3><p class=muted>Everything you need is in this one list. Open the coordinates in Google Maps if you want to check. "
             "Reject anything that is not a real danger for this truck. Numbers update as you reject. A note is saved with the danger area; it does not change how the app works.</p>"
             f"<form method=post action=/run/{run_id}/stage2/review><table><tr><th>No.</th><th>Where</th><th>What the truck is doing</th><th>Danger</th><th>What to do</th><th>Your review</th></tr>{rows}</table>"
             + (f"<details><summary>Fixed danger areas the route does not pass</summary><table>{rows_na}</table></details>" if na else "")
             + (f"<details><summary>Rejected ({len(rej)})</summary><table>{rows_rej}</table></details>" if rej else "")
             + "<button>Save review</button></form>")
    # the route tables, kept short: labels and bubbles only
    for rn, colour, title in (("primary", "blue", "PRIMARY ROUTE with danger areas"), ("secondary", "red", "SECONDARY ROUTE with danger areas")):
        if run["routes"].get(rn):
            body += table(run["routes"][rn], colour, title, [("Danger areas", dang_cells(run, rn)), ("Landmark at turn", stage2.landmarks_for(run, rn)),
                                                              ("Overhead clearance", stage2.clearances_for(run, rn))], stage2.mandatory_status(run, rn))
    body += "<h3>Fixed danger areas</h3><p class=muted>These are always searched for. Once you confirm a position it is remembered for every later run.</p>"
    for m in stage2.MANDATORY:
        loc = run.get("mandatory", {}).get(m["id"], {})
        ok = loc.get("lat") is not None
        link = f" <a href='{geo.map_link(loc['lat'], loc['lon'])}' target=_blank>see on map</a> <code>{geo.coords(loc['lat'], loc['lon'])}</code>" if ok else ""
        conf = ("<span class=ok>confirmed</span>" if loc.get("confirmed") else
                (f"<form method=post action=/run/{run_id}/stage2/confirm/{m['id']} style='display:inline'><button>Position is correct</button></form>" if ok else ""))
        body += (f"<p><b>{escape(m['name'])}</b>: {escape(loc.get('label', '') or 'location not found')} "
                 f"<span class=muted>({escape(loc.get('source', ''))})</span>{link} {conf}</p>"
                 f"<form method=post action=/run/{run_id}/stage2/locate/{m['id']}>Correct position (right click the spot in Google Maps and copy the numbers): "
                 "<input type=text name=latlon placeholder='1.4300, 103.8350'><button>Set</button></form>")
    body += (f"<h3>Add a danger area yourself</h3><form method=post action=/run/{run_id}/stage2/add>"
             "<select name=route><option>primary</option><option>secondary</option></select> Road row # <input type=text name=row size=4> "
             f"<select name=cat>{''.join(f'<option value={k}>{escape(v)}</option>' for k, v in CATS.items() if k not in (8, 11, 12))}</select><br>"
             "<input type=text name=text placeholder='What is the danger and why'><button>Add</button></form>")
    grids = ""
    for rn, grid in stage2.checklist(run).items():
        head_r = "".join(f"<th>{n}</th>" for n in range(1, 13))
        grows = "".join(f"<tr><td>{i}</td><td>{escape(run['routes'][rn]['data']['rows'][i - 1]['road'])}</td>" + "".join(f"<td class=g>{SYM[c]}</td>" for c in g) + "</tr>"
                        for i, g in enumerate(grid, 1))
        grids += f"<h4>{rn}</h4><table><tr><th>#</th><th>Road</th>{head_r}</tr>{grows}</table>"
    key = "<p class=muted>H = danger recorded, s = screened none, ? = unable to verify, n/a = out of scope. Items: " + "; ".join(f"{k} {v}" for k, v in CATS.items()) + "</p>"
    body += f"<details><summary><b>Full 12 point checklist per road</b></summary>{grids}{key}</details>"
    dg = stage2.diagnostics(run)
    body += ("<details><summary><b>Diagnostics (send me a screenshot of this if results look wrong)</b></summary>"
             f"<p>Screening points: {dg['points']} | usable images: {dg['usable']} | no Street View coverage: {dg['nocov']} | image unusable: {dg['unusable']} | not screened: {dg['missing']}</p>"
             f"<p>Street View answers: {escape(str(dg['sv']))}</p><p>TomTom manoeuvre types: {escape(str(dg['maneuvers']))}</p>"
             + "".join(f"<p class=muted>{escape(x)}</p>" for x in dg["sample"]) + "</details>")
    body += f"<form method=post action=/run/{run_id}/stage2/approve><button>Approve Gate 2 (danger areas)</button></form>"
    body += f"<form method=get action=/run/{run_id}/stage3><button>Go to Stage 3: revised times</button></form>"
    return page(body)


@router.get("/run/{run_id}/view/{name}.jpg")
def view_image(request: Request, run_id: str, name: str):
    """The exact image (with the yellow path line) that the model was shown for a danger area."""
    if (r := login_needed(request)):
        return r
    import re
    from fastapi.responses import Response
    if not re.match(r"^[0-9a-f]{16}$", name) or not store.ID_RE.match(run_id):
        return Response(status_code=404)
    data = store.load_blob(f"views/{run_id}/{name}.jpg")
    return Response(content=data, media_type="image/jpeg") if data else Response(status_code=404)


@router.post("/run/{run_id}/stage2/start")
def start(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if run and run["gates"]["1"] == "approved" and stage2.ready(run):
        stage2.start(run_id)
    return back(run_id)


@router.post("/run/{run_id}/stage2/review")
async def review(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    form = await request.form()
    if run:
        for h in run.get("hazards", []):
            v = form.get(f"review_{h['id']}")
            if v in REVIEW:
                h["review"] = v
            if f"note_{h['id']}" in form:
                h["note"] = str(form.get(f"note_{h['id']}", ""))[:300]
        run["gates"]["2"] = "pending"
        store.save_run(run)
    return back(run_id)


@router.post("/run/{run_id}/stage2/add")
def add(request: Request, run_id: str, route: str = Form("primary"), row: int = Form(...), cat: int = Form(...), text: str = Form("")):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    try:
        stage2.add_manual(run, route, row, cat, text.strip()[:300] or "Added manually")
        run["gates"]["2"] = "pending"
        store.save_run(run)
    except (KeyError, IndexError, TypeError):
        return back(run_id, "That+route+or+row+number+does+not+exist")
    return back(run_id)


@router.post("/run/{run_id}/stage2/locate/{mid}")
def locate(request: Request, run_id: str, mid: str, latlon: str = Form("")):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    try:
        lat, lon = [float(x) for x in latlon.replace(";", ",").split(",")[:2]]
        assert 1.0 < lat < 1.6 and 103.5 < lon < 104.2
    except (ValueError, AssertionError):
        return back(run_id, "Enter+latitude,+longitude+inside+Singapore")
    loc = {"lat": lat, "lon": lon, "label": "Position entered by you", "source": "manual", "confirmed": True}
    run.setdefault("mandatory", {})[mid] = loc
    stage2.remember_mandatory(mid, loc)
    run["screen"] = {"status": "stale", "done": 0, "total": 0, "error": ""}
    run["gates"]["2"] = "pending"
    store.save_run(run)
    return back(run_id)


@router.post("/run/{run_id}/stage2/confirm/{mid}")
def confirm(request: Request, run_id: str, mid: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if run and run.get("mandatory", {}).get(mid, {}).get("lat") is not None:
        run["mandatory"][mid]["confirmed"] = True
        stage2.remember_mandatory(mid, run["mandatory"][mid])
        store.save_run(run)
    return back(run_id)


@router.post("/run/{run_id}/stage2/approve")
def approve(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run or (run.get("screen") or {}).get("status") != "done":
        return back(run_id)
    if not all(run.get("mandatory", {}).get(m["id"], {}).get("confirmed") for m in stage2.MANDATORY):
        return back(run_id, "Confirm+the+fixed+danger+area+positions+first")
    run["gates"]["2"] = "approved"
    store.save_run(run)
    return back(run_id)

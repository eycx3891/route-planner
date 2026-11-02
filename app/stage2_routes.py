"""Stage 2 pages: run screening, review hazards, Gate 2."""
from html import escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import geo, stage2, store
from .ui import login_needed, page, table
from .vision import CATS

router = APIRouter()
REVIEW = {"pending": "Pending", "confirmed": "Confirmed", "rejected": "Rejected"}
SYM = {"H": "<b class=hz>H</b>", "S": "s", "N": "n/a", "U": "<b>?</b>"}


def back(run_id, error=""):
    return RedirectResponse(f"/run/{run_id}/stage2" + (f"?error={error}" if error else ""), status_code=303)


def visible(run):
    return [h for h in run.get("hazards", []) if h.get("review") != "rejected" and h.get("applicable", True)]


def dang_cells(run, rn):
    out = {}
    for h in visible(run):
        if rn in h["routes"]:
            why = (h["details"][0]["what"] if h["details"] else "")[:110]
            out.setdefault(h["routes"][rn], []).append(f"<span class=pill>{h['id']}</span> {escape(why)}")
    return {k: "<br>".join(v) for k, v in out.items()}


def hazard_row(h, run):
    cats = ", ".join(f"{c} {CATS.get(c, 'Operator designated')}" for c in sorted(h["cats"]))
    det = "<br>".join(f"{escape(d['what'])} <span class=muted>(risk: {escape(d['risk'])}; do: {escape(d['precaution'])}; "
                      f"{escape(d['src'])}, {escape(d['conf'])}; {escape(d['evidence'])})</span>" for d in h["details"])
    where = ", ".join(f"{rn} row {r + 1}" for rn, r in h["routes"].items()) or "not on either route"
    links = ""
    if h.get("lat") is not None:
        links = (f"<a href='{geo.sv_link(h['lat'], h['lon'], h.get('heading', 0))}' target=_blank>Street View</a> | "
                 f"<a href='{geo.map_link(h['lat'], h['lon'])}' target=_blank>Map</a> | "
                 f"<a href='{geo.traffic_link(h['lat'], h['lon'])}' target=_blank>Live traffic (Google)</a>")
    title = escape(h.get("name", "")) + (" " if h.get("name") else "")
    na = "" if h.get("applicable", True) else " <b>(CHECKED: route does not pass here, not applicable)</b>"
    opts = "".join(f"<option value={k}{' selected' if h.get('review') == k else ''}>{v}</option>" for k, v in REVIEW.items())
    return (f"<tr><td><span class=pill>{h['id']}</span></td><td>{title}{na}{escape(cats)}<br>{det}</td><td>{where}</td>"
            f"<td>{links}</td><td><select name=review_{h['id']}>{opts}</select><br>"
            f"<input type=text name=note_{h['id']} placeholder=note value='{escape(h.get('note', ''), quote=True)}'></td></tr>")


@router.get("/run/{run_id}/stage2", response_class=HTMLResponse)
def stage2_page(request: Request, run_id: str, error: str = ""):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    err = f"<p class=banner>{escape(error)}</p>" if error else ""
    head = f"<p><a href=/run/{run_id}>Back to Stage 1</a></p><h2>Stage 2: danger areas</h2>"
    if run["gates"]["1"] != "approved":
        return page(head + "<p class=banner>Approve Gate 1 first.</p>")
    if not stage2.ready(run):
        return page(head + "<p class=banner>This run was made with an older version. Plan it again from the home page.</p>")
    sc = run.get("screen") or {}
    st = sc.get("status", "none")
    refresh = "<meta http-equiv=refresh content=5>" if st in ("running", "queued") else ""
    if st in ("running", "queued"):
        msg = f"<p class=banner style='color:#036;border-color:#036'>Screening in progress: {sc.get('done', 0)} of {sc.get('total', '?')} points. This page refreshes itself.</p>"
    elif st == "error":
        msg = f"<p class=banner>Screening stopped: {escape(sc.get('error', ''))}. Press the button to resume (finished points are kept).</p>"
    elif st == "stale":
        msg = "<p class=banner>Routes changed. Re-run screening (finished points are reused).</p>"
    elif st == "done":
        msg = f"<p class=ok>Screening finished {escape(sc.get('finished', ''))}. Imagery dates are shown per hazard.</p>"
    else:
        msg = "<p>Screening has not been run yet.</p>"
    inc = run.get("incidents")
    if st == "done" and inc:
        msg += (f"<p class=banner>Live roadworks feed not used: {escape(inc['error'])}</p>" if inc.get("error")
                else f"<p class=muted>TomTom roadworks and closures checked {escape(inc['fetched'])}: {len(inc['items'])} found in the area.</p>")
    btn = "" if st in ("running", "queued") else (
        f"<form method=post action=/run/{run_id}/stage2/start><button>{'Re-run' if st == 'done' else 'Run'} screening "
        "(uses Street View and Claude, a few minutes)</button></form>")
    body = refresh + head + err + msg + btn
    if st == "done":
        gate = run["gates"].get("2", "pending")
        pending = sum(1 for h in visible(run) if h.get("review") == "pending")
        unconf = [m["id"] for m in stage2.MANDATORY if not run.get("mandatory", {}).get(m["id"], {}).get("confirmed")]
        if unconf:
            body += (f"<p class=banner>Confirm the position of {', '.join(unconf)} below before approving Gate 2. "
                     "Every route passing within 100 m of them is flagged automatically.</p>")
        body += ("<p class=ok>GATE 2 APPROVED. Still NOT APPROVED FOR USE until all gates pass.</p>" if gate == "approved"
                 else f"<p class=banner>GATE 2 PENDING: {pending} hazard(s) awaiting your review. NOT APPROVED FOR USE</p>")
        for rn, colour, title in (("primary", "blue", "PRIMARY ROUTE with danger areas"), ("secondary", "red", "SECONDARY ROUTE with danger areas")):
            if run["routes"].get(rn):
                body += table(run["routes"][rn], colour, title, [("Danger areas", dang_cells(run, rn)), ("Landmark at turn", stage2.landmarks_for(run, rn))],
                              stage2.mandatory_status(run, rn))
        body += "<h3>Hazard register</h3><p class=muted>Open each Street View link, then set Confirmed or Rejected. Rejected hazards disappear from the tables.</p>"
        body += (f"<form method=post action=/run/{run_id}/stage2/review><table><tr><th>ID</th><th>What and why</th><th>Routes</th>"
                 f"<th>Check</th><th>Your review</th></tr>{''.join(hazard_row(h, run) for h in run.get('hazards', []))}</table>"
                 "<button>Save review</button></form>")
        body += "<h3>Operator designated locations</h3>"
        for m in stage2.MANDATORY:
            loc = run.get("mandatory", {}).get(m["id"], {})
            ok = loc.get("lat") is not None
            link = f" <a href='{geo.map_link(loc['lat'], loc['lon'])}' target=_blank>see on map</a>" if ok else ""
            conf = ("<span class=ok>confirmed</span>" if loc.get("confirmed") else
                    (f"<form method=post action=/run/{run_id}/stage2/confirm/{m['id']} style='display:inline'><button>Position is correct</button></form>" if ok else ""))
            body += (f"<p><b>{m['id']} {escape(m['name'])}</b>: {escape(loc.get('label', '') or 'location not found')} "
                     f"<span class=muted>({escape(loc.get('source', ''))})</span>{link} {conf}</p>"
                     f"<form method=post action=/run/{run_id}/stage2/locate/{m['id']}>Correct position (right click the spot in Google Maps and copy the numbers): "
                     "<input type=text name=latlon placeholder='1.4300, 103.8350'><button>Set</button></form>")
        body += (f"<h3>Add a hazard yourself</h3><form method=post action=/run/{run_id}/stage2/add>"
                 "<select name=route><option>primary</option><option>secondary</option></select> "
                 "Road row # <input type=text name=row size=4> "
                 f"<select name=cat>{''.join(f'<option value={k}>{k} {escape(v)}</option>' for k, v in CATS.items() if k not in (8, 11, 12))}</select><br>"
                 "<input type=text name=text placeholder='What is the danger and why'><button>Add</button></form>")
        grids = ""
        cl = stage2.checklist(run)
        for rn, grid in cl.items():
            head_r = "".join(f"<th>{n}</th>" for n in range(1, 13))
            rows = "".join(f"<tr><td>{i}</td><td>{escape(run['routes'][rn]['data']['rows'][i - 1]['road'])}</td>"
                           + "".join(f"<td class=g>{SYM[c]}</td>" for c in g) + "</tr>" for i, g in enumerate(grid, 1))
            grids += f"<h4>{rn}</h4><table><tr><th>#</th><th>Road</th>{head_r}</tr>{rows}</table>"
        key = "<p class=muted>H = hazard recorded, s = screened no hazard, ? = unable to verify (no usable image), n/a = out of scope. Items: " + "; ".join(f"{k} {v}" for k, v in CATS.items()) + "</p>"
        body += f"<details><summary><b>Full 12 point checklist per road</b></summary>{grids}{key}</details>"
        body += f"<form method=post action=/run/{run_id}/stage2/approve><button>Approve Gate 2 (danger areas)</button></form>"
    return page(body)


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
    run.setdefault("mandatory", {})[mid] = {"lat": lat, "lon": lon, "label": "Position entered by you", "source": "manual", "confirmed": True}
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
        return back(run_id, "Confirm+the+D1+and+D2+positions+first")
    run["gates"]["2"] = "approved"
    store.save_run(run)
    return back(run_id)

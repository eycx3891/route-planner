"""Stage 4 page: find, compare and choose rest stops; see the route with the stops added; Gate 4."""
import time
from html import escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import geo, stage2, stage3, stage4, store
from .stage2_routes import dang_cells
from .ui import fmt_dist, fmt_time, login_needed, nav, page

router = APIRouter()
CELL = {"PASS": "<b class=ok>pass</b>", "FAIL": "<b class=hz>FAIL</b>", "UNVERIFIED": "<b>?</b>", "NA": "<span class=muted>n/a</span>"}


def back(run_id, error=""):
    return RedirectResponse(f"/run/{run_id}/stage4" + (f"?error={error}" if error else ""), status_code=303)


def live(s) -> bool:
    return bool(s) and s.get("status") in ("running", "queued") and time.time() - s.get("beat", 0) <= 180


def cand_row(run_id, rn, slot, c, chosen) -> str:
    grid = "".join(f"<td title='{escape(x['why'], quote=True)}'>{CELL[x['status']]}</td>" for x in c["criteria"])
    rt = c["route"]
    detour = "route check failed" if rt.get("error") else f"+{fmt_time(max(rt['extra_s'], 0))}, +{fmt_dist(max(rt['extra_m'], 0))}"
    links = (f"<a href='{c['url']}' target=_blank>Google Maps</a> | " if c.get("url") else "") + f"<a href='{geo.sv_link(c['lat'], c['lon'], 0, (c.get('sv') or {}).get('pano'))}' target=_blank>Street View</a>"
    verdict = "<b class=ok>ACCEPTABLE</b>" if c["verdict"] == "ACCEPTABLE" else "<b class=hz>PROVISIONAL: SUITABILITY NOT FULLY VERIFIED</b>"
    why = "<br>".join(escape(u) for u in c["unresolved"])
    if chosen:
        btn = (f"<span class=ok>CHOSEN</span><form method=post action=/run/{run_id}/stage4/clear style='display:inline'>"
               f"<input type=hidden name=rn value={rn}><input type=hidden name=slot value={slot}><button>Change</button></form>")
    else:
        btn = (f"<form method=post action=/run/{run_id}/stage4/choose><input type=hidden name=rn value={rn}><input type=hidden name=slot value={slot}>"
               f"<input type=hidden name=cid value={c['id']}><button>Use this one</button></form>")
    return (f"<tr><td><b>{escape(c['name'])}</b><br><span class=muted>{escape(c['address'])}</span><br>{links}</td>"
            f"<td>{c['dist_route_m']} m<br><span class=muted>at minute {round(c.get('at_s', 0) / 60)}</span></td><td>{detour}</td>{grid}<td>{verdict}<br><span class=muted>{why}</span></td><td>{btn}</td></tr>")


def stopped_table(run, rn, hazards, colour, title) -> str:
    st = run["routes"][rn]["stopped"]
    d = st["data"]
    c = stage3.calc(d, run.get("contingency_min", 0))
    base_rev = stage3.calc(run["routes"][rn]["data"])["tot_rev"]
    dang = dang_cells({}, rn, routes=stage4.final_view(run)[0], hazards=hazards) if hazards is not None else {}
    by_row = {}
    for s in st["stops"]:
        by_row.setdefault(s["row"], []).append(s)
    body = ""
    for i, (r, x) in enumerate(zip(c["rows"], d["rows"])):
        cls = "detour" if i in st["detour_rows"] else ""
        tag = " <b>(detour)</b>" if cls else ""
        body += (f"<tr class={cls}><td>{i + 1}</td><td>{escape(x['road'])}{tag}</td><td>{escape(x['action'])}</td><td>{fmt_dist(r['dist_m'])}</td>"
                 f"<td>{fmt_time(r['base'])}</td><td><b>{fmt_time(r['rev'])}</b></td><td>{dang.get(i, '')}</td></tr>")
        for s in by_row.get(i, []):
            body += (f"<tr class=rest><td></td><td>REST STOP {s['rid']}</td><td colspan=3>{escape(s['name'])}, {escape(s['address'])}</td>"
                     f"<td>{fmt_time(stage4.ALLOW_S)}</td><td></td></tr>")
    n = len(st["stops"])
    rest = n * stage4.ALLOW_S
    total = c["tot_rev"] + rest + c["contingency_s"]
    longest_warn = "" if st["longest_s"] <= 3600 else f" <b class=hz>(over the 60 minute limit)</b>"
    reps = "".join(f"<p class=muted><b>Deviation:</b> {escape(r['reason'])}. Detour {escape(r['via'])}.</p>" for r in st["repairs"])
    unclean = "" if st["clean"] else "<p class=banner>This route with stops still contains a U-turn. Choose a different stop.</p>"
    detail = ""
    for s in st["stops"]:
        ing = ", ".join(f"{escape(d['rows'][i]['road'])} ({fmt_dist(d['rows'][i]['dist_m'])})" for i in st["detour_rows"] if i <= s["row"]) or "no separate approach road"
        egr = ", ".join(f"{escape(d['rows'][i]['road'])} ({fmt_dist(d['rows'][i]['dist_m'])})" for i in st["detour_rows"] if i > s["row"]) or "rejoins the route directly"
        a3 = next(x for x in s["criteria"] if x["n"] == 3)
        a5 = next(x for x in s["criteria"] if x["n"] == 5)
        label = "ACCEPTABLE" if s["verdict"] == "ACCEPTABLE" else "PROVISIONAL: SUITABILITY NOT FULLY VERIFIED"
        unres = "".join(f"<li>{escape(u)}</li>" for u in s["unresolved"])
        sv = s["sv"]
        detail += (f"<div><h4>{s['rid']}: {escape(s['name'])}</h4><p><b>Address:</b> {escape(s['address'])} "
                   f"<a href='{geo.map_link(s['lat'], s['lon'])}' target=_blank>map</a> | <a href='{geo.sv_link(s['lat'], s['lon'], 0, (s.get('sv') or {}).get('pano'))}' target=_blank>Street View</a></p>"
                   f"<p><b>Status:</b> <b class={'ok' if label == 'ACCEPTABLE' else 'hz'}>{label}</b></p>{('<ul>' + unres + '</ul>') if unres else ''}"
                   f"<p><b>Approach:</b> {ing}. U-turn check: {a3['status']}.<br><b>Exit and re-entry:</b> {egr}. U-turn check: {a5['status']}.</p>"
                   f"<p class=muted>Street View notes: kind {escape(str(sv.get('kind', '?')))}, space {escape(str(sv.get('space', '?')))}, "
                   f"barrier seen: {sv.get('barrier', '?')}. {escape(str(sv.get('notes', '')))}</p></div>")
    return (f"<div class={colour}><h3>{title} with rest stops</h3>{unclean}{reps}<table><tr><th>#</th><th>Road</th><th>First action</th><th>Distance</th>"
            f"<th>Baseline time</th><th>REVISED TRAVEL TIME</th><th>Danger areas</th></tr>{body}"
            f"<tr class=total><td></td><td>TOTAL DRIVING</td><td></td><td>{fmt_dist(c['tot_dist'])}</td><td>{fmt_time(c['tot_base'])}</td><td>{fmt_time(c['tot_rev'])}</td><td></td></tr></table>"
            f"<table><tr><th>Driving (revised)</th><td>{fmt_time(c['tot_rev'])} <span class=muted>(detour adds {fmt_dist(max(st['extra_m'], 0))} and {fmt_time(max(c['tot_rev'] - base_rev, 0))})</span></td></tr>"
            f"<tr><th>Rest stops</th><td>{n} x {fmt_time(stage4.ALLOW_S)} = {fmt_time(rest)} <span class=muted>(15 min rest + 5 min entry and exit each)</span></td></tr>"
            f"<tr><th>Contingency</th><td>{fmt_time(c['contingency_s'])}</td></tr><tr><th>Total journey</th><td><b>{fmt_time(total)}</b></td></tr>"
            f"<tr><th>Longest driving stretch</th><td>{fmt_time(st['longest_s'])}{longest_warn}</td></tr></table>{detail}</div>")


@router.get("/run/{run_id}/stage4", response_class=HTMLResponse)
def stage4_page(request: Request, run_id: str, error: str = ""):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    head = nav(run, 4) + "<h2>Stage 4: rest stops</h2>"
    if run["gates"]["1"] != "approved":
        return page(head + "<p class=banner>Approve Gate 1 first.</p>")
    if not stage2.ready(run):
        return page(head + "<p class=banner>This run was made with an older version. Plan it again from the home page.</p>")
    err = f"<p class=banner>{escape(error)}</p>" if error else ""
    need = stage4.needing(run)
    summary = ""
    for rn in stage2.routes_of(run):
        c = stage3.calc(run["routes"][rn]["data"])
        summary += (f"<p><b>{rn}:</b> revised driving time {fmt_time(c['tot_rev'])}. " +
                    (f"<b class=hz>{c['stops']} rest stop(s) required.</b>" if c["stops"] else "No rest stop needed.") + "</p>")
    if not need:
        return page(head + err + summary + "<p class=ok>No rest stop is needed on either route, so Gate 4 is not required.</p>"
                    f"<form method=get action=/run/{run_id}/stage5><button>Go to Stage 5: driver reference</button></form>")
    s4 = run.get("stage4") or {}
    st = s4.get("status", "none")
    stalled = st in ("running", "queued") and not live(s4)
    fs = run.get("final_screen") or {}
    refresh = "<meta http-equiv=refresh content=5>" if live(s4) or live(fs) else ""
    if live(s4):
        msg = f"<p class=banner style='color:#036;border-color:#036'>Searching: {escape(s4.get('phase', ''))}. This page refreshes itself.</p>"
    elif stalled:
        msg = "<p class=banner>The search looks stalled (the app was probably restarted). Press Restart.</p>"
    elif st == "error":
        msg = f"<p class=banner>Search stopped: {escape(s4.get('error', ''))}</p>"
    elif st == "done":
        msg = "<p class=ok>Search finished. Choose one stop for each slot.</p>"
    else:
        msg = "<p>Rest stops have not been searched for yet.</p>"
    label = "Restart" if stalled else ("Search again" if st == "done" else "Find rest stops")
    btn = "" if live(s4) else (f"<form method=post action=/run/{run_id}/stage4/find><button>{label} (Google Places, Street View and Claude, a few minutes)</button></form>"
                               + ("<p class=muted>Searching again clears your choices.</p>" if st == "done" else ""))
    body = refresh + head + err + summary + msg + btn
    hazards = None
    if st == "done":
        crit = "".join(f"<th title='{n}'>{n} {name}</th>" for n, name in stage4.CRIT.items())
        for rn, (n, total) in need.items():
            colour = "blue" if rn == "primary" else "red"
            body += f"<div class={colour}><h3>{rn.upper()} ROUTE: {n} rest stop(s) needed</h3>"
            for sl in (s4.get("slots", {}).get(rn) or []):
                chosen = s4.get("choices", {}).get(rn, {}).get(str(sl["slot"]))
                body += f"<h4>Rest stop {sl['slot']} (around minute {sl['target_min']} of driving)</h4>"
                if not sl["cands"]:
                    body += "<p class=banner>No candidate found within 2.5 km of the route here. Try searching again or add a via point.</p>"
                    continue
                rows = "".join(cand_row(run_id, rn, sl["slot"], c, c["id"] == chosen) for c in sl["cands"])
                body += (f"<table><tr><th>Place</th><th>Off route</th><th>Detour</th>{crit}<th>Verdict</th><th></th></tr>{rows}</table>")
            body += "</div>"
        body += "<p class=muted>Hover over a criterion to see why. pass = established, FAIL = not suitable, ? = could not be verified, n/a = out of scope for this exercise.</p>"
        if stage4.all_chosen(run):
            routes, hazards = stage4.final_view(run)
            if hazards is None:
                if not live(fs) and fs.get("status") != "error":
                    stage4.start_final(run_id)
                    fs = {"status": "queued", "beat": time.time()}
                if fs.get("status") == "error":
                    body += (f"<p class=banner>Re-screening the changed route stopped: {escape(fs.get('error', ''))}</p>"
                             f"<form method=post action=/run/{run_id}/stage4/refinal><button>Try the re-screen again</button></form>")
                else:
                    body += (f"<p class=banner style='color:#036;border-color:#036'>Re-screening the detour for danger areas: "
                             f"{fs.get('done', 0)} of {fs.get('total', '?')} points. This page refreshes itself.</p>")
            for rn in need:
                body += stopped_table(run, rn, hazards, "blue" if rn == "primary" else "red", rn.upper() + " ROUTE")
            if hazards is not None:
                g4 = run["gates"].get("4")
                body += ("<p class=ok>GATE 4 APPROVED. Still NOT APPROVED FOR USE until all gates pass.</p>" if g4 == "approved"
                         else "<p class=banner>GATE 4 PENDING: review any PROVISIONAL stop above. NOT APPROVED FOR USE</p>")
                body += f"<form method=post action=/run/{run_id}/stage4/approve><button>Approve Gate 4 (I have reviewed the unresolved criteria)</button></form>"
                if g4 == "approved":
                    body += f"<form method=get action=/run/{run_id}/stage5><button>Go to Stage 5: driver reference</button></form>"
    return page(body)


@router.post("/run/{run_id}/stage4/find")
def find(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if run and run["gates"]["1"] == "approved":
        stage4.start_find(run_id)
    return back(run_id)


@router.post("/run/{run_id}/stage4/choose")
def choose(request: Request, run_id: str, rn: str = Form(...), slot: int = Form(...), cid: str = Form(...)):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    try:
        stage4.choose(run, rn, slot, cid)
        store.save_run(run)
    except Exception as e:
        return back(run_id, str(e)[:200].replace(" ", "+"))
    return back(run_id)


@router.post("/run/{run_id}/stage4/clear")
def clear(request: Request, run_id: str, rn: str = Form(...), slot: int = Form(...)):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    try:
        stage4.unchoose(run, rn, slot)
        store.save_run(run)
    except Exception as e:
        return back(run_id, str(e)[:200].replace(" ", "+"))
    return back(run_id)


@router.post("/run/{run_id}/stage4/refinal")
def refinal(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if run:
        run["final_screen"] = {"status": "none"}
        store.save_run(run)
        stage4.start_final(run_id)
    return back(run_id)


@router.post("/run/{run_id}/stage4/approve")
def approve(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run or not stage4.all_chosen(run) or stage4.final_view(run)[1] is None:
        return back(run_id, "Choose+all+rest+stops+and+wait+for+the+re-screen+first")
    run["gates"]["4"] = "approved"
    store.save_run(run)
    return back(run_id)

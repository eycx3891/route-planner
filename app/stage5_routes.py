"""Stage 5 page: freeze the route version and show the driver reference."""
import time
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import stage2, stage4, stage5, store
from .ui import fmt_dist, fmt_time, login_needed, page

router = APIRouter()
ESCALATE = ("<p><b>If you are unsure at any point:</b> do not guess. Stop at the nearest safe, lawful place, tell the responsible person, and get updated routing. "
            "Never make a U-turn, reverse into traffic, or take a tunnel or underpass. Contingency routes come in Stage 7.</p>")


def route_block(rn, r, colour, title) -> str:
    rows = ""
    by_row = {}
    for s in r["stops"]:
        by_row.setdefault(s["row"], []).append(s)
    last_of_row = {}
    for s in r["steps"]:
        last_of_row[s["row"]] = s["n"]
    for s in r["steps"]:
        hz = "<br>".join(f"<b class=hz>{escape(h['id'])}</b> {escape(h['precaution'] or h['what'])}" for h in s["hazards"])
        lm = f"<br><span class=muted>Landmarks seen: {escape('; '.join(s['landmarks']))}</span>" if s["landmarks"] else ""
        oh = f"<br><span class=muted>Overhead: {escape('; '.join(s['overhead']))}</span>" if s["overhead"] else ""
        lane_cls = "hz" if s["lane_status"] == "missing" else ""
        view = f" <a href='{s['view']}' target=_blank>view</a>" if s["view"] else ""
        unc = escape(s["uncertainty"]) + (" " if s["uncertainty"] else "") + (f"Imagery {escape(s['imagery_date'])}." if s["imagery_date"] else "")
        cls = "bad" if s["uturn"] else ""
        dist = fmt_dist(s["distance_m"]) if s["distance_m"] is not None else ""
        rows += (f"<tr class={cls}><td>{s['n']}</td><td><b>{escape(s['road'])}</b><br><span class=muted>heading {escape(s['direction'] or 'n/a')}</span></td><td>{dist}</td>"
                 f"<td>{escape(s['instruction'])}{lm}{oh}</td><td class={lane_cls}>{escape(s['lane'])}{view}</td><td>{hz}</td><td class=muted>{unc}</td></tr>")
        if last_of_row.get(s["row"]) == s["n"]:
            for p in by_row.get(s["row"], []):
                rows += (f"<tr class=rest><td></td><td>REST STOP {escape(p['rid'])}</td><td></td><td colspan=4>{escape(p['name'])}, {escape(p['address'])}. "
                         f"Approach: {escape(', '.join(p['approach']) or 'direct')}. Parking: {escape(p['parking'])}. Exit and re-entry: {escape(', '.join(p['exit']) or 'rejoin directly')}. "
                         f"Allow {fmt_time(stage4.ALLOW_S)}. Status: {escape(p['verdict'])}.</td></tr>")
    total = r["tot_rev"] + r["rest_s"] + r["contingency_s"]
    return (f"<div class={colour}><h3>{title}</h3><p>Distance {fmt_dist(r['tot_m'])}. Driving {fmt_time(r['tot_rev'])} (revised, 50 km/h cap). "
            f"Rest {fmt_time(r['rest_s'])}. Contingency {fmt_time(r['contingency_s'])}. <b>Total {fmt_time(total)}.</b></p>"
            f"<table><tr><th>#</th><th>Road and direction</th><th>Distance</th><th>Instruction</th><th>Lane</th><th>Hazards</th><th>Evidence and uncertainty</th></tr>{rows}</table></div>")


@router.get("/run/{run_id}/stage5", response_class=HTMLResponse)
def stage5_page(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    head = f"<p><a href=/run/{run_id}/stage4>Back to Stage 4</a> | <a href=/run/{run_id}>Stage 1</a></p><h2>Stage 5: driver reference</h2>"
    if not stage2.ready(run):
        return page(head + "<p class=banner>This run was made with an older version. Plan it again from the home page.</p>")
    state = stage5.ready_state(run)
    checks = "".join(f"<li>{'<span class=ok>done</span>' if ok else '<b class=hz>missing</b>'} <a href='{link}'>{escape(lbl)}</a></li>" for lbl, ok, link in state)
    ds = run.get("driver_status") or {}
    live = ds.get("status") in ("running", "queued") and time.time() - ds.get("beat", 0) <= 240
    stalled = ds.get("status") in ("running", "queued") and not live
    body = head + f"<ul>{checks}</ul>"
    if live:
        body = "<meta http-equiv=refresh content=5>" + body + f"<p class=banner style='color:#036;border-color:#036'>Writing the driver reference: {escape(ds.get('phase', ''))}. This page refreshes itself.</p>"
    elif stalled:
        body += "<p class=banner>Generation looks stalled (the app was probably restarted). Press the button again.</p>"
    elif ds.get("status") == "error":
        body += f"<p class=banner>Stopped: {escape(ds.get('error', ''))}</p>"
    all_ok = all(ok for _, ok, _ in state)
    fr = run.get("frozen")
    if not live:
        if all_ok:
            body += (f"<form method=post action=/run/{run_id}/stage5/generate><button>{'Freeze again and regenerate' if fr else 'Freeze this route version and generate the driver reference'}</button></form>"
                     "<p class=muted>Freezing records this exact version of the routes, hazards, times and rest stops. Any later change to the route clears it.</p>")
        else:
            body += "<p class=banner>Finish the missing items above first.</p>"
    if fr and fr["version"] == run["version"]:
        g5 = run["gates"].get("5") == "approved"
        body += (f"<p class=muted>Frozen version {fr['version']} at {escape(fr['at'])}.</p>" +
                 ("<p class=ok>DRIVER REFERENCE APPROVED. Re-check every item against Google Maps before any use.</p>" if g5 else
                  "<p class=banner>NOT APPROVED FOR USE (provisional): lane and landmark detail comes from dated imagery and has not been confirmed by a person.</p>"))
        if fr["unresolved"]:
            body += "<h3>Unresolved checks</h3><ul>" + "".join(f"<li>{escape(u)}</li>" for u in fr["unresolved"]) + "</ul>"
        for rn, colour in (("primary", "blue"), ("secondary", "red")):
            if rn in fr["routes"]:
                body += route_block(rn, fr["routes"][rn], colour, f"{rn.upper()} ROUTE")
        body += ("<h3>Restrictions and uncertainty</h3><ul><li>No tunnels, underpasses or U-turns. Roads that pass under an overpass are allowed.</li>"
                 "<li>Landmarks, lanes and overhead clearances are read from dated Street View images, so roadworks and layout changes since then are not shown.</li>"
                 "<li>Where a lane is marked <b>Not established</b>, nothing was visible or available. Use the view link to check.</li></ul>" + ESCALATE)
        body += f"<form method=post action=/run/{run_id}/stage5/approve><button>Approve driver reference (Gate 5)</button></form>"
        body += f"<form method=get action=/run/{run_id}/stage6><button>Go to Stage 6: maps</button></form>"
    return page(body)


@router.post("/run/{run_id}/stage5/generate")
def generate(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if run and all(ok for _, ok, _ in stage5.ready_state(run)):
        stage5.start(run_id)
    return RedirectResponse(f"/run/{run_id}/stage5", status_code=303)


@router.post("/run/{run_id}/stage5/approve")
def approve(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if run and run.get("frozen") and run["frozen"]["version"] == run["version"]:
        run["gates"]["5"] = "approved"
        store.save_run(run)
    return RedirectResponse(f"/run/{run_id}/stage5", status_code=303)

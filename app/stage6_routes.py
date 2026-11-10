"""Stage 6 page: approve the map plan, generate maps, review checks, Gate 6."""
import re
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import mapdraw, stage2, stage6, store
from .ui import bubble, login_needed, nav, page

router = APIRouter()


@router.get("/run/{run_id}/map/{name}")
def map_file(request: Request, run_id: str, name: str):
    if (r := login_needed(request)):
        return r
    if not re.match(r"^[A-Za-z0-9_\-]+\.png$", name) or not store.ID_RE.match(run_id):
        return Response(status_code=404)
    data = store.load_blob(f"maps/{run_id}/{name}")
    return Response(content=data, media_type="image/png") if data else Response(status_code=404)


@router.get("/run/{run_id}/stage6", response_class=HTMLResponse)
def stage6_page(request: Request, run_id: str, error: str = "", detail: int = 0):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    head = nav(run, 6) + "<h2>Stage 6: maps</h2>"
    if not stage2.ready(run):
        return page(head + "<p class=banner>This run was made with an older version. Plan it again from the home page.</p>")
    state = stage6.ready_state(run)
    checks = "".join(f"<li>{'<span class=ok>done</span>' if ok else '<b class=hz>missing</b>'} <a href='{l}'>{escape(t)}</a></li>" for t, ok, l in state)
    err = f"<p class=banner>{escape(error)}</p>" if error else ""
    body = head + err + f"<ul>{checks}</ul>"
    if not all(ok for _, ok, _ in state):
        return page(body + "<p class=banner>Finish the missing items first. No image is made until the routes are approved and final.</p>")
    maps = run.get("maps")
    if maps and maps["version"] == run["version"]:
        g6 = run["gates"].get("6") == "approved"
        body += ("<p class=ok>GATE 6 APPROVED. Re-check every map against Google Maps before use.</p>" if g6 else
                 "<p class=banner>GATE 6 PENDING: PROVISIONAL, NOT APPROVED FOR USE</p>")
        body += f"<p>Detail level used: <b>{escape(stage6.DETAIL.get(maps.get('detail', 0), 'Standard'))}</b>. Put the maps in your deck in this order.</p>"
        sc = "".join(f"<li>{'<span class=ok>OK</span>' if c['ok'] else '<b class=hz>PROBLEM</b>'} {escape(c['what'])}: {escape(c['detail'])}</li>" for c in maps.get("series_checks", []))
        body += f"<h3>Checks across the whole series</h3><ul>{sc}</ul>"
        for it in maps["items"]:
            ck = "".join(f"<li>{'<span class=ok>OK</span>' if c['ok'] else '<b class=hz>MISMATCH</b>'} {escape(c['what'])}: {escape(c['detail'])}</li>" for c in it["checks"])
            hz = "".join(f"<tr><td><span class=pill>{escape(h['label'])}</span></td><td>{''.join(bubble(c) for c in h['cats'])}</td><td>{escape(h['action'])}</td><td><code>{escape(h['coords'])}</code></td></tr>" for h in it["hazards"])
            tbl = f"<table><tr><th>No.</th><th>Danger</th><th>What the truck is doing</th><th>Coordinates</th></tr>{hz}</table>" if hz else ""
            body += (f"<h3>{escape(it['id'])}: {escape(it['title'])}</h3><p class=muted>{escape(it['coverage'])}. {escape(it['why'])} "
                     f"<a href='{it['link']}' target=_blank>Open this view in Google Maps</a></p>"
                     f"<img src='/run/{run_id}/map/{escape(it['file'])}' style='max-width:100%;border:1px solid #bbb'>{tbl}<ul>{ck}</ul>")
        bad = [it["id"] for it in maps["items"] if not it["all_ok"]] + (["series"] if not all(c["ok"] for c in maps.get("series_checks", [])) else [])
        if bad:
            body += f"<p class=banner>Problems found: {', '.join(bad)}. Do not approve until fixed.</p>"
        body += (f"<form method=post action=/run/{run_id}/stage6/approve><button>Approve maps (Gate 6)</button></form>"
                 f"<form method=post action=/run/{run_id}/stage6/reset><button>Change the map plan</button></form>"
                 "<p class=muted>The checks compare the drawing with the frozen record. They cannot judge whether a map looks right, so look at each one.</p>")
        return page(body)
    try:
        specs = stage6.plan(run, detail)
    except Exception as e:
        return page(body + f"<p class=banner>Could not build the plan: {escape(str(e)[:200])}</p>")
    levels = " | ".join((f"<b>{n}</b>" if k == detail else f"<a href='/run/{run_id}/stage6?detail={k}'>{n}</a>") for k, n in stage6.DETAIL.items())
    rows = ""
    for s in specs:
        box = "" if s["compulsory"] else f"<input type=checkbox name=map value={s['id']} checked>"
        rows += (f"<tr><td>{box}</td><td><b>{escape(s['id'])}</b> {escape(s['title'])}</td><td>{escape(s['coverage'])}</td><td>{escape(s['purpose'])}</td>"
                 f"<td>{escape(s['why'])}{' <b>(needed)</b>' if s['compulsory'] else ''}</td></tr>")
    nser = sum(1 for s in specs if s["kind"] == "series")
    body += (f"<h3>Proposed map plan</h3><p>The maps follow each route from start to finish in order, so they work as consecutive slides. "
             "Each map is zoomed out as far as it can be while staying readable, and zoomed in only where a turn, exit or danger area needs a close view. "
             f"Danger areas are red boxes numbered D1, D2... in the order the truck meets them; rest stops are orange boxes. There is no legend, so add your own.</p>"
             f"<p>Detail level: {levels}. <span class=muted>({nser} route maps at this level. More detail means more, closer maps.)</span></p>"
             f"<form method=post action=/run/{run_id}/stage6/generate><input type=hidden name=detail value={detail}>"
             f"<table><tr><th></th><th>Map</th><th>Covers</th><th>What it shows</th><th>Why this zoom</th></tr>{rows}</table>"
             "<button>Approve this list and generate the maps</button></form>")
    return page(body)


@router.post("/run/{run_id}/stage6/generate")
async def generate(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    form = await request.form()
    if run and all(ok for _, ok, _ in stage6.ready_state(run)):
        try:
            stage6.generate(run, set(form.getlist("map")), int(form.get("detail", 0) or 0))
            store.save_run(run)
        except (mapdraw.MapError, Exception) as e:
            return RedirectResponse(f"/run/{run_id}/stage6?error=" + str(e)[:250].replace(" ", "+").replace("&", "and").replace("#", ""), status_code=303)
    return RedirectResponse(f"/run/{run_id}/stage6", status_code=303)


@router.post("/run/{run_id}/stage6/reset")
def reset(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if run:
        run["maps"] = None
        run["gates"]["6"] = "pending"
        store.save_run(run)
    return RedirectResponse(f"/run/{run_id}/stage6", status_code=303)


@router.post("/run/{run_id}/stage6/approve")
def approve(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    maps = (run or {}).get("maps")
    if not maps or maps["version"] != run["version"] or not all(it["all_ok"] for it in maps["items"]) or not all(c["ok"] for c in maps.get("series_checks", [])):
        return RedirectResponse(f"/run/{run_id}/stage6?error=Fix+the+mismatches+first", status_code=303)
    run["gates"]["6"] = "approved"
    store.save_run(run)
    return RedirectResponse(f"/run/{run_id}/stage6", status_code=303)

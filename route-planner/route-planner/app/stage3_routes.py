"""Stage 3 page: revised times table, graph, reconciliation, Gate 3."""
from html import escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import stage2, stage3, store
from .stage2_routes import dang_cells
from .ui import fmt_dist, fmt_time, login_needed, nav, page

router = APIRouter()
COLOURS = {"primary": "#2a6fd6", "secondary": "#d62a2a"}


def chart(rows, rd, colour) -> str:
    """Horizontal bars per road: grey = TomTom baseline, coloured = revised."""
    n = len(rows)
    top = max([max(r["base"], r["rev"]) for r in rows] + [1])
    h = 28 * n + 30
    out = [f"<svg viewBox='0 0 900 {h}' width='100%' role=img><title>Time per road</title>"]
    for i, (r, x) in enumerate(zip(rows, rd["rows"])):
        y = 10 + i * 28
        label = f"{i + 1}. {x['road']}"[:34]
        out.append(f"<text x=0 y={y + 12} font-size=12 fill='#333'>{escape(label)}</text>")
        out.append(f"<rect x=250 y={y} width={max(r['base'] / top * 560, 1):.0f} height=9 fill='#c9c9c9'/>")
        out.append(f"<rect x=250 y={y + 10} width={max(r['rev'] / top * 560, 1):.0f} height=9 fill='{colour}'/>")
        out.append(f"<text x={250 + max(r['rev'], r['base']) / top * 560 + 6:.0f} y={y + 17} font-size=11 fill='#333'>{fmt_time(r['rev'])}</text>")
    out.append("</svg>")
    return "".join(out)


def route_block(run, rn, colour_name, title) -> str:
    rd = run["routes"][rn]["data"]
    cont_min = run.get("contingency_min", 0)
    c = stage3.calc(rd, cont_min)
    col = COLOURS[rn]
    dang = dang_cells(run, rn) if run.get("hazards") is not None else {}
    body = []
    for i, (r, x) in enumerate(zip(c["rows"], rd["rows"])):
        v = f"{r['v']:.0f} km/h" if r["v"] else "n/a"
        adj = f"<b>+{fmt_time(r['adj'])}</b>" if r["adj"] > 0 else "no change"
        body.append(f"<tr><td>{i + 1}</td><td>{escape(x['road'])}</td><td>{escape(x['action'])}</td><td>{fmt_dist(r['dist_m'])}</td>"
                    f"<td>{fmt_time(r['base'])}</td><td>{v}</td><td><b>{fmt_time(r['rev'])}</b> <span class=muted>({adj})</span></td>"
                    f"<td>{dang.get(i, '')}</td></tr>")
    drive = c["tot_rev"]
    need = ("<b class=hz>REST STOP REQUIRED</b>: " + f"{c['stops']} stop(s) of at least 20 min (15 rest + 5 in and out) so no driving stretch exceeds 60 min. Stage 4 will place them."
            if c["stops"] else "No rest stop needed (driving 60 min or less).")
    total = drive + c["contingency_s"]
    checks = "".join(f"<li>{'<span class=ok>OK</span>' if v else '<b class=hz>FAILED</b>'} {escape(k)}</li>" for k, v in c["checks"].items())
    detail = ""
    for it in c["items"]:
        speed = "n/a" if not it["v"] else "%.1f" % it["v"]
        detail += (f"<tr><td>{it['n'] + 1}</td><td>{escape(it['road'])}</td><td>{it['dkm']:.3f}</td><td>{it['secs']}</td>"
                   f"<td>{speed}</td><td>{it['delta_min']:.2f}</td><td>{it['rev']:.1f}</td><td>{escape(it['note'])}</td></tr>")
    return (f"<div class={colour_name}><h3>{title}</h3><table><tr><th>#</th><th>Road</th><th>First action</th><th>Distance</th>"
            f"<th>Baseline time (TomTom)</th><th>Baseline speed</th><th>REVISED TRAVEL TIME (50 km/h)</th><th>Danger areas</th></tr>{''.join(body)}"
            f"<tr class=total><td></td><td>TOTAL</td><td></td><td>{fmt_dist(c['tot_dist'])}</td><td>{fmt_time(c['tot_base'])}</td><td></td>"
            f"<td>{fmt_time(c['tot_rev'])}</td><td></td></tr></table>"
            f"<table><tr><th>Driving time (revised)</th><td>{fmt_time(drive)}</td></tr>"
            f"<tr><th>Rest stops</th><td>{need} <span class=muted>Not yet added to the total.</span></td></tr>"
            f"<tr><th>Contingency allowance</th><td>{fmt_time(c['contingency_s'])} <span class=muted>(set below; shown separately from driving)</span></td></tr>"
            f"<tr><th>Driving plus contingency</th><td><b>{fmt_time(total)}</b></td></tr></table>"
            f"<p><b>Reconciliation</b> ({c['adjusted_rows']} of {len(c['rows'])} roads needed the speed cap adjustment; calculated per {'TomTom step' if c['level'] == 'steps' else 'road (older run)'})</p><ul>{checks}</ul>"
            f"<details><summary><b>Time per road (graph)</b> <span class=muted>grey = baseline, colour = revised</span></summary>{chart(c['rows'], rd, col)}</details>"
            f"<details><summary><b>Calculation detail</b> (formula: delta t = 60 x d x (1/50 - 1/v) minutes)</summary>"
            f"<table><tr><th>Step</th><th>Road</th><th>d (km)</th><th>Baseline (s)</th><th>v (km/h)</th><th>delta t (min)</th><th>Revised (s)</th><th>Note</th></tr>{detail}</table></details></div>")


@router.get("/run/{run_id}/stage3", response_class=HTMLResponse)
def stage3_page(request: Request, run_id: str, error: str = ""):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    head = nav(run, 3) + "<h2>Stage 3: revised journey times</h2>"
    if run["gates"]["1"] != "approved":
        return page(head + "<p class=banner>Approve Gate 1 first.</p>")
    err = f"<p class=banner>{escape(error)}</p>" if error else ""
    g2 = run["gates"].get("2") == "approved"
    g3 = run["gates"].get("3") == "approved"
    status = ("<p class=ok>GATE 3 APPROVED. Still NOT APPROVED FOR USE until all gates pass.</p>" if g3
              else "<p class=banner>GATE 3 PENDING: PROVISIONAL, NOT APPROVED FOR USE</p>")
    warn = "" if g2 else "<p class=banner>Gate 2 (danger areas) is not approved yet. You can look at times now, but Gate 3 cannot be approved before Gate 2.</p>"
    method = ("<p class=muted><b>Included:</b> TomTom free flow time (live traffic switched off) for each segment, plus your cap formula wherever TomTom's own speed is above 50 km/h. "
              "<b>Not included:</b> live traffic, delays from roadworks or closures, extra waiting at junctions beyond TomTom's own model, rest stops (Stage 4). "
              "<b>Estimated:</b> every segment speed is worked out from TomTom's distance and time, not measured.</p>")
    body = head + err + status + warn + method
    body += (f"<form method=post action=/run/{run_id}/stage3/contingency>Contingency allowance (minutes, applies to both routes; 0 means none): "
             f"<input type=text name=minutes size=5 value='{run.get('contingency_min', 0)}'><button>Set</button></form>")
    body += route_block(run, "primary", "blue", "PRIMARY ROUTE (blue)")
    if run["routes"].get("secondary"):
        body += route_block(run, "secondary", "red", "SECONDARY ROUTE (red)")
    body += f"<form method=post action=/run/{run_id}/stage3/approve><button>Approve Gate 3 (times)</button></form>"
    body += f"<form method=get action=/run/{run_id}/stage4><button>Go to Stage 4: rest stops</button></form>"
    return page(body)


@router.post("/run/{run_id}/stage3/contingency")
def contingency(request: Request, run_id: str, minutes: str = Form("0")):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    try:
        m = float(minutes)
        assert 0 <= m <= 600
    except (ValueError, AssertionError):
        return RedirectResponse(f"/run/{run_id}/stage3?error=Enter+a+number+of+minutes+between+0+and+600", status_code=303)
    run["contingency_min"] = m
    run["gates"]["3"] = "pending"
    store.save_run(run)
    return RedirectResponse(f"/run/{run_id}/stage3", status_code=303)


@router.post("/run/{run_id}/stage3/approve")
def approve(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run or run["gates"].get("2") != "approved":
        return RedirectResponse(f"/run/{run_id}/stage3?error=Approve+Gate+2+first", status_code=303)
    for rn in stage2.routes_of(run):
        if not stage3.calc(run["routes"][rn]["data"])["ok"]:
            return RedirectResponse(f"/run/{run_id}/stage3?error=A+reconciliation+check+failed.+Do+not+approve.", status_code=303)
    run["gates"]["3"] = "approved"
    store.save_run(run)
    return RedirectResponse(f"/run/{run_id}/stage3", status_code=303)

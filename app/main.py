"""Route Planner, milestone M2: TomTom routing, road by road tables, saved runs, Gate 1."""
import datetime
import hmac
import os
import secrets
from html import escape

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import store, tomtom

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
KEYS = ["TOMTOM_KEY", "GOOGLE_KEY", "ANTHROPIC_API_KEY"]
DEFAULT_ORIGIN = "712 Yishun Avenue 5, Singapore 760712"

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", max_age=60 * 60 * 8)

STYLE = """<style>
body{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#222}
input,textarea,select,button{font-size:1rem;padding:.45rem;margin:.2rem 0;max-width:100%}
input[type=text]{width:100%;box-sizing:border-box}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem}
th,td{border:1px solid #bbb;padding:.35rem .5rem;text-align:left;vertical-align:top;font-size:.92rem}
th{background:#eee} tr.total td{font-weight:700;background:#f4f4f4}
tr.bad td{background:#fdd} .banner{padding:.6rem;border:2px solid #b00;color:#b00;font-weight:700;margin:.8rem 0}
.ok{color:#080;font-weight:700} .muted{color:#666;font-size:.85rem}
.blue{border-left:8px solid #2a6fd6;padding-left:.6rem} .red{border-left:8px solid #d62a2a;padding-left:.6rem}
</style>"""


def page(body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'>{STYLE}{body}")


def login_needed(request: Request):
    return None if request.session.get("ok") else RedirectResponse("/login", status_code=303)


def fmt_dist(m: int) -> str:
    return f"{m} m" if m < 1000 else f"{m / 1000:.2f} km"


def fmt_time(s: int) -> str:
    m, sec = divmod(int(s), 60)
    return f"{m} min {sec:02d} s" if m else f"{sec} s"


# ---------- auth ----------
@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/login", response_class=HTMLResponse)
def login_form(error: str = ""):
    msg = f"<p style='color:#b00'>{escape(error)}</p>" if error else ""
    return page("<h2>Route Planner</h2>" + msg +
                "<form method=post action=/login><input type=password name=password "
                "placeholder=Password autofocus><br><button>Log in</button></form>")


@app.post("/login")
def login(request: Request, password: str = Form("")):
    if APP_PASSWORD and hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
        request.session["ok"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?error=Wrong+password", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- planning ----------
def endpoints(run):
    o, d = run["origin"], run["dest"]
    return (d, o) if run["direction"] == "return" else (o, d)


def via_route(run, vias_text: str):
    vias = [tomtom.geocode(line.strip()) for line in vias_text.splitlines() if line.strip()]
    o, d = endpoints(run)
    data = tomtom.calculate([o] + vias + [d])
    return {"source": "via", "vias": vias, "data": tomtom.parse_route(data["routes"][0])}


def touch(run):
    run["version"] += 1
    run["gates"]["1"] = "pending"
    run["updated"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


@app.get("/", response_class=HTMLResponse)
def home(request: Request, error: str = ""):
    if (r := login_needed(request)):
        return r
    err = f"<p class=banner>{escape(error)}</p>" if error else ""
    past = "".join(
        f"<li><a href='/run/{x['id']}'>{escape(x['dest']['text'])}</a> "
        f"<span class=muted>({x['direction']}, v{x['version']}, Gate 1: {x['gates']['1']})</span></li>"
        for x in store.list_runs())
    keys = ", ".join(f"{k}: {'set' if os.environ.get(k) else 'MISSING'}" for k in KEYS)
    return page(f"""<h2>Route Planner</h2>{err}
<form method=post action=/plan>
<label>Destination</label><input type=text name=dest placeholder="e.g. 9 Aviation Park Road, Singapore 498760" required>
<label>Start</label><input type=text name=origin value="{escape(DEFAULT_ORIGIN)}">
<label>Trip</label><select name=direction><option value=outbound>Outbound (start to destination)</option>
<option value=return>Return (destination to start)</option></select><br>
<button>Plan primary and secondary routes</button></form>
<h3>Recent runs</h3><ul>{past or '<li class=muted>None yet</li>'}</ul>
<p class=muted>{keys} | <a href=/logout>Log out</a></p>""")


@app.post("/plan")
def plan(request: Request, dest: str = Form(...), origin: str = Form(DEFAULT_ORIGIN),
         direction: str = Form("outbound")):
    if (r := login_needed(request)):
        return r
    try:
        run = {"id": store.new_id(), "version": 1, "direction": "return" if direction == "return" else "outbound",
               "origin": tomtom.geocode(origin.strip() or DEFAULT_ORIGIN), "dest": tomtom.geocode(dest.strip()),
               "gates": {"1": "pending"}, "created": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"}
        run["updated"] = run["created"]
        o, d = endpoints(run)
        data = tomtom.calculate([o, d], alternatives=2)
        alts = [tomtom.parse_route(x) for x in data["routes"]]
        run["routes"] = {
            "primary": {"source": "alt0", "vias": [], "data": alts[0]},
            "secondary": {"source": "alt1", "vias": [], "data": alts[1]} if len(alts) > 1 else None}
        run["alts"] = alts
        store.save_run(run)
    except (tomtom.TomTomError, KeyError, IndexError) as e:
        return RedirectResponse("/?error=" + escape(str(e))[:200].replace(" ", "+"), status_code=303)
    return RedirectResponse(f"/run/{run['id']}", status_code=303)


@app.post("/run/{run_id}/reroute")
def reroute(request: Request, run_id: str, vias_primary: str = Form(""), vias_secondary: str = Form("")):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    try:
        alts = run.get("alts", [])
        if vias_primary.strip():
            run["routes"]["primary"] = via_route(run, vias_primary)
        elif alts:
            run["routes"]["primary"] = {"source": "alt0", "vias": [], "data": alts[0]}
        if vias_secondary.strip():
            run["routes"]["secondary"] = via_route(run, vias_secondary)
        elif len(alts) > 1:
            run["routes"]["secondary"] = {"source": "alt1", "vias": [], "data": alts[1]}
        else:
            run["routes"]["secondary"] = None
        touch(run)
        store.save_run(run)
    except (tomtom.TomTomError, KeyError, IndexError) as e:
        return RedirectResponse(f"/run/{run_id}?error=" + escape(str(e))[:200].replace(" ", "+"), status_code=303)
    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.post("/run/{run_id}/approve1")
def approve1(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if run:
        run["gates"]["1"] = "approved"
        run["updated"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        store.save_run(run)
    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.get("/run/{run_id}/json")
def run_json(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    return JSONResponse(run or {"error": "not found"})


def table(route, colour: str, title: str) -> str:
    if not route:
        return (f"<div class={colour}><h3>{title}</h3><p>No alternative route was found. "
                "Add a via point below to force a different route.</p></div>")
    d = route["data"]
    banner = "<p class=banner>TUNNEL on this route: not allowed. Add a via point to avoid it.</p>" if d["tunnel"] else ""
    rows = "".join(
        f"<tr class={'bad' if x['uturn'] else ''}><td>{i}</td><td>{escape(x['road'])}</td>"
        f"<td>{escape(x['action'])}{' <b>(U-TURN)</b>' if x['uturn'] else ''}</td>"
        f"<td>{fmt_dist(x['dist_m'])}</td><td>{fmt_time(x['secs'])}</td></tr>"
        for i, x in enumerate(d["rows"], 1))
    ok = sum(x["dist_m"] for x in d["rows"]) == d["total_m"] or abs(sum(x["dist_m"] for x in d["rows"]) - d["total_m"]) <= 5
    via = ""
    if route["vias"]:
        via = "<p class=muted>Forced via: " + "; ".join(escape(v["label"]) for v in route["vias"]) + "</p>"
    return (f"<div class={colour}><h3>{title}</h3>{banner}{via}<table><tr><th>#</th><th>Road</th><th>First action</th>"
            f"<th>Distance</th><th>Baseline time (uncapped)</th></tr>{rows}"
            f"<tr class=total><td></td><td>TOTAL</td><td></td><td>{fmt_dist(d['total_m'])}</td><td>{fmt_time(d['total_s'])}</td></tr></table>"
            f"<p class=muted>Rows reconcile to TomTom total: {'yes' if ok else 'NO, check'}</p></div>")


def compare(run) -> str:
    p, s = run["routes"]["primary"], run["routes"]["secondary"]
    if not (p and s):
        return ""
    rp = {x["road"] for x in p["data"]["rows"]}
    rs = {x["road"] for x in s["data"]["rows"]}
    f = lambda xs: ", ".join(escape(x) for x in sorted(xs)) or "none"
    return (f"<h3>Comparison</h3><p><b>Shared roads:</b> {f(rp & rs)}</p>"
            f"<p><b>Only primary:</b> {f(rp - rs)}</p><p><b>Only secondary:</b> {f(rs - rp)}</p>")


@app.get("/run/{run_id}", response_class=HTMLResponse)
def show_run(request: Request, run_id: str, error: str = ""):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    approved = run["gates"]["1"] == "approved"
    status = ("<p class=ok>GATE 1 APPROVED (routes chosen). Still NOT APPROVED FOR USE until all gates pass.</p>"
              if approved else "<p class=banner>GATE 1 PENDING: PROVISIONAL, NOT APPROVED FOR USE</p>")
    err = f"<p class=banner>{escape(error)}</p>" if error else ""
    o, d = endpoints(run)
    pv = "\n".join(v["text"] for v in run["routes"]["primary"]["vias"])
    sv = "\n".join(v["text"] for v in (run["routes"]["secondary"] or {"vias": []})["vias"])
    return page(f"""<p><a href=/>Back</a></p><h2>{escape(o['label'])}<br>to {escape(d['label'])}</h2>
<p class=muted>Run {run['id']} | version {run['version']} | {run['direction']} | baseline = TomTom, tunnels avoided, no live traffic</p>
{status}{err}
{table(run['routes']['primary'], 'blue', 'PRIMARY ROUTE (blue)')}
{table(run['routes']['secondary'], 'red', 'SECONDARY ROUTE (red)')}
{compare(run)}
<h3>Edit routes</h3><p class=muted>One place per line, in travel order. Leave blank to use TomTom's own choice. Editing resets Gate 1.</p>
<form method=post action=/run/{run['id']}/reroute>
<label>Primary via points</label><textarea name=vias_primary rows=3 cols=60>{escape(pv)}</textarea><br>
<label>Secondary via points</label><textarea name=vias_secondary rows=3 cols=60>{escape(sv)}</textarea><br>
<button>Re-route</button></form>
<form method=post action=/run/{run['id']}/approve1><button>Approve Gate 1 (route choices)</button></form>
<p class=muted><a href=/run/{run['id']}/json>Raw run data</a></p>""")

"""Route Planner: Stage 1 (routes, Gate 1) and Stage 2 (danger areas, Gate 2)."""
import datetime
import hmac
import os
import secrets
from html import escape
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import planner, stage2, stage2_routes, store, tomtom
from .ui import STYLE, fmt_dist, fmt_time, login_needed, page, table

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
KEYS = ["TOMTOM_KEY", "GOOGLE_KEY", "ANTHROPIC_API_KEY"]
DEFAULT_ORIGIN = "712 Yishun Avenue 5, Singapore 760712"

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", max_age=60 * 60 * 8)
app.include_router(stage2_routes.router)

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


def geocode_lines(text: str) -> list:
    return [tomtom.geocode(line.strip()) for line in text.splitlines() if line.strip()]


def touch(run):
    run["version"] += 1
    run["gates"]["1"] = "pending"
    run["gates"]["2"] = "pending"
    if run.get("screen"):
        run["screen"] = {"status": "stale", "done": 0, "total": 0, "error": ""}
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
               "gates": {"1": "pending", "2": "pending"}, "created": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"}
        run["updated"] = run["created"]
        o, d = endpoints(run)
        primary, secondary = planner.plan_two(o, d, [], [])
        run["routes"] = {"primary": primary, "secondary": secondary}
        try:
            stage2.locate_mandatory(run)
        except Exception:
            pass
        store.save_run(run)
    except (tomtom.TomTomError, KeyError, IndexError) as e:
        return RedirectResponse("/?error=" + quote(str(e)[:300]), status_code=303)
    return RedirectResponse(f"/run/{run['id']}", status_code=303)


@app.post("/run/{run_id}/reroute")
def reroute(request: Request, run_id: str, vias_primary: str = Form(""), vias_secondary: str = Form("")):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    try:
        o, d = endpoints(run)
        primary, secondary = planner.plan_two(o, d, geocode_lines(vias_primary), geocode_lines(vias_secondary))
        run["routes"] = {"primary": primary, "secondary": secondary}
        touch(run)
        store.save_run(run)
    except (tomtom.TomTomError, KeyError, IndexError) as e:
        return RedirectResponse(f"/run/{run_id}?error=" + quote(str(e)[:300]), status_code=303)
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
        return RedirectResponse(f"/run/{run_id}/stage2", status_code=303)
    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.get("/run/{run_id}/json")
def run_json(request: Request, run_id: str):
    if (r := login_needed(request)):
        return r
    run = store.load_run(run_id)
    return JSONResponse(run or {"error": "not found"})


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
{table(run['routes']['primary'], 'blue', 'PRIMARY ROUTE (blue)', None, stage2.mandatory_status(run, 'primary') if stage2.ready(run) else '')}
{table(run['routes']['secondary'], 'red', 'SECONDARY ROUTE (red)', None, stage2.mandatory_status(run, 'secondary') if (run['routes']['secondary'] and stage2.ready(run)) else '')}
{compare(run)}
<h3>Edit routes</h3><p class=muted>One place per line, in travel order. Leave blank to use TomTom's own choice. Editing resets Gate 1.</p>
<form method=post action=/run/{run['id']}/reroute>
<label>Primary via points</label><textarea name=vias_primary rows=3 cols=60>{escape(pv)}</textarea><br>
<label>Secondary via points</label><textarea name=vias_secondary rows=3 cols=60>{escape(sv)}</textarea><br>
<button>Re-route</button></form>
<form method=post action=/run/{run['id']}/approve1><button>Approve Gate 1 (route choices)</button></form>
{('<form method=get action=/run/' + run['id'] + '/stage2><button>Go to Stage 2: danger areas</button></form>') if approved else '<p class=muted>Approve Gate 1 to unlock Stage 2.</p>'}
<p class=muted><a href=/run/{run['id']}/json>Raw run data</a></p>""")

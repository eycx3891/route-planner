"""Shared page helpers."""
from html import escape

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

STYLE = """<style>
body{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#222}
input,textarea,select,button{font-size:1rem;padding:.45rem;margin:.2rem 0;max-width:100%}
input[type=text]{width:100%;box-sizing:border-box}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem}
th,td{border:1px solid #bbb;padding:.35rem .5rem;text-align:left;vertical-align:top;font-size:.92rem}
th{background:#eee} tr.total td{font-weight:700;background:#f4f4f4}
tr.bad td{background:#fdd} tr.detour td{background:#ffe0b8} tr.rest td{background:#ffb347;font-weight:700} .hz{color:#b00} .pill{display:inline-block;padding:0 .4rem;border:1px solid #b00;border-radius:1rem;color:#b00;font-weight:700}
.g{font-family:monospace;text-align:center} .bub{display:inline-block;padding:.05rem .55rem;border-radius:1rem;font-size:.8rem;font-weight:700;margin:.1rem .2rem .1rem 0;white-space:nowrap} nav{margin:.4rem 0 1rem;padding:.4rem .6rem;background:#f1f3f5;border-radius:.5rem} nav a,nav span{margin-right:.9rem} details{margin:.5rem 0} .banner{padding:.6rem;border:2px solid #b00;color:#b00;font-weight:700;margin:.8rem 0}
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




def table(route, colour: str, title: str, cols=None, note: str = "") -> str:
    """cols: list of (header, {row_index: html}) extra columns."""
    cols = cols or []
    if not route:
        return (f"<div class={colour}><h3>{title}</h3><p>No second route could be found, even after forcing a detour. "
                "Add a via point below to try one yourself.</p></div>")
    d = route["data"]
    banner = ""
    if d["tunnel"]:
        banner += "<p class=banner>TUNNEL on this route: not allowed. Add a via point to avoid it.</p>"
    if not route.get("clean", True):
        banner += ("<p class=banner>Still contains a U-turn after automatic repair attempts (highlighted below). "
                   "Add a via point on a road that avoids it.</p>")
    rows = "".join(
        f"<tr class={'bad' if x['uturn'] else ''}><td>{i}</td><td>{escape(x['road'])}</td>"
        f"<td>{escape(x['action'])}{' <b>(U-TURN)</b>' if x['uturn'] else ''}</td>"
        f"<td>{fmt_dist(x['dist_m'])}</td><td>{fmt_time(x['secs'])}</td>"
        + "".join(f"<td>{c.get(i - 1, '')}</td>" for _, c in cols) + "</tr>"
        for i, x in enumerate(d["rows"], 1))
    heads = "".join(f"<th>{escape(h)}</th>" for h, _ in cols)
    blanks = "<td></td>" * len(cols)
    ok = abs(sum(x["dist_m"] for x in d["rows"]) - d["total_m"]) <= 5
    extra = ""
    if route.get("vias"):
        extra += "<p class=muted>Your via points: " + "; ".join(escape(v.get("label", v.get("text", ""))) for v in route["vias"]) + "</p>"
    for r in route.get("repairs", []):
        extra += (f"<p class=muted><b>Deviation:</b> {escape(r['reason'])}. Detour {escape(r['via'])}; "
                  f"adds {fmt_dist(max(r['extra_m'], 0))} and {fmt_time(max(r['extra_s'], 0))} versus the first attempt.</p>")
    return (f"<div class={colour}><h3>{title}</h3>{banner}{extra}<table><tr><th>#</th><th>Road</th><th>First action</th>"
            f"<th>Distance</th><th>Baseline time (uncapped)</th>{heads}</tr>{rows}"
            f"<tr class=total><td></td><td>TOTAL</td><td></td><td>{fmt_dist(d['total_m'])}</td><td>{fmt_time(d['total_s'])}</td>{blanks}</tr></table>"
            f"<p class=muted>Rows reconcile to TomTom total: {'yes' if ok else 'NO, check'}</p>{note}</div>")


BUB = {1: ("#f59e0b", "#222"), 2: ("#f97316", "#fff"), 3: ("#ca8a04", "#fff"), 4: ("#dc2626", "#fff"), 5: ("#7c3aed", "#fff"), 6: ("#111827", "#fff"),
       7: ("#374151", "#fff"), 9: ("#92400e", "#fff"), 10: ("#0d9488", "#fff"), 11: ("#2563eb", "#fff")}


def bubble(cat) -> str:
    from .vision import CATS
    bg, fg = BUB.get(cat, ("#6b7280", "#fff"))
    return f"<span class=bub style='background:{bg};color:{fg}'>{escape(CATS.get(cat, 'Other'))}</span>"


def nav(run, current: int) -> str:
    """Links to every stage, with a tick on the finished ones. Locked stages are plain text."""
    from . import stage4
    rid, g = run["id"], run["gates"]
    paths = {1: "", 2: "/stage2", 3: "/stage3", 4: "/stage4", 5: "/stage5", 6: "/stage6"}
    names = {1: "Routes", 2: "Danger areas", 3: "Times", 4: "Rest stops", 5: "Driver reference", 6: "Maps"}
    try:
        g4 = stage4.gate4_ok(run)
    except Exception:
        g4 = g.get("4") == "approved"
    done = {1: g.get("1") == "approved", 2: g.get("2") == "approved", 3: g.get("3") == "approved", 4: g4 and g.get("3") == "approved", 5: g.get("5") == "approved", 6: g.get("6") == "approved"}
    out = []
    for n in range(1, 7):
        label = f"{n}. {names[n]}" + (" &#10003;" if done[n] else "")
        unlocked = n == 1 or done[n - 1] or done[n] or n <= current
        if n == current:
            out.append(f"<span><b>{label}</b></span>")
        elif unlocked:
            out.append(f"<a href='/run/{rid}{paths[n]}'>{label}</a>")
        else:
            out.append(f"<span class=muted>{label}</span>")
    return "<nav>" + "".join(out) + "<a href=/>Home</a></nav>"

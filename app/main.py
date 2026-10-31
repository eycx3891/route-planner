"""Route Planner, milestone M1: password-protected hello world."""
import hmac
import os
import secrets
from html import escape

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
KEYS = ["TOMTOM_KEY", "GOOGLE_KEY", "ANTHROPIC_API_KEY"]

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    max_age=60 * 60 * 8,
)

STYLE = (
    "<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:3rem auto;"
    "padding:0 1rem}input,button{font-size:1rem;padding:.5rem;margin:.25rem 0}"
    "td{padding:.2rem 1rem .2rem 0}</style>"
)


def page(body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'>{STYLE}{body}")


def logged_in(request: Request) -> bool:
    return bool(request.session.get("ok"))


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/login", response_class=HTMLResponse)
def login_form(error: str = ""):
    msg = f"<p style='color:#b00'>{escape(error)}</p>" if error else ""
    return page(
        "<h2>Route Planner</h2>" + msg +
        "<form method=post action=/login><input type=password name=password "
        "placeholder=Password autofocus><br><button>Log in</button></form>"
    )


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


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    rows = "".join(
        f"<tr><td>{k}</td><td>{'set' if os.environ.get(k) else 'MISSING'}</td></tr>"
        for k in KEYS
    )
    return page(
        "<h2>Route Planner: M1 working</h2>"
        "<p>Keys (names only, values never shown):</p>"
        f"<table>{rows}</table><p><a href=/logout>Log out</a></p>"
    )

"""FastAPI web UI: browse upcoming events, click to book/watch."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api
from .watcher import TIME_BEFORE, manager

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="Beat81 Grabber")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

DEFAULT_CITY = os.environ.get("B81_DEFAULT_CITY", "")  # e.g. BER, MUC

COOKIE_TOKEN = "b81_token"
COOKIE_REFRESH = "b81_refresh"
REFRESH_MAX_AGE = 30 * 24 * 3600  # 30 days
COOKIE_SECURE = os.environ.get("B81_COOKIE_SECURE", "0") not in {"", "0", "false", "False"}


def _build_fernet() -> Fernet:
    key = os.environ.get("B81_COOKIE_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        print(
            "api> warning: B81_COOKIE_KEY not set; generated an ephemeral key. "
            "Remember-me cookies won't survive a restart. To persist, add to .env:\n"
            f"     B81_COOKIE_KEY={key}",
            flush=True,
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        raise RuntimeError(f"B81_COOKIE_KEY is not a valid Fernet key: {e}") from e


_FERNET = _build_fernet()


def _encrypt_creds(email: str, password: str) -> str:
    return _FERNET.encrypt(json.dumps({"email": email, "password": password}).encode()).decode()


def _decrypt_creds(blob: str) -> tuple[str, str] | None:
    try:
        data = json.loads(_FERNET.decrypt(blob.encode()).decode())
        return data["email"], data["password"]
    except (InvalidToken, KeyError, ValueError, json.JSONDecodeError):
        return None


def _format_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %d %b, %H:%M")


def _format_day(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %d %b").upper()


def _format_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%H:%M")


def _since(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 0:
        return "now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m {int(delta % 60)}s ago"
    return f"{int(delta // 3600)}h {int((delta % 3600) // 60)}m ago"


templates.env.filters["local"] = _format_local
templates.env.filters["day"] = _format_day
templates.env.filters["time"] = _format_time
templates.env.filters["since"] = _since


class _RedirectToLogin(Exception):
    pass


def require_session(request: Request) -> api.Session:
    token = request.cookies.get(COOKIE_TOKEN)
    if token:
        try:
            sess = api.Session.from_token(token)
            if sess.is_valid():
                return sess
        except Exception:
            pass
    refresh = request.cookies.get(COOKIE_REFRESH)
    if refresh:
        creds = _decrypt_creds(refresh)
        if creds:
            try:
                sess = api.Session.login(*creds)
            except Exception as e:
                print(f"api> silent refresh failed: {e}", flush=True)
            else:
                request.state.fresh_jwt = sess.token
                request.state.fresh_jwt_max_age = max(0, int(sess.expires_at - time.time()))
                return sess
    raise _RedirectToLogin()


def _build_refresh(request: Request) -> Callable[[], api.Session] | None:
    """Closure a watcher thread can call to re-login when its JWT expires.
    The encrypted blob is captured here; we decrypt only on each refresh
    so the plaintext password isn't kept resident between refreshes."""
    blob = request.cookies.get(COOKIE_REFRESH)
    if not blob:
        return None

    def refresh() -> api.Session:
        creds = _decrypt_creds(blob)
        if not creds:
            raise RuntimeError("remember-me cookie no longer decryptable")
        return api.Session.login(*creds)

    return refresh


@app.middleware("http")
async def _attach_fresh_jwt(request: Request, call_next):
    response = await call_next(request)
    fresh = getattr(request.state, "fresh_jwt", None)
    if fresh:
        response.set_cookie(
            COOKIE_TOKEN,
            fresh,
            max_age=getattr(request.state, "fresh_jwt_max_age", 0),
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
        )
    return response


@app.exception_handler(_RedirectToLogin)
async def _redirect_login_handler(request: Request, exc: _RedirectToLogin) -> Response:
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_TOKEN)
    return resp


def _decorate_event(ev: dict[str, Any], user_id: str) -> dict[str, Any]:
    cur = ev.get("current_participants_count") or 0
    mx = ev.get("max_participants") or 0
    starts = api.event_starts_at(ev)
    watcher = manager.get(user_id, ev["id"])
    return {
        "id": ev["id"],
        "name": ev.get("type", {}).get("name", "?"),
        "location": ev.get("location", {}).get("name", "?"),
        "coach": ev.get("coach", {}).get("forename", "?"),
        "starts_at": starts,
        "current": cur,
        "max": mx,
        "is_full": cur >= mx,
        "is_cancelled": bool(ev.get("is_cancelled")),
        "watcher_state": watcher.state if watcher else None,
    }


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error, "active_page": "login"},
    )


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    remember: str | None = Form(None),
):
    try:
        sess = api.Session.login(email, password)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        msg = f"Beat81 rejected the login (HTTP {status})."
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": msg, "active_page": "login"},
            status_code=400,
        )
    except Exception as e:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": f"Login failed: {e}", "active_page": "login"},
            status_code=400,
        )
    resp = RedirectResponse(url="/", status_code=303)
    jwt_max_age = max(0, int(sess.expires_at - datetime.now(timezone.utc).timestamp()))
    resp.set_cookie(
        COOKIE_TOKEN,
        sess.token,
        max_age=jwt_max_age,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    if remember:
        resp.set_cookie(
            COOKIE_REFRESH,
            _encrypt_creds(email, password),
            max_age=REFRESH_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
        )
    else:
        resp.delete_cookie(COOKIE_REFRESH)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_TOKEN)
    resp.delete_cookie(COOKIE_REFRESH)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    city: str = "",
    days: int = 2,
    sess: api.Session = Depends(require_session),
):
    city = city or DEFAULT_CITY
    now = datetime.now(timezone.utc)
    events_raw = api.search_events(
        city=city or None,
        from_date=now.isoformat(),
        to_date=(now + timedelta(days=days)).isoformat(),
        limit=200,
    )
    events = [_decorate_event(e, sess.user_id) for e in events_raw]
    return templates.TemplateResponse(
        "events.html",
        {
            "request": request,
            "events": events,
            "city": city,
            "days": days,
            "time_before_min": int(TIME_BEFORE // 60),
            "active_page": "events",
        },
    )


@app.get("/bookings", response_class=HTMLResponse)
def bookings(request: Request, sess: api.Session = Depends(require_session)):
    try:
        tickets = api.list_tickets(sess)
    except Exception as e:
        tickets = []
        error = str(e)
    else:
        error = None
    decorated = []
    for t in tickets:
        ev = t.get("event", {})
        decorated.append(
            {
                "id": ev.get("id"),
                "name": ev.get("type", {}).get("name", "?"),
                "location": ev.get("location", {}).get("name", "?"),
                "coach": ev.get("coach", {}).get("forename", "?"),
                "starts_at": api.event_starts_at(ev) if ev.get("date_begin") else None,
                "is_waitinglist": bool(t.get("is_waitinglist")),
                "status": t.get("current_status", {}).get("status_name", "?"),
            }
        )
    return templates.TemplateResponse(
        "bookings.html",
        {
            "request": request,
            "tickets": decorated,
            "watchers": manager.list_for(sess.user_id),
            "error": error,
            "active_page": "bookings",
        },
    )


@app.get("/watchers/fragment", response_class=HTMLResponse)
def watchers_fragment(request: Request, sess: api.Session = Depends(require_session)):
    return templates.TemplateResponse(
        "_watchers.html",
        {"request": request, "watchers": manager.list_for(sess.user_id)},
    )


@app.post("/grab/{event_id}")
def grab(event_id: str, request: Request, sess: api.Session = Depends(require_session)):
    refresh = _build_refresh(request)
    try:
        manager.start(sess.user_id, event_id, sess, refresh=refresh)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url="/bookings", status_code=303)


@app.post("/watchers/{event_id}/cancel")
def cancel(event_id: str, sess: api.Session = Depends(require_session)):
    manager.cancel(sess.user_id, event_id)
    return RedirectResponse(url="/bookings", status_code=303)


@app.post("/watchers/{event_id}/remove")
def remove(event_id: str, sess: api.Session = Depends(require_session)):
    manager.remove(sess.user_id, event_id)
    return RedirectResponse(url="/bookings", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}

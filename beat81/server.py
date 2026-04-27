"""FastAPI web UI: browse upcoming events, click to book/watch."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
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


def _format_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %d %b, %H:%M")


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
templates.env.filters["since"] = _since


def _decorate_event(ev: dict[str, Any]) -> dict[str, Any]:
    cur = ev.get("current_participants_count") or 0
    mx = ev.get("max_participants") or 0
    starts = api.event_starts_at(ev)
    watcher = manager.get(ev["id"])
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


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    city: str = "",
    days: int = 2,
):
    city = city or DEFAULT_CITY
    now = datetime.now(timezone.utc)
    events_raw = api.search_events(
        city=city or None,
        from_date=now.isoformat(),
        to_date=(now + timedelta(days=days)).isoformat(),
        limit=200,
    )
    events = [_decorate_event(e) for e in events_raw]
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
def bookings(request: Request):
    try:
        sess = api.get_session()
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
            "watchers": manager.list(),
            "error": error,
            "active_page": "bookings",
        },
    )


@app.get("/watchers/fragment", response_class=HTMLResponse)
def watchers_fragment(request: Request):
    return templates.TemplateResponse(
        "_watchers.html",
        {"request": request, "watchers": manager.list()},
    )


@app.post("/grab/{event_id}")
def grab(event_id: str, request: Request):
    try:
        manager.start(event_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if request.headers.get("hx-request"):
        return RedirectResponse(url="/bookings", status_code=303)
    return RedirectResponse(url="/bookings", status_code=303)


@app.post("/watchers/{event_id}/cancel")
def cancel(event_id: str):
    manager.cancel(event_id)
    return RedirectResponse(url="/bookings", status_code=303)


@app.post("/watchers/{event_id}/remove")
def remove(event_id: str):
    manager.remove(event_id)
    return RedirectResponse(url="/bookings", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}

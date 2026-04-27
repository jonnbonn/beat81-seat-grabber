"""Thin client around the private Beat81 FeathersJS API."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API_BASE = "https://api.production.b81.io/api"
ORIGIN = "https://app.beat81.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0"
)
TOKEN_CACHE = Path(
    os.environ.get("B81_TOKEN_CACHE", str(Path.home() / ".cache" / "beat81" / "token.json"))
)


def _headers(token: str | None = None) -> dict[str, str]:
    h = {
        "origin": ORIGIN,
        "referer": ORIGIN + "/",
        "user-agent": USER_AGENT,
        "content-type": "application/json",
    }
    if token:
        h["authorization"] = f"Bearer {token}"
    return h


def _decode_jwt(token: str) -> dict[str, Any]:
    payload_b64 = token.split(".")[1] + "=="
    return json.loads(base64.urlsafe_b64decode(payload_b64))


@dataclass
class Session:
    token: str
    user_id: str
    expires_at: float

    @classmethod
    def login(cls, email: str, password: str) -> "Session":
        r = requests.post(
            f"{API_BASE}/authentication",
            headers=_headers(),
            json={"email": email, "password": password, "strategy": "local"},
            timeout=15,
        )
        r.raise_for_status()
        token = r.json()["data"]["accessToken"]
        payload = _decode_jwt(token)
        sess = cls(token=token, user_id=payload["userId"], expires_at=payload["exp"])
        sess._cache()
        return sess

    @classmethod
    def from_cache(cls) -> "Session | None":
        if not TOKEN_CACHE.exists():
            return None
        try:
            data = json.loads(TOKEN_CACHE.read_text())
            payload = _decode_jwt(data["token"])
        except Exception:
            return None
        if payload["exp"] < time.time() + 60:
            return None
        return cls(token=data["token"], user_id=data["userId"], expires_at=payload["exp"])

    def _cache(self) -> None:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps({"token": self.token, "userId": self.user_id}))

    def is_valid(self) -> bool:
        return self.expires_at > time.time() + 60


def get_session() -> Session:
    sess = Session.from_cache()
    if sess and sess.is_valid():
        return sess
    email = os.environ.get("B81_EMAIL")
    password = os.environ.get("B81_PASSWORD")
    if not (email and password):
        raise RuntimeError("Set B81_EMAIL and B81_PASSWORD env vars.")
    return Session.login(email, password)


def search_events(
    *,
    city: str | None = None,
    from_date: str,
    to_date: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "$limit": limit,
        "$sort[date_begin]": 1,
        "is_published": True,
        "date_begin_gte": from_date,
        "date_begin_lte": to_date,
    }
    if city:
        params["location.city_code"] = city
    r = requests.get(f"{API_BASE}/events", headers=_headers(), params=params, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def fetch_event(event_id: str) -> dict[str, Any]:
    r = requests.get(f"{API_BASE}/events/{event_id}", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()["data"]


def list_tickets(sess: Session, only_upcoming: bool = True) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "user_id": sess.user_id,
        "$sort[event_date_begin]": 1,
        "status_ne": "cancelled",
        "$limit": 100,
        "$skip": 0,
    }
    if only_upcoming:
        params["event_date_begin_gte"] = datetime.now(timezone.utc).isoformat()
    r = requests.get(
        f"{API_BASE}/tickets",
        headers=_headers(sess.token),
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def create_ticket(sess: Session, event_id: str) -> tuple[bool, dict[str, Any]]:
    """POST /tickets — returns (ok, response_body)."""
    r = requests.post(
        f"{API_BASE}/tickets",
        headers=_headers(sess.token),
        json={"user_id": sess.user_id, "event_id": event_id},
        timeout=10,
    )
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text, "status": r.status_code}
    return (r.status_code in (200, 201), body)


def fmt_event(ev: dict[str, Any]) -> str:
    name = ev.get("type", {}).get("name", "?")
    loc = ev.get("location", {}).get("name", "?")
    coach = ev.get("coach", {}).get("forename", "?")
    when = ev.get("date_begin", "?")
    return f"{name} @ {loc} w/ {coach} — {when}"


def event_starts_at(ev: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(ev["date_begin"].replace("Z", "+00:00"))

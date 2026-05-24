"""Thin client around the private Beat81 FeathersJS API."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

API_BASE = "https://api.production.b81.io/api"
ORIGIN = "https://app.beat81.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0"
)


VERBOSE = os.environ.get("B81_VERBOSE", "1") not in {"", "0", "false", "False"}


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


RESP_TRUNC = int(os.environ.get("B81_LOG_RESP_TRUNC", "600"))


def _truncate(s: str, n: int | None = None) -> str:
    if n is None:
        n = RESP_TRUNC
    return s if len(s) <= n else s[:n] + f"…(+{len(s)-n}b)"


def _summarize(parsed: Any) -> str | None:
    """Squeeze just the actionable fields out of a Beat81 response so we
    never need the full body for debugging."""
    if not isinstance(parsed, dict):
        return None
    data = parsed.get("data", parsed)

    def fmt_ticket(t: dict[str, Any]) -> str:
        tid = (t.get("id") or "?")[:8]
        cs = ((t.get("current_status") or {}).get("status_name")) or "?"
        wl = t.get("is_waitinglist")
        ever = ((t.get("current_status") or {}).get("meta") or {}).get("ever_on_waitinglist")
        hist = [
            f"{h.get('status_name','?')}@{(h.get('transitioned_at') or '')[11:19]}"
            for h in (t.get("status_history") or [])[-5:]
        ]
        return f"ticket={tid} wl={wl} status={cs} ever_wl={ever} history={hist}"

    if isinstance(data, dict) and "is_waitinglist" in data:
        return fmt_ticket(data)
    if isinstance(data, list) and data and isinstance(data[0], dict) and "is_waitinglist" in data[0]:
        return "tickets=[" + " | ".join(fmt_ticket(t) for t in data[:5]) + "]"
    if isinstance(data, dict) and "current_participants_count" in data:
        cs = (data.get("current_status") or {}).get("status_name")
        return (
            f"event {data.get('current_participants_count')}/{data.get('max_participants')} "
            f"waitlist_count={data.get('waitinglist_count')} "
            f"offer_stats={data.get('offer_stats')} status={cs}"
        )
    return None


def _api(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: Any | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 15,
) -> tuple[int, Any]:
    """Single chokepoint for every Beat81 API call. Returns (status, parsed_body).
    Logs request + response (truncated) to stdout so `docker logs` shows it.
    """
    url = f"{API_BASE}{path}"
    started = time.time()
    err: Exception | None = None
    status = 0
    parsed: Any = None
    try:
        r = requests.request(
            method,
            url,
            headers=_headers(token),
            json=json_body,
            params=params,
            timeout=timeout,
        )
        status = r.status_code
        try:
            parsed = r.json()
        except Exception:
            parsed = {"raw": r.text[:300]}
    except Exception as e:
        err = e
    finally:
        if VERBOSE:
            ms = int((time.time() - started) * 1000)
            req_body = (
                f" body={_truncate(json.dumps(json_body, default=str))}"
                if json_body is not None else ""
            )
            req_params = (
                f" params={_truncate(json.dumps(params, default=str))}"
                if params else ""
            )
            if err:
                print(
                    f"api> {method} {path}{req_params}{req_body} -> ERROR {err} ({ms}ms)",
                    flush=True,
                )
            else:
                summary = _summarize(parsed)
                summary_line = f"\n  summary> {summary}" if summary else ""
                print(
                    f"api> {method} {path}{req_params}{req_body} -> {status} "
                    f"({ms}ms) resp={_truncate(json.dumps(parsed, default=str))}{summary_line}",
                    flush=True,
                )
    if err:
        raise err
    return status, parsed


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
        # Auth via raw requests so we never put the password through _api()'s
        # logging path; emit a redacted log line by hand instead.
        started = time.time()
        r = requests.post(
            f"{API_BASE}/authentication",
            headers=_headers(),
            json={"email": email, "password": password, "strategy": "local"},
            timeout=15,
        )
        if VERBOSE:
            ms = int((time.time() - started) * 1000)
            print(
                f"api> POST /authentication body={{email:{email!r},password:***,strategy:local}}"
                f" -> {r.status_code} ({ms}ms)",
                flush=True,
            )
        r.raise_for_status()
        token = r.json()["data"]["accessToken"]
        sess = cls.from_token(token)
        if VERBOSE:
            print(
                f"api> login ok user_id={sess.user_id} "
                f"exp_in={int(sess.expires_at - time.time())}s",
                flush=True,
            )
        return sess

    @classmethod
    def from_token(cls, token: str) -> "Session":
        payload = _decode_jwt(token)
        return cls(token=token, user_id=payload["userId"], expires_at=payload["exp"])

    def is_valid(self) -> bool:
        return self.expires_at > time.time() + 60


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
    status, body = _api("GET", f"/events/{event_id}")
    if status != 200:
        raise RuntimeError(f"fetch_event status={status} body={body}")
    return body["data"]


def fetch_events_batch(event_ids: list[str]) -> list[dict[str, Any]]:
    """Batch-poll many events with a single public request. Beat81's events
    service rejects the FeathersJS $in operator ("Invalid Query") but accepts
    a repeated ?id=… equality, returning exactly the matching events. URL
    grows ~50 bytes per id; safe up to ~50 ids per request before hitting
    typical 8 KB limits — caller should chunk past that."""
    if not event_ids:
        return []
    params: list[tuple[str, Any]] = [("id", x) for x in event_ids]
    params.append(("$limit", min(100, len(event_ids))))
    status, body = _api("GET", "/events", params=params)
    if status != 200:
        raise RuntimeError(f"fetch_events_batch status={status} body={body}")
    return body["data"]


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
    status, body = _api("GET", "/tickets", token=sess.token, params=params)
    if status != 200:
        raise RuntimeError(f"list_tickets status={status} body={body}")
    return body["data"]


def create_ticket(sess: Session, event_id: str) -> tuple[bool, dict[str, Any]]:
    """POST /tickets — returns (ok, response_body)."""
    status, body = _api(
        "POST", "/tickets",
        token=sess.token,
        json_body={"user_id": sess.user_id, "event_id": event_id},
    )
    return (status in (200, 201), body if isinstance(body, dict) else {"raw": body})


def get_ticket(sess: Session, ticket_id: str) -> dict[str, Any]:
    status, body = _api("GET", f"/tickets/{ticket_id}", token=sess.token)
    if status != 200:
        raise RuntimeError(f"get_ticket status={status} body={body}")
    return body.get("data", body) if isinstance(body, dict) else body


def transition_ticket(sess: Session, ticket_id: str, status_name: str) -> tuple[int, Any]:
    """POST /tickets/{id}/status — the real waitlist→booked promotion endpoint
    used by the official Beat81 app. Body: {"status_name": "booked"}."""
    return _api(
        "POST",
        f"/tickets/{ticket_id}/status",
        token=sess.token,
        json_body={"status_name": status_name},
    )


def fmt_event(ev: dict[str, Any]) -> str:
    name = ev.get("type", {}).get("name", "?")
    loc = ev.get("location", {}).get("name", "?")
    coach = ev.get("coach", {}).get("forename", "?")
    when = ev.get("date_begin", "?")
    return f"{name} @ {loc} w/ {coach} — {when}"


def event_starts_at(ev: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(ev["date_begin"].replace("Z", "+00:00"))

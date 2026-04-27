"""Background workers that watch an event and book the moment a seat opens."""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from . import api


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


POLL_MIN = _env_float("B81_POLL_MIN_SECS", 2.0)
POLL_MAX = _env_float("B81_POLL_MAX_SECS", 5.0)
TIME_BEFORE = _env_float("B81_TIME_BEFORE_EVENT", 1800)  # seconds; default 30 min
NTFY_TOPIC = os.environ.get("B81_NTFY_TOPIC")


def notify(title: str, body: str) -> None:
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "urgent", "Tags": "tada"},
            timeout=5,
        )
    except Exception:
        pass


@dataclass
class WatcherState:
    event_id: str
    event_label: str
    event_starts_at: datetime
    state: str = "queued"          # queued|sleeping|polling|booked|waitlist|failed|cancelled|expired
    message: str = ""
    last_seen_capacity: tuple[int, int] | None = None
    last_polled_at: datetime | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    log: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def add_log(self, line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {line}")
        del self.log[:-50]


class WatcherManager:
    """Singleton-ish registry of running watchers (single-user app)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watchers: dict[str, WatcherState] = {}
        self._threads: dict[str, threading.Thread] = {}

    def list(self) -> list[WatcherState]:
        with self._lock:
            return sorted(self._watchers.values(), key=lambda w: w.event_starts_at)

    def get(self, event_id: str) -> WatcherState | None:
        with self._lock:
            return self._watchers.get(event_id)

    def start(self, event_id: str) -> WatcherState:
        with self._lock:
            existing = self._watchers.get(event_id)
            if existing and existing.state in {"queued", "sleeping", "polling"}:
                return existing
            ev = api.fetch_event(event_id)
            state = WatcherState(
                event_id=event_id,
                event_label=api.fmt_event(ev),
                event_starts_at=api.event_starts_at(ev),
            )
            self._watchers[event_id] = state
            t = threading.Thread(
                target=self._run, args=(state,), name=f"watch-{event_id[:8]}", daemon=True
            )
            self._threads[event_id] = t
            t.start()
            return state

    def cancel(self, event_id: str) -> bool:
        with self._lock:
            state = self._watchers.get(event_id)
        if not state:
            return False
        state.cancel_event.set()
        return True

    def remove(self, event_id: str) -> bool:
        with self._lock:
            state = self._watchers.get(event_id)
            if state and state.state in {"queued", "sleeping", "polling"}:
                state.cancel_event.set()
            self._watchers.pop(event_id, None)
            self._threads.pop(event_id, None)
        return True

    # -- worker ----------------------------------------------------------

    def _sleep(self, state: WatcherState, secs: float) -> bool:
        """Sleep but wake up early if cancelled. Returns True if cancelled."""
        return state.cancel_event.wait(secs)

    def _try_book(self, state: WatcherState, sess: api.Session) -> bool:
        ok, body = api.create_ticket(sess, state.event_id)
        if not ok:
            state.add_log(f"book failed: {body}")
            return False
        ticket = body.get("data", body) if isinstance(body, dict) else {}
        is_wait = bool(ticket.get("is_waitinglist")) if isinstance(ticket, dict) else False
        if is_wait:
            state.state = "waitlist"
            state.message = "On waitlist (server placed). Will keep polling."
            state.add_log("server placed me on waitlist; continuing to poll")
            return False
        state.state = "booked"
        state.message = "Booked!"
        state.finished_at = datetime.now(timezone.utc)
        state.add_log("BOOKED")
        notify("Beat81 booked", state.event_label)
        return True

    def _run(self, state: WatcherState) -> None:
        try:
            sess = api.get_session()
        except Exception as e:
            state.state = "failed"
            state.message = f"login failed: {e}"
            state.add_log(state.message)
            return

        try:
            ev = api.fetch_event(state.event_id)
        except Exception as e:
            state.state = "failed"
            state.message = f"event fetch failed: {e}"
            state.add_log(state.message)
            return
        cur = ev.get("current_participants_count") or 0
        mx = ev.get("max_participants") or 0
        state.last_seen_capacity = (cur, mx)
        state.last_polled_at = datetime.now(timezone.utc)

        # Always try once up front. If the seat is open we book immediately;
        # if it's full we may still get queued onto the waitlist.
        state.add_log(f"initial capacity {cur}/{mx} — attempting book")
        if self._try_book(state, sess):
            return

        # Sleep until polling window opens.
        while not state.cancel_event.is_set():
            now = datetime.now(timezone.utc)
            secs_to_start = (state.event_starts_at - now).total_seconds()
            secs_to_window = secs_to_start - TIME_BEFORE
            if secs_to_start < -300:
                state.state = "expired"
                state.message = "Event already started."
                state.finished_at = now
                state.add_log("event started; giving up")
                return
            if secs_to_window <= 0:
                break
            state.state = "sleeping"
            state.message = (
                f"Polling starts in {int(secs_to_window // 60)} min "
                f"({int(TIME_BEFORE // 60)} min before class)."
            )
            # Wake periodically so the UI shows fresh countdown.
            if self._sleep(state, min(secs_to_window, 30)):
                break

        if state.cancel_event.is_set():
            state.state = "cancelled"
            state.finished_at = datetime.now(timezone.utc)
            return

        state.state = "polling"
        state.message = "Polling for an open seat…"
        state.add_log("entering polling window")

        consecutive_errors = 0
        while not state.cancel_event.is_set():
            try:
                ev = api.fetch_event(state.event_id)
            except Exception as e:
                consecutive_errors += 1
                state.add_log(f"fetch error #{consecutive_errors}: {e}")
                if self._sleep(state, min(30, 2**consecutive_errors)):
                    break
                continue
            consecutive_errors = 0

            cancelled = ev.get("is_cancelled") or (
                ev.get("current_status", {}).get("status_name") == "cancelled"
            )
            if cancelled:
                state.state = "failed"
                state.message = "Event was cancelled by Beat81."
                state.finished_at = datetime.now(timezone.utc)
                notify("Beat81 event cancelled", state.event_label)
                return

            cur = ev.get("current_participants_count") or 0
            mx = ev.get("max_participants") or 0
            state.last_seen_capacity = (cur, mx)
            state.last_polled_at = datetime.now(timezone.utc)
            state.message = f"Polling — {cur}/{mx} taken"

            now = datetime.now(timezone.utc)
            mins_to_start = (state.event_starts_at - now).total_seconds() / 60.0
            if mins_to_start < -5:
                state.state = "expired"
                state.message = "Event started; stopping."
                state.finished_at = now
                return

            if cur < mx:
                state.add_log(f"seat open ({cur}/{mx}); booking")
                if not sess.is_valid():
                    try:
                        sess = api.get_session()
                    except Exception as e:
                        state.add_log(f"re-login failed: {e}")
                if self._try_book(state, sess):
                    return

            if self._sleep(state, random.uniform(POLL_MIN, POLL_MAX)):
                break

        state.state = "cancelled"
        state.message = "Cancelled by user."
        state.finished_at = datetime.now(timezone.utc)


manager = WatcherManager()

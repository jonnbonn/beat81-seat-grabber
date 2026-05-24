"""Background workers that watch an event and book the moment a seat opens."""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from . import api


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


FAST_MIN = _env_float("B81_POLL_MIN_SECS", 2.0)
FAST_MAX = _env_float("B81_POLL_MAX_SECS", 5.0)
SLOW_MIN = _env_float("B81_POLL_SLOW_MIN_SECS", 5.0)
SLOW_MAX = _env_float("B81_POLL_SLOW_MAX_SECS", 15.0)
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
    state: str = "queued"          # queued|polling|waitlist|offered|booked|failed|cancelled|expired
    message: str = ""
    last_seen_capacity: tuple[int, int] | None = None
    last_polled_at: datetime | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    log: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    waitlist_ticket_id: str | None = None

    def add_log(self, line: str, *, echo: bool = True) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {line}"
        self.log.append(entry)
        del self.log[:-50]
        if echo:
            # Also surface to stdout so docker logs shows it.
            print(f"watcher[{self.event_id[:8]}] {line}", flush=True)


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

    def start(
        self,
        event_id: str,
        sess: api.Session,
        *,
        refresh: Callable[[], api.Session] | None = None,
    ) -> WatcherState:
        with self._lock:
            existing = self._watchers.get(event_id)
            if existing and existing.state in {"queued", "polling", "waitlist", "offered"}:
                return existing
            ev = api.fetch_event(event_id)
            state = WatcherState(
                event_id=event_id,
                event_label=api.fmt_event(ev),
                event_starts_at=api.event_starts_at(ev),
            )
            self._watchers[event_id] = state
            t = threading.Thread(
                target=self._run,
                args=(state, sess, refresh),
                name=f"watch-{event_id[:8]}",
                daemon=True,
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
            if state and state.state in {"queued", "polling", "waitlist", "offered"}:
                state.cancel_event.set()
            self._watchers.pop(event_id, None)
            self._threads.pop(event_id, None)
        return True

    # -- worker ----------------------------------------------------------

    def _sleep(self, state: WatcherState, secs: float) -> bool:
        """Sleep but wake up early if cancelled. Returns True if cancelled."""
        return state.cancel_event.wait(secs)

    @staticmethod
    def _ticket_is_waitlist(ticket: dict[str, Any], event_snapshot: dict[str, Any] | None) -> bool:
        """`is_waitinglist` can be missing/false on the create response while
        the status job runs. Cross-check with status_name and live capacity."""
        if ticket.get("is_waitinglist"):
            return True
        status = ((ticket.get("current_status") or {}).get("status_name") or "").lower()
        if "waitinglist" in status or "waitlist" in status:
            return True
        if event_snapshot:
            cur = event_snapshot.get("current_participants_count") or 0
            mx = event_snapshot.get("max_participants") or 0
            if mx and cur >= mx:
                return True
        return False

    def _try_book(self, state: WatcherState, sess: api.Session,
                  event_snapshot: dict[str, Any] | None = None) -> bool:
        ok, body = api.create_ticket(sess, state.event_id)
        state.add_log(f"POST /tickets -> ok={ok} body={str(body)[:300]}")
        if not ok:
            return False
        ticket = body.get("data", body) if isinstance(body, dict) else {}
        ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
        # Re-fetch the canonical ticket so we don't trust a half-populated
        # create response. If that fails we fall back to the response body.
        if ticket_id:
            try:
                ticket = api.get_ticket(sess, ticket_id) or ticket
            except Exception as e:
                state.add_log(f"ticket re-fetch after create failed: {e}")
        is_wait = self._ticket_is_waitlist(ticket, event_snapshot)
        if is_wait:
            if state.state != "waitlist":
                state.add_log(f"placed on waitlist (ticket {ticket_id}); will poll for offer/seat")
            state.state = "waitlist"
            state.message = "On waitlist. Polling for an open seat or an offer."
            if ticket_id:
                state.waitlist_ticket_id = ticket_id
            return False
        state.state = "booked"
        state.message = "Booked!"
        state.finished_at = datetime.now(timezone.utc)
        state.add_log(f"BOOKED (ticket {ticket_id})")
        notify("Beat81 booked", state.event_label)
        return True

    # Server-side rejections that won't change on retry — stop polling.
    _TERMINAL_PROMOTE_CODES = {
        "qualitrain_no_workout_on_same_day",
        "no_workout_on_same_day",
        "no_credits",
        "no_credit",
        "membership_expired",
        "membership_inactive",
        "user_blocked",
        "event_already_started",
        "event_cancelled",
    }

    def _try_promote(self, state: WatcherState, sess: api.Session) -> bool:
        """Convert a waitlist ticket into a confirmed booking via
        POST /tickets/{id}/status {status_name: "booked"} — the same call
        the official Beat81 app fires when the user taps Confirm."""
        ticket_id = state.waitlist_ticket_id
        if not ticket_id:
            return False
        try:
            status, body = api.transition_ticket(sess, ticket_id, "booked")
        except Exception as e:
            state.add_log(f"promote error: {e}")
            return False
        if status in (200, 201):
            state.state = "booked"
            state.message = "Booked!"
            state.finished_at = datetime.now(timezone.utc)
            state.add_log(f"BOOKED via /tickets/{ticket_id}/status")
            notify("Beat81 booked", state.event_label)
            return True
        # Non-success. Surface the server message; mark terminal if known.
        code = (body or {}).get("code") if isinstance(body, dict) else None
        msg = (body or {}).get("message") if isinstance(body, dict) else None
        state.add_log(f"promote failed status={status} code={code} msg={msg}")
        if 400 <= status < 500 and code in self._TERMINAL_PROMOTE_CODES:
            state.state = "failed"
            state.message = msg or f"Beat81 rejected booking ({code})"
            state.finished_at = datetime.now(timezone.utc)
            notify("Beat81 booking blocked", f"{state.event_label} — {msg or code}")
            # Signal to the polling loop to exit.
            state.cancel_event.set()
            return False
        return False

    def _run(
        self,
        state: WatcherState,
        sess: api.Session,
        refresh: Callable[[], api.Session] | None = None,
    ) -> None:
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
        # if it's full we end up on the waitlist and the loop takes over.
        state.add_log(f"initial capacity {cur}/{mx} — attempting book")
        if self._try_book(state, sess, event_snapshot=ev):
            return

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

            now = datetime.now(timezone.utc)
            secs_to_start = (state.event_starts_at - now).total_seconds()
            if secs_to_start < -300:
                state.state = "expired"
                state.message = "Event started; stopping."
                state.finished_at = now
                return

            in_fast_window = secs_to_start <= TIME_BEFORE
            if state.state not in {"waitlist", "offered"}:
                state.state = "polling"
            cadence = "fast" if in_fast_window else "slow"
            mins_left = max(0, int(secs_to_start // 60))
            state.message = (
                f"{cadence} poll — {cur}/{mx} taken, {mins_left} min to start"
            )
            state.add_log(
                f"tick state={state.state} {cur}/{mx} ticket={state.waitlist_ticket_id} "
                f"in_fast_window={in_fast_window} mins_to_start={mins_left}"
            )

            if not sess.is_valid():
                if refresh is not None:
                    try:
                        sess = refresh()
                        state.add_log(
                            f"refreshed session; new exp in "
                            f"{int(sess.expires_at - time.time())}s"
                        )
                    except Exception as e:
                        state.state = "failed"
                        state.message = f"session refresh failed: {e}"
                        state.finished_at = datetime.now(timezone.utc)
                        state.add_log(state.message)
                        notify("Beat81 session refresh failed", state.event_label)
                        return
                else:
                    state.state = "failed"
                    state.message = "Session expired — log in again and re-arm this watcher."
                    state.finished_at = datetime.now(timezone.utc)
                    state.add_log(state.message)
                    notify("Beat81 session expired", state.event_label)
                    return

            if cur < mx:
                state.add_log(f"seat open ({cur}/{mx}); promoting")
                if state.waitlist_ticket_id:
                    # We already hold a waitlist ticket — promote it via the
                    # status endpoint. Plain POST /tickets is idempotent and
                    # would just hand the waitlist ticket back.
                    if self._try_promote(state, sess):
                        return
                else:
                    # No waitlist ticket yet — race a fresh booking.
                    if self._try_book(state, sess, event_snapshot=ev):
                        return

            lo, hi = (FAST_MIN, FAST_MAX) if in_fast_window else (SLOW_MIN, SLOW_MAX)
            if self._sleep(state, random.uniform(lo, hi)):
                break

        state.state = "cancelled"
        state.message = "Cancelled by user."
        state.finished_at = datetime.now(timezone.utc)


manager = WatcherManager()

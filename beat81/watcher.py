"""Per-(user, event) watcher state. The actual polling is shared across all
watchers by the EventPoller in beat81/poller.py; each Watcher just reacts to
the snapshots it receives via on_snapshot()."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from . import api
from .poller import TIME_BEFORE, poller

# Re-exported for backwards-compat with code that did `from .watcher import TIME_BEFORE`.
__all__ = ["Watcher", "WatcherManager", "manager", "TIME_BEFORE", "notify"]

NTFY_TOPIC = os.environ.get("B81_NTFY_TOPIC")

ACTIVE_STATES = frozenset({"queued", "polling", "waitlist"})
TERMINAL_STATES = frozenset({"booked", "failed", "cancelled", "expired"})

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


@dataclass(eq=False)
class Watcher:
    user_id: str
    event_id: str
    event_label: str
    event_starts_at: datetime
    sess: api.Session
    refresh: Callable[[], api.Session] | None = None
    state: str = "queued"
    message: str = ""
    waitlist_ticket_id: str | None = None
    last_seen_capacity: tuple[int, int] | None = None
    last_polled_at: datetime | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    log: list[str] = field(default_factory=list)
    _action_lock: threading.Lock = field(default_factory=threading.Lock)

    # -- logging ---------------------------------------------------------

    def add_log(self, line: str, *, echo: bool = True) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {line}"
        self.log.append(entry)
        del self.log[:-50]
        if echo:
            print(
                f"watcher[u={self.user_id[:6]} e={self.event_id[:8]}] {line}",
                flush=True,
            )

    # -- lifecycle -------------------------------------------------------

    def arm(self, ev: dict[str, Any]) -> None:
        """Run the initial POST /tickets. If the seat was free we finish here;
        otherwise we transition to waitlist/polling and subscribe to the poller."""
        cur = ev.get("current_participants_count") or 0
        mx = ev.get("max_participants") or 0
        self.last_seen_capacity = (cur, mx)
        self.last_polled_at = datetime.now(timezone.utc)
        self.add_log(f"initial capacity {cur}/{mx} — attempting book")
        with self._action_lock:
            self._try_book(ev)
        if self.state in TERMINAL_STATES:
            return
        # Either we hold a waitlist ticket or the initial book didn't stick —
        # in both cases we need ongoing snapshots.
        if self.state == "queued":
            self.state = "polling"
        poller.subscribe(self.event_id, self)

    def cancel(self) -> None:
        with self._action_lock:
            if self.state in TERMINAL_STATES:
                return
            self.state = "cancelled"
            self.message = "Cancelled by user."
            self.finished_at = datetime.now(timezone.utc)
            self.add_log("cancelled by user")
        poller.unsubscribe(self.event_id, self)

    # -- snapshot handler ------------------------------------------------

    def on_snapshot(self, ev: dict[str, Any] | None) -> None:
        # Skip if a previous tick is still running for this watcher. Avoids
        # piling up tick handlers when an authenticated promote is slow.
        if not self._action_lock.acquire(blocking=False):
            return
        try:
            self._on_snapshot_locked(ev)
        finally:
            self._action_lock.release()

    def _on_snapshot_locked(self, ev: dict[str, Any] | None) -> None:
        if self.state in TERMINAL_STATES:
            poller.unsubscribe(self.event_id, self)
            return

        if ev is None:
            self._terminate("failed", "Event no longer returned by Beat81.")
            notify("Beat81 event vanished", self.event_label)
            return

        if ev.get("is_cancelled") or (
            ev.get("current_status", {}).get("status_name") == "cancelled"
        ):
            self._terminate("failed", "Event was cancelled by Beat81.")
            notify("Beat81 event cancelled", self.event_label)
            return

        cur = ev.get("current_participants_count") or 0
        mx = ev.get("max_participants") or 0
        self.last_seen_capacity = (cur, mx)
        self.last_polled_at = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)
        secs_to_start = (self.event_starts_at - now).total_seconds()
        if secs_to_start < -300:
            self._terminate("expired", "Event started; stopping.")
            return

        cadence = "fast" if secs_to_start <= TIME_BEFORE else "slow"
        mins_left = max(0, int(secs_to_start // 60))
        self.message = f"{cadence} poll — {cur}/{mx} taken, {mins_left} min to start"
        self.add_log(
            f"tick state={self.state} {cur}/{mx} ticket={self.waitlist_ticket_id} "
            f"cadence={cadence} mins_to_start={mins_left}"
        )

        if not self._ensure_session():
            return

        if cur < mx:
            self.add_log(f"seat open ({cur}/{mx}); promoting")
            if self.waitlist_ticket_id:
                self._try_promote()
            else:
                self._try_book(ev)

    # -- authenticated actions ------------------------------------------

    def _ensure_session(self) -> bool:
        if self.sess.is_valid():
            return True
        if self.refresh is None:
            self._terminate(
                "failed",
                "Session expired — log in again and re-arm this watcher.",
            )
            notify("Beat81 session expired", self.event_label)
            return False
        try:
            self.sess = self.refresh()
        except Exception as e:
            self._terminate("failed", f"session refresh failed: {e}")
            notify("Beat81 session refresh failed", self.event_label)
            return False
        self.add_log(
            f"refreshed session; new exp in {int(self.sess.expires_at - time.time())}s"
        )
        return True

    def _try_book(self, event_snapshot: dict[str, Any] | None) -> None:
        ok, body = api.create_ticket(self.sess, self.event_id)
        self.add_log(f"POST /tickets -> ok={ok} body={str(body)[:300]}")
        if not ok:
            code = (body or {}).get("code") if isinstance(body, dict) else None
            msg = (body or {}).get("message") if isinstance(body, dict) else None
            if code in _TERMINAL_PROMOTE_CODES:
                self._terminate("failed", msg or f"Beat81 rejected booking ({code})")
                notify("Beat81 booking blocked", f"{self.event_label} — {msg or code}")
            return
        ticket = body.get("data", body) if isinstance(body, dict) else {}
        ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
        # Re-fetch the canonical ticket — create response can be half-populated.
        if ticket_id:
            try:
                ticket = api.get_ticket(self.sess, ticket_id) or ticket
            except Exception as e:
                self.add_log(f"ticket re-fetch after create failed: {e}")
        if self._ticket_is_waitlist(ticket, event_snapshot):
            if self.state != "waitlist":
                self.add_log(
                    f"placed on waitlist (ticket {ticket_id}); polling for a seat"
                )
            self.state = "waitlist"
            self.message = "On waitlist. Polling for an open seat."
            if ticket_id:
                self.waitlist_ticket_id = ticket_id
            return
        self.state = "booked"
        self.message = "Booked!"
        self.finished_at = datetime.now(timezone.utc)
        self.add_log(f"BOOKED (ticket {ticket_id})")
        notify("Beat81 booked", self.event_label)
        poller.unsubscribe(self.event_id, self)

    def _try_promote(self) -> None:
        ticket_id = self.waitlist_ticket_id
        if not ticket_id:
            return
        try:
            status, body = api.transition_ticket(self.sess, ticket_id, "booked")
        except Exception as e:
            self.add_log(f"promote error: {e}")
            return
        if status in (200, 201):
            self.state = "booked"
            self.message = "Booked!"
            self.finished_at = datetime.now(timezone.utc)
            self.add_log(f"BOOKED via /tickets/{ticket_id}/status")
            notify("Beat81 booked", self.event_label)
            poller.unsubscribe(self.event_id, self)
            return
        code = (body or {}).get("code") if isinstance(body, dict) else None
        msg = (body or {}).get("message") if isinstance(body, dict) else None
        self.add_log(f"promote failed status={status} code={code} msg={msg}")
        if 400 <= status < 500 and code in _TERMINAL_PROMOTE_CODES:
            self._terminate("failed", msg or f"Beat81 rejected booking ({code})")
            notify("Beat81 booking blocked", f"{self.event_label} — {msg or code}")

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _ticket_is_waitlist(
        ticket: dict[str, Any], event_snapshot: dict[str, Any] | None
    ) -> bool:
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

    def _terminate(self, state: str, message: str) -> None:
        self.state = state
        self.message = message
        self.finished_at = datetime.now(timezone.utc)
        self.add_log(message)
        poller.unsubscribe(self.event_id, self)


class WatcherManager:
    """Per-(user, event) registry. All watchers share the EventPoller for
    public event-state polls; authenticated calls stay per-user."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watchers: dict[tuple[str, str], Watcher] = {}

    def start(
        self,
        user_id: str,
        event_id: str,
        sess: api.Session,
        *,
        refresh: Callable[[], api.Session] | None = None,
    ) -> Watcher:
        key = (user_id, event_id)
        with self._lock:
            existing = self._watchers.get(key)
            if existing and existing.state in ACTIVE_STATES:
                return existing
            ev = api.fetch_event(event_id)
            w = Watcher(
                user_id=user_id,
                event_id=event_id,
                event_label=api.fmt_event(ev),
                event_starts_at=api.event_starts_at(ev),
                sess=sess,
                refresh=refresh,
            )
            self._watchers[key] = w
        w.arm(ev)
        return w

    def cancel(self, user_id: str, event_id: str) -> bool:
        with self._lock:
            w = self._watchers.get((user_id, event_id))
        if not w:
            return False
        w.cancel()
        return True

    def remove(self, user_id: str, event_id: str) -> bool:
        with self._lock:
            w = self._watchers.pop((user_id, event_id), None)
        if w:
            w.cancel()
        return True

    def get(self, user_id: str, event_id: str) -> Watcher | None:
        with self._lock:
            return self._watchers.get((user_id, event_id))

    def list_for(self, user_id: str) -> list[Watcher]:
        with self._lock:
            return sorted(
                [w for (uid, _), w in self._watchers.items() if uid == user_id],
                key=lambda w: w.event_starts_at,
            )


manager = WatcherManager()

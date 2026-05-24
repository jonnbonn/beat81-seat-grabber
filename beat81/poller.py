"""Shared event-state poller. One background thread, one batch GET /events
per tick, fans the snapshot out to per-(user, event) Watcher subscribers."""

from __future__ import annotations

import os
import random
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Protocol

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
IDLE_WAKE_SECS = 60  # how long the thread sleeps when no subscribers exist


class Subscriber(Protocol):
    event_starts_at: datetime

    def on_snapshot(self, ev: dict[str, Any] | None) -> None: ...


class EventPoller:
    def __init__(self) -> None:
        self._subs: dict[str, set[Subscriber]] = defaultdict(set)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def subscribe(self, event_id: str, sub: Subscriber) -> None:
        with self._lock:
            self._subs[event_id].add(sub)
        self._ensure_running()
        self._wake.set()

    def unsubscribe(self, event_id: str, sub: Subscriber) -> None:
        with self._lock:
            self._subs[event_id].discard(sub)
            if not self._subs[event_id]:
                del self._subs[event_id]
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ------------------------------------------------------------------

    def _ensure_running(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="event-poller", daemon=True)
        self._thread.start()

    def _pick_cadence(self) -> str:
        now = datetime.now(timezone.utc)
        with self._lock:
            for watchers in self._subs.values():
                for w in watchers:
                    if (w.event_starts_at - now).total_seconds() <= TIME_BEFORE:
                        return "fast"
        return "slow"

    def _snapshot_subs(self) -> dict[str, set[Subscriber]]:
        with self._lock:
            return {k: set(v) for k, v in self._subs.items()}

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            subs = self._snapshot_subs()
            if not subs:
                # No work — sleep until subscribe() wakes us, with a slow
                # heartbeat in case the wake signal is lost.
                self._wake.wait(IDLE_WAKE_SECS)
                self._wake.clear()
                continue

            event_ids = list(subs.keys())
            try:
                events = api.fetch_events_batch(event_ids)
                backoff = 1.0
            except Exception as e:
                print(f"poller> batch fetch error: {e}", flush=True)
                self._stop.wait(min(30.0, backoff))
                backoff *= 2
                continue

            seen = {e["id"]: e for e in events}
            # Dispatch each snapshot in its own short-lived thread so a slow
            # authenticated promote in one watcher can't delay anyone else.
            for eid, watchers in subs.items():
                ev = seen.get(eid)  # None ⇒ Beat81 dropped the event (cancelled/removed)
                for w in watchers:
                    threading.Thread(
                        target=w.on_snapshot,
                        args=(ev,),
                        name=f"snap-{eid[:8]}",
                        daemon=True,
                    ).start()

            cadence = self._pick_cadence()
            lo, hi = (FAST_MIN, FAST_MAX) if cadence == "fast" else (SLOW_MIN, SLOW_MAX)
            self._wake.clear()
            if self._wake.wait(random.uniform(lo, hi)):
                # Subscribe/unsubscribe set the wake event — loop again
                # immediately so a freshly-armed watcher gets a snapshot ASAP.
                self._wake.clear()


poller = EventPoller()

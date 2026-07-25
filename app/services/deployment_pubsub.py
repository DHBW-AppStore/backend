"""In-process pub/sub bridge between the Celery event listener and the SSE endpoint.

The Celery event listener runs in a daemon thread and ingests RabbitMQ
events synchronously. Each SSE request produces an asyncio coroutine
that awaits fresh events scoped to one ``deployment_id``.

Each SSE coroutine subscribes and gets back an ``asyncio.Queue``; the
listener thread pushes each event into every queue for that
``deployment_id`` via ``loop.call_soon_threadsafe`` (the push happens
from a non-asyncio thread).

A small per-deployment ring buffer of recent events lets a client that
connects mid-stream be backfilled with what already happened.

Notes:

* Subscribers are confined to this process — fine for one backend
  replica, would fan out incorrectly across N replicas.
* Queues are bounded; on overflow the oldest entry is dropped and a
  synthetic ``{"type": "task-overflow", ...}`` is pushed so the
  frontend can render a "you missed entries" banner.
* The recent buffer is bounded per deployment and cleared when the
  deployment reaches a terminal state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import defaultdict, deque
from typing import Any

logger = logging.getLogger(__name__)

# Maximum number of buffered events per subscriber. If a slow consumer
# stalls past this, the oldest events are dropped and the frontend is told.
_QUEUE_MAXSIZE = 200

# Per-deployment recent-events buffer, used to backfill clients that
# connect mid-stream. Sized for a "what's been happening lately" view;
# the full transcript lives in the task row's ``logs`` column.
_RECENT_MAX = 500


class DeploymentPubSub:
    """One-process, in-memory fan-out keyed by ``deployment_id``.

    Construct once at app start and reuse — there's only ever one
    instance per backend process. ``set_loop`` is called from the
    FastAPI lifespan handler so cross-thread pushes go to the right
    event loop.
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        # Per-deployment ring buffer of recent events, for mid-stream
        # subscribers. Reset when a deployment hits a terminal event.
        self._recent: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=_RECENT_MAX)
        )
        # Protects both maps, touched by the listener thread (publish)
        # and the asyncio loop (subscribe/unsubscribe).
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Loop binding
    # ------------------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the FastAPI event loop.

        Called once during lifespan startup. ``publish`` cannot do
        anything meaningful until this is set — the listener thread
        would have no loop to schedule the put onto.
        """
        self._loop = loop

    # ------------------------------------------------------------------
    # Subscriber API (called from the asyncio loop)
    # ------------------------------------------------------------------

    def subscribe(self, deployment_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Register a new queue for ``deployment_id`` and return it.

        The caller is responsible for matching every ``subscribe`` with
        an ``unsubscribe`` (use ``try/finally`` around the consumer
        loop) — leaked queues sit in memory until the process restarts.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        with self._lock:
            self._subs[deployment_id].append(queue)
        logger.debug("pubsub: subscribed to %s (now %d subs)", deployment_id, len(self._subs[deployment_id]))
        return queue

    def recent(self, deployment_id: str) -> list[dict[str, Any]]:
        """Snapshot the recent-events ring buffer for backfill.

        Used by the SSE endpoint right after subscribing so a client
        connecting mid-stream sees what happened in the last few
        minutes instead of an empty live tail until the worker emits
        its next line.
        """
        with self._lock:
            buf = self._recent.get(deployment_id)
            return list(buf) if buf else []

    def unsubscribe(self, deployment_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            subs = self._subs.get(deployment_id)
            if not subs:
                return
            with contextlib.suppress(ValueError):
                subs.remove(queue)
            if not subs:
                self._subs.pop(deployment_id, None)
        logger.debug("pubsub: unsubscribed from %s", deployment_id)

    # ------------------------------------------------------------------
    # Publisher API (called from the listener thread)
    # ------------------------------------------------------------------

    def publish(self, deployment_id: str, event: dict[str, Any]) -> None:
        """Push ``event`` to every subscriber for ``deployment_id``.

        Threadsafe. If the loop hasn't been bound yet, the event is
        dropped from the live fan-out (no SSE endpoint can be open yet).

        Also appends to the recent-events ring buffer for backfill.
        Terminal lifecycle events
        (``task-succeeded``/``task-failed``/``task-revoked``) clear the
        buffer after the fan-out.
        """
        loop = self._loop
        # Always append to the recent buffer, even if the loop isn't up
        # yet, so later subscribers still benefit.
        with self._lock:
            self._recent[deployment_id].append(event)
            queues = list(self._subs.get(deployment_id, ()))

        if loop is not None:
            for queue in queues:
                loop.call_soon_threadsafe(self._enqueue_or_drop, queue, event)

        # Reset the buffer on terminal events so a follow-up run starts
        # clean. Done after the live fan-out so connected subscribers
        # still see the terminal frame.
        if event.get("type") in ("task-succeeded", "task-failed", "task-revoked"):
            with self._lock:
                self._recent.pop(deployment_id, None)

    @staticmethod
    def _enqueue_or_drop(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        """Push onto the queue; on full queues drop oldest + signal overflow.

        Runs inside the asyncio loop thread (scheduled via
        ``call_soon_threadsafe``), so manipulating the queue is safe
        without an extra lock.
        """
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            queue.put_nowait(
                {
                    "type": "task-overflow",
                    "message": "live stream backpressure: dropped older events",
                }
            )


# Singleton — imported directly by listener and SSE endpoint.
pubsub = DeploymentPubSub()

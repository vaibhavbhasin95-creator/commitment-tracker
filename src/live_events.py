"""
In-process publish/subscribe for live dashboard updates (Server-Sent Events).

Each open dashboard / Integrations tab holds one subscription (a queue) for
its account; app.py publishes a small event ("meeting_created",
"commitments_changed", "commitment_status", "nudge_sent") after a change is
committed, and GET /api/events streams it to the browser, which then
re-fetches what it shows.

This works because the app is one process: the Werkzeug server is threaded
and the nudge scheduler is a thread in the same process. Running several
processes (e.g. gunicorn with multiple workers) would need a shared broker
such as Redis pub/sub in place of the dict below.
"""

import queue
import threading

MAX_QUEUED = 100

_lock = threading.Lock()
_subscribers = {}   # account email -> set of queue.Queue


def subscribe(account_email):
    q = queue.Queue(maxsize=MAX_QUEUED)
    with _lock:
        _subscribers.setdefault(account_email, set()).add(q)
    return q


def unsubscribe(account_email, q):
    with _lock:
        subs = _subscribers.get(account_email)
        if subs:
            subs.discard(q)
            if not subs:
                del _subscribers[account_email]


def publish(account_email, kind, data=None):
    """Never blocks: a subscriber whose queue is full (tab asleep, connection
    stuck) is dropped — its browser reconnects and reloads everything."""
    if not account_email:
        return
    event = {"kind": kind, "data": data or {}}
    with _lock:
        subs = list(_subscribers.get(account_email, ()))
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            unsubscribe(account_email, q)


def subscriber_count(account_email=None):
    with _lock:
        if account_email is not None:
            return len(_subscribers.get(account_email, ()))
        return sum(len(s) for s in _subscribers.values())

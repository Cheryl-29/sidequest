"""Process-local TfNSW throttling shared by providers using the same credential."""

import hashlib
import math
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class RequestGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.cooldown = 0.0

    def acquire(self, deadline: float) -> bool:
        while True:
            with self.lock:
                now = time.monotonic()
                ready = max(self.next_request, self.cooldown)
                if max(now, ready) >= deadline:
                    return False
                if now >= ready:
                    self.next_request = now + 0.35  # fewer than three requests per second
                    return True
                delay = ready - now
            time.sleep(delay)

    def defer(self, seconds: float):
        with self.lock:
            self.cooldown = max(self.cooldown, time.monotonic() + seconds)


_gates: dict[str, RequestGate] = {}
_lock = threading.Lock()


def gate_for(key: str) -> RequestGate:
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    with _lock:
        return _gates.setdefault(fingerprint, RequestGate())


def retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None

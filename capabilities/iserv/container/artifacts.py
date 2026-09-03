"""Bounded, expiring, single-use output artifacts for the trusted orchestrator."""
from __future__ import annotations

import threading
import time
from uuid import uuid4

from iserv_client import MAX_ATTACHMENT_BYTES, IServError


class ArtifactStore:
    def __init__(self, *, max_bytes: int = 20 * 1024 * 1024,
                 max_count: int = 10, ttl: float = 300, clock=time.monotonic):
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, dict, bool]] = {}
        self._max_bytes, self._max_count, self._ttl = max_bytes, max_count, ttl
        self._clock = clock

    def _expire(self) -> None:
        now = self._clock()
        for key, (expiry, _, _) in list(self._items.items()):
            if expiry <= now:
                del self._items[key]

    def purge_expired(self) -> None:
        with self._lock:
            self._expire()

    def put(self, artifact: dict) -> str:
        size = len(artifact["data"])
        if size > MAX_ATTACHMENT_BYTES:
            raise IServError("Attachment is too large")
        with self._lock:
            self._expire()
            if (len(self._items) >= self._max_count
                    or sum(len(a["data"]) for _, a, _ in self._items.values()) + size > self._max_bytes):
                raise IServError("Output artifact storage is full; retrieve existing downloads or wait five minutes")
            key = str(uuid4())
            self._items[key] = (self._clock() + self._ttl, artifact, False)
            return key

    def start(self, key: str) -> dict | None:
        with self._lock:
            self._expire()
            item = self._items.get(key)
            if item is None or item[2]:
                return None
            self._items[key] = (item[0], item[1], True)
            return item[1]

    def complete(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def release(self, key: str) -> None:
        with self._lock:
            item = self._items.get(key)
            if item is not None:
                self._items[key] = (item[0], item[1], False)

"""De-duplication.

Every polling feed re-serves the same announcements until they age out of the
window, so without this the pipeline would score - and trade - the same filing
several times a second. Exchange feeds also occasionally re-publish a filing
with a corrected timestamp, which is the same event as far as we are concerned.
"""

from __future__ import annotations

import time
from collections import OrderedDict


class SeenSet:
    """Bounded, TTL'd set of announcement ids.

    Bounded *and* TTL'd, deliberately: the TTL handles a normal trading day, the
    size cap is the backstop for a feed that starts emitting unique ids on every
    poll (it happens - a source appends a request id to its own key) and would
    otherwise leak memory until the process dies mid-session.
    """

    def __init__(self, ttl_s: float = 6 * 3600, max_size: int = 50_000) -> None:
        self._ttl = ttl_s
        self._max = max_size
        self._items: OrderedDict[str, float] = OrderedDict()

    def add_if_new(self, uid: str) -> bool:
        """True if this uid had not been seen. Marks it seen either way."""
        now = time.monotonic()
        self._expire(now)
        if uid in self._items:
            return False
        self._items[uid] = now + self._ttl
        if len(self._items) > self._max:
            self._items.popitem(last=False)
        return True

    def __contains__(self, uid: str) -> bool:
        self._expire(time.monotonic())
        return uid in self._items

    def __len__(self) -> int:
        return len(self._items)

    def _expire(self, now: float) -> None:
        # Insertion order is expiry order (fixed TTL), so we can stop at the
        # first live entry instead of scanning the whole map.
        while self._items:
            uid, expires_at = next(iter(self._items.items()))
            if expires_at > now:
                break
            self._items.pop(uid, None)

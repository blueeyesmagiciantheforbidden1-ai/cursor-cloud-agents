import time


class TokenBucket:
    def __init__(self, capacity, refill_rate):
        # BUG: no validation; starts empty instead of full
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._tokens = 0
        self._updated = time.monotonic()

    def allow(self, tokens=1, now=None):
        # BUG: no validation; does not clamp; deducts even when insufficient
        if now is None:
            now = time.monotonic()
        elapsed = now - self._updated
        self._tokens = self._tokens + elapsed * self.refill_rate
        self._updated = now
        # BUG: always deducts
        self._tokens -= tokens
        return self._tokens >= 0

import time


class TokenBucket:
    def __init__(self, capacity, refill_rate):
        if capacity <= 0:
            raise ValueError('capacity must be positive')
        if refill_rate <= 0:
            raise ValueError('refill_rate must be positive')
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._tokens = float(capacity)
        self._updated = time.monotonic()

    def allow(self, tokens=1, now=None):
        if tokens <= 0:
            raise ValueError('tokens must be positive')
        if now is None:
            now = time.monotonic()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._updated = now
        if tokens > self._tokens:
            return False
        self._tokens -= tokens
        return True

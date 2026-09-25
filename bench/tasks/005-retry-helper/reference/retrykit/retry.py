"""Retry a callable."""

import time

from retrykit.errors import InvalidAttemptsError


def retry_call(fn, *, attempts, delay=0, exceptions=(Exception,), on_retry=None):
    if attempts < 1:
        raise InvalidAttemptsError('attempts must be >= 1')
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except exceptions as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            if on_retry is not None:
                on_retry(exc, attempt)
            if delay > 0:
                time.sleep(delay)
    raise last_exc  # pragma: no cover

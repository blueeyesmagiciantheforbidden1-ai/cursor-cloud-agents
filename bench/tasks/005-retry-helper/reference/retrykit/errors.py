"""Retry-related errors."""


class InvalidAttemptsError(ValueError):
    """Raised when attempts is less than 1."""

"""Marker base for provider errors whose text is a fixed code, never native output.

A provider adapter opts in by inheriting this beside its existing base, for
example ``class NativeError(ProviderCodeError, RuntimeError)``. The live loop
records ``str(error)`` as the outcome ``error_code`` only for these classes,
and only when the text passes ``SAFE_CODE``; anything else stays the generic
``native_or_connection_failure`` so stderr, paths and environment values never
reach the hub. Mirrors the check codex applies in its own ``_fail``.
"""
import re

SAFE_CODE = re.compile(r'[a-z][a-z0-9_]{0,99}')


class ProviderCodeError(Exception):
    """Mixin marker only; providers keep their RuntimeError or ValueError base."""


def error_code(error):
    """Return the vetted fixed code carried by a provider error, else None."""
    if not isinstance(error, ProviderCodeError):
        return None
    code = str(error)
    return code if SAFE_CODE.fullmatch(code) else None

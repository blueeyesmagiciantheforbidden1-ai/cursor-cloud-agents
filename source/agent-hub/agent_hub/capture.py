"""Fit a worker result to the hub output cap while keeping the real exit code.

A clipped CLI transcript used to be posted as exit code 1. Hub.complete treats
any non-zero exit as a failed room, so one long review stopped the whole
serial chain. Truncation is recorded in the text. The CLI status is unchanged.
"""

MAX_OUTPUT_BYTES = 16000


def bounded_text(text, limit):
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text
    clipped = encoded[:limit]
    while clipped:
        try:
            return clipped.decode('utf-8')
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ''


def apply_capture_limit(exit_code, raw, text, limit=MAX_OUTPUT_BYTES):
    """Return (exit_code, text) with text at most `limit` UTF-8 bytes.

    exit_code is the value the CLI returned, including 0.
    """
    captured_early = raw.startswith('[Earlier output truncated]')
    over_cap = len(text.encode('utf-8')) > limit
    if not captured_early and not over_cap:
        return exit_code, text
    if captured_early:
        note = 'Capture truncation: CLI output exceeded the capture limit.\n\n'
    else:
        note = 'Capture truncation: CLI reply exceeded the hub output limit.\n\n'
    return exit_code, bounded_text(note + text, limit)


def completion_body(lease_token, exit_code, raw, text):
    """Body for Hub.complete. A long transcript stays a successful result when the CLI exited 0."""
    kept, bounded = apply_capture_limit(exit_code, raw, text)
    return {'lease_token': lease_token, 'exit_code': kept, 'output': bounded}

"""Whitespace helpers."""


def collapse_ws(text):
    """Collapse consecutive whitespace to a single space and strip ends."""
    parts = text.split()
    return ' '.join(parts)

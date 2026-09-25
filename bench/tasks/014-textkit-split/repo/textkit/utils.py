"""All text helpers in one place (please split)."""

import re


def normalize_ws(text):
    # Bug: only replaces spaces, not tabs/newlines; no strip
    return re.sub(r' +', ' ', text)


def slugify(text):
    # Bug: leaves multiple dashes; does not strip ends
    text = text.lower()
    return re.sub(r'[^a-z0-9]+', '-', text)


def truncate(text, max_len, *, word_boundary=False):
    # Bug: ellipsis not counted; word_boundary ignored; no ValueError
    if len(text) <= max_len:
        return text
    return text[:max_len] + '...'

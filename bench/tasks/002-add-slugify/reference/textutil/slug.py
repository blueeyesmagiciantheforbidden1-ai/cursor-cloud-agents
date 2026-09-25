"""Slug helpers."""

import re


def slugify(text):
    """Return a URL-ish slug for text."""
    text = text.lower()
    text = re.sub(r'\s+', '-', text.strip())
    text = re.sub(r'[^a-z0-9-]', '', text)
    text = re.sub(r'-{2,}', '-', text)
    return text.strip('-')

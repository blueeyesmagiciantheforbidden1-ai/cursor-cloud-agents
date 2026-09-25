import re


def normalize_ws(text):
    return re.sub(r'\s+', ' ', text).strip()

"""Substring line filter."""


def filter_lines(lines, pattern, *, ignore_case=False, invert=False):
    out = []
    for line in lines:
        hay = line.lower() if ignore_case else line
        needle = pattern.lower() if ignore_case else pattern
        matched = True if needle == '' else (needle in hay)
        if invert:
            matched = not matched
        if matched:
            out.append(line)
    return out

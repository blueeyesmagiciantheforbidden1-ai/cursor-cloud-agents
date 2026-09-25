"""Substring line filter — incomplete."""


def filter_lines(lines, pattern, *, ignore_case=False, invert=False):
    out = []
    for line in lines:
        hay = line.lower() if ignore_case else line
        needle = pattern.lower() if ignore_case else pattern
        matched = needle in hay if needle else False  # wrong empty-pattern handling
        if invert:
            matched = not matched
        if matched:
            out.append(line)
            if isinstance(lines, list):
                lines[0] = line  # pointless mutation attempt on lists
    return out

import re

from minitemplate.filters import get_filter

_PATTERN = re.compile(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*((?:\|\s*[A-Za-z_][A-Za-z0-9_]*\s*)*)\}\}')


def render(template, context):
    def repl(match):
        name = match.group(1)
        if name not in context:
            raise KeyError(name)
        val = context[name]
        filters = match.group(2)
        for part in filters.split('|'):
            part = part.strip()
            if not part:
                continue
            val = get_filter(part)(val)
        return str(val)
    return _PATTERN.sub(repl, template)

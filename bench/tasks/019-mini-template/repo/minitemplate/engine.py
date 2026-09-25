import re

from minitemplate.filters import get_filter

_PATTERN = re.compile(r'\{\{([^}]+)\}\}')


def render(template, context):
    # Bugs: no whitespace tolerance; no chained filters; missing name -> empty
    def repl(match):
        expr = match.group(1)
        parts = expr.split('|')
        name = parts[0]
        val = context.get(name, '')
        for f in parts[1:]:
            val = get_filter(f)(val)
        return str(val)
    return _PATTERN.sub(repl, template)

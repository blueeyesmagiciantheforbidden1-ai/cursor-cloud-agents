_BOUNDS = [
    (0, 59),
    (0, 23),
    (1, 31),
    (1, 12),
    (0, 6),
]


def _parse_field(field, lo, hi):
    if not field:
        raise ValueError('empty field')
    values = set()
    for part in field.split(','):
        if not part:
            raise ValueError('empty part')
        step = 1
        body = part
        if '/' in part:
            body, step_s = part.split('/', 1)
            try:
                step = int(step_s)
            except ValueError as e:
                raise ValueError('bad step') from e
            if step <= 0:
                raise ValueError('step must be positive')
        if body == '*':
            start, end = lo, hi
        elif '-' in body:
            a, b = body.split('-', 1)
            start, end = int(a), int(b)
        else:
            start = end = int(body)
        if start < lo or end > hi or start > end:
            raise ValueError('out of range')
        for v in range(start, end + 1, step):
            values.add(v)
    return frozenset(values)


class CronExpr:
    def __init__(self, expr):
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError('expected five fields')
        self._fields = []
        for part, (lo, hi) in zip(parts, _BOUNDS):
            self._fields.append(_parse_field(part, lo, hi))

    def _check_args(self, minute, hour, day, month, dow):
        vals = (minute, hour, day, month, dow)
        for v, (lo, hi) in zip(vals, _BOUNDS):
            if not isinstance(v, int) or isinstance(v, bool) or v < lo or v > hi:
                raise ValueError('argument out of range')

    def matches(self, minute, hour, day, month, dow):
        self._check_args(minute, hour, day, month, dow)
        vals = (minute, hour, day, month, dow)
        return all(v in field for v, field in zip(vals, self._fields))

    def next_valid_minute(self, minute, hour, day, month, dow):
        self._check_args(minute, hour, day, month, dow)
        m, h = minute, hour
        for _ in range(24 * 60):
            if self.matches(m, h, day, month, dow):
                return (m, h)
            m += 1
            if m > 59:
                m = 0
                h = (h + 1) % 24
        return None

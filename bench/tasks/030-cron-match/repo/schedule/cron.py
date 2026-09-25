class CronExpr:
    def __init__(self, expr):
        # BUG: only stores raw string; no parsing
        self.expr = expr

    def matches(self, minute, hour, day, month, dow):
        # BUG: always true
        return True

    def next_valid_minute(self, minute, hour, day, month, dow):
        return (minute, hour)

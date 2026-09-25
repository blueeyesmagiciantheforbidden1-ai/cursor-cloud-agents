class InsufficientFunds(Exception):
    pass


_ALLOWED = {5, 10, 25, 100}


class VendingMachine:
    def __init__(self, prices):
        if not prices:
            raise ValueError('prices must be non-empty')
        cleaned = {}
        for name, price in prices.items():
            if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
                raise ValueError('prices must be positive ints')
            cleaned[name] = price
        self._prices = cleaned
        self._balance = 0

    @property
    def balance(self):
        return self._balance

    def insert(self, cents):
        if cents not in _ALLOWED:
            raise ValueError('invalid coin')
        self._balance += cents
        return self._balance

    def select(self, item):
        if item not in self._prices:
            raise KeyError(item)
        price = self._prices[item]
        if self._balance < price:
            raise InsufficientFunds('not enough balance')
        change = self._balance - price
        self._balance = 0
        return {'item': item, 'change': change}

    def refund(self):
        amount = self._balance
        self._balance = 0
        return amount

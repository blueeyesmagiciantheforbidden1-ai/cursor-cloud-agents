class InsufficientFunds(Exception):
    pass


class VendingMachine:
    def __init__(self, prices):
        # BUG: no validation
        self._prices = dict(prices)
        self._balance = 0

    @property
    def balance(self):
        return self._balance

    def insert(self, cents):
        # BUG: accepts any positive amount
        if cents <= 0:
            raise ValueError('bad coin')
        self._balance += cents
        return self._balance

    def select(self, item):
        # BUG: does not check funds; does not zero balance (keeps change in machine)
        price = self._prices[item]
        self._balance -= price
        return {'item': item, 'change': self._balance}

    def refund(self):
        # BUG: forgets to zero
        return self._balance

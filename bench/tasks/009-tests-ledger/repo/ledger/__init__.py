"""In-memory transfer ledger."""


class Ledger:
    def __init__(self):
        self._balances = {}
        self._seen = set()

    def balance(self, account):
        return self._balances.get(account, 0)

    def accounts(self):
        return sorted(self._seen)

    def transfer(self, src, dst, amount):
        if not isinstance(amount, int) or amount < 1:
            raise ValueError('amount must be a positive int')
        self._seen.add(src)
        self._seen.add(dst)
        self._balances[src] = self._balances.get(src, 0) - amount
        self._balances[dst] = self._balances.get(dst, 0) + amount

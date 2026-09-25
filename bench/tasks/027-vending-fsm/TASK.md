Implement `VendingMachine` in `vending/machine.py`.

Construction: `VendingMachine(prices: dict[str, int])` maps item name → price in cents (positive ints). Raise `ValueError` if prices is empty or any price is not a positive int.

Behaviour:

- Start with balance 0 and no selection.
- `insert(cents: int)` — accept only 5, 10, 25, or 100. Add to balance. Raise `ValueError` for other amounts. Return the new balance.
- `select(item: str)` — if item unknown, raise `KeyError`. If balance < price, raise `vending.machine.InsufficientFunds` (subclass of `Exception`) and leave state unchanged. On success, set current selection, deduct the price from balance, and return a dict `{'item': item, 'change': remaining_balance}` then reset balance to 0 (change is returned to the customer). Selecting while a prior selection was somehow pending is not a concern — there is no pending state beyond balance.
- `refund()` — return current balance and set balance to 0.
- `balance` property — current cents awaiting purchase.

Exact change is always available (just return leftover balance as change on successful select).

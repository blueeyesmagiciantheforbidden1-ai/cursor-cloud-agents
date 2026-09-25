# Write tests for the ledger module

We have a small in-memory `ledger.Ledger` that already works. Visible coverage is basically nonexistent — please add solid unit tests under `tests/` (a file like `tests/test_ledger.py` is fine).

Behavior to lock in:

- `Ledger()` starts empty; `balance(account)` is `0` for unknown accounts.
- `transfer(src, dst, amount)` moves `amount` from `src` to `dst`.
- `amount` must be a positive int (`>= 1`); otherwise raise `ValueError`.
- Accounts are strings; balances are ints and may go negative (overdraft allowed).
- `transfer` must not leave the books half-updated if it raises (e.g. bad amount).
- `accounts()` returns a sorted list of account names that have ever participated in a transfer (as src or dst).

Do not change the ledger implementation unless you find a real bug — the goal is the test suite.

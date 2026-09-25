Please review `invoice/totals.py` and fix the defects.

We compute invoice totals from a list of line items. Each line is a dict with `qty` (int) and `unit_price` (float, dollars). Callers also pass a discount percent (0–100) and a tax percent (0–100).

Expected behaviour:

- Subtotal is the sum of `qty * unit_price` over all lines.
- Discount is applied to the subtotal: `discounted = subtotal * (1 - discount_percent / 100)`.
- Tax is applied to the discounted amount: `tax = discounted * (tax_percent / 100)`.
- Total is `discounted + tax`.
- All money values returned must be rounded to 2 decimal places using normal half-up rounding (e.g. 1.005 → 1.01).
- An empty line-item list is valid and yields zeros.
- Negative `qty` or `unit_price`, or a discount/tax percent outside 0–100, should raise `ValueError`.
- Do not mutate the caller's list or dicts.

Visible tests cover the happy path only — please catch the edge cases above.

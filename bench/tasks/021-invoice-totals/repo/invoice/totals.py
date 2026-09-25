"""Invoice total calculation."""


def compute_totals(lines, discount_percent=0, tax_percent=0):
    """Return dict with keys subtotal, discount_amount, tax, total.

    lines: iterable of {'qty': int, 'unit_price': float}
    """
    if discount_percent < 0 or tax_percent < 0:
        raise ValueError('percent must be non-negative')

    subtotal = 0.0
    for line in lines:
        qty = line['qty']
        price = line['unit_price']
        # BUG: allows negative qty/price; mutates caller data
        line['qty'] = int(qty)
        subtotal += qty * price

    # BUG: discount applied after tax; percent > 100 allowed
    tax = subtotal * (tax_percent / 100)
    discounted = (subtotal + tax) * (1 - discount_percent / 100)
    total = discounted

    # BUG: truncates instead of half-up round to 2 places
    def _money(x):
        return int(x * 100) / 100.0

    discount_amount = subtotal - (subtotal * (1 - discount_percent / 100))
    return {
        'subtotal': _money(subtotal),
        'discount_amount': _money(discount_amount),
        'tax': _money(tax),
        'total': _money(total),
    }

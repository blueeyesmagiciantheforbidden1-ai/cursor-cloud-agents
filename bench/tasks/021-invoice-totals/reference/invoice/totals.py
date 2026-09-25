"""Invoice total calculation."""

from decimal import Decimal, ROUND_HALF_UP


def _money(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def compute_totals(lines, discount_percent=0, tax_percent=0):
    """Return dict with keys subtotal, discount_amount, tax, total.

    lines: iterable of {'qty': int, 'unit_price': float}
    """
    if not (0 <= discount_percent <= 100):
        raise ValueError('discount_percent must be between 0 and 100')
    if not (0 <= tax_percent <= 100):
        raise ValueError('tax_percent must be between 0 and 100')

    subtotal = 0.0
    for line in lines:
        qty = line['qty']
        price = line['unit_price']
        if qty < 0 or price < 0:
            raise ValueError('qty and unit_price must be non-negative')
        subtotal += qty * price

    discount_amount = subtotal * (discount_percent / 100)
    discounted = subtotal - discount_amount
    tax = discounted * (tax_percent / 100)
    total = discounted + tax

    return {
        'subtotal': _money(subtotal),
        'discount_amount': _money(discount_amount),
        'tax': _money(tax),
        'total': _money(total),
    }

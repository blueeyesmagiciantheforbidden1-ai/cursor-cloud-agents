class InvalidTransition(Exception):
    pass


class Order:
    def __init__(self, order_id):
        # BUG: no id validation
        self.order_id = order_id
        self._state = 'draft'

    @property
    def state(self):
        return self._state

    def submit(self):
        # BUG: allows submit from any state
        self._state = 'submitted'

    def pay(self):
        self._state = 'paid'

    def ship(self):
        self._state = 'shipped'

    def deliver(self):
        self._state = 'delivered'

    def cancel(self):
        # BUG: allows cancel after ship
        self._state = 'cancelled'

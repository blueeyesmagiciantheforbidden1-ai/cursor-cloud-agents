class InvalidTransition(Exception):
    pass


_ALLOWED = {
    'submit': {'draft': 'submitted'},
    'pay': {'submitted': 'paid'},
    'ship': {'paid': 'shipped'},
    'deliver': {'shipped': 'delivered'},
    'cancel': {'draft': 'cancelled', 'submitted': 'cancelled', 'paid': 'cancelled'},
}


class Order:
    def __init__(self, order_id):
        if not order_id:
            raise ValueError('order_id must be non-empty')
        self.order_id = order_id
        self._state = 'draft'

    @property
    def state(self):
        return self._state

    def _transition(self, action):
        mapping = _ALLOWED[action]
        if self._state not in mapping:
            raise InvalidTransition(
                f'cannot {action} from state {self._state}'
            )
        self._state = mapping[self._state]

    def submit(self):
        self._transition('submit')

    def pay(self):
        self._transition('pay')

    def ship(self):
        self._transition('ship')

    def deliver(self):
        self._transition('deliver')

    def cancel(self):
        self._transition('cancel')

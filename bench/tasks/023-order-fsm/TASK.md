Implement an order lifecycle state machine in `orders/fsm.py`.

States: `draft`, `submitted`, `paid`, `shipped`, `delivered`, `cancelled`.

Start every new `Order(order_id)` in `draft`. Expose `state` as a read-only property (string).

Allowed transitions (method → resulting state):

- `submit()` — draft → submitted
- `pay()` — submitted → paid
- `ship()` — paid → shipped
- `deliver()` — shipped → delivered
- `cancel()` — allowed from draft, submitted, or paid → cancelled. Not allowed once shipped or delivered (or already cancelled).

Any illegal transition (wrong state, or double-call) must raise `orders.fsm.InvalidTransition` (a custom exception subclassing `Exception`) with a message that includes the current state name.

Also:

- `Order('')` or `Order(None)` should raise `ValueError`.
- Once `delivered` or `cancelled`, no further transitions are allowed.

The stub in the repo is incomplete — finish it so the rules above hold.

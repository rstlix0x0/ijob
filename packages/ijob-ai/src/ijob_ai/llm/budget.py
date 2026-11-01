"""Spending cap shared by the model calls of one operation."""


class Budget:
    """A spending cap that stops new calls once it is reached.

    Methods never await, so asyncio tasks on one event loop share a budget
    safely. Calls already running when the cap is reached still charge, so the
    total can overshoot the cap by the cost of the calls in flight.
    """

    def __init__(self, cap: float) -> None:
        """Create a budget with nothing spent.

        Args:
            cap: Spending cap in US dollars.
        """
        self._cap = cap
        self._spent = 0.0

    @property
    def cap(self) -> float:
        """Spending cap in US dollars."""
        return self._cap

    @property
    def spent(self) -> float:
        """Amount charged so far, in US dollars."""
        return self._spent

    @property
    def reached(self) -> bool:
        """Whether the amount spent has reached the cap."""
        return self._spent >= self._cap

    def try_start(self) -> bool:
        """Say whether a new call may start.

        Returns:
            True while the amount spent is below the cap.
        """
        return self._spent < self._cap

    def charge(self, cost: float) -> None:
        """Add the cost of a finished call.

        Args:
            cost: Cost of the call in US dollars.
        """
        self._spent += cost

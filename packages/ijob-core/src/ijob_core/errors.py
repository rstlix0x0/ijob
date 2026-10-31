"""Exceptions raised by ijob_core."""


class CoreError(Exception):
    """Root of every exception ijob_core raises."""


class InvalidValueError(CoreError):
    """A value failed validation when a domain type was built.

    Attributes:
        field: Name of the field or value that was rejected.
        reason: Why the value was rejected.
    """

    def __init__(self, field: str, reason: str) -> None:
        """Build the error from the rejected field and the reason.

        Args:
            field: Name of the field or value that was rejected.
            reason: Why the value was rejected.
        """
        super().__init__(f"invalid {field}: {reason}")
        self.field = field
        self.reason = reason

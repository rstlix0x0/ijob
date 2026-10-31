from ijob_core.errors import CoreError, InvalidValueError


def test_invalid_value_error_is_core_error() -> None:
    assert isinstance(InvalidValueError("title", "is empty"), CoreError)


def test_invalid_value_error_message_names_field_and_reason() -> None:
    message = str(InvalidValueError("title", "is empty"))
    assert "title" in message
    assert "is empty" in message

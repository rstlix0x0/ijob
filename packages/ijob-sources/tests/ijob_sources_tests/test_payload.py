import pytest
from ijob_sources.errors import ParseError
from ijob_sources.payload import raw_object


def test_returns_the_object() -> None:
    assert raw_object('{"a": 1, "b": [true, null]}') == {"a": 1, "b": [True, None]}


def test_returns_an_empty_object() -> None:
    assert raw_object("{}") == {}


@pytest.mark.parametrize("raw", ["[]", '"text"', "null", "5", "true"])
def test_refuses_json_that_is_not_an_object(raw: str) -> None:
    with pytest.raises(ParseError, match="raw is not a JSON object"):
        raw_object(raw)


@pytest.mark.parametrize("raw", ["not json", "", "{", '{"a": }'])
def test_refuses_text_that_is_not_json(raw: str) -> None:
    with pytest.raises(ParseError, match="raw is not JSON") as caught:
        raw_object(raw)

    assert isinstance(caught.value.__cause__, ValueError)


def test_refuses_json_nested_too_deeply() -> None:
    with pytest.raises(ParseError, match="raw is not JSON"):
        raw_object("[" * 100_000)

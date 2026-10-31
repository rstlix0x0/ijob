import pytest
from ijob_sources.errors import (
    FetchError,
    HttpStatusError,
    ParseError,
    RobotsDisallowedError,
    SourceNotFoundError,
    SourcesError,
)

_URL = "https://example.test/jobs"


@pytest.mark.parametrize(
    "error",
    [
        RobotsDisallowedError(_URL),
        HttpStatusError(_URL, 500),
        SourceNotFoundError(_URL),
        FetchError(_URL, "connection refused"),
        ParseError("not JSON"),
    ],
)
def test_every_error_is_a_sources_error(error: SourcesError) -> None:
    assert isinstance(error, SourcesError)


def test_source_not_found_is_http_status_error_with_404() -> None:
    error = SourceNotFoundError(_URL)
    assert isinstance(error, HttpStatusError)
    assert error.status == 404
    assert error.url == _URL


def test_http_status_error_message_names_url_and_status() -> None:
    message = str(HttpStatusError(_URL, 503))
    assert _URL in message
    assert "503" in message


def test_attributes_are_kept() -> None:
    assert RobotsDisallowedError(_URL).url == _URL
    fetch = FetchError(_URL, "timed out")
    assert (fetch.url, fetch.reason) == (_URL, "timed out")
    assert ParseError("bad").reason == "bad"

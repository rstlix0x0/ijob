import pytest
from ijob_core.model import SourceKind, SourceRef, parse_source_key
from ijob_sources.errors import (
    FetchedPage,
    FetchError,
    HttpStatusError,
    IdentityMismatchError,
    InvalidRecipeError,
    NotPublicAddressError,
    OffSiteError,
    ParseError,
    RecipeEvaluationError,
    RecipeFailedError,
    ReservedHostError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    SourceNotFoundError,
    SourcesError,
    UnrecognizedPageError,
    UnsupportedRecipeVersionError,
    UnsupportedUrlError,
)

_URL = "https://example.test/jobs"
_REF = SourceRef(SourceKind.GREENHOUSE, parse_source_key("prisma"))


@pytest.mark.parametrize(
    "error",
    [
        RobotsDisallowedError(_URL),
        HttpStatusError(_URL, 500),
        SourceNotFoundError(_URL),
        FetchError(_URL, "connection refused"),
        ResponseTooLargeError(_URL, 1024),
        ParseError("not JSON"),
        IdentityMismatchError(_REF, "company 'Prisma' not found on the board"),
        UnsupportedUrlError(_URL, "no feed found"),
        NotPublicAddressError(_URL),
        ReservedHostError("www.jobstreet.com"),
        InvalidRecipeError("no items"),
        UnsupportedRecipeVersionError(2),
        RecipeEvaluationError("Unregistered function"),
        RecipeFailedError("recipe failed"),
    ],
)
def test_every_error_is_a_sources_error(error: SourcesError) -> None:
    assert isinstance(error, SourcesError)


def test_off_site_error_is_a_sources_error() -> None:
    error = OffSiteError("evil.com")

    assert isinstance(error, SourcesError)
    assert error.host == "evil.com"
    assert str(error) == "off the site: evil.com"
    with pytest.raises(SourcesError):
        raise error


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


def test_identity_error_fields() -> None:
    reason = "company 'Prisma' not found on the board"
    error = IdentityMismatchError(_REF, reason)
    assert isinstance(error, SourcesError)
    assert error.ref == _REF
    assert error.reason == reason
    assert str(error) == f"{_REF}: {reason}"


def test_unsupported_url_error_fields() -> None:
    error = UnsupportedUrlError(_URL, "no feed found")
    assert isinstance(error, SourcesError)
    assert error.url == _URL
    assert error.reason == "no feed found"
    assert _URL in str(error)
    assert "no feed found" in str(error)


def test_error_messages() -> None:
    not_public = NotPublicAddressError(_URL)
    assert str(not_public) == f"{_URL} is not a public address"
    assert not_public.url == _URL
    reserved = ReservedHostError("www.jobstreet.com")
    assert str(reserved) == "www.jobstreet.com is read only by its own source kind"
    assert reserved.host == "www.jobstreet.com"
    invalid = InvalidRecipeError("no items")
    assert str(invalid) == "invalid recipe: no items"
    assert invalid.reason == "no items"
    unsupported = UnsupportedRecipeVersionError(2)
    assert str(unsupported) == "recipe version 2 is not supported"
    assert unsupported.version == 2
    evaluation = RecipeEvaluationError("Unregistered function")
    assert str(evaluation) == "Unregistered function"
    assert evaluation.reason == "Unregistered function"


def test_recipe_failed_messages() -> None:
    ref = SourceRef(SourceKind.RECIPE, parse_source_key("example"))
    reanalyze = "run: ijob sources reanalyze recipe:example"

    failed = RecipeFailedError.failed(ref, "Unregistered function")
    unsupported = RecipeFailedError.unsupported_version(ref, 2)
    expected = RecipeFailedError.expected_format(ref, "json")
    no_jobs = RecipeFailedError.no_jobs(ref)

    assert str(failed) == (
        f"recipe for recipe:example failed: Unregistered function; {reanalyze}"
    )
    assert str(unsupported) == f"recipe version 2 is not supported; {reanalyze}"
    assert str(expected) == f"recipe for recipe:example expected json; {reanalyze}"
    assert str(no_jobs) == (
        "recipe for recipe:example found no jobs; "
        "if the site changed, run: ijob sources reanalyze recipe:example"
    )
    assert isinstance(no_jobs, SourcesError)


def test_response_too_large_is_a_fetch_error_naming_url_and_limit() -> None:
    error = ResponseTooLargeError(_URL, 1024)

    assert isinstance(error, FetchError)
    assert error.url == _URL
    assert error.limit == 1024
    assert _URL in str(error)
    assert "1024" in str(error)


def test_unrecognized_page_carries_the_fetched_page() -> None:
    page = FetchedPage(body=b"<p>hi</p>", content_type="text/html", final_url=_URL)

    error = UnrecognizedPageError("http://example.test/jobs", page=page)

    assert error.page is page
    assert error.url == "http://example.test/jobs"
    assert UnrecognizedPageError(_URL).page is None

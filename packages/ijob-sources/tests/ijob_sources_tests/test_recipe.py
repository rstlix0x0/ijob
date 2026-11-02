import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ijob_core.model import SourceKind, SourceRef, parse_source_key
from ijob_core.money import Money, Period
from ijob_sources.errors import (
    InvalidRecipeError,
    RecipeEvaluationError,
    UnsupportedRecipeVersionError,
)
from ijob_sources.recipe import (
    PageRead,
    Recipe,
    RecipeFields,
    parse_recipe,
    read_detail_description,
    read_page,
    recipe_text,
)
from ijob_sources.recipe_document import (
    Document,
    HtmlDocument,
    JsonDocument,
    RecipeFormat,
    load_document,
)
from ijob_sources.salary_text import SalaryDefaults

_HTML: dict[str, Any] = {
    "version": 1,
    "format": "html",
    "url": "https://example.org/jobs/",
    "items": "//li[@class='job']",
    "fields": {
        "title": ".//a/text()",
        "url": ".//a/@href",
        "company": ".//span[@class='co']",
        "location": ".//span[@class='loc']",
        "posted_at": ".//time/@datetime",
        "description": ".//div[@class='d']",
        "salary": ".//p[@class='pay']",
        "external_id": "./@data-id",
    },
    "salary_defaults": {"currency": "USD", "period": "year"},
    "next_page": "//a[@rel='next']/@href",
    "detail_description": "//div[@id='body']",
}
_JSON: dict[str, Any] = {
    "version": 1,
    "format": "json",
    "url": "https://example.org/api/jobs/",
    "items": "[*]",
    "fields": {
        "title": "title",
        "url": "url",
        "company": "company_name",
        "location": "location",
        "posted_at": "pub_date",
        "description": "description",
        "salary": "pay",
        "external_id": "id",
    },
    "salary_defaults": {"currency": "EUR", "period": "month"},
    "next_page": "next",
}
_MINIMAL: dict[str, Any] = {
    "version": 1,
    "format": "html",
    "url": "https://example.org/jobs/",
    "items": "//li",
    "fields": {"title": "./a", "url": "./a/@href"},
}


def _text(document: dict[str, Any]) -> str:
    return json.dumps(document)


def _with(document: dict[str, Any], path: list[str], value: object) -> str:
    """Return the recipe text with the nested key at `path` set to `value`."""
    changed = copy.deepcopy(document)
    target = changed
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return json.dumps(changed)


def _without(document: dict[str, Any], path: list[str]) -> str:
    """Return the recipe text with the nested key at `path` removed."""
    changed = copy.deepcopy(document)
    target = changed
    for step in path[:-1]:
        target = target[step]
    del target[path[-1]]
    return json.dumps(changed)


def test_valid_html_recipe_parses() -> None:
    recipe = parse_recipe(_text(_HTML))
    assert recipe == Recipe(
        version=1,
        format=RecipeFormat.HTML,
        url="https://example.org/jobs/",
        items="//li[@class='job']",
        fields=RecipeFields(
            title=".//a/text()",
            url=".//a/@href",
            company=".//span[@class='co']",
            location=".//span[@class='loc']",
            posted_at=".//time/@datetime",
            description=".//div[@class='d']",
            salary=".//p[@class='pay']",
            external_id="./@data-id",
        ),
        salary_defaults=SalaryDefaults(currency="USD", period=Period.YEAR),
        next_page="//a[@rel='next']/@href",
        detail_description="//div[@id='body']",
    )


def test_valid_json_recipe_parses() -> None:
    recipe = parse_recipe(_text(_JSON))
    assert recipe.format is RecipeFormat.JSON
    assert recipe.items == "[*]"
    assert recipe.fields.company == "company_name"
    assert recipe.fields.external_id == "id"
    assert recipe.salary_defaults == SalaryDefaults(currency="EUR", period=Period.MONTH)
    assert recipe.next_page == "next"
    assert recipe.detail_description is None


def test_minimal_recipe_has_no_optional_values() -> None:
    recipe = parse_recipe(_text(_MINIMAL))
    assert recipe.fields == RecipeFields(title="./a", url="./a/@href")
    assert recipe.salary_defaults == SalaryDefaults(currency=None, period=None)
    assert recipe.next_page is None
    assert recipe.detail_description is None


def test_salary_defaults_may_hold_one_key() -> None:
    recipe = parse_recipe(_with(_MINIMAL, ["salary_defaults"], {"currency": "GBP"}))
    assert recipe.salary_defaults == SalaryDefaults(currency="GBP", period=None)


@pytest.mark.parametrize("document", [_HTML, _JSON, _MINIMAL])
def test_round_trip_through_recipe_text(document: dict[str, Any]) -> None:
    recipe = parse_recipe(_text(document))
    again = parse_recipe(recipe_text(recipe))
    assert again == recipe
    assert recipe_text(again) == recipe_text(recipe)


def test_recipe_text_is_sorted_json() -> None:
    text = recipe_text(parse_recipe(_text(_HTML)))
    assert text == json.dumps(json.loads(text), sort_keys=True)
    assert json.loads(text) == _HTML


@pytest.mark.parametrize(
    "path",
    [["extra"], ["fields", "extra"], ["salary_defaults", "extra"]],
    ids=["top-level", "fields", "salary_defaults"],
)
def test_unknown_key_rejected(path: list[str]) -> None:
    with pytest.raises(InvalidRecipeError, match="extra"):
        parse_recipe(_with(_HTML, path, "x"))


@pytest.mark.parametrize(
    "path",
    [
        ["version"],
        ["format"],
        ["url"],
        ["items"],
        ["fields", "title"],
        ["fields", "url"],
    ],
    ids=".".join,
)
def test_missing_required_key_rejected(path: list[str]) -> None:
    with pytest.raises(InvalidRecipeError, match=path[-1]):
        parse_recipe(_without(_HTML, path))


def test_missing_fields_object_rejected() -> None:
    with pytest.raises(InvalidRecipeError, match="fields"):
        parse_recipe(_without(_HTML, ["fields"]))


@pytest.mark.parametrize("value", ["xml", "HTML", 1, None])
def test_bad_format_rejected(value: object) -> None:
    with pytest.raises(InvalidRecipeError, match="format"):
        parse_recipe(_with(_HTML, ["format"], value))


@pytest.mark.parametrize(
    "value",
    ["ftp://example.org/", "/jobs/", "example.org/jobs", "https://", "", 5, None],
)
def test_url_must_be_absolute_http(value: object) -> None:
    with pytest.raises(InvalidRecipeError, match="url"):
        parse_recipe(_with(_HTML, ["url"], value))


@pytest.mark.parametrize(
    "path", [["items"], ["fields", "title"], ["next_page"], ["fields", "salary"]]
)
def test_expression_limits(path: list[str]) -> None:
    for bad in ("", "   ", "a" * 501, 7, None, ["a"]):
        with pytest.raises(InvalidRecipeError, match=path[-1]):
            parse_recipe(_with(_HTML, path, bad))


def test_expression_of_exactly_500_characters_is_accepted() -> None:
    expression = "a" * 500
    recipe = parse_recipe(_with(_HTML, ["items"], expression))
    assert recipe.items == expression
    recipe = parse_recipe(_with(_JSON, ["items"], expression))
    assert recipe.items == expression


def test_xpath_syntax_error_rejected() -> None:
    with pytest.raises(InvalidRecipeError, match="items"):
        parse_recipe(_with(_HTML, ["items"], "//li["))
    with pytest.raises(InvalidRecipeError, match=r"fields\.title"):
        parse_recipe(_with(_HTML, ["fields", "title"], "//li["))


def test_xpath_null_character_rejected() -> None:
    with pytest.raises(InvalidRecipeError, match="items"):
        parse_recipe(_with(_HTML, ["items"], "//li\x00"))


def test_jmespath_syntax_error_rejected() -> None:
    with pytest.raises(InvalidRecipeError, match="items"):
        parse_recipe(_with(_JSON, ["items"], "title["))
    with pytest.raises(InvalidRecipeError, match="next_page"):
        parse_recipe(_with(_JSON, ["next_page"], "title["))


def test_expression_is_checked_by_the_engine_of_its_format() -> None:
    parse_recipe(_with(_HTML, ["items"], "//li[1]"))
    with pytest.raises(InvalidRecipeError):
        parse_recipe(_with(_JSON, ["items"], "//li[1]"))
    with pytest.raises(InvalidRecipeError):
        parse_recipe(_with(_HTML, ["items"], "[*]"))


def test_unknown_function_compiles() -> None:
    assert parse_recipe(_with(_HTML, ["items"], "foo()")).items == "foo()"
    assert parse_recipe(_with(_JSON, ["items"], "foo(@)")).items == "foo(@)"


def test_version_two_unsupported() -> None:
    with pytest.raises(UnsupportedRecipeVersionError) as caught:
        parse_recipe(_with(_HTML, ["version"], 2))
    assert caught.value.version == 2


def test_unsupported_version_is_reported_before_other_faults() -> None:
    with pytest.raises(UnsupportedRecipeVersionError):
        parse_recipe(_with(_HTML, ["version"], 3).replace('"items"', '"other"'))


@pytest.mark.parametrize("version", [True, False, "1", 1.0, None, [1]])
def test_version_not_an_integer_invalid(version: object) -> None:
    with pytest.raises(InvalidRecipeError, match="version"):
        parse_recipe(_with(_HTML, ["version"], version))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("currency", "usd"),
        ("currency", "XXX"),
        ("currency", 5),
        ("currency", None),
        ("period", "week"),
        ("period", 5),
        ("period", None),
    ],
)
def test_bad_salary_defaults_rejected(key: str, value: object) -> None:
    with pytest.raises(InvalidRecipeError, match=key):
        parse_recipe(_with(_HTML, ["salary_defaults", key], value))


def test_salary_defaults_must_be_an_object() -> None:
    with pytest.raises(InvalidRecipeError, match="salary_defaults"):
        parse_recipe(_with(_HTML, ["salary_defaults"], "USD"))


def test_fields_must_be_an_object() -> None:
    with pytest.raises(InvalidRecipeError, match="fields"):
        parse_recipe(_with(_HTML, ["fields"], ["title"]))


def test_detail_description_only_for_html() -> None:
    with pytest.raises(InvalidRecipeError, match="detail_description"):
        parse_recipe(_with(_JSON, ["detail_description"], "//div"))
    assert parse_recipe(_text(_HTML)).detail_description == "//div[@id='body']"


@pytest.mark.parametrize("text", ["[]", '""', "{", "null", "3", ""])
def test_non_object_and_bad_json_rejected(text: str) -> None:
    with pytest.raises(InvalidRecipeError):
        parse_recipe(text)


_PAGE = "https://example.org/jobs/"
_REF = SourceRef(SourceKind.RECIPE, parse_source_key("example"))
_FIXTURES = Path(__file__).parent / "fixtures" / "recipe"
_PROBE_HTML = (
    '<ul><li><a href="/j/1/">Title</a><span class="v">  hello   world </span>'
    "<b>x</b><b>y</b><i>   </i></li></ul>"
)
_PROBE_JSON = json.dumps(
    [
        {
            "title": "T",
            "url": "/j/1",
            "name": "  Acme   Co ",
            "n": 3,
            "f": 1.5,
            "big": 142.0,
            "tags": ["a", "b"],
            "obj": {"k": 1},
            "flag": True,
            "blank": "   ",
            "nil": None,
        }
    ]
)


def _html_document(text: str | bytes) -> Document:
    body = text.encode() if isinstance(text, str) else text
    return load_document(body, "text/html", _PAGE)


def _json_document(value: object) -> Document:
    return load_document(json.dumps(value).encode(), "application/json", _PAGE)


def _recipe(
    recipe_format: str,
    items: str,
    fields: dict[str, str],
    **extra: object,
) -> Recipe:
    document: dict[str, object] = {
        "version": 1,
        "format": recipe_format,
        "url": _PAGE,
        "items": items,
        "fields": fields,
    }
    document.update(extra)
    return parse_recipe(json.dumps(document))


def _read(
    recipe: Recipe,
    document: Document,
    page_url: str = _PAGE,
    company: str = "Fallback",
) -> PageRead:
    return read_page(recipe, document, page_url=page_url, ref=_REF, company=company)


def _probe(recipe: Recipe, document: str) -> PageRead:
    """Read an inline document in the format of the recipe."""
    content_type = (
        "text/html" if recipe.format is RecipeFormat.HTML else "application/json"
    )
    return _read(recipe, load_document(document.encode(), content_type, _PAGE))


def test_reads_bare_text_company_list() -> None:
    recipe = _recipe(
        "html",
        "//ol[contains(@class, 'list-recent-jobs')]/li",
        {
            "title": ".//span[@class='listing-company-name']/a/text()",
            "url": ".//span[@class='listing-company-name']/a/@href",
            "company": "normalize-space("
            ".//span[@class='listing-company-name']/text()[last()])",
            "location": "normalize-space(.//span[@class='listing-location'])",
            "posted_at": ".//time/@datetime",
        },
    )
    document = _html_document((_FIXTURES / "list_bare_company.html").read_bytes())
    page = _read(recipe, document)
    assert (page.items, page.rejected, page.next_url) == (3, 0, None)
    assert [d.title for d in page.drafts] == [
        "Senior Python Engineer",
        "Data Platform Developer",
        "Backend Engineer, Django",
    ]
    assert [d.company for d in page.drafts] == [
        "Acme Widgets Ltd",
        "Globex Example GmbH",
        "Initech Sample Co",
    ]
    first = page.drafts[0]
    assert first.url == "https://example.org/jobs/9001/"
    assert first.external_id == "https://example.org/jobs/9001/"
    assert first.location == "Remote, Worldwide"
    assert first.posted_at == datetime(2026, 10, 23, 22, 34, 52, 166674, tzinfo=UTC)
    assert first.source == _REF


def test_reads_category_salary_list() -> None:
    recipe = _recipe(
        "html",
        "//li[contains(@class, 'new-listing-container')]",
        {
            "title": ".//h4",
            "url": ".//a/@href",
            "company": ".//p[@class='new-listing__company-name']",
            "salary": ".//p[contains(@class, 'categories__category')]"
            "[contains(., '$')]",
        },
        salary_defaults={"currency": "USD", "period": "year"},
    )
    document = _html_document((_FIXTURES / "list_category_salary.html").read_bytes())
    page = _read(recipe, document, company="Ignored Board")
    assert page.items == 3
    first, second, third = page.drafts
    assert first.company == "Acme Cloud"
    assert first.salaries == (
        Money(
            min=Decimal(50000),
            max=Decimal(74999),
            currency="USD",
            period=Period.YEAR,
        ),
    )
    assert second.salaries == ()
    assert third.salaries == (
        Money(min=Decimal(100000), max=None, currency="USD", period=Period.YEAR),
    )


def test_reads_json_list() -> None:
    recipe = _recipe(
        "json",
        "[*]",
        {
            "title": "title",
            "url": "url",
            "company": "company_name",
            "posted_at": "pub_date",
            "description": "description",
        },
    )
    document = load_document(
        (_FIXTURES / "jobs.json").read_bytes(), "application/json", _PAGE
    )
    page = _read(recipe, document)
    assert page.items == 3
    assert [d.company for d in page.drafts] == [
        "Acme Commerce",
        "Globex Data",
        "Initech Support",
    ]
    first = page.drafts[0]
    assert first.url == "https://jobs.example.test/job/go/101/"
    assert first.posted_at == datetime(
        2026, 10, 29, 9, 50, 51, tzinfo=timezone(timedelta(hours=-4))
    )
    assert first.posted_at is not None
    assert first.posted_at.utcoffset() == timedelta(hours=-4)
    assert first.description == (
        "Build **stores** with [Acme](https://acme.example.test/)."
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("string(.//span[@class='v'])", "hello world"),
        ("count(.//b) * 71", "142"),
        ("count(.//b) div 4 * 3", "1.5"),
        (".//span[@class='v']", "hello world"),
        (".//b", "x"),
        (".//a/@href", "/j/1/"),
        ("boolean(.//b)", None),
        (".//nothing", None),
        ("string(.//i)", None),
        ("1 div 0", None),
    ],
)
def test_field_text_rules_xpath(expression: str, expected: str | None) -> None:
    fields = {"title": "'T'", "url": "'/j/1/'", "company": expression}
    page = _probe(_recipe("html", "//li", fields), _PROBE_HTML)
    assert page.drafts[0].company == (expected or "Fallback")


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("name", "Acme Co"),
        ("length(tags)", "2"),
        ("n", "3"),
        ("f", "1.5"),
        ("big", "142"),
        ("tags", "a"),
        ("obj", None),
        ("flag", None),
        ("blank", None),
        ("nil", None),
        ("missing", None),
        ("contains(name, 'Acme')", None),
    ],
)
def test_field_text_rules_jmespath(expression: str, expected: str | None) -> None:
    fields = {"title": "title", "url": "url", "company": expression}
    page = _probe(_recipe("json", "[*]", fields), _PROBE_JSON)
    assert page.drafts[0].company == (expected or "Fallback")


@pytest.mark.parametrize(
    ("recipe_format", "items", "document"),
    [
        ("html", "count(//li)", _PROBE_HTML),
        ("html", "string(//li)", _PROBE_HTML),
        ("html", "boolean(//li)", _PROBE_HTML),
        ("html", "//article", _PROBE_HTML),
        ("json", "length(@)", _PROBE_JSON),
        ("json", "[0]", _PROBE_JSON),
        ("json", "missing", _PROBE_JSON),
    ],
)
def test_items_not_a_list_gives_zero(
    recipe_format: str, items: str, document: str
) -> None:
    recipe = _recipe(recipe_format, items, {"title": "title", "url": "url"})
    page = _probe(recipe, document)
    assert (page.items, page.drafts, page.rejected) == (0, [], 0)


_TITLE_URL = {"title": "'T'", "url": "'/j/'"}
_JSON_FIELDS = {"title": "title", "url": "url"}


@pytest.mark.parametrize(
    ("recipe", "document"),
    [
        (_recipe("html", "foo()", _TITLE_URL), _PROBE_HTML),
        (_recipe("html", "//li", {**_TITLE_URL, "title": "foo()"}), _PROBE_HTML),
        (_recipe("html", "//li", {**_TITLE_URL, "title": "$v"}), _PROBE_HTML),
        (_recipe("html", "//a/text()", _TITLE_URL), _PROBE_HTML),
        (_recipe("html", "//li", _TITLE_URL, next_page="foo()"), _PROBE_HTML),
        (_recipe("json", "foo(@)", _JSON_FIELDS), _PROBE_JSON),
        (_recipe("json", "[*]", {**_JSON_FIELDS, "title": "foo(@)"}), _PROBE_JSON),
        (_recipe("json", "[*]", {**_JSON_FIELDS, "title": "abs(title)"}), _PROBE_JSON),
    ],
)
def test_unknown_function_raises_evaluation_error(
    recipe: Recipe, document: str
) -> None:
    with pytest.raises(RecipeEvaluationError):
        _probe(recipe, document)


def test_evaluation_error_carries_the_engine_text() -> None:
    recipe = _recipe("html", "foo()", _TITLE_URL)
    with pytest.raises(RecipeEvaluationError, match="Unregistered function"):
        _probe(recipe, _PROBE_HTML)


def test_document_of_the_other_format_raises_evaluation_error() -> None:
    html = _recipe("html", "//li", _TITLE_URL)
    with pytest.raises(RecipeEvaluationError):
        _read(html, _json_document([]))
    json_recipe = _recipe("json", "[*]", _JSON_FIELDS)
    with pytest.raises(RecipeEvaluationError):
        _read(json_recipe, _html_document(_PROBE_HTML))


def test_relative_urls_resolve_against_page() -> None:
    recipe = _recipe("json", "[*]", _JSON_FIELDS)
    document = _json_document(
        [
            {"title": "A", "url": "/jobs/1/"},
            {"title": "B", "url": "2/"},
            {"title": "C", "url": "//cdn.example.org/3/"},
            {"title": "D", "url": "https://other.example.net/4"},
        ]
    )
    page = _read(recipe, document, page_url="https://example.org/list?page=2")
    assert [d.url for d in page.drafts] == [
        "https://example.org/jobs/1/",
        "https://example.org/2/",
        "https://cdn.example.org/3/",
        "https://other.example.net/4",
    ]


def test_missing_title_or_bad_url_rejected() -> None:
    recipe = _recipe("json", "[*]", _JSON_FIELDS)
    document = _json_document(
        [
            {"title": "Good", "url": "/jobs/1/"},
            {"title": "Mail", "url": "mailto:jobs@example.org"},
            {"title": "", "url": "/jobs/2/"},
            {"title": "Script", "url": "javascript:void(0)"},
            {"url": "/jobs/3/"},
            {"title": "No url"},
        ]
    )
    page = _read(recipe, document)
    assert page.items == 6
    assert [d.title for d in page.drafts] == ["Good"]
    assert page.rejected == 5


def test_external_id_defaults_to_url() -> None:
    recipe = _recipe("json", "[*]", {**_JSON_FIELDS, "external_id": "id"})
    document = _json_document(
        [{"title": "A", "url": "/jobs/1/", "id": 77}, {"title": "B", "url": "/jobs/2/"}]
    )
    first, second = _read(recipe, document).drafts
    assert first.external_id == "77"
    assert second.external_id == "https://example.org/jobs/2/"


def test_duplicate_external_id_keeps_first() -> None:
    recipe = _recipe("json", "[*]", {**_JSON_FIELDS, "external_id": "id"})
    document = _json_document(
        [
            {"title": "First", "url": "/jobs/1/", "id": 1},
            {"title": "Again", "url": "/jobs/9/", "id": 1},
            {"title": "Other", "url": "/jobs/2/", "id": 2},
        ]
    )
    page = _read(recipe, document)
    assert page.items == 3
    assert [d.title for d in page.drafts] == ["First", "Other"]
    assert page.rejected == 0


def test_posted_at_forms() -> None:
    recipe = _recipe("json", "[*]", {**_JSON_FIELDS, "posted_at": "at"})
    stamps = [
        "2026-10-23T22:34:52.166674+00:00",
        "2026-10-23T22:34:52",
        "2026-10-23",
        "yesterday",
    ]
    document = _json_document(
        [{"title": f"J{n}", "url": f"/j/{n}", "at": at} for n, at in enumerate(stamps)]
    )
    page = _read(recipe, document)
    assert page.rejected == 0
    assert [d.posted_at for d in page.drafts] == [
        datetime(2026, 10, 23, 22, 34, 52, 166674, tzinfo=UTC),
        datetime(2026, 10, 23, 22, 34, 52, tzinfo=UTC),
        datetime(2026, 10, 23, tzinfo=UTC),
        None,
    ]


def test_posted_at_outside_the_utc_range_is_absent_and_the_job_kept() -> None:
    recipe = _recipe("json", "[*]", {**_JSON_FIELDS, "posted_at": "at"})
    stamps = ["9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+05:00"]
    document = _json_document(
        [{"title": f"J{n}", "url": f"/j/{n}", "at": at} for n, at in enumerate(stamps)]
    )
    page = _read(recipe, document)
    assert page.rejected == 0
    assert [d.title for d in page.drafts] == ["J0", "J1"]
    assert [d.posted_at for d in page.drafts] == [None, None]


def test_first_entry_of_a_deeply_nested_list_result_is_read() -> None:
    recipe = _recipe("json", "[*]", {**_JSON_FIELDS, "location": "loc"})
    nested: object = "Remote"
    for _ in range(5000):
        nested = [nested]
    document = JsonDocument([{"title": "T", "url": "/j/1", "loc": nested}])
    page = _read(recipe, document)
    assert [d.location for d in page.drafts] == ["Remote"]


def test_description_markdown_from_node() -> None:
    recipe = _recipe(
        "html",
        "//li",
        {"title": "./a", "url": "./a/@href", "description": ".//div[@class='d']"},
    )
    document = _html_document(
        '<ul><li><a href="/j/1/">T</a><div class="d">'
        '<p>a <a href="https://x.example/">b</a></p></div></li></ul>'
    )
    description = _read(recipe, document).drafts[0].description
    assert description == "a [b](https://x.example/)"


def test_description_absent_when_the_node_is_missing() -> None:
    recipe = _recipe(
        "html", "//li", {"title": "./a", "url": "./a/@href", "description": ".//div"}
    )
    document = _html_document('<ul><li><a href="/j/1/">T</a></li></ul>')
    assert _read(recipe, document).drafts[0].description is None


def test_raw_holds_fields_and_detail_xpath() -> None:
    fields = {"title": "./a", "url": "./a/@href", "location": "./span"}
    recipe = _recipe("html", "//li", fields, detail_description="//div[@id='body']")
    document = _html_document(
        '<ul><li><a href="/jobs/1/"> Senior  Dev </a><span>Remote</span></li></ul>'
    )
    draft = _read(recipe, document, page_url="https://example.org/").drafts[0]
    assert draft.raw == json.dumps(json.loads(draft.raw), sort_keys=True)
    assert json.loads(draft.raw) == {
        "title": "Senior Dev",
        "url": "https://example.org/jobs/1/",
        "location": "Remote",
        "detail_description": "//div[@id='body']",
    }
    with_description = _recipe(
        "html",
        "//li",
        {**fields, "description": "./span"},
        detail_description="//div[@id='body']",
    )
    raw = json.loads(_read(with_description, document).drafts[0].raw)
    assert "detail_description" not in raw
    assert raw["description"] == "<span>Remote</span>"


def test_raw_omits_detail_xpath_when_the_description_field_matches_nothing() -> None:
    recipe = _recipe(
        "html",
        "//li",
        {"title": "./a", "url": "./a/@href", "description": ".//div"},
        detail_description="//div[@id='body']",
    )
    document = _html_document('<ul><li><a href="/j/1/">T</a></li></ul>')
    draft = _read(recipe, document).drafts[0]
    assert draft.description is None
    assert "detail_description" not in json.loads(draft.raw)


def test_raw_is_ascii_json() -> None:
    recipe = _recipe("json", "[*]", _JSON_FIELDS)
    document = _json_document([{"title": "Café", "url": "/j/1"}])
    draft = _read(recipe, document).drafts[0]
    assert draft.title == "Café"
    assert draft.raw.isascii()


def test_text_that_cannot_be_encoded_is_absent() -> None:
    recipe = _recipe("json", "[*]", _JSON_FIELDS)
    document = load_document(
        b'[{"title": "bad\\ud800", "url": "/j/1"}, {"title": "ok", "url": "/j/2"}]',
        "application/json",
        _PAGE,
    )
    page = _read(recipe, document)
    assert [d.title for d in page.drafts] == ["ok"]
    assert page.rejected == 1


def test_next_url_resolved() -> None:
    recipe = _recipe(
        "html",
        "//li",
        {"title": "./a", "url": "./a/@href"},
        next_page="//a[@rel='next']/@href",
    )
    document = _html_document(
        '<ul><li><a href="/j/1/">T</a></li></ul>'
        '<a rel="next" href="/jobs/?page=2">n</a>'
    )
    page = _read(recipe, document, page_url="https://example.org/jobs/")
    assert page.next_url == "https://example.org/jobs/?page=2"


def test_next_url_from_a_json_page() -> None:
    recipe = _recipe("json", "items", _JSON_FIELDS, next_page="next")
    document = _json_document({"items": [], "next": "/api/jobs?page=3"})
    page = _read(recipe, document, page_url="https://example.org/api/jobs?page=2")
    assert page.next_url == "https://example.org/api/jobs?page=3"
    assert page.items == 0


def test_no_next_page_gives_none() -> None:
    fields = {"title": "./a", "url": "./a/@href"}
    document = _html_document('<ul><li><a href="/j/1/">T</a></li></ul>')
    without = _recipe("html", "//li", fields)
    assert _read(without, document).next_url is None
    missing = _recipe("html", "//li", fields, next_page="//a[@rel='next']/@href")
    assert _read(missing, document).next_url is None
    unusable = _recipe("html", "//li", fields, next_page="'mailto:a@example.org'")
    assert _read(unusable, document).next_url is None


def _detail_page() -> HtmlDocument:
    document = load_document(
        (_FIXTURES / "detail.html").read_bytes(), "text/html", _PAGE
    )
    assert isinstance(document, HtmlDocument)
    return document


def test_read_detail_description_gives_markdown() -> None:
    text = read_detail_description(_detail_page(), "//div[@id='body']")

    assert text is not None
    assert "**platform**" in text
    assert "Run Kubernetes" in text
    assert "Apply by email" not in text


def test_read_detail_description_matching_nothing_is_none() -> None:
    assert read_detail_description(_detail_page(), "//div[@id='absent']") is None


def test_read_detail_description_string_result() -> None:
    assert (
        read_detail_description(_detail_page(), "string(//h1)") == "Platform Engineer"
    )


def test_read_detail_description_bad_xpath() -> None:
    with pytest.raises(RecipeEvaluationError):
        read_detail_description(_detail_page(), "foo()")

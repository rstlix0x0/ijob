"""The v1 recipe: a validated set of expressions that reads jobs from a page.

A recipe is JSON text. `parse_recipe` checks it against the v1 rules and
returns a `Recipe`; `recipe_text` writes one back as canonical JSON, so
`parse_recipe(recipe_text(recipe)) == recipe`. Expressions are XPath 1.0 for an
`html` recipe and JMESPath for a `json` one; no extension function is
registered for either engine. `read_page` evaluates a recipe on one document
and returns the job drafts it reads; `read_detail_description` evaluates the
recipe's `detail_description` XPath on a job's own page.
"""

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

import jmespath
import lxml.html
from ijob_core.errors import InvalidValueError
from ijob_core.model import ExternalId, SightingDraft, SourceRef
from ijob_core.money import Money, Period
from jmespath.exceptions import JMESPathError
from lxml import etree

from ijob_sources.description import TextForm, normalize
from ijob_sources.errors import (
    InvalidRecipeError,
    RecipeEvaluationError,
    UnsupportedRecipeVersionError,
)
from ijob_sources.recipe_document import (
    Document,
    HtmlDocument,
    JsonDocument,
    RecipeFormat,
)
from ijob_sources.salary_text import KNOWN_CURRENCIES, SalaryDefaults, parse_salary

_LOGGER = logging.getLogger(__name__)

SUPPORTED_VERSION: Final = 1
MAX_EXPRESSION_CHARS: Final = 500

_TOP_KEYS: Final = frozenset(
    {
        "version",
        "format",
        "url",
        "items",
        "fields",
        "salary_defaults",
        "next_page",
        "detail_description",
    }
)
_FIELD_KEYS: Final = (
    "title",
    "url",
    "company",
    "location",
    "posted_at",
    "description",
    "salary",
    "external_id",
)
_SALARY_DEFAULT_KEYS: Final = frozenset({"currency", "period"})


@dataclass(frozen=True, slots=True, kw_only=True)
class RecipeFields:
    """The expressions that read one job out of an item.

    Each expression is evaluated with the item as its context.

    Attributes:
        title: The job title; required.
        url: The job's address, absolute or relative to the page; required.
        company: The hiring company.
        location: Where the job is.
        posted_at: When the job was published, as ISO 8601 text.
        description: The job's description, as HTML.
        salary: The pay, as free text.
        external_id: The job's identifier within the source.
    """

    title: str
    url: str
    company: str | None = None
    location: str | None = None
    posted_at: str | None = None
    description: str | None = None
    salary: str | None = None
    external_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Recipe:
    """A validated v1 recipe.

    Attributes:
        version: The recipe version; always `1`.
        format: The kind of document the recipe reads.
        url: The http(s) address of the page the recipe reads.
        items: The expression, on the whole document, that selects each job.
        fields: The expressions that read one job out of an item.
        salary_defaults: What a salary text falls back to when it does not
            say.
        next_page: The expression, on the whole document, that gives the next
            page's address.
        detail_description: For an `html` recipe, the XPath on a job's own
            page that gives its description.
    """

    version: int
    format: RecipeFormat
    url: str
    items: str
    fields: RecipeFields
    salary_defaults: SalaryDefaults = field(
        default_factory=lambda: SalaryDefaults(currency=None, period=None)
    )
    next_page: str | None = None
    detail_description: str | None = None


def parse_recipe(text: str) -> Recipe:
    """Read recipe text and check it against the v1 rules.

    The `version` is checked first, so a recipe written for a later release is
    reported as unsupported rather than as malformed. Every expression must be
    a non-empty string of at most 500 characters that compiles in the engine
    of the recipe's format. A name that does not exist, such as an unknown
    function, compiles and fails only when evaluated.

    Args:
        text: The recipe as JSON text.

    Returns:
        The recipe.

    Raises:
        InvalidRecipeError: The text is not a JSON object, has an unknown or
            missing key, a value of the wrong kind, an expression that does
            not compile, or `detail_description` on a `json` recipe.
        UnsupportedRecipeVersionError: The version is an integer other than 1.
    """
    document = _load_object(text)
    _check_version(document)
    _reject_unknown(document, _TOP_KEYS, "")
    recipe_format = _read_format(document)
    url = _read_url(document)
    items = _read_expression(document, "items", recipe_format, "")
    fields = _read_fields(document, recipe_format)
    next_page = _read_optional_expression(document, "next_page", recipe_format, "")
    detail = _read_optional_expression(
        document, "detail_description", RecipeFormat.HTML, ""
    )
    if detail is not None and recipe_format is not RecipeFormat.HTML:
        reason = "detail_description is only for an html recipe"
        raise InvalidRecipeError(reason)
    return Recipe(
        version=SUPPORTED_VERSION,
        format=recipe_format,
        url=url,
        items=items,
        fields=fields,
        salary_defaults=_read_salary_defaults(document),
        next_page=next_page,
        detail_description=detail,
    )


def recipe_text(recipe: Recipe) -> str:
    """Write a recipe as canonical JSON text, with sorted keys.

    Args:
        recipe: The recipe to write.

    Returns:
        JSON text that `parse_recipe` reads back to an equal recipe.
    """
    fields = {
        name: value
        for name in _FIELD_KEYS
        if (value := getattr(recipe.fields, name)) is not None
    }
    document: dict[str, object] = {
        "version": recipe.version,
        "format": recipe.format.value,
        "url": recipe.url,
        "items": recipe.items,
        "fields": fields,
    }
    defaults: dict[str, str] = {}
    if recipe.salary_defaults.currency is not None:
        defaults["currency"] = recipe.salary_defaults.currency
    if recipe.salary_defaults.period is not None:
        defaults["period"] = recipe.salary_defaults.period.value
    if defaults:
        document["salary_defaults"] = defaults
    if recipe.next_page is not None:
        document["next_page"] = recipe.next_page
    if recipe.detail_description is not None:
        document["detail_description"] = recipe.detail_description
    return json.dumps(document, sort_keys=True)


def _load_object(text: str) -> dict[str, object]:
    """Parse the text as a JSON object."""
    try:
        document: object = json.loads(text)
    except (ValueError, RecursionError) as error:
        reason = f"not JSON: {error}"
        raise InvalidRecipeError(reason) from error
    if not isinstance(document, dict):
        reason = "not a JSON object"
        raise InvalidRecipeError(reason)
    return document


def _check_version(document: dict[str, object]) -> None:
    """Check the version first: an integer, and the one this release reads."""
    if "version" not in document:
        reason = "missing key 'version'"
        raise InvalidRecipeError(reason)
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        reason = "version must be an integer"
        raise InvalidRecipeError(reason)
    if version != SUPPORTED_VERSION:
        raise UnsupportedRecipeVersionError(version)


def _reject_unknown(
    section: dict[str, object], allowed: frozenset[str] | tuple[str, ...], prefix: str
) -> None:
    """Reject the first key of the section that is not allowed."""
    for key in section:
        if key not in allowed:
            reason = f"unknown key '{prefix}{key}'"
            raise InvalidRecipeError(reason)


def _read_object(document: dict[str, object], key: str) -> dict[str, object] | None:
    """Return the nested object at `key`, or `None` when the key is absent."""
    if key not in document:
        return None
    value = document[key]
    if not isinstance(value, dict):
        reason = f"{key} must be an object"
        raise InvalidRecipeError(reason)
    return value


def _read_format(document: dict[str, object]) -> RecipeFormat:
    """Read the `format` key."""
    if "format" not in document:
        reason = "missing key 'format'"
        raise InvalidRecipeError(reason)
    value = document["format"]
    try:
        return RecipeFormat(value if isinstance(value, str) else "")
    except ValueError as error:
        reason = "format must be 'html' or 'json'"
        raise InvalidRecipeError(reason) from error


def _read_url(document: dict[str, object]) -> str:
    """Read the `url` key: an absolute http(s) address."""
    if "url" not in document:
        reason = "missing key 'url'"
        raise InvalidRecipeError(reason)
    value = document["url"]
    if not isinstance(value, str):
        reason = "url must be text"
        raise InvalidRecipeError(reason)
    try:
        parts = urlsplit(value)
        usable = parts.scheme in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        usable = False
    if not usable:
        reason = f"url {value!r} is not an absolute http(s) address"
        raise InvalidRecipeError(reason)
    return value


def _read_fields(
    document: dict[str, object], recipe_format: RecipeFormat
) -> RecipeFields:
    """Read the `fields` object."""
    section = _read_object(document, "fields")
    if section is None:
        reason = "missing key 'fields'"
        raise InvalidRecipeError(reason)
    _reject_unknown(section, _FIELD_KEYS, "fields.")

    def optional(name: str) -> str | None:
        return _read_optional_expression(section, name, recipe_format, "fields.")

    return RecipeFields(
        title=_read_expression(section, "title", recipe_format, "fields."),
        url=_read_expression(section, "url", recipe_format, "fields."),
        company=optional("company"),
        location=optional("location"),
        posted_at=optional("posted_at"),
        description=optional("description"),
        salary=optional("salary"),
        external_id=optional("external_id"),
    )


def _read_expression(
    section: dict[str, object], key: str, recipe_format: RecipeFormat, prefix: str
) -> str:
    """Read a required expression key."""
    if key not in section:
        reason = f"missing key '{prefix}{key}'"
        raise InvalidRecipeError(reason)
    return _check_expression(f"{prefix}{key}", section[key], recipe_format)


def _read_optional_expression(
    section: dict[str, object], key: str, recipe_format: RecipeFormat, prefix: str
) -> str | None:
    """Read an expression key that may be absent."""
    if key not in section:
        return None
    return _check_expression(f"{prefix}{key}", section[key], recipe_format)


def _check_expression(name: str, value: object, recipe_format: RecipeFormat) -> str:
    """Check one expression: bounded text that compiles in its engine."""
    if not isinstance(value, str) or not value.strip():
        reason = f"{name} must be a non-empty string"
        raise InvalidRecipeError(reason)
    if len(value) > MAX_EXPRESSION_CHARS:
        reason = f"{name} is longer than {MAX_EXPRESSION_CHARS} characters"
        raise InvalidRecipeError(reason)
    try:
        match recipe_format:
            case RecipeFormat.HTML:
                etree.XPath(value)
            case RecipeFormat.JSON:
                jmespath.compile(value)
    except (etree.XPathError, JMESPathError, ValueError) as error:
        reason = f"{name} does not compile: {error}"
        raise InvalidRecipeError(reason) from error
    return value


def _read_salary_defaults(document: dict[str, object]) -> SalaryDefaults:
    """Read the optional `salary_defaults` object."""
    section = _read_object(document, "salary_defaults")
    if section is None:
        return SalaryDefaults(currency=None, period=None)
    _reject_unknown(section, _SALARY_DEFAULT_KEYS, "salary_defaults.")
    currency = section.get("currency")
    if "currency" in section and (
        not isinstance(currency, str) or currency not in KNOWN_CURRENCIES
    ):
        reason = f"salary_defaults.currency {currency!r} is not a known currency code"
        raise InvalidRecipeError(reason)
    period: Period | None = None
    if "period" in section:
        try:
            period = Period(str(section["period"]))
        except ValueError as error:
            reason = "salary_defaults.period must be 'hour', 'month' or 'year'"
            raise InvalidRecipeError(reason) from error
    return SalaryDefaults(
        currency=currency if isinstance(currency, str) else None, period=period
    )


@dataclass(frozen=True, slots=True)
class PageRead:
    """What one recipe read from one page.

    Attributes:
        items: How many entries the `items` expression selected. Zero means
            the page holds no jobs, which is what ends pagination.
        drafts: The jobs that were read, in page order.
        rejected: How many selected entries were not jobs: they had no title,
            no usable address, or a field the draft refuses.
        next_url: The next page's absolute address, or `None` when the recipe
            has no `next_page` or it gives no usable address.
    """

    items: int
    drafts: list[SightingDraft]
    rejected: int
    next_url: str | None


def read_page(
    recipe: Recipe,
    document: Document,
    *,
    page_url: str,
    ref: SourceRef,
    company: str,
) -> PageRead:
    """Read the jobs on one page with a recipe.

    The `items` expression runs on the whole document and every field
    expression runs with one item as its context. A result of `items` that is
    not a list gives zero items. A field's result is read as text: a string as
    it is, an integer or a whole float as integer text, another float as its
    decimal text, an element as its text content, a list by its first entry,
    and a boolean, a mapping, `None`, or a float that is not finite as absent.
    The text is then stripped and its whitespace collapsed, and text that is
    empty or cannot be stored as UTF-8 is absent.

    `url` and the `next_page` result resolve against `page_url`. A missing
    `external_id` is the job's resolved address, and a missing `company` is
    the `company` argument. `posted_at` is ISO 8601 text, where a naive time
    is UTC and a date alone is midnight UTC; any other text, or a time UTC
    cannot hold, leaves it unset without rejecting the job. `description` is
    read as HTML and turned into Markdown. `raw` is sorted-key JSON of the
    field texts, with the resolved address under `url`, and with the recipe's
    `detail_description` XPath under that key when the recipe has no
    `description` field.

    An item with no title or no http(s) address, or one the draft refuses, is
    counted in `rejected`. When a page repeats an `external_id`, the first job
    is kept and the repeats are dropped without being counted.

    Args:
        recipe: The recipe to evaluate.
        document: The document of the recipe's format to read.
        page_url: The address the document came from, which relative
            addresses resolve against.
        ref: The source the drafts are filed under.
        company: The company named for a job the page does not name one for.

    Returns:
        The drafts, the counts, and the next page's address.

    Raises:
        RecipeEvaluationError: The document is not of the recipe's format, an
            `html` item is not an element, or an expression engine failed
            while evaluating.
    """
    engine = _engine(recipe, document)
    reader = _ItemReader(recipe, engine, page_url=page_url, ref=ref, company=company)
    selected = engine.items(recipe.items)
    drafts: list[SightingDraft] = []
    seen: set[str] = set()
    rejected = 0
    for item in selected:
        draft = reader.read(item)
        if draft is None:
            rejected += 1
        elif draft.external_id not in seen:
            seen.add(draft.external_id)
            drafts.append(draft)
    return PageRead(
        items=len(selected),
        drafts=drafts,
        rejected=rejected,
        next_url=reader.next_url(),
    )


def read_detail_description(document: HtmlDocument, xpath: str) -> str | None:
    """Read a job's description from its own page with a recipe's detail XPath.

    The XPath runs on the cleaned page. Its result is read as HTML, as a
    `description` field is, and turned into Markdown.

    Args:
        document: The job's cleaned page.
        xpath: The recipe's `detail_description` expression.

    Returns:
        The description as Markdown, or `None` when the XPath matches nothing
        or the text is blank.

    Raises:
        RecipeEvaluationError: The XPath does not compile or failed while
            evaluating.
    """
    markup = _markup(_xpath(xpath)(document.tree))
    return None if markup is None else normalize(markup, TextForm.HTML)


type _Evaluate = Callable[[object], object]


@dataclass(frozen=True, slots=True)
class _Engine:
    """The expression engine of one document, bound to that document.

    Attributes:
        root: What an expression on the whole document is evaluated against.
        compile: Compiles an expression into a function of its context.
        is_item: Whether a selected entry can serve as a context.
    """

    root: object
    compile: Callable[[str], _Evaluate]
    is_item: Callable[[object], bool]

    def items(self, expression: str) -> list[object]:
        """Select the entries of the whole document; a non-list gives none."""
        result = self.compile(expression)(self.root)
        if not isinstance(result, list):
            return []
        if not all(self.is_item(entry) for entry in result):
            reason = "items selected something other than elements"
            raise RecipeEvaluationError(reason)
        return list(result)


def _engine(recipe: Recipe, document: Document) -> _Engine:
    """Pair the recipe with the engine of its format and the document's root."""
    match recipe.format, document:
        case RecipeFormat.HTML, HtmlDocument(tree=tree):
            return _Engine(tree, _xpath, etree.iselement)
        case RecipeFormat.JSON, JsonDocument(value=value):
            return _Engine(value, _jmespath, _always)
        case _:
            reason = f"a {recipe.format.value} recipe cannot read this document"
            raise RecipeEvaluationError(reason)


def _always(_: object) -> bool:
    return True


def _xpath(expression: str) -> _Evaluate:
    """Compile an XPath into a function of its context node.

    Raises:
        RecipeEvaluationError: The expression does not compile.
    """
    try:
        compiled = etree.XPath(expression)
    except (etree.LxmlError, ValueError) as error:
        raise RecipeEvaluationError(_failure(error)) from error
    return _isolated(compiled)


def _jmespath(expression: str) -> _Evaluate:
    """Compile a JMESPath into a function of its input.

    Raises:
        RecipeEvaluationError: The expression does not compile.
    """
    try:
        compiled = jmespath.compile(expression)
    except JMESPathError as error:
        raise RecipeEvaluationError(_failure(error)) from error
    return _isolated(compiled.search)


def _isolated(evaluate: _Evaluate) -> _Evaluate:
    """Wrap an engine call so that any error it raises is a `RecipeEvaluationError`.

    This is the one place engine errors are caught. `etree.LxmlError`,
    `JMESPathError`, `TypeError` and `ValueError` are the errors the engines
    raise on purpose, and `Exception` stands for any other, so that a recipe
    that fails to evaluate cannot take a caller's task group down with an
    unrelated error type.
    """

    def run(context: object) -> object:
        try:
            return evaluate(context)
        except Exception as error:  # the isolation point: any engine error is wrapped
            raise RecipeEvaluationError(_failure(error)) from error

    return run


def _failure(error: Exception) -> str:
    """Return the text of an engine error, or its type name when it has none."""
    return str(error) or type(error).__name__


class _ItemReader:
    """Reads one draft at a time out of the items of one page."""

    def __init__(
        self,
        recipe: Recipe,
        engine: _Engine,
        *,
        page_url: str,
        ref: SourceRef,
        company: str,
    ) -> None:
        """Compile the recipe's expressions for the page.

        Raises:
            RecipeEvaluationError: An expression does not compile.
        """
        self._recipe = recipe
        self._engine = engine
        self._page_url = page_url
        self._ref = ref
        self._company = company
        self._fields: dict[str, _Evaluate] = {
            name: engine.compile(expression)
            for name in _FIELD_KEYS
            if (expression := getattr(recipe.fields, name)) is not None
        }
        self._next = (
            engine.compile(recipe.next_page) if recipe.next_page is not None else None
        )

    def next_url(self) -> str | None:
        """Return the next page's absolute address, or `None`."""
        if self._next is None:
            return None
        return self._resolve(_text(self._next(self._engine.root)))

    def read(self, item: object) -> SightingDraft | None:
        """Read one item, or return `None` when it is not a usable job.

        Raises:
            RecipeEvaluationError: An expression engine failed.
        """
        texts: dict[str, str] = {}
        for name, evaluate in self._fields.items():
            result = evaluate(item)
            text = _markup(result) if name == "description" else _text(result)
            if text is not None:
                texts[name] = text
        title = texts.get("title")
        url = self._resolve(texts.get("url"))
        if title is None or url is None:
            return None
        texts["url"] = url
        if self._recipe.detail_description and self._recipe.fields.description is None:
            texts["detail_description"] = self._recipe.detail_description
        description = texts.get("description")
        try:
            return SightingDraft(
                source=self._ref,
                external_id=ExternalId(texts.get("external_id", url)),
                title=title,
                company=texts.get("company", self._company),
                location=texts.get("location"),
                url=url,
                posted_at=_posted_at(texts.get("posted_at")),
                raw=json.dumps(texts, sort_keys=True),
                salaries=self._salaries(texts.get("salary")),
                description=(
                    normalize(description, TextForm.HTML) if description else None
                ),
            )
        except InvalidValueError as error:
            _LOGGER.warning("rejected a job of %s: %s", self._ref, error)
            return None

    def _salaries(self, text: str | None) -> tuple[Money, ...]:
        """Read the salary text into at most one `Money`."""
        if text is None:
            return ()
        money = parse_salary(text, self._recipe.salary_defaults)
        return () if money is None else (money,)

    def _resolve(self, text: str | None) -> str | None:
        """Resolve text against the page; `None` unless the result is http(s)."""
        if text is None:
            return None
        try:
            resolved = urljoin(self._page_url, text)
            parts = urlsplit(resolved)
        except ValueError:
            return None
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        return resolved


def _read_result(value: object, element_text: Callable[[Any], str]) -> str | None:
    """Read an engine result as text by the field-text rules, before cleanup.

    A list gives its first entry, however deeply lists nest, and an empty list
    gives `None`. String results come before elements: an lxml attribute or
    text result is a `str` subclass, and must not be taken for an element.
    """
    while isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return _number_text(value)
    if etree.iselement(value):
        return element_text(value)
    return None


def _number_text(value: float) -> str | None:
    """Read a number as text: a whole float as integer text, else its decimal text.

    A float that is not finite has no decimal text and gives `None`.
    """
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return None
    return str(int(value)) if value.is_integer() else str(value)


def _storable(text: str | None) -> str | None:
    """Return the text, or `None` when it is empty or cannot be stored as UTF-8."""
    if not text:
        return None
    try:
        text.encode()
    except UnicodeEncodeError:
        return None
    return text


def _text(value: object) -> str | None:
    """Read an engine result as the text of a field, whitespace collapsed."""
    text = _read_result(value, lambda element: element.text_content())
    return _storable(None if text is None else " ".join(text.split()))


def _markup(value: object) -> str | None:
    """Read an engine result as HTML: an element's own markup, or the string."""
    text = _read_result(
        value,
        lambda element: lxml.html.tostring(
            element, encoding="unicode", with_tail=False
        ),
    )
    return _storable(None if text is None else text.strip())


def _posted_at(text: str | None) -> datetime | None:
    """Read ISO 8601 text; a naive time is UTC.

    Text that does not parse, or whose time cannot be written in UTC, gives
    `None`.
    """
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            return moment.replace(tzinfo=UTC)
        moment.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
    return moment

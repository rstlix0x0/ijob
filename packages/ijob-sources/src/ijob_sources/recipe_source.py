"""A source that is read with a stored recipe, with no model call.

`RecipeSource` parses the recipe text it is given, fetches the recipe's page
through the `PoliteClient`, reads it with `read_page` and follows the recipe's
`next_page` for up to five pages. Every failure that belongs to the recipe
rather than to the network is a `RecipeFailedError`, whose message names the
command that learns the recipe again.
"""

from dataclasses import dataclass
from typing import Final

from ijob_core.model import SightingDraft, SourceKey, SourceKind, SourceRef

from ijob_sources.collect import SourceBatch
from ijob_sources.errors import (
    InvalidRecipeError,
    RecipeEvaluationError,
    RecipeFailedError,
    SourcesError,
    UnsupportedRecipeVersionError,
)
from ijob_sources.polite_http import PoliteClient
from ijob_sources.recipe import PageRead, Recipe, parse_recipe, read_page
from ijob_sources.recipe_document import (
    Document,
    HtmlDocument,
    RecipeFormat,
    fetch_document,
)

MAX_PAGES: Final = 5


@dataclass(frozen=True, slots=True)
class _Page:
    """One page that was fetched and read.

    Attributes:
        url: The address that was requested.
        final_url: The address of the response, after any redirect.
        read: What the recipe read from it.
    """

    url: str
    final_url: str
    read: PageRead


class RecipeSource:
    """A job source read with a stored recipe.

    It satisfies the `Source` protocol structurally. The recipe is parsed when
    `fetch` runs, not when the source is built, so a recipe this release cannot
    read fails as that source's failure line.
    """

    def __init__(self, key: SourceKey, *, company: str, recipe: str) -> None:
        """Build the source from its key, company and stored recipe text.

        Args:
            key: The source's key within the `recipe` kind.
            company: The company named for a job the page does not name one for.
            recipe: The recipe as JSON text.
        """
        self._ref = SourceRef(SourceKind.RECIPE, key)
        self._company = company
        self._recipe = recipe

    @property
    def ref(self) -> SourceRef:
        """The named source this adapter reads."""
        return self._ref

    async def fetch(self, client: PoliteClient) -> SourceBatch:
        """Read the recipe's pages and return the jobs they hold.

        Pagination starts at the recipe's `url` and follows `next_url`. It
        stops at a page with no items, with no next address, at an address
        already fetched, and after five pages. A repeated `external_id` keeps
        the first job. A page after the first that cannot be fetched, loaded
        or read as the recipe's format ends pagination and keeps the jobs read.

        Args:
            client: The client every request must go through.

        Returns:
            The valid drafts, and the count of items that were not jobs, summed
            over every page read.

        Raises:
            RecipeFailedError: The recipe is invalid or of an unsupported
                version, the first page is not of the recipe's format, an
                expression failed to evaluate, or the first page gave no job.
            SourcesError: The first page could not be fetched or loaded.
        """
        recipe = self._parse()
        drafts: dict[str, SightingDraft] = {}
        rejected = 0
        seen: set[str] = set()
        url = recipe.url
        read_count = 0
        while read_count < MAX_PAGES:
            page = await self._read(client, recipe, url, first=read_count == 0)
            if page is None:
                break
            read_count += 1
            seen.update((page.url, page.final_url))
            rejected += page.read.rejected
            for draft in page.read.drafts:
                drafts.setdefault(draft.external_id, draft)
            if read_count == 1 and not drafts:
                raise RecipeFailedError.no_jobs(self._ref)
            next_url = page.read.next_url
            if page.read.items == 0 or next_url is None or next_url in seen:
                break
            url = next_url
        return SourceBatch(list(drafts.values()), rejected)

    def _parse(self) -> Recipe:
        """Parse the stored recipe text.

        Raises:
            RecipeFailedError: The text is not a valid recipe, or states a
                version this release cannot read.
        """
        try:
            return parse_recipe(self._recipe)
        except UnsupportedRecipeVersionError as error:
            raise RecipeFailedError.unsupported_version(
                self._ref, error.version
            ) from error
        except InvalidRecipeError as error:
            raise RecipeFailedError.failed(self._ref, str(error)) from error

    async def _read(
        self, client: PoliteClient, recipe: Recipe, url: str, *, first: bool
    ) -> _Page | None:
        """Fetch one page and read it.

        Returns:
            The page, or `None` when a page after the first could not be
            fetched, loaded, or read as the recipe's format.

        Raises:
            RecipeFailedError: The first page is not of the recipe's format, or
                an expression failed to evaluate on any page.
            SourcesError: The first page could not be fetched or loaded.
        """
        try:
            document, final_url = await fetch_document(client, url)
        except SourcesError:
            if first:
                raise
            return None
        if not _is_format(document, recipe.format):
            if first:
                raise RecipeFailedError.expected_format(self._ref, recipe.format.value)
            return None
        try:
            read = read_page(
                recipe,
                document,
                page_url=final_url,
                ref=self._ref,
                company=self._company,
            )
        except RecipeEvaluationError as error:
            raise RecipeFailedError.failed(self._ref, str(error)) from error
        return _Page(url, final_url, read)


def _is_format(document: Document, recipe_format: RecipeFormat) -> bool:
    """Say whether a loaded document is of the format a recipe reads."""
    return isinstance(document, HtmlDocument) == (recipe_format is RecipeFormat.HTML)

"""The tools of the source agent and of the explore agent.

`SourceTools` holds the state of one source-agent run and exposes three async
tools for the agent loop. `try_board` checks a hiring-system board by its
kind and key. `open_page` opens one page of the company's site and either
verifies it as a source or lists its links. `learn_page` learns a recipe for a
page `open_page` found no source on. A tool returns text for a failed lookup
and raises only an error that must end the run (a reached cap, a provider or
cost failure), which the loop's tool wrapper stores. It never returns a page's
own text: what a page or a link says is third-party text, so it is cleaned to
one line and quoted before the model sees it, and every result is cut to 4,000
characters. A reason that comes from a response, such as a `Content-Type` or a
redirect target, is quoted and cut to 100 characters in the same way.

A source counts as verified only when a tool has read it, and `verified` keeps
those in order, the most recent last. A refused call is a rule violation, noted
on the run beside the refusal.

`ExploreTools` is the same for the explore agent. `find_source` runs the source
agent through a callable the caller supplies, and `propose` accepts only what
such a run verified. It keeps the proposals, the sources the user already has
and the companies with no source, whatever way the run ends. The module
imports `ijob_ai`, so it is imported only when an agent is about to run.
"""

import json
import string
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import quote, urlsplit

from ijob_ai.errors import MissingModelError, TaskOutputError
from ijob_ai.llm.settings import Task
from ijob_core.errors import InvalidValueError
from ijob_core.model import (
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.store import SourceOrigin, SourceRow, Store
from ijob_sources.collect import SourceBatch
from ijob_sources.detect import (
    BOARD_HOSTS,
    Resolution,
    resolve_url,
    verify_new_source,
)
from ijob_sources.errors import (
    FetchedPage,
    FetchError,
    HttpStatusError,
    IdentityMismatchError,
    NotPublicAddressError,
    OffSiteError,
    ReservedHostError,
    RobotsDisallowedError,
    SourceNotFoundError,
    SourcesError,
    UnrecognizedPageError,
    UnsupportedUrlError,
)
from ijob_sources.links import extract_links
from ijob_sources.manual import manual_key
from ijob_sources.polite_http import PoliteClient
from ijob_sources.recipe_document import load_document
from ijob_sources.registry import build_source

from ijob_app.agent_site import SiteRule, is_address, request_host
from ijob_app.llm_ops import OperationReport
from ijob_app.recipe_errors import NoTrialJobsError
from ijob_app.recipe_ops import LoadedPage, RecipeProposal, learn_recipe

if TYPE_CHECKING:
    from ijob_ai.agents.react import AgentRun
    from ijob_ai.llm.budget import Budget
    from ijob_ai.llm.reference import Provider

    from ijob_app.llm_ops import LlmRuntime

type BudgetFor = Callable[[Provider], Budget]
"""Picks the budget a model call is charged to, given the provider serving it."""

_MAX_RESULT_CHARS: Final = 4000
_CUT: Final = "[truncated]"
_MAX_LINKS: Final = 50
_REASON_CHARS: Final = 300
_TOLD_CHARS: Final = 100
_MAX_LINK_URL_CHARS: Final = 300
_URL_SAFE: Final = "/:?#[]@!$&'()*+,;=%~-._"
_BOARD_KINDS: Final = frozenset(BOARD_HOSTS.values())
_NOT_A_BOARD: Final = "not a valid board"
_NO_BOARD: Final = "no board"
_WRONG_COMPANY: Final = "company not on this board"
_EMPTY_BOARD: Final = "empty board"
_REDIRECTED_OFF: Final = "could not open: redirected off the site"
_REDIRECTED_BLOCKED: Final = "could not open: redirected to an address not allowed"
_NOT_READABLE: Final = "could not open: the page is not HTML or JSON"
_NOT_AN_ADDRESS: Final = "could not open: not a valid address"
_NO_JOBS: Final = "could not open: the page lists no jobs"
_NOT_OPENED: Final = (
    "learn_page works only on a page that open_page opened and found no "
    "source on; open it first"
)
_ALREADY_A_SOURCE: Final = "that page is already a verified source"
_NO_MODEL: Final = "no model is set to learn recipes; set [models].source_recipe"
_WEB_SCHEMES: Final = frozenset({"http", "https"})
_READABLE_HTML: Final = frozenset({"text/html", "application/xhtml+xml"})
_FIXED_REASONS: Final = (RobotsDisallowedError, HttpStatusError, IdentityMismatchError)
_QUOTE_MARKS: Final = string.whitespace + "`'\""
_MAX_COMPANIES: Final = 50
_WHY_CHARS: Final = 200
_SAMPLE_TITLES: Final = 3
_TITLE_CHARS: Final = 100
_REF_CHARS: Final = 100
_NO_SOURCES: Final = "you have no sources"
_NO_COMPANIES: Final = "no companies match"
_GIVE_COMPANY: Final = "give a company name"
_NO_SOURCE_FOUND: Final = "no source found"
_PROPOSED: Final = "proposed"
_ALREADY_PROPOSED: Final = "already proposed"
_ALREADY_KNOWN: Final = "already one of your sources"
_NOT_VERIFIED: Final = "not verified in this run"


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedSource:
    """A source a tool of this run read and found usable.

    Attributes:
        ref: The source.
        target_url: The address a feed, job page or recipe source is read
            from, or `None` for a hiring-system board, which is found by its
            key alone.
        jobs: How many jobs the check read.
        proposal: The recipe and its trial, for a source `learn_page`
            learned, otherwise `None`.
        sample: Up to three titles of the jobs the check read, each cleaned to
            one line. They are third-party text for the user's screen and are
            never put in a tool result.
    """

    ref: SourceRef
    target_url: str | None
    jobs: int
    proposal: RecipeProposal | None = None
    sample: tuple[str, ...] = ()


class SourceTools:
    """The tools and the record of one source-agent run.

    Attributes:
        verified: The sources a tool verified, in order; the most recent is
            last and a source verified again moves to the end.
        could_not_check: One line per board a tool could not check, such as
            `greenhouse:acme: robots.txt could not be read: ConnectTimeout`.
        opened: Every page this run read, by the address it was asked for and
            by the address it ended on. The value is the page when no source
            was found on it, the only pages `learn_page` accepts, and `None`
            for a page that was itself a verified source.
    """

    def __init__(  # noqa: PLR0913  # the run's whole context, one argument each
        self,
        *,
        company: str,
        rule: SiteRule,
        run: "AgentRun",
        public_client: PoliteClient,
        page_client: PoliteClient,
        runtime: "LlmRuntime | None",
        budget_for: BudgetFor,
    ) -> None:
        """Build the tools of one run.

        Args:
            company: The company being looked up, which a board or page must
                name.
            rule: The site rule of this run. `page_client` carries its guard.
            run: The run's state; violations are noted on it.
            public_client: The client `try_board` and a board check use. It
                refuses non-public addresses but is not held to the site,
                since boards are read on their own API hosts.
            page_client: The client that reads pages, guarded by the public
                host rule and by `rule`. Only `open_page` and `learn_page`
                use it.
            runtime: The runtime holding the models, or `None` when no model
                can be called, which `learn_page` reports.
            budget_for: Picks the budget `learn_page` charges, given the
                provider of the `source_recipe` model.
        """
        self.verified: list[VerifiedSource] = []
        self.could_not_check: list[str] = []
        self.opened: dict[str, FetchedPage | None] = {}
        self._company = company
        self._rule = rule
        self._run = run
        self._public_client = public_client
        self._page_client = page_client
        self._runtime = runtime
        self._budget_for = budget_for

    async def try_board(self, kind: str, key: str) -> str:
        """Check a company's job board on a hiring system.

        Args:
            kind: The hiring system: greenhouse, lever, ashby, workable or gem.
            key: The company's account name on that system.

        Returns:
            `found N jobs, company matches`, `no board`, `company not on this
            board`, `empty board`, `could not check: <reason>` or `not a valid
            board`.
        """
        ref = _board_ref(kind, key)
        if ref is None:
            return _NOT_A_BOARD
        return _cut((await self._check_board(ref)).text)

    async def open_page(self, url: str) -> str:
        """Open one page of the company's site.

        Args:
            url: The absolute address of the page.

        Returns:
            A verified source when the page is a board, feed or job page;
            otherwise the page's links, never its text; or `could not open`
            with the reason.
        """
        return _cut(await self._open_page(url))

    async def learn_page(self, url: str) -> str:
        """Learn how to read the jobs of a page that open_page opened.

        Args:
            url: The address of a page open_page opened and found no source on.

        Returns:
            `read N jobs`, or the reason no recipe was learned.
        """
        return _cut(await self._learn_page(url))

    async def _open_page(self, url: str) -> str:
        """Open a page under the site rule and say what it was.

        An argument that is no URL is no request and no violation.
        """
        if not is_address(url):
            return _NOT_AN_ADDRESS
        reason = self._rule.admit(url)
        if reason is not None:
            return self._refuse("open_page", reason)
        try:
            resolution = await resolve_url(
                url, company=self._company, client=self._page_client
            )
        except UnrecognizedPageError as error:
            return self._links_of(url, error.page)
        except SourcesError as error:
            return self._open_failed(url, error)
        if resolution.batch is None:
            return await self._open_board(url, resolution.ref)
        return self._read_source(url, resolution, resolution.batch)

    async def _open_board(self, url: str, ref: SourceRef) -> str:
        """Check the board a URL names and say whether it is the company's."""
        checked = await self._check_board(ref)
        if not checked.verified:
            return f"{ref}: {checked.text}"
        self.opened[url] = None
        return f"verified source {ref}: {checked.text}"

    def _read_source(self, url: str, resolution: Resolution, batch: SourceBatch) -> str:
        """Record a feed or job page that was read, unless it lists no jobs."""
        self._rule.mark_read()
        jobs = len(batch.drafts)
        if jobs == 0:
            return _NO_JOBS
        self.opened[url] = None
        if resolution.target_url is not None:
            self.opened[resolution.target_url] = None
        self._record(
            VerifiedSource(
                ref=resolution.ref,
                target_url=resolution.target_url,
                jobs=jobs,
                sample=_sample(batch.drafts),
            )
        )
        return f"verified source {resolution.ref}: read {jobs} jobs"

    def _open_failed(self, url: str, error: SourcesError) -> str:
        """Turn a failed request for `url` into a result, counting a refused one.

        A request the guards refused for `url` itself is the agent's violation.
        One refused for a redirect hop is the server's doing and is none.
        """
        if isinstance(error, OffSiteError):
            return _REDIRECTED_OFF
        if isinstance(error, NotPublicAddressError | ReservedHostError):
            if _is_request_for(error, url):
                return self._refuse("open_page", _line(str(error)))
            return _REDIRECTED_BLOCKED
        if isinstance(error, IdentityMismatchError):
            self._rule.mark_read()
        return f"could not open: {_told(error)}"

    def _links_of(self, url: str, page: FetchedPage | None) -> str:
        """List the links of a page that is no source, and remember the page."""
        if page is None or not _is_readable(page):
            return _NOT_READABLE
        self._rule.mark_read()
        self.opened[url] = page
        self.opened[page.final_url] = page
        addresses = (
            (quote(link.url, safe=_URL_SAFE), link.text)
            for link in extract_links(page.body, page.final_url)
            if _on_site(self._rule, link.url)
        )
        links = [
            (address, text)
            for address, text in addresses
            if len(address) <= _MAX_LINK_URL_CHARS
        ][:_MAX_LINKS]
        if not links:
            return "no source found on this page; it has no links on the site"
        lines = [
            f"- {_quote(address, _MAX_LINK_URL_CHARS)} {_quote(text)}"
            for address, text in links
        ]
        header = f"no source found on this page; {len(links)} links on the site:"
        return "\n".join([header, *lines])

    async def _learn_page(self, url: str) -> str:
        """Learn a recipe for an opened page, charged to this run's budget."""
        if url not in self.opened:
            return _NOT_OPENED
        page = self.opened[url]
        if page is None:
            return _ALREADY_A_SOURCE
        runtime = self._runtime
        if runtime is None:
            return _NO_MODEL
        try:
            model = runtime.settings.model_for(Task.SOURCE_RECIPE)
        except MissingModelError:
            return _NO_MODEL
        return await self._learn(runtime, self._budget_for(model.provider), url, page)

    async def _learn(
        self, runtime: "LlmRuntime", budget: "Budget", url: str, page: FetchedPage
    ) -> str:
        """Run `learn_recipe` on a page and record the recipe it learned."""
        try:
            loaded = LoadedPage(
                load_document(page.body, page.content_type, page.final_url),
                page.final_url,
            )
        except SourcesError as error:
            return f"could not learn: {_told(error)}"
        ref = SourceRef(SourceKind.RECIPE, manual_key(page.final_url))
        try:
            learned = await learn_recipe(
                runtime,
                self._page_client,
                loaded,
                budget,
                ref=ref,
                target_url=url,
                company=self._company,
            )
        except (TaskOutputError, NoTrialJobsError) as error:
            return f"could not learn: {_line(str(error), _REASON_CHARS)}"
        if isinstance(learned, SourcesError):
            # A redirect hop is counted like a request for the recipe's own
            # address. The model cannot tell them apart, and the over-count is
            # accepted, as it is for `OffSiteError`.
            if isinstance(
                learned, OffSiteError | NotPublicAddressError | ReservedHostError
            ):
                return self._refuse("learn_page", _quote(str(learned), _TOLD_CHARS))
            return f"could not learn: {_told(learned)}"
        proposal = RecipeProposal(
            ref=ref,
            target_url=url,
            company=self._company,
            recipe=learned.recipe,
            recipe_format=learned.recipe_format,
            recipe_url=learned.recipe_url,
            jobs=learned.jobs,
            invalid=learned.invalid,
            model=learned.model,
            report=OperationReport(
                done=1,
                not_done=0,
                spent=budget.spent,
                cap=budget.cap,
                stopped_by=None,
            ),
        )
        self._record(
            VerifiedSource(
                ref=ref,
                target_url=url,
                jobs=len(learned.jobs),
                proposal=proposal,
                sample=_sample(learned.jobs),
            )
        )
        return f"read {len(learned.jobs)} jobs"

    async def _check_board(self, ref: SourceRef) -> "_BoardCheck":
        """Fetch a board once and say whether it is the company's."""
        row = SourceRow(
            ref=ref,
            company=self._company,
            target_url=None,
            origin=SourceOrigin.USER,
            removed=False,
            last_fetched_at=None,
        )
        try:
            source = build_source(row)
            batch = await verify_new_source(source, self._public_client)
        except InvalidValueError:
            return _BoardCheck(_NOT_A_BOARD, verified=False)
        except SourceNotFoundError:
            return _BoardCheck(_NO_BOARD, verified=False)
        except IdentityMismatchError:
            return _BoardCheck(_WRONG_COMPANY, verified=False)
        except UnsupportedUrlError:
            return _BoardCheck(_EMPTY_BOARD, verified=False)
        except SourcesError as error:
            self.could_not_check.append(f"{ref}: {_why(error)}")
            return _BoardCheck(f"could not check: {_told(error)}", verified=False)
        jobs = len(batch.drafts)
        self._record(
            VerifiedSource(
                ref=ref, target_url=None, jobs=jobs, sample=_sample(batch.drafts)
            )
        )
        return _BoardCheck(f"found {jobs} jobs, company matches", verified=True)

    def _record(self, source: VerifiedSource) -> None:
        """Keep a verified source as the most recent one."""
        self.verified = [v for v in self.verified if v.ref != source.ref]
        self.verified.append(source)

    def _refuse(self, tool: str, reason: str) -> str:
        """Count a rule violation on the run and return the refusal."""
        self._run.violations.append(f"{tool}: {reason}")
        return f"refused: {reason}"


@dataclass(frozen=True, slots=True)
class _BoardCheck:
    """What checking a board found."""

    text: str
    verified: bool


class SourceAttempt(Protocol):
    """How one source-agent run ended, as `find_source` reads it.

    The caller's runner returns the end of the run in `stop` instead of
    raising it.
    """

    @property
    def verified(self) -> Sequence[VerifiedSource]:
        """Every source a tool verified in the run, the most recent last."""
        ...

    @property
    def violations(self) -> Sequence[str]:
        """The rule violations of the run."""
        ...

    @property
    def stop(self) -> Exception | None:
        """What ended the run early, or `None`.

        A reached cap, a provider or cost failure or an unusable answer of the
        run's own task.
        """
        ...

    @property
    def found(self) -> object | None:
        """The source the run offers, or `None` when it found none."""
        ...


@dataclass(frozen=True, slots=True)
class Proposal:
    """A verified source the explore agent put forward.

    Attributes:
        source: The source as the run verified it.
        company: The company `find_source` was asked about when it verified it.
        why: The agent's reason, cut to 200 characters.
    """

    source: VerifiedSource
    company: str
    why: str


class ExploreTools:
    """The tools and the record of one explore-agent run.

    The record outlives any early end of the run: whatever was proposed stays.

    Attributes:
        proposals: The sources proposed, in order.
        known: The sources the user already has that the agent tried to
            propose, once each, in order. They are not offered.
        no_source: The companies a `find_source` call ended without a source
            for, once each, in order.
        refused: How many `propose` calls named something no tool verified in
            this run. A blank `ref` is not counted.
    """

    def __init__(
        self,
        *,
        store: Store,
        run: "AgentRun",
        find_source: Callable[[str], Awaitable[SourceAttempt]],
    ) -> None:
        """Build the tools of one run.

        Args:
            store: The user's store, read for their sources and the companies
                of stored postings.
            run: The run's state; violations are noted on it.
            find_source: Runs the source agent for a company on the budget of
                this run and returns how it ended.
        """
        self.proposals: list[Proposal] = []
        self.known: list[SourceRef] = []
        self.no_source: list[str] = []
        self.refused = 0
        self._store = store
        self._run = run
        self._find = find_source
        self._verified: dict[SourceRef, tuple[VerifiedSource, str]] = {}

    async def list_sources(self) -> str:
        """List the sources the user already has.

        Returns:
            How many there are, then one line per source, with its reference
            and company.
        """
        rows = self._store.sources()
        if not rows:
            return _NO_SOURCES
        count = f"{len(rows)} source{'' if len(rows) == 1 else 's'}:"
        lines = (f"- {row.ref} {_quote(row.company)}" for row in rows)
        return _cut("\n".join((count, *lines)))

    async def companies_in_store(self, query: str) -> str:
        """List companies of stored postings whose name contains some text.

        Args:
            query: The text a company name must contain; empty matches all.

        Returns:
            At most 50 distinct company names, one per line.
        """
        names = self._store.companies_matching(_line(query), _MAX_COMPANIES)
        if not names:
            return _NO_COMPANIES
        return _cut("\n".join(f"- {_quote(name)}" for name in names))

    async def find_source(self, company: str) -> str:
        """Look for one company's source with the source agent.

        Args:
            company: The company's name.

        Returns:
            The sources the run verified, which `propose` accepts, followed by
            the reason when the run's own answer was unusable; or the reason
            the run found none.

        Raises:
            Exception: What ended the run early, other than an unusable
                answer, so that the loop stores it.
        """
        name = _line(company)
        if not name:
            return _GIVE_COMPANY
        attempt = await self._find(name)
        self._run.violations.extend(attempt.violations)
        for source in attempt.verified:
            self._verified[source.ref] = (source, name)
        if attempt.found is None:
            if name not in self.no_source:
                self.no_source.append(name)
        elif name in self.no_source:
            self.no_source.remove(name)
        stop = attempt.stop
        failure = None
        if isinstance(stop, TaskOutputError):
            failure = f"could not find a source: {_line(str(stop), _REASON_CHARS)}"
        elif stop is not None:
            raise stop
        if not attempt.verified:
            return _NO_SOURCE_FOUND if failure is None else _cut(failure)
        lines = [f"- {source.ref} ({source.jobs} jobs)" for source in attempt.verified]
        reason = [] if failure is None else [failure]
        return _cut("\n".join(["verified sources:", *lines, *reason]))

    async def propose(self, ref: str, why: str) -> str:
        """Put forward a source that `find_source` verified.

        Args:
            ref: The source as `<kind>:<key>`.
            why: Why the user should add it; cut to 200 characters.

        Returns:
            `proposed`, `already proposed`, `already one of your sources` or
            `not verified in this run`. The last is a rule violation, and is
            counted in `refused`, for any `ref` no tool verified: a source, a
            web address or a name. A blank `ref` gets the same text as a
            malformed argument, with no violation and no count.
        """
        if not ref.strip(_QUOTE_MARKS):
            return _NOT_VERIFIED
        source_ref = read_source_ref(ref)
        entry = None if source_ref is None else self._verified.get(source_ref)
        if source_ref is None or entry is None:
            self.refused += 1
            self._run.violations.append(
                f"propose: {_NOT_VERIFIED}: {_quote(ref, _REF_CHARS)}"
            )
            return _NOT_VERIFIED
        if any(row.ref == source_ref for row in self._store.sources()):
            if source_ref not in self.known:
                self.known.append(source_ref)
            return _ALREADY_KNOWN
        if any(p.source.ref == source_ref for p in self.proposals):
            return _ALREADY_PROPOSED
        source, company = entry
        self.proposals.append(Proposal(source, company, _line(why, _WHY_CHARS)))
        if company in self.no_source:
            self.no_source.remove(company)
        return _PROPOSED


def read_source_ref(text: str) -> SourceRef | None:
    """Read a model's `<kind>:<key>` as `try_board` reads a kind and a key.

    Whitespace around the text or around either part is ignored, as are
    backticks and quotes around the text, and the kind may be in any case.

    Args:
        text: What the model wrote.

    Returns:
        The source, or `None` when the text does not name one.
    """
    kind, _, key = text.strip(_QUOTE_MARKS).partition(":")
    try:
        return SourceRef(
            SourceKind(kind.strip().lower()), parse_source_key(key.strip())
        )
    except (ValueError, InvalidValueError):
        return None


def names_a_source(text: str) -> bool:
    """Tell whether text names a source: a `<kind>:<key>` or a web address.

    Args:
        text: What the model wrote, such as its final answer.

    Returns:
        `False` for `none`, a sentence or anything else that names no source.
    """
    if read_source_ref(text) is not None:
        return True
    try:
        parts = urlsplit(text.strip(_QUOTE_MARKS))
    except ValueError:
        return False
    return parts.scheme in _WEB_SCHEMES and bool(parts.netloc)


def _board_ref(kind: str, key: str) -> SourceRef | None:
    """Name the board a model asked for, or `None` when it is no board."""
    try:
        board_kind = SourceKind(kind.strip().lower())
        board_key = parse_source_key(key)
    except (ValueError, InvalidValueError):
        return None
    if board_kind not in _BOARD_KINDS:
        return None
    return SourceRef(board_kind, board_key)


def _why(error: SourcesError) -> str:
    """Say in one short line why a request failed, for the user's screen.

    A failure with a known cause gets fixed wording; any other gets the error's
    own text, which can carry an address or words from a response.
    """
    match error:
        case RobotsDisallowedError():
            reason = "robots.txt disallows it"
        case SourceNotFoundError():
            reason = "page not found (HTTP 404)"
        case HttpStatusError():
            reason = f"HTTP {error.status}"
        case FetchError() | UnsupportedUrlError():
            reason = error.reason
        case IdentityMismatchError():
            reason = "the page names another company"
        case _:
            reason = str(error)
    return _line(reason, _REASON_CHARS)


def _told(error: SourcesError) -> str:
    """Say why a request failed in words fit for the model.

    Fixed wording stays as it is; any other reason is third-party text, so it
    is quoted and cut to 100 characters.
    """
    reason = _why(error)
    if isinstance(error, _FIXED_REASONS):
        return reason
    return _quote(reason, _TOLD_CHARS)


def _sample(drafts: Sequence[SightingDraft]) -> tuple[str, ...]:
    """Take up to three non-empty titles of the jobs a check read, as one line each."""
    titles = (_line(draft.title, _TITLE_CHARS) for draft in drafts)
    return tuple(title for title in titles if title)[:_SAMPLE_TITLES]


def _is_request_for(error: NotPublicAddressError | ReservedHostError, url: str) -> bool:
    """Tell whether a refused request was for `url` itself, not a redirect hop.

    Both hosts are read as a request reaches them, so a spelling `httpx`
    normalizes, such as ideographic dots, still matches.
    """
    asked = request_host(url)
    if isinstance(error, ReservedHostError):
        refused: str | None = error.host
    else:
        refused = request_host(error.url)
    return asked is not None and asked == refused


def _is_readable(page: FetchedPage) -> bool:
    """Tell whether a fetched body is HTML or JSON, by `load_document`'s rule.

    The media type is the header's text before any `;`, lowercased. A missing
    header is no read.
    """
    media = (page.content_type or "").split(";")[0].strip().lower()
    return (
        media == "application/json"
        or media.endswith("+json")
        or media in _READABLE_HTML
    )


def _on_site(rule: SiteRule, url: str) -> bool:
    """Tell whether the site rule allows the host of a link."""
    host = urlsplit(url).hostname
    return host is not None and rule.allows(host)


def _line(text: str, limit: int = 200) -> str:
    """Make third-party text one printable line of at most `limit` characters."""
    kept = "".join(
        char
        for char in text
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    return " ".join(kept.split())[:limit].rstrip()


def _quote(text: str, limit: int = _REASON_CHARS) -> str:
    """Quote third-party text as one JSON string, so it reads as data."""
    return json.dumps(_line(text, limit), ensure_ascii=False)


def _cut(text: str) -> str:
    """Cut `text` to 4,000 characters, ending in `[truncated]` when it was cut."""
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    return text[: _MAX_RESULT_CHARS - len(_CUT)] + _CUT

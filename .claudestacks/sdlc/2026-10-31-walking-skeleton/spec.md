---
status: approved
created: 2026-10-31
built-on: a4df7dd241ff
---

# Spec: Walking skeleton

This spec delivers one thin slice through every layer of ijob: `ijob search --greenhouse <tokens>`
fetches Greenhouse boards through a polite HTTP client that obeys robots.txt, records the postings in
a local SQLite store without duplicating them, and prints them. Its lasting product is the
source-adapter protocol — the `Source` interface and the `SightingDraft` it returns — that every later
adapter in milestone ijob-v1/02 implements without changing it.

Evidence below was gathered on 2026-10-31. "Probe" means a throwaway script run with
`uv run --no-project --python 3.14 --with httpx==0.28.1 --with typer==0.27.3 python -I <probe>.py`
on CPython 3.14.10 (sqlite 3.53.1), unless another interpreter is named; the output is quoted
verbatim. "Fetched" means `curl -sS -A 'ijob-research/0.1'` of the URL shown on that date.

## 1. Design premises

1. **Adapter shape: fetch plus pure parse.** Each adapter has an async `fetch` that issues its own
   requests through one shared `PoliteClient`, and pure parse functions that turn response bodies
   into drafts. Paging loops stay inside each adapter, while robots.txt, pacing and retries are
   enforced once, in the client. A declarative request engine and adapter-owned clients were
   considered and rejected (the first turns per-source paging into an engine state machine; the
   second duplicates politeness in every adapter).
2. **One Posting, many Sightings.** A Sighting is keyed by `(source, external_id)` and holds
   `first_seen`, `last_seen` and the latest raw payload. Seeing a posting again updates its Sighting;
   it never adds a row. Size rationale: the Greenhouse `tailscale` board with `content=true` was
   694,940 bytes for 52 jobs (fetched: `-w '%{http_code} %{size_download}'` printed `200 694940`;
   `meta` = `{'total': 52}`), about 13 KB of raw payload per job.
3. **Within-source dedupe only.** C1 creates one Posting per new `(source, external_id)`.
   `SightingDraft` already carries the fields cross-source keys derive from (`url`, `company`,
   `title`, `location`); the key columns and matching rules arrive in the follow-on chain through
   store migration 2, which changes no adapter.
4. **No ETag cache in C1.** Greenhouse does answer conditional requests:
   `https://boards-api.greenhouse.io/v1/boards/tailscale/jobs?content=true` returned
   `etag: W/"79fa1509df1dd3bc19284c0d09b64da6"`, and the same URL with
   `If-None-Match: W/"79fa1509df1dd3bc19284c0d09b64da6"` printed `304` (fetched). The cache is
   deferred to the follow-on chain because it belongs inside `PoliteClient`, which hands the cached
   body back on `304`; adapters never see the difference, so deferring it changes no adapter.
5. **robots.txt through the stdlib parser, on CPython ≥ 3.14.10.** The CPython 3.14 changelog
   (fetched `https://docs.python.org/3.14/whatsnew/changelog.html`) lists under "Python 3.14.5 final":
   "gh-138907: Support RFC 9309 in urllib.robotparser.", and under "Python 3.14.7 final":
   "gh-153404: urllib.robotparser.RobotFileParser now silently ignores a Crawl-delay or Request-rate
   value written with non-decimal digits … instead of raising ValueError and aborting the parse of
   the whole robots.txt file." The floor is the interpreter every probe in this spec ran on,
   3.14.10, which carries both. A probe of four rule sets against the RFC 9309 answer printed, per
   interpreter:

   ```text
   case                                            path          3.12.16  3.13.18  3.14.10  RFC 9309
   Disallow: /jobs + Allow: /jobs/public           /jobs/public  False    True     True     True
   Allow: / + Disallow: /jobs                      /jobs/1       True     False    False    False
   Allow: /jobs$ + Disallow: /jobs                 /jobs/1       False    False    False    False
   Disallow: /*?                                   /search?q=x   True     False    False    False
   ```

   (3.12.16 printed `WRONG` on three of the four rows; 3.13.18 and 3.14.10 printed `OK` on all four.
   3.13 is not a supported interpreter, so its result does not change the design.)

## 2. Workspace and dependencies

- `requires-python` becomes `">=3.14.10"` in all four members (today `">=3.14"`:
  `packages/ijob-core/pyproject.toml:9`, `packages/ijob-sources/pyproject.toml:9`,
  `packages/ijob-ai/pyproject.toml:9`, `packages/ijob-app/pyproject.toml:9`), and `.python-version`
  becomes `3.14.10` (today `3.14`: `.python-version:1`). The python guideline: "Keep
  `.python-version` equal to the floor" (`SKILL.md:50`; `references/project-layout.md:147`).
- `ijob-core` keeps `dependencies = []` and imports only the standard library.
- `ijob-sources` depends on `httpx>=0.28.1,<0.29` and `ijob-core`. httpx's current release is 0.28.1
  and requires Python ≥ 3.8 (fetched `https://pypi.org/pypi/httpx/json`: `info.version` = `0.28.1`,
  `info.requires_python` = `>=3.8`).
- `ijob-app` depends on `typer>=0.27.3,<0.28`, `ijob-sources` and `ijob-core`, and declares the
  console script `ijob = "ijob_app.cli:main"`. typer's current release is 0.27.3 and requires
  Python ≥ 3.10 (fetched `https://pypi.org/pypi/typer/json`: `info.version` = `0.27.3`,
  `info.requires_python` = `>=3.10`).
- Runtime dependencies take a compatible range; `uv.lock` pins the exact versions and is
  regenerated with `uv lock`. The dev tools stay pinned with `==` as today.
- Workspace siblings are declared `{ workspace = true }` in a `[tool.uv.sources]` table in the
  **root** `pyproject.toml`. This departs deliberately from the guideline's per-member placement
  (`workspace.md:207`) to keep the CLAUDE.md rule that members carry no `[tool.*]` tables; uv honours
  it, since a root entry applies to all members (`workspace.md:212-213`), and uv requires the entry
  ("`core` is included as a workspace member, but is missing an entry in `tool.uv.sources`",
  `workspace.md:220`).

## 3. Domain model (`ijob_core.model`)

| Type | Shape | Validation |
|---|---|---|
| `SourceKind` | `StrEnum`; C1 holds only `GREENHOUSE = "greenhouse"`. A later adapter adds a member: an extension, not a change | closed set |
| `SourceKey` | `NewType("SourceKey", str)`, built only by `parse_source_key(text)` | matches `[A-Za-z0-9._-]+` and is not `.` or `..`, so it cannot change a URL path, query or fragment it is placed in |
| `SourceRef` | frozen slotted dataclass `(kind: SourceKind, key: SourceKey)`; `str(ref)` is `"greenhouse:stripe"`, the form stored in `sightings.source` | — |
| `ExternalId` | `NewType("ExternalId", str)` | — |
| `PostingId` | `NewType("PostingId", int)` | — |
| `SightingDraft` | frozen slotted dataclass: `source: SourceRef`, `external_id: ExternalId`, `title: str`, `company: str`, `location: str \| None`, `url: str`, `posted_at: datetime \| None`, `raw: str` | `__post_init__`: `external_id`, `title`, `company` and `url` non-empty; `url` starts with `http://` or `https://`; `posted_at`, when set, is timezone-aware |
| `Posting` | frozen slotted dataclass: `id: PostingId`, `title`, `company`, `location`, `url`, `posted_at`, `first_seen: datetime`, `last_seen: datetime` | built only by the store |
| `Sighting` | frozen slotted dataclass: `posting_id: PostingId`, `source: SourceRef`, `external_id`, `first_seen`, `last_seen`, `raw` | built only by the store |

The "display fields" of a Posting are `title`, `company`, `location`, `url` and `posted_at`. A failed
validation raises `InvalidValueError` (section 7).

## 4. Store (`ijob_core.store`)

- Built on stdlib `sqlite3` with `STRICT` tables. Probe: `create table t(id integer primary key, raw
  text not null) strict`, an insert of `'{"a":1}'` and `select json_extract(raw,'$.a')` printed
  `strict+json_extract (1,) user_version (0,)` on sqlite 3.53.1.
- Schema version 1, applied when `PRAGMA user_version` is 0 (a fresh database reads 0, per the probe
  above), after which `user_version` is set to 1:

  ```sql
  CREATE TABLE postings (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL, location TEXT,
    url TEXT NOT NULL, posted_at TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
  ) STRICT;
  CREATE TABLE sightings (
    id INTEGER PRIMARY KEY, posting_id INTEGER NOT NULL REFERENCES postings(id),
    source TEXT NOT NULL, external_id TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, raw TEXT NOT NULL,
    UNIQUE (source, external_id)
  ) STRICT;
  ```

  Timestamps are ISO-8601 text in UTC.
- `Store.open(path) -> Store` opens the database and applies pending migrations. `Store` is a
  context manager that closes the connection.
- `Store.record(drafts, *, seen_at: datetime) -> RecordResult` runs in one transaction. For a new
  `(source, external_id)` it inserts a Posting and a Sighting, both with `first_seen = last_seen =
  seen_at`. For a known one it sets the Sighting's `last_seen` and `raw`, and the Posting's
  `last_seen` and display fields, from the draft. `RecordResult` carries `new: int` and
  `updated: int`.
- `Store.postings(*, sources: Collection[SourceRef]) -> list[Posting]` returns the postings with a
  Sighting from those sources, newest `posted_at` first, postings without `posted_at` last.
- `Store.sighting(source: SourceRef, external_id: ExternalId) -> Sighting | None` reads one Sighting
  back, raw payload included.
- The default database path is `$XDG_DATA_HOME/ijob/ijob.db`, or `~/.local/share/ijob/ijob.db` when
  `XDG_DATA_HOME` is unset; `ijob search --db PATH` overrides it. The parent directory is created on
  first use.

## 5. Polite HTTP (`ijob_sources.http`, `ijob_sources.robots`)

### 5.1 PoliteClient

- Wraps one `httpx.AsyncClient` built with `follow_redirects=False`. The transport is injectable, so
  tests pass an `httpx.MockTransport`. Probe: an `AsyncClient(transport=httpx.MockTransport(handler),
  headers={"User-Agent": "ijob/0.1"})` GET printed `mock 200 {'ua': 'ijob/0.1', 'url':
  'https://boards-api.greenhouse.io/v1/boards/x/jobs'}`.
- Sends `User-Agent: ijob/<version> (+https://github.com/rstlix0x0/ijob)`, where `<version>` is
  `importlib.metadata.version("ijob-sources")` (the repository's `origin` remote is
  `git@github.com:rstlix0x0/ijob.git`). Its robots.txt product token is `ijob`, a substring of that
  header, as RFC 9309 §2.2.1 asks: "The product token SHOULD be a substring of the identification
  string that the crawler sends to the service" (fetched `https://www.rfc-editor.org/rfc/rfc9309.txt`,
  lines 177-178).
- Request timeout: 30 s, set explicitly.
- The clock (`Callable[[], float]`, default `time.monotonic`) and sleep
  (`Callable[[float], Awaitable[None]]`, default `asyncio.sleep`) are injected.
- `_send(url, *, follow_redirects: bool) -> httpx.Response` is the one paced primitive: one
  `asyncio.Lock` per host and a minimum interval of 1.0 s between the start of two requests to that
  host. It maps `httpx.TransportError` to `FetchError` (raised `from` it; probe: `httpx.ConnectError`'s
  MRO printed `['ConnectError', 'NetworkError', 'TransportError', 'RequestError', 'HTTPError']`) and
  returns every status unmapped.
- `async get(url) -> httpx.Response`, in order:
  1. Ask `RobotsPolicy.allowed(url)`; when it answers no, raise `RobotsDisallowedError`.
  2. `_send(url, follow_redirects=False)`. A source request never follows a redirect, so no request
     reaches a path or host the robots check did not clear.
  3. On `429` or `503`, wait for `Retry-After` and resend, at most 3 attempts in total. RFC 9110
     §10.2.3: "The Retry-After field value can be either an HTTP-date or a number of seconds to delay
     after receiving the response" (fetched `https://www.rfc-editor.org/rfc/rfc9110.txt`, lines
     4809-4810). The HTTP-date form is parsed with `email.utils.parsedate_to_datetime` (probe:
     `"Fri, 30 Oct 2026 17:28:39 GMT"` printed `2026-10-30 17:28:39+00:00`). A missing or
     unparseable `Retry-After` waits the pacing interval; a date in the past waits 0 s. A wait above
     60 s, or a third failed attempt, raises `HttpStatusError`.
  4. `404` raises `SourceNotFoundError`; any other non-2xx, 3xx included, raises `HttpStatusError`.

### 5.2 RobotsPolicy

Holds an in-memory cache, one entry per origin (scheme, host, port), for the life of its
`PoliteClient`: one CLI run today, longer for later long-lived callers. It fetches
`<origin>/robots.txt` with `_send(url, follow_redirects=True)` — paced, never asking itself (RFC 9309
§2.2.2: "The /robots.txt URI is implicitly allowed", line 259) — on a redirect-following path limited
to 5 redirects, and acts on the result:

| Result | Behaviour | Authority (RFC 9309, fetched as above) |
|---|---|---|
| 2xx | decode the first 500 KiB of the body as UTF-8 with `errors="replace"`, parse with `RobotFileParser.parse`, answer with `can_fetch("ijob", url)`; a `ValueError` from `parse` counts as unreachable | §2.5: "The parsing limit MUST be at least 500 kibibytes" (line 425) |
| more than 5 redirects | disallow all | §2.3.1.2: "SHOULD follow at least five consecutive redirects" (line 377); beyond five "MAY assume that the robots.txt file is unavailable" (lines 384-385) — ijob takes the conservative side |
| 429 | disallow all for this client's life (treated as unreachable, the conservative side) | — |
| other 4xx | allow all | §2.3.1.3: "the crawler MAY access any resources on the server" (lines 395-396) |
| 5xx, `FetchError` | disallow all | §2.3.1.4: "the crawler MUST assume complete disallow" (lines 401-402) |
| cache | an entry is reused for at most 24 h, then refetched | §2.4: "SHOULD NOT use the cached version for more than 24 hours" (lines 419-420) |

Parser behaviour this relies on, by probe on CPython 3.14.10 (in addition to section 1, premise 5):

```text
merged groups (two "User-agent: ijob" groups, Disallow /foo and /baz): /foo False /baz False /ok True
specific over *  ("*" Disallow: /, "IJOB" Allow: /), /jobs:            True
no matching group and no "*", /jobs:                                   True
empty file, /jobs:                                                     True
```

Redirect limit, by probe: a client built with `max_redirects=5, follow_redirects=True` against a
transport that always answers `301` printed `TooManyRedirects after requests: 6 RequestError`
(five redirects followed, then the error, whose base class is `RequestError`). The probe used
`AsyncClient`; the review's re-run with the sync `httpx.Client` printed the same.

The Greenhouse API host allows the board endpoint: `https://boards-api.greenhouse.io/robots.txt`
(fetched, `200`) reads `User-agent: *` / `Disallow: /embed/`.

## 6. Source adapters and collection

### 6.1 The `Source` protocol (`ijob_sources.collect`)

```python
class Source(Protocol):
    @property
    def ref(self) -> SourceRef: ...
    async def fetch(self, client: PoliteClient) -> SourceBatch: ...
```

`SourceBatch` is a frozen slotted dataclass `(drafts: list[SightingDraft], rejected: int)`: the
valid drafts, and how many jobs the adapter dropped because they failed `SightingDraft` validation.
**Contract:** an adapter builds each draft inside its parse function, catches `InvalidValueError` per
job, logs it at `WARNING` with the job's id and the reason, counts it in `rejected`, and keeps going.
A body it cannot read at all raises `ParseError`. No other exception leaves `fetch` except a
`SourcesError` from the client.

The protocol sits next to its consumer, `collect`, as the guideline's protocol rule asks; Greenhouse
and the test fake implement it without importing it.

`async collect(sources, client) -> CollectResult` runs one task per source in an
`asyncio.TaskGroup`. Each task catches `SourcesError` only and turns it into
`SourceFailure(ref: SourceRef, error: SourcesError)`, so one failing source never cancels the others.
`CollectResult` carries `batches: dict[SourceRef, SourceBatch]` and `failures: list[SourceFailure]`.

The module docstring of `ijob_sources.collect` and an "Adding a source" section in
`packages/ijob-sources/README.md` document this contract: implement `Source`, add a `SourceKind`
member, keep parsing pure, honour the rejection rule above, add a trimmed recorded fixture.

### 6.2 Greenhouse (`ijob_sources.greenhouse`)

- `GreenhouseBoard(key: SourceKey)`; `ref` is `SourceRef(SourceKind.GREENHOUSE, key)`.
- `fetch` issues one `GET https://boards-api.greenhouse.io/v1/boards/{key}/jobs?content=true` and
  passes the body to `parse_jobs`. The response is `{"jobs": [...], "meta": {"total": N}}` with every
  job in one response: for `tailscale` (fetched) top keys were `['jobs', 'meta']`, `meta` was
  `{'total': 52}` and 52 jobs came back; as a large-board control, `anthropic` (fetched, without
  `content`) returned `200 482353`, `meta` `{'total': 674}` and 674 jobs.
- An unknown board answers `404` with body `{"status":404,"error":"Job not found"}` (fetched for
  `zz-no-such-board-ijob`), which surfaces as `SourceNotFoundError`.
- `parse_jobs(body: bytes, key: SourceKey) -> SourceBatch` is pure. Field mapping, with the values
  observed on the first `tailscale` job (fetched):

  | Draft field | Greenhouse field | Observed |
  |---|---|---|
  | `external_id` | `id` (as text) | `4721252005` |
  | `title` | `title` | `'Analytics Engineer, Data'` |
  | `company` | `company_name`; the board key when absent or empty | `'Tailscale'` |
  | `location` | `location.name`; `None` when absent | `'Remote (Canada)'` |
  | `url` | `absolute_url` | `'https://job-boards.greenhouse.io/tailscale/jobs/4721252005'` |
  | `posted_at` | `first_published`; `None` when absent | `'2026-08-04T14:21:41-04:00'` |
  | `raw` | the job object, re-serialised as JSON | — |

  `datetime.fromisoformat` reads the timestamp as timezone-aware (probe: `fromisoformat tz:
  UTC-04:00`). A body that is not JSON, or lacks a `jobs` list, raises `ParseError`. A job without
  `id`, `title` or `absolute_url`, or one that fails `SightingDraft` validation, is rejected under the
  section 6.1 contract.

## 7. Errors

- `ijob_core.errors`: `CoreError(Exception)`, and `InvalidValueError(CoreError)` for a failed
  validation.
- `ijob_sources.errors`: `SourcesError(Exception)`, with `RobotsDisallowedError`, `HttpStatusError`
  (carrying URL and status), `SourceNotFoundError(HttpStatusError)`, `FetchError` and `ParseError`.
- Every `raise` inside an `except` uses `from`. `collect` is the one isolation point, and it catches
  `SourcesError`, the package root, nothing wider; the section 6.1 contract keeps `InvalidValueError`
  inside the adapter.

## 8. Command line (`ijob_app.cli`)

```text
ijob search --greenhouse stripe,anthropic [--db PATH]
```

1. Parse the comma-separated tokens with `parse_source_key`; a bad token exits `2` with the reason.
2. Open the store, build the `PoliteClient` through a factory parameter (tests inject a transport),
   run `collect` with `asyncio.run`.
3. `Store.record(<all batches' drafts>, seen_at=<now, UTC>)`, then print one line per posting from
   `Store.postings(sources=<the succeeded refs>)`: title, company, location, URL.
4. Print to stderr, choosing the line by the type of `SourceFailure.error`:
   `skipped greenhouse:<key>: disallowed by robots.txt` (`RobotsDisallowedError`),
   `not found: greenhouse:<key>` (`SourceNotFoundError`),
   `failed greenhouse:<key>: HTTP <status>` (`HttpStatusError`),
   `failed greenhouse:<key>: <reason>` (`FetchError`, `ParseError`); and for a batch with
   `rejected > 0`, `greenhouse:<key>: skipped <n> invalid jobs`.
5. Exit `0` when at least one source succeeded, `1` when every source failed.

typer runs on the target interpreter: a probe app with a `search` command and a second command,
invoked through `typer.testing.CliRunner` with `["search", "--greenhouse", "stripe,anthropic"]`,
printed `CliRunner: 0 tokens=['stripe', 'anthropic']`.

## 9. Testing

No test touches the network or really sleeps. Every logic-bearing module gets a `test_<module>.py`
under `packages/<member>/tests/<import>_tests/`. The gate runs them on CPython 3.14.10, the
interpreter every probe above ran on.

| Test module | Covers |
|---|---|
| `ijob_core_tests/test_model.py` | each validation rule in section 3 raises `InvalidValueError`, including `SourceKey` values `x?y`, `a/b`, `..`, `` and `a b`; `str(SourceRef)` |
| `ijob_core_tests/test_store.py` | on a `tmp_path` database: migration 0 → 1; first `record` gives 1 posting and 1 sighting; the same draft again leaves 1/1, moves `last_seen`, replaces `raw` (read back with `Store.sighting`), reports `updated=1`; ordering by `posted_at` |
| `ijob_sources_tests/test_robots.py` | the eight parser cases quoted in sections 1 and 5.2; 404 → allow; 429, 500 and `FetchError` → disallow; more than five redirects → disallow; reuse within 24 h and refetch after, with a fake clock; a body past 500 KiB is cut |
| `ijob_sources_tests/test_http.py` | User-Agent header; pacing waits recorded by the fake sleep; `Retry-After` in seconds, as an HTTP-date, missing, unparseable and past; stop after 3 attempts and on a wait above 60 s; 3xx, 404, other non-2xx and transport-error mapping |
| `ijob_sources_tests/test_greenhouse.py` | `parse_jobs` on a fixture trimmed from the live `tailscale` response (two jobs, `content` shortened), asserting the exact values in section 6.2; company fallback; absent `location` and `first_published` → `None`; a job with an empty title or a non-http URL is rejected and counted while the other job survives; non-JSON and missing `jobs` → `ParseError`; `fetch` through `httpx.MockTransport` |
| `ijob_sources_tests/test_collect.py` | a source raising `SourcesError` yields a `SourceFailure` while the other source's batch still returns |
| `ijob_app_tests/test_cli.py` | `CliRunner` with an injected transport and a `tmp_path` database: listing printed; a second run adds no postings; each stderr line in section 8 step 4; exit `1` when every source fails; exit `2` on a bad token |

The fixture lives at `packages/ijob-sources/tests/ijob_sources_tests/fixtures/greenhouse/tailscale_jobs.json`.

## 10. Non-goals

- Every adapter other than Greenhouse, including Gem.
- Seed-company import, and adding or removing sources or manual URLs.
- The source identity check.
- Cross-source dedupe keys and matching (store migration 2 in the follow-on chain).
- Salary, country and role normalization, and all filters.
- Greenhouse per-job pay ranges (`pay_transparency`) and the Greenhouse EU host.
- The ETag cache (follow-on chain, inside `PoliteClient`).
- A persisted robots.txt cache.
- Output formatting beyond a plain listing.

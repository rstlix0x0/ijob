---
status: approved
created: 2026-10-31
milestone: ijob-v1/02
---

# Intent: Walking skeleton

## Problem

ijob has a green workspace but no feature code. Milestone ijob-v1/02 needs about twenty source
adapters, normalization, dedupe and filters, and each of them depends on two things that do not exist
yet: a domain model for postings and a protocol that every source adapter follows. If adapters are
built before those are settled and proven end to end, each one will shape the model its own way, and
the model will churn across every adapter at once. One thin slice through every layer — fetch, store,
show — has to work first, so the follow-on chain (`2026-10-31-sources-and-filters`) can fan out on a
stable base.

## Affected systems

- `ijob-core`: the posting domain model and the SQLite store.
- `ijob-sources`: the polite HTTP client, the robots.txt checker, the source-adapter protocol and one
  Greenhouse adapter.
- `ijob-app`: the `ijob` command and a bare `search` subcommand.
- Root `pyproject.toml` and `uv.lock`: new runtime dependencies.

## Desired outcome

- `ijob search --greenhouse <token>[,<token>...]` fetches those Greenhouse boards politely, stores
  the postings in a local SQLite database and prints them (title, company, location, URL).
- Running it twice does not duplicate postings: a re-seen posting updates the `last_seen` of its
  existing sighting.
- A board whose robots.txt forbids the request is skipped and reported, not fetched.
- The source-adapter protocol (the adapter interface and the draft it returns) is documented well
  enough that the follow-on chain adds the next adapter without changing it. Adding a source kind to
  a closed set is an extension, not a change. The store may change through migrations.
- The gate passes, and every network path is tested with recorded fixtures.

## Constraints

- Design decisions already approved for milestone ijob-v1/02, which the spec honours rather than
  reopens:
  - One Posting, many Sightings. Within a source the key is `(source, external_id)`; across sources
    the canonical apply URL, falling back to a normalized company + title + country fingerprint.
  - Every Sighting keeps its raw payload, so normalization can be re-run without refetching.
  - Async `httpx` with a per-host rate limit, `Retry-After`, a robots.txt cache and ijob's own
    honest User-Agent. The ETag cache is deferred to the follow-on chain: it lives inside the HTTP
    client and hands cached bodies back on `304`, so adding it later changes no adapter.
  - Tests use `httpx.MockTransport` with trimmed recorded fixtures.
  - Store on stdlib `sqlite3`, migrations tracked by `PRAGMA user_version`.
- robots.txt uses RFC 9309 longest-match semantics. The stdlib `urllib.robotparser` gained them in
  CPython 3.14.5; CPython 3.12 still uses first match.
- `ijob-core` imports no network library and no LLM library.
- Tests and CI never touch the network.
- The python guideline gate in CLAUDE.md must pass.

## Non-goals

- Every adapter other than Greenhouse, including Gem.
- Importing the seed companies, and adding or removing sources or manual URLs.
- The source identity check.
- Cross-source dedupe matching and its keys. The adapter draft carries the fields the keys derive
  from (apply URL, company, title, location); the key columns and matching rules belong to the
  follow-on chain, through a store migration.
- Salary, country and role normalization, and all filters.
- Greenhouse per-job pay ranges (`pay_transparency`) and the Greenhouse EU host.
- Output formatting beyond a plain listing.

# ijob-sources

Adapters that collect job postings from hiring-system APIs, job boards, feeds and user-added URLs,
over a polite HTTP client with an honest User-Agent and robots.txt checks.

The package holds the polite client (`ijob_sources.polite_http`) and its response cache
(`ijob_sources.http_cache`), the source contract and the concurrent runner `collect`
(`ijob_sources.collect`), one adapter module per source, the registry that turns a sources-table row
into an adapter (`ijob_sources.registry`), the bundled seed list (`ijob_sources.seeds`), and the
resolution of a pasted URL into a source (`ijob_sources.detect`).

## Adding a source

1. Implement `Source` (`ref` and `fetch`) in a new module. A class satisfies the protocol
   structurally; it does not import it.
2. Add the `SourceKind` member in `ijob_core.model`.
3. Register the kind in `registry.py`: add its branch to `build_source`, which must stay exhaustive,
   and add a branch to `refresh_interval` if the source asks to be read less often than every
   run.
4. Add a row to `data/seeds.csv` when the source belongs in the default list. A job board, feed or
   aggregator lists many companies and has `-` as its company; a hiring-system board names the
   company it belongs to. A source only one user wants is added with `ijob sources add` instead.
5. Make every request through `PoliteClient.get`; never open a connection of your own. Pass
   `conditional=True` only for a URL that is fixed for the source, such as an index or a sitemap,
   so a `304` can replay the stored body; read `response.content` and decode it yourself.
6. Keep parsing in a pure function that takes the response body and returns a `SourceBatch`.
7. Reject invalid jobs per the contract in the `ijob_sources.collect` module docstring: build each
   draft inside the parse function, catch `InvalidValueError` per job, log it at `WARNING` with
   the job's id and the reason, count it in `rejected`, and keep going. Raise `ParseError` for a
   body that cannot be read at all. Let no other exception leave `fetch` except a `SourcesError`
   from the client.
8. When the source fetches a page per job, accept the ids of the jobs already stored and report
   those it still lists but did not re-fetch in `SourceBatch.still_listed`. The store keeps a
   posting open only while a source's latest fetch showed it, so a known job left out of both
   `drafts` and `still_listed` disappears from the listing.
9. For a hiring-system source that is registered for a company, check the board against that
   company with `ijob_sources.identity.check_identity`, so a board of the wrong company raises
   `IdentityMismatchError`.
10. Add a trimmed recorded fixture under `tests/ijob_sources_tests/fixtures/<kind>/`.
11. Never call the network from a test; serve the fixture through `httpx.MockTransport`.

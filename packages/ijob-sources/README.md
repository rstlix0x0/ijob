# ijob-sources

Adapters that collect job postings from hiring-system APIs, job boards and user-added URLs,
over a polite HTTP client with an honest User-Agent and robots.txt checks.

Today the package holds the polite client (`ijob_sources.polite_http`), the source contract and
the concurrent runner `collect` (`ijob_sources.collect`), and one adapter, Greenhouse
(`ijob_sources.greenhouse`).

## Adding a source

1. Implement `Source` (`ref` and `fetch`) in a new module. A class satisfies the protocol
   structurally; it does not import it.
2. Add the `SourceKind` member in `ijob_core.model`.
3. Make every request through `PoliteClient.get`; never open a connection of your own.
4. Keep parsing in a pure function that takes the response body and returns a `SourceBatch`.
5. Reject invalid jobs per the contract in the `ijob_sources.collect` module docstring: build each
   draft inside the parse function, catch `InvalidValueError` per job, log it at `WARNING` with
   the job's id and the reason, count it in `rejected`, and keep going. Raise `ParseError` for a
   body that cannot be read at all. Let no other exception leave `fetch` except a `SourcesError`
   from the client.
6. Add a trimmed recorded fixture under `tests/ijob_sources_tests/fixtures/<kind>/`.
7. Never call the network from a test; serve the fixture through `httpx.MockTransport`.

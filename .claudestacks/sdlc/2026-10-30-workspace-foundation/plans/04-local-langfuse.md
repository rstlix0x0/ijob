---
status: approved
created: 2026-10-30
depends-on: [01]
built-on: cc0c09ea9127
---

# Local Langfuse Implementation Plan

**Goal:** `docker compose up` starts a local Langfuse stack whose connection settings are documented in `.env.example`.

**Content authority:** `intent.md` of this chain (spec skipped).

**Architecture:** `compose.yaml` at the root is Langfuse's official self-hosting compose file taken
from tag `v4.72.0` of `langfuse/langfuse` (latest release, published 2026-10-29), changed only to bind
every published port to `127.0.0.1`. `.env.example` documents the variables the stack and ijob's
OTLP exporter read. ijob never requires the stack.

**Tech Stack:** Docker Compose, Langfuse v4 self-hosted, OpenTelemetry OTLP/HTTP.

Recorded facts (fetched 2026-10-30):
- `https://raw.githubusercontent.com/langfuse/langfuse/v4.72.0/docker-compose.yml` (235 lines):
  images `docker.langfuse.com/langfuse/langfuse-worker:4` (l.8), `docker.langfuse.com/langfuse/langfuse:4`
  (l.118), `docker.io/clickhouse/clickhouse-server:25.12` (l.148), `cgr.dev/chainguard/minio` (l.169),
  `docker.io/redis:7` (l.190), `docker.io/postgres:${POSTGRES_VERSION:-17}` (l.207). Secrets use
  `${VAR:-default} # CHANGEME` (l.23-25, 44, 54, 61, 75, 81, 125, 154, 176, 216). Ports published on
  all interfaces: `3000:3000` (l.122, web) and `9090:9000` (l.178, minio); all others already
  `127.0.0.1:…`.
- `https://langfuse.com/integrations/native/opentelemetry`: self-hosted endpoint
  `http://localhost:3000/api/public/otel`; "Langfuse currently supports OTLP over HTTP with both
  `HTTP/JSON` and `HTTP/protobuf`" and "`gRPC` is not supported yet"; header example
  `OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic ${AUTH_STRING},x-langfuse-ingestion-version=4"`
  where `AUTH_STRING` is `base64("<public key>:<secret key>")`.

---

## Files that change

```
compose.yaml  — [create] local Langfuse stack (upstream v4.72.0, ports on localhost)
.env.example  — [create] documented environment variables (no real values)
```

---

### Task 1 — Compose file

**Files:**
- Create `compose.yaml`

**Change:** copy the v4.72.0 upstream file above unchanged except: a header comment naming the
source URL and tag; `3000:3000` → `127.0.0.1:3000:3000`; `9090:9000` → `127.0.0.1:9090:9000`.
Images and the `${VAR:-default} # CHANGEME` secrets stay as upstream ships them, so the stack starts
with no `.env` and `.env` can override every secret.

**Proof:** these succeed, in order —
- `docker compose -f compose.yaml config --quiet`
- `docker compose -f compose.yaml config | grep -E 'published|host_ip'` shows every published port with `host_ip: 127.0.0.1`
- `docker compose -f compose.yaml up -d`, then within 3 minutes
  `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000` prints a 2xx or 3xx code
- `docker compose -f compose.yaml down` (volumes kept)

**Done when:** the checks pass. Commit `build: add local Langfuse compose stack`.

### Task 2 — Environment template

**Files:**
- Create `.env.example`

**Change:** placeholder values only, grouped with a comment line each:
- Providers: `OPENROUTER_API_KEY=` (the `claude-sdk` provider uses the local Claude Code login and needs no variable).
- Tracing: `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel` and
  `OTEL_EXPORTER_OTLP_HEADERS=` with a comment showing the cited form
  `Authorization=Basic <base64(public_key:secret_key)>,x-langfuse-ingestion-version=4` and that the
  keys come from a Langfuse project's settings page. HTTP only; no gRPC.
- Stack overrides (all commented out): `SALT`, `ENCRYPTION_KEY` (with the upstream note
  `openssl rand -hex 32`), `NEXTAUTH_SECRET`, `POSTGRES_PASSWORD`, `CLICKHOUSE_PASSWORD`,
  `REDIS_AUTH`, `MINIO_ROOT_PASSWORD`.
A header comment says: copy to `.env`, never commit `.env`, ijob runs without the stack.

**Proof:** these succeed —
- `git check-ignore -q .env` and `! git check-ignore -q .env.example` (plan 01's `.gitignore`)
- `! grep -nE 'sk-or-|pk-lf-|sk-lf-' .env.example`
- `grep -c '^OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel$' .env.example` prints `1`

**Done when:** the checks pass. Commit `build: document environment variables`.

---

## Risks

- **Floating images:** upstream uses major tags (`langfuse:4`) and an untagged minio image; kept as
  upstream ships them so the file stays a faithful copy. Reproducibility comes from the pinned compose tag.
- **Default secrets:** upstream placeholders are fine for a localhost-only stack; Task 1 binds every port to `127.0.0.1`.
- **Resource use:** ClickHouse needs several GB of RAM; the stack is optional.
- **Not done here:** OpenTelemetry wiring in code (milestone 04), DeepEval/GEPA score export (milestone 05).

## Proof

The stack starts and answers on `127.0.0.1:3000`, every published port is localhost-only, and
`.env.example` documents every variable with no real values while `.env` stays ignored.

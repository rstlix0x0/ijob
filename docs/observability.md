# Observing ijob's model calls

Observability answers one question: **what did ijob ask a model, what came back, how long did it
take and what did it cost?** Every model call leaves a record you can read in the terminal. When you
want to browse them, compare runs or see scores beside the answers, you can send them to a local
Phoenix.

This page has no results. It says what is recorded, where it lives and how to read it.

## The two layers

```text
every model call
      |
      v
~/.ijob/traces.db     always on, local, read with `ijob traces`
      |
      |  ijob traces export      you run it, when you want it
      v
Phoenix               optional, local container, read in a browser
      ^
      |  labels, eval runs and scores, from ~/.ijob/ijob.db
```

A model call only ever writes to `traces.db`, so it never waits on Phoenix or any other service.
ijob works the same whether Phoenix is running or not. Tracing cannot be turned off:
`OTEL_SDK_DISABLED` is ignored with a warning.

## What is recorded

One command is one **trace**. Each step inside it is a **span**.

```text
ijob eval-scorer          the root: one operation, such as a search or an eval run
├─ chat <model>           one model call
├─ chat <model>
└─ tool <name>            one tool call, in the agents
```

An operation that calls no model writes no trace. Tuning runs are named `ijob tune-judge` and
`ijob tune-scorer`.

A model call's span holds:

| Attribute | Holds |
|---|---|
| `gen_ai.provider.name` | Who served it |
| `gen_ai.request.model`, `gen_ai.response.model` | The model asked for, and the one that answered |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | Tokens used |
| `gen_ai.usage.cost` | What the call cost |
| `ijob.task`, `ijob.task_version` | Which job it did, and under which prompt |
| `ijob.posting_id` | The posting it was about |
| `ijob.outcome` | How it ended, see below |
| `ijob.prompt`, `ijob.completion` | The text sent and received, each cut to 16 KB |

`ijob.task_version` is how you tell a base prompt from a tuned one: a tuned program's version is the
base version, then `+` and a hash.

| `ijob.outcome` | Means |
|---|---|
| `ok` | The call answered, and the answer could be read |
| `task_output_error` | The call answered, but the answer could not be used |
| `timeout` | The call took longer than `[llm].timeout_s` |
| `provider_failed` | The provider refused or failed the call, such as for low credit |
| `provider_unavailable` | The provider can't be used at all, such as a missing CLI |
| `cost_unknown` | The call finished, but its cost can't be found |

## In the terminal

| Command | Does |
|---|---|
| `ijob traces` | Lists the newest traces: id, start, operation, calls, cost and duration |
| `ijob traces show ID` | Shows one trace as nested spans |
| `ijob traces prune` | Deletes the oldest traces until the file fits `[tracing].max_size_mb` |
| `ijob traces export` | Sends everything to Phoenix |

ijob also prunes on its own when an operation ends, so a deleted trace is gone for good, from the
terminal and from any later export.

## In Phoenix

Phoenix is one container, set up in `compose.yaml`. It needs Docker.

```bash
docker compose up -d        # start it; the UI is at http://127.0.0.1:6006
uv run ijob traces export   # send what it lacks
docker compose down         # stop it; its data stays in the volume
```

An export sends four things, in this order. It checks that Phoenix is reachable first, so nothing is
sent to a stopped one.

| ijob has | Phoenix shows it as | Name |
|---|---|---|
| Spans | Traces in a project | `ijob` |
| Labels | A dataset per label set and task | `ijob-<task>-set-<id>` |
| Full and held-out eval runs | An experiment on that dataset | `run-<id>-<model>` |
| Whether each answer matched its label | An evaluation on each experiment run | `matches_label` |

Spans get a kind that Phoenix reads: the root is a `CHAIN`, a model call an `LLM` and a tool call a
`TOOL`.

Where each thing helps:

- **A slow or failed run.** Open its trace and look for spans with a long duration or an outcome that
  isn't `ok`.
- **A wrong answer.** Open an experiment, find a run that scored 0, and follow its trace link to the
  exact prompt and reply.
- **Two models or programs.** Compare two experiments on the same dataset. A held-out experiment's
  metadata also holds its optimizer, program, tuning run, validation scores and tuning spend.
- **The split.** Each example's metadata holds its `split_part`: `train`, `val`, `held_out` or
  `unused`.

A run links to its trace by time, task, posting, version and model. A run with no matching span is
still sent, with no link.

### Running it again

An export can stop and run again, and it adds only what Phoenix lacks.

| Item | On a rerun |
|---|---|
| Spans | Sent again; Phoenix drops those it holds |
| Examples | Only missing ones are added; existing ones are not updated |
| Experiments and runs | Found by name; only missing ones are added |
| Scores | Sent again, replacing the old ones |

Because existing examples are not updated, export after `ijob tune screen`, so that examples carry
their split part.

Deleting the volume with `docker compose down -v` is safe for ijob's own records: the next export
refills Phoenix with whatever `traces.db` and `ijob.db` still hold.

### When it fails

Each of these exits 1, and a rerun is safe.

| Message | Do |
|---|---|
| `Phoenix is not running at <url>; run docker compose up -d` | Start it |
| A status and the first 200 characters of the reply | Read the reply. The calls are those of image 21.0.0, so check `compose.yaml` if you changed the tag |
| `sending spans to <url> failed` | Read `docker compose logs phoenix` |

The command also says how many runs or results it skipped, if any, and why.

## Cost

Read cost from ijob, not Phoenix. Phoenix can show a cost total, but on a checked run it did not match
ijob's own records. `ijob tune report` and `ijob eval report` read ijob's records. See
[tuning.md](tuning.md).

## Your data

| Where | What it holds |
|---|---|
| `~/.ijob/traces.db` | Prompts and replies, which hold your rules, CV profile and postings |
| Phoenix's volume | All of that after an export, plus labels, posting titles, model answers and tuned instructions |

- Neither belongs in the repository.
- Nothing leaves your machine on export: `[phoenix].endpoint` must be a local address, and ijob ignores
  proxy settings for it.
- Phoenix runs with auth off, because its port is bound to `127.0.0.1` only. While it runs, anything on
  the same Mac can read, change or delete its data, so stop it after use.
- `docker compose down -v` deletes the volume.

## Settings

```toml
[tracing]
max_size_mb = 256                    # the size traces.db is kept to

[phoenix]
endpoint = "http://127.0.0.1:6006"   # must be on this machine
```

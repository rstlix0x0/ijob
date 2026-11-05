# Tuning ijob's judge and scorer

Tuning asks one question: **can a cheaper model do the `judge` and `scorer` jobs as well as a dear
one, if its prompt is tuned on your labels?** An optimizer rewrites the prompt's instructions and
adds worked examples. ijob then tests the result on postings the optimizer never saw.

This page has no results. It says what each command does and how to read what it prints. Any number
here is a setting or a count that ijob uses as a rule, not something measured.

## The ten steps

```text
ijob eval label --add 100     vote on 100 more postings
ijob eval review              decide the splits and spot checks
ijob tune screen              freeze the split; measure candidates, baseline and configured models
ijob tune report              read train and val numbers, pick two, set [tune].models
ijob tune run                 tune each model x optimizer x task, then test on held-out
ijob tune report --out docs/results/optimization.md
ijob tune accept <id>         test against hostile postings, then use it in search
docker compose up -d          start Phoenix
ijob traces export            send traces, labels, runs and scores
docker compose down           stop Phoenix; its data stays in the volume
```

Commands that can spend show an estimate and ask first. `tune run` always asks. `tune screen` asks when
the split isn't frozen yet or a model is on OpenRouter. `tune accept` asks for an OpenRouter model.
Pass `--yes` to skip the question. With no terminal and no `--yes`, the command exits 1 before it
spends.

1. **`ijob eval label --add 100`** gives the tuner more labels. Tuning needs enough keeps and rejects
   left after the held-out part, at least 20 of each. A set with too few is refused, and the message
   names the numbers. See [evals.md](evals.md) for how labels work.
2. **`ijob eval review`** is where you settle the splits and the spot checks. Tuning refuses a set
   with labels still waiting for review.
3. **`ijob tune screen`** freezes the split, then runs `judge` and `scorer` on every labelled posting
   for each model in `[tune].candidates`, the `[tune].baseline` and the configured `[models].judge`
   and `[models].scorer`. A model whose latest run is already full is skipped, so a rerun only does
   what is missing. A call that times out is asked up to three times. A run that still loses a call
   to its provider reads `incomplete`, with the reason, and the screen goes on to the next model. It
   stops only for a spent ledger, an unavailable provider, an unknown cost or Ctrl-C.
4. **`ijob tune report`** shows the `Choosing` table. You read it, pick two models and write them
   into `[tune].models`.
5. **`ijob tune run`** tunes each model in `[tune].models` with each optimizer on each task. Then it
   tests every program on the held-out postings.
6. **`ijob tune report --out PATH`** writes the same tables to a Markdown file.
7. **`ijob tune accept ID`** tests one program against hostile postings. If it passes, search uses
   it.
8. **`docker compose up -d`** starts Phoenix, a local trace viewer.
9. **`ijob traces export`** sends traces, labels, runs and scores to Phoenix. It can stop and rerun.
10. **`docker compose down`** stops Phoenix. Its data stays in the Docker volume.

Other commands:

| Command | Does |
|---|---|
| `ijob tune list` | Lists saved programs and their status |
| `ijob tune clear TASK` | Goes back to the base prompt for `judge` or `scorer` |

## The split

`tune screen` freezes the labelled postings into parts, once per label set:

```text
held-out   half the postings, with half the keeps; natural keep rate
tuning     the rest, balanced: as many keeps as rejects
           train takes 60% of them, val takes 40%
```

The freeze can't be undone. A label set that gets more labels later keeps its split; the new labels
are left out and counted in `tune report`. Changing your rules, CV profile or jury starts a new label
set with its own split.

Only train and val are used to choose a model or to tune. Held-out postings are used once, to test.

## The three optimizers

| Optimizer | `--optimizer` | What it does |
|---|---|---|
| GEPA | `gepa` | Reads where the prompt failed and rewrites its instructions |
| BootstrapFewShotWithRandomSearch | `bootstrap` | Tries sets of worked examples and keeps the best set |
| MIPROv2 | `mipro` | Proposes new instructions and picks among them |

GEPA and MIPROv2 use the model in `[models].tuner` to reflect and propose. It must be a `claude-sdk`
model, because its prompts carry your rules and postings.

Worked examples come from the model's own right answers. Labels carry no reasons, so ijob does not
use a label itself as an example.

The scores that guide them:

| Task | Score |
|---|---|
| `judge` | 1 when keep matches the label, else 0 |
| `scorer` | `1 - abs(answer - label) / 100` |

A tuned prompt must still carry the notice that posting text is untrusted. If tuning drops it, ijob
puts it back before the held-out test, so what is tested is what would run.

A run that finds nothing better ends as `no change`. It saves no program.

Narrow a run with `--model`, `--optimizer` and `--task`. `--again` tunes a finished combination
again. A model with no full run is refused, with a pointer to `tune screen`. `--model` may name a model
that is not in `[tune].models`, so you can try one extra.

The grid runs one combination at a time. It goes on to the next when a run hits its cap, when too
many cases fail, when the optimizer raises, when a held-out test loses a call, or when the train or
val part has no unchanged posting left. It stops when the ledger is spent, a provider is down, a cost
can't be known, or you press Ctrl-C, whether that happens in tuning or in a held-out test.

It ends with a table of every combination and exits 1 if any is incomplete, or 2 for a setting or
option that can't be used. After Ctrl-C, the combination that was running reads `incomplete`, with
the spend so far where it is known. A note after the table counts postings left out because their
text changed. When every combination is already done, it says so and exits 0.

A later `tune run` retries every saved program that has no complete held-out run, older ones
included, without tuning it again.

## Cost

Two limits hold the spend.

| Limit | Setting | Holds |
|---|---|---|
| Run cap | `[tune].max_cost_usd` | One tuning run, across both providers |
| Ledger | `[eval].max_cost_usd` | All eval and tuning runs together |

The run cap stops one runaway run. The ledger stops the sum. A tuning run is one `eval_runs` row of
suite `tune`, so its OpenRouter spend joins the ledger. When the ledger is empty, even
subscription-only combinations stop; raise `[eval].total_cost_usd` to go on.

Calls through `claude-sdk` are on your subscription. They count against the run cap as metered value,
not as money spent. Only OpenRouter calls are real money.

### Why cost comes from ijob and not Phoenix

Phoenix can show a cost total, but its total is wrong for ijob. On a checked run it did not match
ijob's own records. So cost in `tune report` and in the README comes from ijob's records, never from
the Phoenix screen. Use Phoenix to read traces, not to count money.

## Reading `tune report`

On a terminal the tables are drawn in boxes. In a pipe the command prints the same Markdown that
`--out` writes. `--out PATH` replaces the file, also prints the tables, and notes the path on
standard error. If the file can't be written, it exits 1.

```text
Choosing (train and val only)
TASK  MODEL  VENDOR  MACRO F1  RECALL  NDCG@10  MEAN ERROR  COST / 100 POSTINGS  CASES  MISSING  UNUSABLE

Held-out (N postings, K keeps)
TASK  MODEL  PROGRAM  MAIN  MARGIN  MAIN VS BASELINE  RECALL  PRECISION  COST / 100 POSTINGS
      COST VS BASELINE  P50 MS  TUNE SPEND  CASES  MISSING
```

With no split yet, the report prints one notice, `No split yet: run ijob tune screen to freeze one`,
and no tables.

### Choosing

`Choosing` lists every full run, limited to train and val postings. Held-out postings never appear,
so nothing you pick here is picked by looking at the test. Use it to choose which two models to tune.

| Column | Means |
|---|---|
| `MACRO F1`, `RECALL` | Macro F1 and keep recall of the `judge` |
| `NDCG@10`, `MEAN ERROR` | NDCG@10 and mean score error of the `scorer` |
| `COST / 100 POSTINGS` | Cost for 100 postings |
| `CASES`, `MISSING`, `UNUSABLE` | Postings counted, postings with no stored answer, and answers that could not be read |

The columns mean what they mean in [evals.md](evals.md). A note under the table says the cost is the
whole run's rate, held-out postings included, because ijob stores no cost per posting. A cost on a
`claude-sdk` model reads `(metered value)`: it is not money spent.

### Held-out

`Held-out` appears once a tuned program has a complete held-out run. It then lists every untuned model
and every program, so each program sits beside its own model without tuning. A program with no
complete held-out run is listed with dashes and its tuning spend.

| Column | Means |
|---|---|
| `PROGRAM` | `untuned`, or `program N (optimizer)` |
| `MAIN` | Macro F1 for `judge`, NDCG@10 for `scorer` |
| `MARGIN` | The `±` half-width of a 95% interval on keep recall. `judge` only; the `scorer` shows `-` |
| `MAIN VS BASELINE` | The gap in `MAIN` to the model in `[tune].baseline` |
| `COST VS BASELINE` | The gap in cost to that model; `(metered value)` if either side is |
| `COST / 100 POSTINGS` | Cost for 100 postings |
| `P50 MS` | The middle case's time |
| `TUNE SPEND` | What tuning the program cost |

`TUNE SPEND` shows money and metered value apart. Money is `$a`. Metered value is `$b (metered value)`.
A tuner that used both reads `$a + $b (metered value)`. They are never added together.

Read a row against the `untuned` row for the same model first. That gap is what tuning bought. Then
read the gap to the baseline. `MARGIN` is the interval of keep recall, not of `MAIN`, so use it as a
hint only. A note under the table says so.

Untuned numbers cost nothing to produce: they come from the stored per-case results of full runs.

Under the tables are two footer lines. One counts labels added after the freeze and postings that
changed. The other appears when postings have no stored answer, or changed since the draw, and are
left out. It points to `tune screen`. A last line says cost comes from ijob's records, not Phoenix.

The `--out` file holds the same tables as Markdown. It holds model, optimizer and vendor names and
numbers only. It has no prompt text, rules, CV profile or postings.

## Accepting a program

`ijob tune accept ID` runs in this order.

1. It refuses first, before it spends. The program must be tested: it needs a complete held-out run.
   Its model must be the task's configured model, its rules or profile must be the ones it was built
   on, and its base prompt version must be the current one. A program text without the untrusted-text
   notice is refused too. The message says what to change.
2. It asks, for an OpenRouter model only. The question says that the program's instructions and demos
   may hold your rules and postings, and that they go to OpenRouter and the model's vendor. A
   `claude-sdk` program is not asked about. `--yes` answers the question. With no terminal and no
   `--yes`, it refuses.
3. It runs the `hostile` suite on the program's model, for the program's task only.
4. It decides. The program is accepted if the run is complete, finds 0 attacks, and gets an answer for
   every pair. There are 5 pairs, each a clean and a hostile posting, so 10 answers.

| Exit | Means |
|---|---|
| `0` | Accepted |
| `1` | Not accepted, refused, declined, or interrupted with Ctrl-C |
| `2` | Unknown program number, or a config setting that can't be used |

If the program fails, nothing changes. The result lines end `not accepted`, and an error line gives
the counts and the run:

```text
error: program N is not accepted: attacks A, unanswered U, run R
```

Read that run with `ijob eval report R --failures`. If a provider stopped the run, the line adds the
reason. A Ctrl-C says no program was accepted. The run it left stands.

Better scores are not required. You read the report and decide.

One program per task is in use at a time, and a new one replaces the old. Search uses a program only
while its model is the configured model and its rules or profile and base version are current.
Otherwise it runs the base prompt, and `tune list` says why. A replaced CV counts too: while the
profile is still to be rebuilt, scoring runs the base prompt.

Postings with no score under the program's version are scored as search reaches them.

If the accepted program's text is malformed, search, judge, score, the score list and `tune list` stop
and name the program, and `ijob search` exits 2, until you run `ijob tune clear TASK`. A program that
has lost its untrusted-data sentence also stops search, and `tune list` shows it as `accepted, not in
use`.

`ijob tune clear TASK` goes back to the base prompt for `judge` or `scorer`. It prints
`<task> goes back to the base prompt`, or `no program is accepted for <task>`. Both exit 0. The
program row stays. It reads no config and loads no model, so a broken config does not block it. An
unknown task exits 2.

A program's status in `tune list` is `tested`, `untested`, `accepted`, or `accepted, not in use`
with the reason.

## Phoenix

`docker compose up -d` starts one local container, bound to `127.0.0.1` only. Auth is off. While it
runs, anything on the same Mac can read, change or delete its data, which holds your CV and rules.
Stop it after use with `docker compose down`.

`ijob traces export` sends spans, labels, runs and scores. A rerun adds what is missing and does not
duplicate. The endpoint is `[phoenix].endpoint`, and only a local address is accepted. See
[observability.md](observability.md) for what is recorded and how to read it.

## Data

- Programs, splits and tuning runs stay in `~/.ijob/ijob.db`. Prompts and replies stay in
  `~/.ijob/traces.db`. Neither belongs in the repository.
- A tuned program holds instructions and examples that may quote your rules and postings. Do not
  commit one.
- Tuning sends your rules, CV profile and postings to every model it calls. ijob asks before it
  spends.

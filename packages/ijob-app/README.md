# ijob-app

The ijob command-line interface and terminal user interface.

## Agents

```bash
ijob sources find "Example Co"                          # find the source behind a company
ijob sources find https://example.com/careers --company "Example Co"
ijob explore "Find fintech companies hiring in Tokyo"   # propose sources to add
```

`sources find` and `explore` use the models set under `[models].source_agent` and
`[models].explore_agent`, or the one given with `--model`. Each shows what the agent
did and what it cost, then asks before it adds anything; `--yes` answers for you, and
without a terminal and without `--yes` they exit `1` before any call. An answer counts
only if a tool verified it, and the agent's pages stay on the company's site.

## Evals

```bash
ijob eval label --limit 10             # a jury of models votes on a sample of postings
ijob eval review                       # settle the cases the jury split on
ijob eval run <suite> [--model PROVIDER/MODEL] [--limit N] [--yes]
ijob eval report [RUN] [--failures]    # the stored runs, or one run and its failures
```

| Suite | Measures | Default model | `--limit` |
|---|---|---|---|
| `judge` | keep or reject against your labels | `[models].judge` | 1 to 100 |
| `scorer` | scores against your labels | `[models].scorer` | 1 to 100 |
| `reasons` | whether the reasons the judge and scorer gave match the posting | `[models].judge` | 1 to 100 |
| `hostile` | whether injected text in a posting changes an answer | `[models].judge` | not offered |
| `source-agent` | how often the source the agent verifies is the seeded one | `[models].source_agent` | 1 to 150 |
| `explore-agent` | how many proposals are accepted, and how well they follow the instruction | `[models].explore_agent` | 1 to 5 |

`source-agent` runs the board seeds that ship with ijob, 100 unless `--limit` says
otherwise, and three made-up careers pages that carry injected instructions. Its
cases are easy, since most board names are simple company slugs, so its score flatters
the agent. `explore-agent` runs five made-up instructions, each on an empty
store so it reads none of your data, and has the `[models].eval_grader` model grade
how well the proposals follow them. Neither agent suite sends your data anywhere.

A run that calls an OpenRouter model, the grader included, shows its estimate and asks
first. An agent suite's estimate reads "up to the cap", because the smaller of
`[eval].max_cost_usd` and what is left of `[eval].total_cost_usd` is what bounds it.
In a pipe the plan goes to stderr and the
result is `name  value` lines. `hostile`, `source-agent` and `explore-agent` exit `1`
on any rule violation, attack or incomplete run, since their target is 0.

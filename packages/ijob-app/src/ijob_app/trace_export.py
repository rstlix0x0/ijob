"""Export traces, labels, eval runs and their scores to Phoenix.

`export_all` runs in a fixed order: reachability, spans, label datasets, run
experiments, scores. Every step can run again. Phoenix drops a span it already
holds; a dataset is found by name and only missing examples are appended;
an experiment is listed by name before one is made and only missing runs are
posted; a score is an upsert, so it is sent for every run each time and a stop
between a run and its score heals on the next export.
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from ijob_ai.tracing import attributes
from ijob_ai.tracing.phoenix import SPAN_BATCH_SIZE, PhoenixClient
from ijob_core.eval_records import EvalLabel, EvalRun, EvalSet, EvalTask, RunStatus
from ijob_core.store import Store
from ijob_core.traces import TraceStore
from ijob_core.tune_records import TunedProgram

_TASKS: Final = frozenset(task.value for task in EvalTask)
_SCORE_NAME: Final = "matches_label"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ExportReport:
    """What an export did.

    Attributes:
        spans_sent: Spans handed to Phoenix, which drops those it holds.
        datasets_added: Datasets created.
        examples_added: Examples appended.
        examples_present: Examples Phoenix already held.
        experiments_added: Experiments created.
        runs_added: Runs posted.
        runs_present: Runs Phoenix already held.
        scores_sent: Evaluations posted, new or replacing.
        runs_skipped: Complete full or held-out `judge` and `scorer` runs left out
            because their set has no label dataset.
        results_skipped: Results left out because no dataset example carries
            their posting.
    """

    spans_sent: int = 0
    datasets_added: int = 0
    examples_added: int = 0
    examples_present: int = 0
    experiments_added: int = 0
    runs_added: int = 0
    runs_present: int = 0
    scores_sent: int = 0
    runs_skipped: int = 0
    results_skipped: int = 0


@dataclass(slots=True)
class _Tally:
    """The counts of `ExportReport`, while they are still growing."""

    spans_sent: int = 0
    datasets_added: int = 0
    examples_added: int = 0
    examples_present: int = 0
    experiments_added: int = 0
    runs_added: int = 0
    runs_present: int = 0
    scores_sent: int = 0
    runs_skipped: int = 0
    results_skipped: int = 0


@dataclass(slots=True)
class _Dataset:
    """A label dataset in Phoenix: its id and its examples by posting id."""

    id: str
    examples: dict[str, str] = field(default_factory=dict)


def export_all(store: Store, traces: TraceStore, client: PhoenixClient) -> ExportReport:
    """Send spans, labels, runs and scores to Phoenix.

    Reachability is checked first, so nothing is sent to a stopped Phoenix.

    Args:
        store: The postings and eval records.
        traces: The stored spans.
        client: The Phoenix client.

    Returns:
        What was sent, added and already there.

    Raises:
        PhoenixUnreachableError: If nothing answers at the endpoint.
        PhoenixHttpError: If a REST call is answered with 400 or above.
        SpanBatchRefusedError: If Phoenix refuses a batch of spans.
    """
    client.check_reachable()
    tally = _Tally()
    for batch in traces.span_batches(SPAN_BATCH_SIZE):
        client.send_spans(batch)
        tally.spans_sent += len(batch)
    datasets: dict[tuple[str, int], _Dataset] = {}
    for eval_set in store.evals.sets():
        for task in EvalTask:
            labels = store.evals.labels(eval_set.id, task)
            if labels:
                datasets[(task.value, eval_set.id)] = _export_labels(
                    store, client, tally, eval_set, task, labels
                )
    for run in store.evals.runs():
        held_out = _held_out(store, run)
        if run.status is RunStatus.COMPLETE and (not run.limited or held_out):
            dataset = datasets.get((run.suite, run.set_id or 0))
            if dataset is not None:
                _export_run(store, traces, client, tally, run, dataset, held_out)
            elif run.suite in _TASKS:
                tally.runs_skipped += 1
    return ExportReport(**asdict(tally))


def _dataset_name(task: EvalTask, set_id: int) -> str:
    """Name the dataset that holds a set's labels for a task."""
    return f"ijob-{task.value}-set-{set_id}"


def _export_labels(  # noqa: PLR0913, PLR0917  # the store, the target, a count, one set's labels
    store: Store,
    client: PhoenixClient,
    tally: _Tally,
    eval_set: EvalSet,
    task: EvalTask,
    labels: list[EvalLabel],
) -> _Dataset:
    """Create the set's dataset or append the labels it lacks; return it."""
    name = _dataset_name(task, eval_set.id)
    buckets = {case.posting_id: case.bucket for case in store.evals.cases(eval_set.id)}
    parts = {row.posting_id: row.part for row in store.tunes.split(eval_set.id)}
    dataset_id = client.find_dataset(name)
    held = {} if dataset_id is None else _examples_by_posting(client, dataset_id)
    missing: list[Mapping[str, Any]] = []
    for label in labels:
        if str(label.posting_id) in held:
            continue
        posting = store.posting(label.posting_id)
        metadata: dict[str, Any] = {
            "origin": label.origin.value,
            "bucket": buckets[label.posting_id].value
            if label.posting_id in buckets
            else None,
        }
        if label.posting_id in parts:
            metadata["split_part"] = parts[label.posting_id].value
        missing.append(
            {
                "input": {
                    "posting_id": str(label.posting_id),
                    "title": "" if posting is None else posting.title,
                },
                "output": (
                    {"keep": label.keep, "rule": label.rule}
                    if task is EvalTask.JUDGE
                    else {"score": label.score}
                ),
                "metadata": metadata,
            }
        )
    tally.examples_present += len(held)
    if missing:
        action = "create" if dataset_id is None else "append"
        dataset_id = client.upload_dataset(action, name, missing)
        if action == "create":
            tally.datasets_added += 1
        tally.examples_added += len(missing)
        held = _examples_by_posting(client, dataset_id)
    return _Dataset(dataset_id or "", held)


def _examples_by_posting(client: PhoenixClient, dataset_id: str) -> dict[str, str]:
    """Map posting ids to example ids, leaving out examples ijob did not make."""
    return {
        str(e.input["posting_id"]): e.id
        for e in client.dataset_examples(dataset_id)
        if "posting_id" in e.input
    }


def _held_out(store: Store, run: EvalRun) -> TunedProgram | None:
    """Find the stored program a limited run at a program's version tested."""
    if not run.limited or run.suite not in {t.value for t in EvalTask}:
        return None
    programs = [
        program
        for program in store.tunes.programs(EvalTask(run.suite))
        if program.version == run.task_version
    ]
    same_set = [program for program in programs if program.set_id == run.set_id]
    same_model = [
        program for program in same_set or programs if program.model == run.model
    ]
    found = same_model or same_set or programs
    return found[0] if found else None


def _ns(moment: datetime) -> int:
    """Read a moment as nanoseconds since the Unix epoch."""
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000


def _read_json(text: str) -> Any:  # noqa: ANN401  # whatever JSON a run stored
    """Decode stored metrics, keeping the text if it is not JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _run_metadata(
    store: Store, run: EvalRun, program: TunedProgram | None
) -> dict[str, Any]:
    """Describe a run, adding the tuning facts when it tested a program."""
    metadata: dict[str, Any] = {
        "run_id": run.id,
        "suite": run.suite,
        "model": run.model,
        "task_version": run.task_version,
        "set_id": run.set_id,
        "limited": run.limited,
        "spent_usd": run.spent_usd,
        "subscription_usd": run.subscription_usd,
        "started_at": run.started_at.isoformat(),
        "finished_at": None if run.finished_at is None else run.finished_at.isoformat(),
        "metrics": _read_json(run.metrics),
        "held_out": program is not None,
    }
    if program is None:
        return metadata
    metadata["optimizer"] = program.optimizer
    metadata["program_id"] = program.id
    tune = None if program.tune_run_id is None else store.evals.run(program.tune_run_id)
    if tune is not None:
        metadata["tune_run"] = tune.id
        metadata["tune_spent_usd"] = tune.spent_usd
        metadata["tune_subscription_usd"] = tune.subscription_usd
        tuned = _read_json(tune.metrics)
        if isinstance(tuned, dict):
            for key in ("optimizer", "calls", "failures", "unusable"):
                if key in tuned:
                    metadata[key if key == "optimizer" else f"tune_{key}"] = tuned[key]
            for key in ("baseline_val", "best_val", "no_change"):
                if key in tuned:
                    metadata[key] = tuned[key]
    return metadata


def _export_run(  # noqa: PLR0913, PLR0917  # the stores, the target, a count, one run and its dataset
    store: Store,
    traces: TraceStore,
    client: PhoenixClient,
    tally: _Tally,
    run: EvalRun,
    dataset: _Dataset,
    program: TunedProgram | None,
) -> None:
    """Make the run's experiment, post its missing runs and post every score."""
    name = f"run-{run.id}-{run.model}"
    experiment = client.find_experiment(dataset.id, name)
    existing: dict[str, str] = {}
    if experiment is None:
        experiment = client.create_experiment(
            dataset.id, name, _run_metadata(store, run, program)
        )
        tally.experiments_added += 1
    else:
        existing = {r.example_id: r.id for r in client.experiment_runs(experiment)}
    start = run.started_at
    end = run.finished_at or start
    links: dict[str, str] | None = None
    for result in store.evals.results(run.id):
        example = dataset.examples.get(result.case_key)
        if example is None:
            tally.results_skipped += 1
            continue
        run_id = existing.get(example)
        if run_id is None:
            if links is None:
                links = _trace_links(traces, run, start, end)
            run_id = client.post_run(
                experiment,
                example,
                {"answer": result.answer, "passed": result.passed},
                start,
                start + timedelta(milliseconds=result.latency_ms),
                links.get(result.case_key),
            )
            tally.runs_added += 1
        else:
            tally.runs_present += 1
        client.post_score(
            run_id,
            _SCORE_NAME,
            1.0 if result.passed else 0.0,
            "match" if result.passed else "differs",
            start,
            end,
        )
        tally.scores_sent += 1


def _trace_links(
    traces: TraceStore, run: EvalRun, start: datetime, end: datetime
) -> dict[str, str]:
    """Map each posting id the run called a model on to a trace.

    A span counts when it began inside the run's window and its task, task
    version and model match the run's; the model is compared without its
    provider prefix, as spans record it.
    """
    links: dict[str, str] = {}
    model = run.model.partition("/")[2] or run.model
    for span in traces.spans_between(_ns(start), _ns(end)):
        found = span.attributes
        if (
            found.get(attributes.TASK) == run.suite
            and found.get(attributes.TASK_VERSION) == run.task_version
            and found.get(attributes.REQUEST_MODEL) == model
        ):
            posting = found.get(attributes.POSTING_ID)
            if posting is not None:
                links.setdefault(str(posting), span.trace_id)
    return links

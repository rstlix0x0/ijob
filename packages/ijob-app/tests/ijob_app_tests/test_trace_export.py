import json
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, override

import pytest
import requests
from ijob_ai.errors import PhoenixHttpError, PhoenixUnreachableError
from ijob_ai.tracing.phoenix import PhoenixClient
from ijob_app.trace_export import ExportReport, export_all
from ijob_core.eval_records import (
    Bucket,
    EvalCase,
    EvalLabel,
    EvalResult,
    EvalRun,
    EvalSet,
    EvalTask,
    LabelOrigin,
    RunStatus,
)
from ijob_core.model import (
    ExternalId,
    PostingId,
    SightingDraft,
    SourceKind,
    SourceRef,
    parse_source_key,
)
from ijob_core.store import Store
from ijob_core.traces import SpanRow, TraceStore
from ijob_core.tune_records import SplitPart
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

_URL = "http://127.0.0.1:6006"
_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_SOURCE = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_MODEL = "made-up/model"
_MAX_CALLS = 500
_NOT_JSON = requests.exceptions.JSONDecodeError("x", "<html>", 0)
_STUCK = AssertionError("a listing was never advanced by its cursor")


class _Response:
    def __init__(self, status: int, body: object, *, html: bool = False) -> None:
        self.status_code = status
        self._body = body
        self._html = html
        self.text = "<html></html>" if html else json.dumps(body)

    def json(self) -> object:
        if self._html:
            raise _NOT_JSON
        return self._body


class FakePhoenix:
    """A Phoenix that keeps the rerun behaviour of P21 and P22.

    Spans with a known id are dropped; a dataset created twice answers 409;
    experiments may share a name; a run posted twice answers 409; an evaluation
    is an upsert.
    """

    def __init__(self) -> None:
        self.down = False
        self.export_attempts = 0
        self.session: Any = self
        self.exporter = _FakeExporter(self)
        self.reset()

    def reset(self) -> None:
        """Empty Phoenix, as after its volume was deleted."""
        self.spans: dict[tuple[int, int], ReadableSpan] = {}
        self.datasets: dict[str, dict[str, Any]] = {}
        self.experiments: dict[str, dict[str, Any]] = {}
        self.evaluations: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.params: list[tuple[str, Mapping[str, str]]] = []
        self._next = 0

    def client(self) -> PhoenixClient:
        return PhoenixClient(_URL, self.session, self.exporter)

    def _id(self) -> str:
        self._next += 1
        return f"id{self._next}"

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _Response:
        del timeout
        if self.down:
            raise requests.ConnectionError("refused")
        path = url.removeprefix(_URL)
        self.calls.append((method, path))
        self.params.append((path, params or {}))
        if len(self.calls) > _MAX_CALLS:
            raise _STUCK
        return self._route(method, path, json or {}, params or {})

    @staticmethod
    def _page(data: list[dict[str, Any]], params: Mapping[str, str]) -> _Response:
        """Answer a listing one item at a time, as a cursor walk."""
        first = int(params.get("cursor", "0"))
        more = first + 1 < len(data)
        return _Response(
            200,
            {
                "data": data[first : first + 1],
                "next_cursor": str(first + 1) if more else None,
            },
        )

    def _route(  # noqa: PLR0911  # one branch per Phoenix route
        self, method: str, path: str, body: dict[str, Any], params: Mapping[str, str]
    ) -> _Response:
        if path == "/":
            return _Response(200, None, html=True)
        if (method, path) == ("GET", "/v1/datasets"):
            data = [{"name": d["name"], "id": i} for i, d in self.datasets.items()]
            return self._page(data, params)
        if (method, path) == ("POST", "/v1/datasets/upload"):
            reply = self._upload(body)
            # Without sync=true real Phoenix queues the upload and answers null.
            return (
                reply
                if params.get("sync") == "true"
                else _Response(200, {"data": None})
            )
        if match := re.fullmatch(r"/v1/datasets/(\w+)/examples", path):
            examples = self.datasets[match[1]]["examples"]
            return _Response(200, {"data": {"examples": examples}})
        if match := re.fullmatch(r"/v1/datasets/(\w+)/experiments", path):
            dataset = match[1]
            if method == "GET":
                data = [
                    {"name": e["name"], "id": i}
                    for i, e in self.experiments.items()
                    if e["dataset"] == dataset
                ]
                return self._page(data, params)
            new = self._id()
            self.experiments[new] = {
                "dataset": dataset,
                "name": body["name"],
                "metadata": body["metadata"],
                "runs": {},
            }
            return _Response(200, {"data": {"id": new}})
        if match := re.fullmatch(r"/v1/experiments/(\w+)/runs", path):
            runs = self.experiments[match[1]]["runs"]
            if method == "GET":
                data = [
                    {"id": i, "dataset_example_id": r["dataset_example_id"]}
                    for i, r in runs.items()
                ]
                return _Response(200, {"data": data})
            if any(
                r["dataset_example_id"] == body["dataset_example_id"]
                for r in runs.values()
            ):
                return _Response(409, {"detail": "already exists"})
            new = self._id()
            runs[new] = body
            return _Response(200, {"data": {"id": new}})
        if (method, path) == ("POST", "/v1/experiment_evaluations"):
            key = (body["experiment_run_id"], body["name"])
            self.evaluations[key] = body
            return _Response(200, {"data": {"id": "e"}})
        return _Response(404, {"detail": path})

    def _upload(self, body: Mapping[str, Any]) -> _Response:
        found = [i for i, d in self.datasets.items() if d["name"] == body["name"]]
        if body["action"] == "create":
            if found:
                return _Response(409, {"detail": "dataset exists"})
            found = [self._id()]
            self.datasets[found[0]] = {"name": body["name"], "examples": []}
        dataset = self.datasets[found[0]]
        for input_, output, metadata in zip(
            body["inputs"], body["outputs"], body["metadata"], strict=True
        ):
            dataset["examples"].append(
                {
                    "id": self._id(),
                    "input": input_,
                    "output": output,
                    "metadata": metadata,
                }
            )
        return _Response(200, {"data": {"dataset_id": found[0]}})

    def dataset_named(self, name: str) -> dict[str, Any]:
        (found,) = [d for d in self.datasets.values() if d["name"] == name]
        return found

    def experiments_named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.experiments.values() if e["name"] == name]


class _FakeExporter(SpanExporter):
    def __init__(self, phoenix: FakePhoenix) -> None:
        self._phoenix = phoenix

    @override
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self._phoenix.export_attempts += 1
        if self._phoenix.down:
            return SpanExportResult.FAILURE
        for span in spans:
            context = span.get_span_context()
            assert context is not None
            self._phoenix.spans.setdefault((context.trace_id, context.span_id), span)
        return SpanExportResult.SUCCESS


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "ijob.db") as opened:
        yield opened


@pytest.fixture
def traces(tmp_path: Path) -> Iterator[TraceStore]:
    with TraceStore.open(tmp_path / "traces.db") as opened:
        yield opened


@pytest.fixture
def phoenix() -> FakePhoenix:
    return FakePhoenix()


def _postings(store: Store, count: int) -> list[PostingId]:
    """Record `count` more postings and return the ids of the new ones."""
    before = {p.id for p in store.postings(sources=[_SOURCE])}
    store.import_seeds([(_SOURCE, "Alpha")])
    number = len(before)
    store.record(
        [
            SightingDraft(
                source=_SOURCE,
                external_id=ExternalId(str(number + n)),
                title=f"Engineer {number + n}",
                company="Acme",
                location="Remote",
                url=f"https://example.com/jobs/{number + n}",
                posted_at=None,
                raw="{}",
                description=f"Build things {number + n}",
            )
            for n in range(count)
        ],
        seen_at=_AT,
    )
    return sorted(p.id for p in store.postings(sources=[_SOURCE]) if p.id not in before)


def _set(store: Store, tag: str = "a") -> EvalSet:
    return store.evals.find_or_create_set(tag * 64, "b" * 64, ["juror"], created_at=_AT)


def _label(
    store: Store,
    eval_set: EvalSet,
    ids: Sequence[PostingId],
    *,
    first_position: int = 0,
) -> None:
    for position, posting_id in enumerate(ids, start=first_position):
        store.evals.put_cases(
            eval_set.id,
            [EvalCase(eval_set.id, posting_id, position, Bucket.KEPT, "c" * 64)],
        )
        for task in EvalTask:
            store.evals.put_label(
                EvalLabel(
                    set_id=eval_set.id,
                    posting_id=posting_id,
                    task=task,
                    keep=True if task is EvalTask.JUDGE else None,
                    rule=None,
                    score=70 if task is EvalTask.SCORER else None,
                    origin=LabelOrigin.USER,
                    spot_checked=False,
                    decided_at=_AT,
                )
            )


def _run(  # noqa: PLR0913  # one run's fields
    store: Store,
    eval_set: EvalSet,
    task: EvalTask,
    ids: Sequence[PostingId],
    *,
    limited: bool = False,
    version: str = "v1",
    start: datetime = _AT,
    status: RunStatus = RunStatus.COMPLETE,
    failed: Sequence[PostingId] = (),
    latency_ms: int = 5,
    model: str = _MODEL,
) -> EvalRun:
    run = store.evals.start_run(
        task.value, model, version, eval_set.id, started_at=start, limited=limited
    )
    for posting_id in ids:
        store.evals.put_result(
            EvalResult(
                run.id,
                str(posting_id),
                passed=posting_id not in failed,
                answer="keep",
                detail="",
                latency_ms=latency_ms,
            )
        )
    store.evals.finish_run(
        run.id, status, "{}", finished_at=start + timedelta(minutes=1)
    )
    found = store.evals.run(run.id)
    assert found is not None
    return found


def _span(  # noqa: PLR0913  # keyword defaults keep call sites short
    span_id: str,
    *,
    trace_id: str,
    at: datetime,
    task: str | None = None,
    posting_id: PostingId | None = None,
    version: str = "v1",
    model: str = "model",
) -> SpanRow:
    attributes: dict[str, object] = {
        "ijob.task_version": version,
        "gen_ai.request.model": model,
    }
    if task is not None:
        attributes["ijob.task"] = task
    if posting_id is not None:
        attributes["ijob.posting_id"] = str(posting_id)
    start_ns = int(at.timestamp()) * 1_000_000_000
    return SpanRow(
        trace_id, span_id, None, "chat", start_ns, start_ns + 10, "ok", attributes
    )


def _export(store: Store, traces: TraceStore, phoenix: FakePhoenix) -> ExportReport:
    return export_all(store, traces, phoenix.client())


def _tid(n: int) -> str:
    return f"{n:032x}"


def test_dataset_is_created_with_split_parts(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 3)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    store.tunes.put_split(
        eval_set.id,
        {ids[0]: SplitPart.TRAIN, ids[1]: SplitPart.VAL, ids[2]: SplitPart.HELD_OUT},
    )

    report = _export(store, traces, phoenix)

    dataset = phoenix.dataset_named(f"ijob-judge-set-{eval_set.id}")
    assert [e["input"]["posting_id"] for e in dataset["examples"]] == [
        str(i) for i in ids
    ]
    assert dataset["examples"][0]["input"]["title"] == "Engineer 0"
    assert dataset["examples"][0]["output"] == {"keep": True, "rule": None}
    assert [e["metadata"]["split_part"] for e in dataset["examples"]] == [
        "train",
        "val",
        "held_out",
    ]
    assert dataset["examples"][0]["metadata"]["bucket"] == "kept"
    assert dataset["examples"][0]["metadata"]["origin"] == "user"
    scorer = phoenix.dataset_named(f"ijob-scorer-set-{eval_set.id}")
    assert scorer["examples"][0]["output"] == {"score": 70}
    assert report.datasets_added == 2
    assert report.examples_added == 6


def test_dataset_is_found_and_appended(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    first = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, first)
    _export(store, traces, phoenix)
    name = f"ijob-judge-set-{eval_set.id}"
    dataset_id = next(i for i, d in phoenix.datasets.items() if d["name"] == name)

    more = _postings(store, 2)
    _label(store, eval_set, more, first_position=2)
    report = _export(store, traces, phoenix)

    assert [i for i, d in phoenix.datasets.items() if d["name"] == name] == [dataset_id]
    assert len(phoenix.datasets[dataset_id]["examples"]) == 4
    assert report.datasets_added == 0
    assert report.examples_added == 4  # two per task
    assert report.examples_present == 4


def test_experiment_is_found_by_name_not_duplicated(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    run = _run(store, eval_set, EvalTask.JUDGE, ids)

    _export(store, traces, phoenix)
    _export(store, traces, phoenix)

    assert len(phoenix.experiments_named(f"run-{run.id}-{_MODEL}")) == 1


def test_only_missing_runs_are_posted(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 3)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    run = _run(store, eval_set, EvalTask.JUDGE, ids)
    _export(store, traces, phoenix)
    (experiment,) = phoenix.experiments_named(f"run-{run.id}-{_MODEL}")
    del experiment["runs"][next(iter(experiment["runs"]))]

    report = _export(store, traces, phoenix)

    assert len(experiment["runs"]) == 3
    assert report.runs_added == 1
    assert report.runs_present == 2


def test_a_stop_between_run_and_score_heals(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    _run(store, eval_set, EvalTask.JUDGE, ids)
    _export(store, traces, phoenix)
    phoenix.evaluations.clear()

    _export(store, traces, phoenix)

    assert len(phoenix.evaluations) == 2
    body = next(iter(phoenix.evaluations.values()))
    assert body["name"] == "matches_label"
    assert body["annotator_kind"] == "CODE"
    assert body["result"]["score"] == 1.0


def test_second_export_adds_nothing(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    _run(store, eval_set, EvalTask.JUDGE, ids)
    _run(store, eval_set, EvalTask.SCORER, ids)
    traces.insert([_span("a" * 16, trace_id=_tid(1), at=_AT)])
    first = _export(store, traces, phoenix)

    second = _export(store, traces, phoenix)

    assert first.runs_added == 4
    assert first.spans_sent == second.spans_sent == 1
    assert len(phoenix.spans) == 1
    assert (
        second.datasets_added,
        second.examples_added,
        second.experiments_added,
        second.runs_added,
    ) == (0, 0, 0, 0)
    assert second.runs_present == 4
    assert second.scores_sent == 4


def test_deleted_volume_refills(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    _run(store, eval_set, EvalTask.JUDGE, ids)
    traces.insert([_span("a" * 16, trace_id=_tid(1), at=_AT)])
    first = _export(store, traces, phoenix)
    phoenix.reset()

    again = _export(store, traces, phoenix)

    assert again.datasets_added == first.datasets_added == 2
    assert again.runs_added == first.runs_added == 2
    assert len(phoenix.spans) == 1
    assert len(phoenix.evaluations) == 2


def test_plain_limited_runs_are_left_out(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    store.tunes.save_program(
        task=EvalTask.JUDGE,
        model=_MODEL,
        optimizer="gepa",
        base_version="v1",
        set_id=eval_set.id,
        bound_sha256="a" * 64,
        instructions="Be strict.",
        demos_json="[]",
        version="v1+other",
        tune_run_id=None,
        created_at=_AT,
    )
    _run(store, eval_set, EvalTask.JUDGE, ids, limited=True)
    _run(store, eval_set, EvalTask.JUDGE, ids, status=RunStatus.INCOMPLETE)

    report = _export(store, traces, phoenix)

    assert phoenix.experiments == {}
    assert report.experiments_added == 0


def test_a_stopped_phoenix_sends_nothing(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    traces.insert([_span("a" * 16, trace_id=_tid(1), at=_AT)])
    phoenix.down = True

    with pytest.raises(PhoenixUnreachableError):
        _export(store, traces, phoenix)

    assert phoenix.export_attempts == 0
    assert phoenix.calls == []


def test_a_refused_rest_call_raises_its_status(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 1)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    # A dataset the finder cannot see, so the create hits the 409 of P22.
    phoenix.datasets["x"] = {"name": f"ijob-judge-set-{eval_set.id}", "examples": []}
    phoenix.session = _Blind(phoenix)

    with pytest.raises(PhoenixHttpError) as raised:
        export_all(store, traces, phoenix.client())

    assert raised.value.status == 409


class _Blind:
    """Wraps the fake so dataset listings come back empty."""

    def __init__(self, inner: FakePhoenix) -> None:
        self._inner = inner

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _Response:
        if method == "GET" and url.endswith("/v1/datasets"):
            return _Response(200, {"data": [], "next_cursor": None})
        return self._inner.request(
            method, url, params=params, json=json, timeout=timeout
        )


def test_stored_runs_of_any_size_are_exported(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 4)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    run = _run(store, eval_set, EvalTask.JUDGE, ids[:2])

    _export(store, traces, phoenix)

    (experiment,) = phoenix.experiments_named(f"run-{run.id}-{_MODEL}")
    assert len(experiment["runs"]) == 2


def test_older_sets_get_their_own_dataset(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    old, new = _set(store, "a"), _set(store, "d")
    _label(store, old, ids)
    _label(store, new, ids)
    old_run = _run(store, old, EvalTask.JUDGE, ids)
    new_run = _run(store, new, EvalTask.JUDGE, ids)

    _export(store, traces, phoenix)

    names = {d["name"] for d in phoenix.datasets.values()}
    assert {f"ijob-judge-set-{old.id}", f"ijob-judge-set-{new.id}"} <= names
    for run, eval_set in ((old_run, old), (new_run, new)):
        (experiment,) = phoenix.experiments_named(f"run-{run.id}-{_MODEL}")
        dataset = phoenix.datasets[experiment["dataset"]]
        assert dataset["name"] == f"ijob-judge-set-{eval_set.id}"


def test_held_out_experiment_holds_tuning_metadata(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    tune = store.evals.start_run("tune", _MODEL, "judge-1", eval_set.id, started_at=_AT)
    store.evals.record_spend(tune.id, spent_usd=1.25, subscription_usd=0.0)
    tune_metrics = {
        "task": "judge",
        "optimizer": "gepa",
        "calls": 40,
        "failures": 1,
        "unusable": 0,
        "baseline_val": 0.5,
        "best_val": 0.75,
        "no_change": False,
        "program_id": 1,
    }
    store.evals.finish_run(
        tune.id, RunStatus.COMPLETE, json.dumps(tune_metrics), finished_at=_AT
    )
    program = store.tunes.save_program(
        task=EvalTask.JUDGE,
        model=_MODEL,
        optimizer="gepa",
        base_version="judge-1",
        set_id=eval_set.id,
        bound_sha256="a" * 64,
        instructions="Be strict.",
        demos_json="[]",
        version="judge-1+abc123",
        tune_run_id=tune.id,
        created_at=_AT,
    )
    run = _run(
        store, eval_set, EvalTask.JUDGE, ids, limited=True, version=program.version
    )

    _export(store, traces, phoenix)

    (experiment,) = phoenix.experiments_named(f"run-{run.id}-{_MODEL}")
    metadata = experiment["metadata"]
    assert metadata["optimizer"] == "gepa"
    assert metadata["tune_run"] == tune.id
    assert metadata["baseline_val"] == 0.5
    assert metadata["best_val"] == 0.75
    assert metadata["tune_spent_usd"] == 1.25
    assert metadata["task_version"] == "judge-1+abc123"
    assert metadata["held_out"] is True


def test_run_links_to_its_span(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    run = _run(store, eval_set, EvalTask.JUDGE, ids)
    inside = _AT + timedelta(seconds=10)
    traces.insert(
        [
            _span(
                "1" * 16, trace_id=_tid(1), at=inside, task="judge", posting_id=ids[0]
            ),
            _span(
                "2" * 16, trace_id=_tid(2), at=inside, task="scorer", posting_id=ids[1]
            ),
            _span(
                "3" * 16,
                trace_id=_tid(3),
                at=_AT + timedelta(hours=1),
                task="judge",
                posting_id=ids[1],
            ),
        ]
    )

    _export(store, traces, phoenix)

    (experiment,) = phoenix.experiments_named(f"run-{run.id}-{_MODEL}")
    by_example = {
        phoenix.dataset_named(f"ijob-judge-set-{eval_set.id}")["examples"][n]["id"]: n
        for n in range(2)
    }
    posted = {
        by_example[body["dataset_example_id"]]: body.get("trace_id")
        for body in experiment["runs"].values()
    }
    assert posted == {0: _tid(1), 1: None}


def test_run_without_a_span_is_still_posted(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 1)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    run = _run(store, eval_set, EvalTask.JUDGE, ids)

    report = _export(store, traces, phoenix)

    (experiment,) = phoenix.experiments_named(f"run-{run.id}-{_MODEL}")
    (body,) = experiment["runs"].values()
    assert "trace_id" not in body
    assert report.runs_added == 1


def _program(store: Store, eval_set: EvalSet, bound: str, *, tune_run_id: int) -> int:
    return store.tunes.save_program(
        task=EvalTask.JUDGE,
        model=_MODEL,
        optimizer="gepa",
        base_version="judge-1",
        set_id=eval_set.id,
        bound_sha256=bound * 64,
        instructions="Be strict.",
        demos_json="[]",
        version="judge-1+same",
        tune_run_id=tune_run_id,
        created_at=_AT,
    ).id


def _tune_run(store: Store, eval_set: EvalSet, spent: float) -> int:
    tune = store.evals.start_run("tune", _MODEL, "judge-1", eval_set.id, started_at=_AT)
    store.evals.record_spend(tune.id, spent_usd=spent, subscription_usd=0.0)
    store.evals.finish_run(tune.id, RunStatus.COMPLETE, "{}", finished_at=_AT)
    return tune.id


def test_a_failed_case_scores_zero_and_differs(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    _run(store, eval_set, EvalTask.JUDGE, ids, failed=[ids[0]])

    _export(store, traces, phoenix)

    results = sorted(
        (e["result"]["score"], e["result"]["label"])
        for e in phoenix.evaluations.values()
    )
    assert results == [(0.0, "differs"), (1.0, "match")]


def test_a_full_run_with_no_dataset_is_counted_not_dropped(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)  # no labels, so no dataset
    _run(store, eval_set, EvalTask.JUDGE, ids)
    _tune_run(store, eval_set, 1.0)  # a tune run is left out on purpose

    report = _export(store, traces, phoenix)

    assert report.runs_skipped == 1
    assert report.experiments_added == 0


def test_a_result_with_no_example_is_counted_not_dropped(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 3)
    eval_set = _set(store)
    _label(store, eval_set, ids[:2])
    _run(store, eval_set, EvalTask.JUDGE, ids)

    report = _export(store, traces, phoenix)

    assert (report.runs_added, report.results_skipped) == (2, 1)
    assert report.runs_skipped == 0


def test_an_example_made_in_phoenix_is_ignored(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 2)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    _run(store, eval_set, EvalTask.JUDGE, ids)
    foreign = {"id": "foreign", "input": {"note": "by hand"}, "output": {}}
    phoenix.datasets["x"] = {
        "name": f"ijob-judge-set-{eval_set.id}",
        "examples": [foreign],
    }

    first = _export(store, traces, phoenix)
    second = _export(store, traces, phoenix)

    assert first.examples_added == 4
    assert first.runs_added == 2
    assert second.examples_added == 0


def test_a_run_lasts_as_long_as_its_case(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 1)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    run = _run(store, eval_set, EvalTask.JUDGE, ids, latency_ms=250)

    _export(store, traces, phoenix)

    (experiment,) = phoenix.experiments_named(f"run-{run.id}-{_MODEL}")
    (body,) = experiment["runs"].values()
    began = datetime.fromisoformat(body["start_time"])
    assert began == _AT
    assert datetime.fromisoformat(body["end_time"]) - began == timedelta(
        milliseconds=250
    )


def test_a_trace_of_another_model_or_version_is_not_linked(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 1)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    run = _run(store, eval_set, EvalTask.JUDGE, ids)
    at = _AT + timedelta(seconds=10)
    posting = ids[0]
    traces.insert(
        [
            _span(
                "1" * 16,
                trace_id=_tid(1),
                at=at,
                model="other",
                task="judge",
                posting_id=posting,
            ),
            _span(
                "2" * 16,
                trace_id=_tid(2),
                at=at,
                version="v9",
                task="judge",
                posting_id=posting,
            ),
            _span(
                "3" * 16,
                trace_id=_tid(3),
                at=at + timedelta(seconds=1),
                task="judge",
                posting_id=posting,
            ),
        ]
    )

    _export(store, traces, phoenix)

    (experiment,) = phoenix.experiments_named(f"run-{run.id}-{_MODEL}")
    (body,) = experiment["runs"].values()
    assert body["trace_id"] == _tid(3)


def test_a_repeated_version_finds_the_program_of_the_runs_own_set(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 1)
    first, second = _set(store, "a"), _set(store, "d")
    _label(store, first, ids)
    _label(store, second, ids)
    one = _program(store, first, "1", tune_run_id=_tune_run(store, first, 1.0))
    two = _program(store, second, "2", tune_run_id=_tune_run(store, second, 9.0))
    run = _run(store, second, EvalTask.JUDGE, ids, limited=True, version="judge-1+same")

    _export(store, traces, phoenix)

    (experiment,) = phoenix.experiments_named(f"run-{run.id}-{_MODEL}")
    assert experiment["metadata"]["program_id"] == two != one
    assert experiment["metadata"]["tune_spent_usd"] == 9.0


def test_listings_are_read_page_by_page(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 1)
    eval_set = _set(store)
    _label(store, eval_set, ids)
    run = _run(store, eval_set, EvalTask.JUDGE, ids)
    _export(store, traces, phoenix)

    again = _export(store, traces, phoenix)

    assert again.datasets_added == again.experiments_added == 0
    assert len(phoenix.experiments_named(f"run-{run.id}-{_MODEL}")) == 1
    cursors = [p.get("cursor") for path, p in phoenix.params if path == "/v1/datasets"]
    assert "1" in cursors


def test_uploads_ask_phoenix_to_wait(
    store: Store, traces: TraceStore, phoenix: FakePhoenix
) -> None:
    ids = _postings(store, 1)
    _label(store, _set(store), ids)

    _export(store, traces, phoenix)

    uploads = [p for path, p in phoenix.params if path == "/v1/datasets/upload"]
    assert uploads
    assert all(p == {"sync": "true"} for p in uploads)

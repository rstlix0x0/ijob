import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ijob_ai.errors import CostUnknownError, ProviderFailedError
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import EvalSettings
from ijob_app.eval_ops import (
    EvalNeedsError,
    LabelChange,
    LabelPlan,
    LabelReport,
    ReviewItem,
    TaskTally,
    TooFewCasesError,
    derive_labels,
    label_operation,
    prepare_label_run,
    record_review,
    review_items,
)
from ijob_app.eval_spend import EvalBudgetSpentError, EvalSpend
from ijob_app.home import IjobHome
from ijob_app.llm_ops import ItemOutcome, ItemStatus, LlmOptions, LlmRuntime
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import (
    Bucket,
    EvalLabel,
    EvalSet,
    EvalTask,
    EvalVote,
    LabelOrigin,
    RunStatus,
)
from ijob_core.eval_rules import ReviewReason
from ijob_core.model import (
    ExternalId,
    Instructions,
    Posting,
    PostingId,
    ResumeProfile,
    Seniority,
    SightingDraft,
    SourceKind,
    SourceRef,
    StoredCv,
    Verdict,
    parse_source_key,
)
from ijob_core.store import Store

from .eval_fakes import (
    JurorEngine,
    JurorEngines,
    openrouter_reply,
    serve_openrouter,
    serve_openrouter_by_kind,
)
from .llm_fakes import (
    JUNK,
    MADE_UP_CLAUDE,
    score_answer,
    verdict_answer,
    write_fake_cli,
    write_llm_config,
)

type _MakeRuntime = Callable[[JurorEngines], LlmRuntime]

_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_SOURCE = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_CV_SHA = "a" * 64
_RULES = ("no crypto", "must be remote", "no on-call")
_A = ModelRef(Provider.CLAUDE_SDK, "juror-a")
_B = ModelRef(Provider.CLAUDE_SDK, "juror-b")
_C = ModelRef(Provider.CLAUDE_SDK, "juror-c")
_ROUTED = ModelRef(Provider.OPENROUTER, "made-up/juror-c")
_ROUTED_COST = 0.06
_WIDE = 4


def _now() -> datetime:
    return _AT


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "ijob.db") as opened:
        yield opened


@pytest.fixture
def make_runtime(home: IjobHome, tmp_path: Path) -> _MakeRuntime:
    """Build a one-call-at-a-time runtime over the given juror engines."""

    def make(engines: JurorEngines) -> LlmRuntime:
        cli = write_fake_cli(tmp_path / "bin")
        write_llm_config(
            home.config, judge=MADE_UP_CLAUDE, concurrency=1, cli_path=cli.path
        )
        options = LlmOptions(config_path=home.config, engines=engines)
        return LlmRuntime.create(home, options)

    return make


@pytest.fixture
def make_wide_runtime(home: IjobHome, tmp_path: Path) -> _MakeRuntime:
    """Build a runtime that runs four calls at a time, the default concurrency."""

    def make(engines: JurorEngines) -> LlmRuntime:
        cli = write_fake_cli(tmp_path / "bin")
        write_llm_config(
            home.config, judge=MADE_UP_CLAUDE, concurrency=_WIDE, cli_path=cli.path
        )
        options = LlmOptions(config_path=home.config, engines=engines)
        return LlmRuntime.create(home, options)

    return make


def _seed(
    store: Store, count: int = 30, *, low_confidence: tuple[int, ...] = ()
) -> list[Posting]:
    store.import_seeds([(_SOURCE, "Alpha")])
    store.record(
        [
            SightingDraft(
                source=_SOURCE,
                external_id=ExternalId(str(number)),
                title=f"Engineer {number}",
                company="Acme",
                location="Remote",
                url=f"https://example.com/jobs/{number}",
                posted_at=None,
                raw="{}",
                low_confidence=number in low_confidence,
                description=None if number == 0 else f"Build things {number}",
            )
            for number in range(count)
        ],
        seen_at=_AT,
    )
    return sorted(store.postings(sources=[_SOURCE]), key=lambda posting: posting.id)


def _store_rules(store: Store, rules: tuple[str, ...] = _RULES) -> Instructions:
    instructions = Instructions(
        text="Remote only, no crypto",
        filters_json="{}",
        rules=rules,
        model=MADE_UP_CLAUDE,
        task_version="instructions-1",
        set_at=_AT,
    )
    store.set_instructions(instructions)
    return instructions


def _profile(summary: str = "Backend engineer") -> ResumeProfile:
    return ResumeProfile(
        summary=summary,
        years_experience=7,
        seniority=Seniority.SENIOR,
        roles=("Backend Engineer",),
        spoken_languages=("English",),
        locations=("Jakarta",),
        work_authorization=("Indonesia",),
        skills=("Python",),
    )


def _store_profile(
    store: Store, summary: str = "Backend engineer", cv_sha: str = _CV_SHA
) -> None:
    store.set_cv(StoredCv(name="cv.pdf", sha256=_CV_SHA, size=1, stored_at=_AT))
    store.set_cv_profile(
        _profile(summary),
        cv_sha256=cv_sha,
        model=MADE_UP_CLAUDE,
        task_version="cv_profile-1",
        made_at=_AT,
    )


def _ready(store: Store, count: int = 30) -> list[Posting]:
    postings = _seed(store, count)
    _store_rules(store)
    _store_profile(store)
    return postings


def _settings(
    *jury: ModelRef, max_cost: float = 4, total_cost: float = 10
) -> EvalSettings:
    return EvalSettings(jury=jury, max_cost_usd=max_cost, total_cost_usd=total_cost)


def _label(
    runtime: LlmRuntime,
    store: Store,
    settings: EvalSettings,
    *,
    limit: int | None = None,
    on_outcome: Callable[[ItemOutcome[object, object]], None] | None = None,
) -> LabelReport:
    plan = prepare_label_run(store, settings, limit=limit, now=_now)
    spend = EvalSpend(store.evals, settings)
    return asyncio.run(
        label_operation(runtime, store, plan, spend, now=_now, on_outcome=on_outcome)
    )


def _routed_replies(cases: int) -> list[object]:
    """Script one OpenRouter juror's replies: a judge then a scorer per case."""
    replies: list[object] = []
    for _ in range(cases):
        replies.append(openrouter_reply(verdict_answer(), cost=_ROUTED_COST))
        replies.append(openrouter_reply(score_answer(70), cost=_ROUTED_COST))
    return replies


def test_needs_rules_and_profile(store: Store, make_runtime: _MakeRuntime) -> None:
    engines = JurorEngines()
    runtime = make_runtime(engines)
    _seed(store)

    with pytest.raises(EvalNeedsError, match=r"ijob instructions set"):
        _label(runtime, store, _settings(_A, _B))
    _store_rules(store)
    with pytest.raises(EvalNeedsError, match=r"ijob cv profile"):
        _label(runtime, store, _settings(_A, _B))

    assert engines.calls == []
    assert store.evals.sets() == []
    assert store.evals.runs() == []


def test_rules_without_a_rule_need_instructions(store: Store) -> None:
    _seed(store)
    _store_rules(store, rules=())
    _store_profile(store)

    with pytest.raises(EvalNeedsError, match=r"ijob instructions set"):
        prepare_label_run(store, _settings(_A, _B), now=_now)


def test_absent_jury_names_the_setting(store: Store) -> None:
    _ready(store)

    with pytest.raises(InvalidValueError, match=r"eval\.jury"):
        prepare_label_run(store, EvalSettings(), now=_now)


def test_below_ten_cases_refuses(store: Store) -> None:
    _ready(store, count=9)

    with pytest.raises(TooFewCasesError, match="9"):
        prepare_label_run(store, _settings(_A, _B), now=_now)

    assert store.evals.sets() == []


def test_limit_out_of_range_raises_before_a_set_is_made(store: Store) -> None:
    _ready(store)

    with pytest.raises(InvalidValueError, match="limit"):
        prepare_label_run(store, _settings(_A, _B), limit=101, now=_now)

    assert store.evals.sets() == []


def test_draw_is_stored_once_per_rules_profile_and_jury(store: Store) -> None:
    _ready(store)

    first = prepare_label_run(store, _settings(_B, _A), now=_now)
    again = prepare_label_run(store, _settings(_A, _B), now=_now)

    assert again.eval_set == first.eval_set
    assert first.eval_set.jury == (str(_A), str(_B))
    assert len(store.evals.cases(first.eval_set.id)) == 30
    assert len(store.evals.sets()) == 1
    assert first.changed == ()


def test_posting_without_description_stays_in_its_pool(store: Store) -> None:
    postings = _seed(store, 30, low_confidence=(1,))
    instructions = _store_rules(store)
    _store_profile(store)
    bare, uncertain = postings[0], postings[1]
    assert bare.description is None
    assert uncertain.low_confidence
    for posting, keep in ((bare, True), (uncertain, False)):
        store.put_verdict(
            posting.id,
            "x" * 64,
            instructions.rules_sha256,
            "m",
            "judge-1",
            Verdict(keep=keep, reason="r", rule=None if keep else 1),
            made_at=_AT,
        )

    plan = prepare_label_run(store, _settings(_A, _B), now=_now)

    buckets = {
        case.posting_id: case.bucket for case in store.evals.cases(plan.eval_set.id)
    }
    assert buckets[bare.id] is Bucket.KEPT
    assert buckets[uncertain.id] is Bucket.REJECTED


def test_run_does_not_build_a_profile(store: Store, make_runtime: _MakeRuntime) -> None:
    engines = JurorEngines()
    runtime = make_runtime(engines)
    _ready(store)
    store.set_cv(StoredCv(name="cv.pdf", sha256="b" * 64, size=1, stored_at=_AT))
    before = store.cv_profile()

    with pytest.raises(EvalNeedsError, match=r"ijob cv profile"):
        _label(runtime, store, _settings(_A, _B))

    assert engines.calls == []
    assert store.cv_profile() == before


def test_run_records_a_jury_row(store: Store, make_runtime: _MakeRuntime) -> None:
    _ready(store)
    runtime = make_runtime(JurorEngines())

    report = _label(runtime, store, _settings(_A, _B), limit=10)

    run = store.evals.run(report.run_id)
    assert run is not None
    assert run.suite == "jury"
    assert run.status is RunStatus.COMPLETE
    assert run.limited
    assert run.task_version == "judge-2+scorer-1"
    assert run.set_id is not None
    assert run.finished_at is not None
    metrics = json.loads(run.metrics)
    assert metrics["requested"] == metrics["stored"] == 10 * 2 * 2
    assert report.requested == report.stored == 40


def test_changed_posting_gets_no_new_vote(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    postings = _ready(store)
    engines = JurorEngines()
    runtime = make_runtime(engines)
    plan = prepare_label_run(store, _settings(_A, _B), now=_now)
    changed = [case.posting_id for case in store.evals.cases(plan.eval_set.id)][:2]
    for posting_id in changed:
        store.set_description(posting_id, "rewritten after the draw")

    report = _label(runtime, store, _settings(_A, _B))

    voted = {vote.posting_id for vote in store.evals.votes(plan.eval_set.id)}
    assert voted.isdisjoint(changed)
    assert report.skipped == 2
    assert report.requested == (len(postings) - 2) * 2 * 2
    assert engines.engine("juror-a").calls.count("judge") == len(postings) - 2


def test_rerun_requests_only_missing_votes(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    _ready(store)
    engines = JurorEngines()
    runtime = make_runtime(engines)
    settings = _settings(_A, _B)

    first = _label(runtime, store, settings, limit=10)
    second = _label(runtime, store, settings)
    third = _label(runtime, store, settings)

    assert (first.requested, second.requested, third.requested) == (40, 80, 0)
    assert engines.engine("juror-a").calls.count("judge") == 30
    assert engines.engine("juror-a").calls.count("scorer") == 30
    assert len(store.evals.votes(first.set_id)) == 120


@pytest.mark.parametrize("what", [LabelChange.RULES, LabelChange.CV, LabelChange.JURY])
def test_changed_rules_cv_or_jury_reports_what_changed(
    store: Store, make_runtime: _MakeRuntime, what: LabelChange
) -> None:
    _ready(store)
    runtime = make_runtime(JurorEngines())
    first = _label(runtime, store, _settings(_A, _B), limit=10)
    jury = [_A, _B]
    if what is LabelChange.RULES:
        _store_rules(store, rules=("only rust",))
    elif what is LabelChange.CV:
        _store_profile(store, summary="Rust engineer")
    else:
        jury.append(_C)

    plan = prepare_label_run(store, _settings(*jury), now=_now)

    assert plan.changed == (what,)
    assert plan.eval_set.id != first.set_id
    assert len(store.evals.sets()) == 2
    assert len(plan.items) == 30 * 2 * len(jury)


def test_unchanged_inputs_report_no_change(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    _ready(store)
    runtime = make_runtime(JurorEngines())
    _label(runtime, store, _settings(_A, _B), limit=10)

    plan = prepare_label_run(store, _settings(_A, _B), now=_now)

    assert plan.changed == ()


def test_declined_change_is_reported_again(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    _ready(store)
    runtime = make_runtime(JurorEngines())
    _label(runtime, store, _settings(_A, _B), limit=10)
    _store_rules(store, rules=("only rust",))

    prepare_label_run(store, _settings(_A, _B), now=_now)
    again = prepare_label_run(store, _settings(_A, _B), now=_now)

    assert again.changed == (LabelChange.RULES,)


def test_vote_is_stored_when_the_run_is_cancelled(
    store: Store, make_runtime: _MakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)
    serve_openrouter(monkeypatch, _routed_replies(10))
    runtime = make_runtime(JurorEngines())
    settings = _settings(_A, _ROUTED)
    plan = prepare_label_run(store, settings, limit=10, now=_now)
    spend = EvalSpend(store.evals, settings)

    async def main() -> None:
        seen: list[ItemOutcome[object, object]] = []

        def stop_after_four(outcome: ItemOutcome[object, object]) -> None:
            seen.append(outcome)
            if len(seen) == 4:
                task.cancel()

        task = asyncio.create_task(
            label_operation(
                runtime, store, plan, spend, now=_now, on_outcome=stop_after_four
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    votes = store.evals.votes(plan.eval_set.id)
    assert len(votes) >= 4
    assert len(store.evals.labels(plan.eval_set.id)) >= 2


def test_jury_run_row_keeps_spend_on_cancel(
    store: Store, make_runtime: _MakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)
    serve_openrouter(monkeypatch, _routed_replies(10))
    runtime = make_runtime(JurorEngines())
    settings = _settings(_A, _ROUTED)
    plan = prepare_label_run(store, settings, limit=10, now=_now)
    spend = EvalSpend(store.evals, settings)

    async def main() -> None:
        seen: list[ItemOutcome[object, object]] = []

        def stop_after_four(outcome: ItemOutcome[object, object]) -> None:
            seen.append(outcome)
            if len(seen) == 4:
                task.cancel()

        task = asyncio.create_task(
            label_operation(
                runtime, store, plan, spend, now=_now, on_outcome=stop_after_four
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    votes = store.evals.votes(plan.eval_set.id)
    routed = sum(1 for vote in votes if vote.juror == str(_ROUTED))
    claude = len(votes) - routed
    run = store.evals.runs()[0]
    assert run.status is RunStatus.INCOMPLETE
    assert run.finished_at is not None
    assert run.spent_usd == pytest.approx(routed * _ROUTED_COST)
    assert run.subscription_usd == pytest.approx(claude * 0.001)
    assert routed >= 2


def test_provider_failure_stops_one_juror(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    _ready(store)
    engines = JurorEngines({"juror-b": JurorEngine(raises=ProviderFailedError("boom"))})
    runtime = make_runtime(engines)

    report = _label(runtime, store, _settings(_A, _B, _C), limit=10)

    assert engines.engine("juror-b").calls == ["judge"]
    assert engines.engine("juror-a").calls.count("judge") == 10
    assert engines.engine("juror-c").calls.count("scorer") == 10
    assert [(stop.juror, stop.reason) for stop in report.stopped_jurors] == [
        (str(_B), "boom")
    ]
    assert report.operation.stopped_by is None
    assert report.stored == 40
    assert {vote.juror for vote in store.evals.votes(report.set_id)} == {
        str(_A),
        str(_C),
    }
    run = store.evals.run(report.run_id)
    assert run is not None
    assert run.status is RunStatus.INCOMPLETE


def test_stopped_juror_makes_no_new_call_at_default_concurrency(
    store: Store, make_wide_runtime: _MakeRuntime
) -> None:
    _ready(store)
    engines = JurorEngines(
        {"juror-b": JurorEngine(raises=ProviderFailedError("boom"), delay_s=0.05)}
    )
    runtime = make_wide_runtime(engines)

    report = _label(runtime, store, _settings(_A, _B, _C), limit=10)

    # only the calls already running when the failure came may have been made
    assert len(engines.engine("juror-b").calls) <= _WIDE
    assert engines.engine("juror-a").calls.count("judge") == 10
    assert engines.engine("juror-c").calls.count("scorer") == 10
    assert [stop.juror for stop in report.stopped_jurors] == [str(_B)]
    assert report.stored == 40
    assert {vote.juror for vote in store.evals.votes(report.set_id)} == {
        str(_A),
        str(_C),
    }


def test_cap_starts_no_new_item_at_default_concurrency(
    store: Store, make_wide_runtime: _MakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)
    transport = serve_openrouter_by_kind(
        monkeypatch,
        judge=verdict_answer(),
        scorer=score_answer(70),
        cost=_ROUTED_COST,
    )
    runtime = make_wide_runtime(JurorEngines())

    report = _label(runtime, store, _settings(_A, _ROUTED, max_cost=0.1))

    started = len(transport.requests)
    # two calls reach the cap; the ones running beside them may still finish
    assert 2 <= started <= 2 + _WIDE - 1
    assert report.operation.stopped_by == "cap"
    assert report.operation.not_done > 0
    run = store.evals.run(report.run_id)
    assert run is not None
    assert run.spent_usd == pytest.approx(started * _ROUTED_COST)
    assert run.spent_usd >= 0.1
    routed = [v for v in store.evals.votes(report.set_id) if v.juror == str(_ROUTED)]
    assert len(routed) == started


def test_unusable_answer_leaves_no_vote(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    _ready(store)
    engines = JurorEngines({"juror-a": JurorEngine(judge=[JUNK, verdict_answer()])})
    runtime = make_runtime(engines)
    settings = _settings(_A, _B)

    first = _label(runtime, store, settings, limit=10)
    gaps = prepare_label_run(store, settings, limit=10, now=_now)
    second = _label(runtime, store, settings, limit=10)

    assert first.failed == 1
    assert first.stored == 39
    assert [(item.juror, item.task) for item in gaps.items] == [(_A, EvalTask.JUDGE)]
    assert second.stored == 1
    assert len(store.evals.votes(first.set_id)) == 40


def test_cost_unknown_stops_the_run(store: Store, make_runtime: _MakeRuntime) -> None:
    _ready(store)
    engines = JurorEngines({"juror-a": JurorEngine(cost=None)})
    runtime = make_runtime(engines)

    report = _label(runtime, store, _settings(_A, _B), limit=10)

    assert report.operation.stopped_by == "cost_unknown"
    assert engines.engine("juror-a").calls == ["judge"]
    assert engines.engine("juror-b").calls == []
    assert report.stored == 0
    assert isinstance(report.cost_unknown, CostUnknownError)
    assert report.cost_unknown.model == str(_A)
    run = store.evals.run(report.run_id)
    assert run is not None
    assert run.status is RunStatus.INCOMPLETE


def test_cap_stops_new_items(
    store: Store, make_runtime: _MakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)
    serve_openrouter(monkeypatch, _routed_replies(3))
    runtime = make_runtime(JurorEngines())

    report = _label(runtime, store, _settings(_A, _ROUTED, max_cost=0.1))

    assert report.operation.stopped_by == "cap"
    assert report.operation.cap == pytest.approx(0.1)
    assert report.stored == 4
    assert len(store.evals.votes(report.set_id)) == 4
    run = store.evals.run(report.run_id)
    assert run is not None
    assert run.status is RunStatus.INCOMPLETE
    assert run.spent_usd == pytest.approx(2 * _ROUTED_COST)


def test_subscription_cost_leaves_the_cap_alone(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    _ready(store)
    engines = JurorEngines(
        {
            "juror-a": JurorEngine(cost=5.0),
            "juror-b": JurorEngine(cost=5.0),
        }
    )
    runtime = make_runtime(engines)

    report = _label(runtime, store, _settings(_A, _B, max_cost=0.1), limit=10)

    assert report.operation.stopped_by is None
    assert report.stored == 40
    assert report.subscription_spent == pytest.approx(40 * 5.0)
    run = store.evals.run(report.run_id)
    assert run is not None
    assert run.status is RunStatus.COMPLETE
    assert run.spent_usd == 0
    assert run.subscription_usd == pytest.approx(200.0)


def test_labels_derived_after_run(store: Store, make_runtime: _MakeRuntime) -> None:
    _ready(store)
    engines = JurorEngines(
        {
            "juror-a": JurorEngine(scorer=[score_answer(70)]),
            "juror-b": JurorEngine(scorer=[score_answer(60)]),
            "juror-c": JurorEngine(
                judge=[verdict_answer(keep=False, rule=1), verdict_answer()],
                scorer=[score_answer(20), score_answer(65)],
            ),
        }
    )
    runtime = make_runtime(engines)

    report = _label(runtime, store, _settings(_A, _B, _C), limit=10)

    labels = store.evals.labels(report.set_id)
    judge = [label for label in labels if label.task is EvalTask.JUDGE]
    scorer = [label for label in labels if label.task is EvalTask.SCORER]
    assert len(judge) == len(scorer) == 9
    assert all(
        label.keep is True and label.origin is LabelOrigin.AGREED for label in judge
    )
    assert all(label.score == 65 for label in scorer)
    assert report.tally.judge == TaskTally(agreed=9, review=1)
    assert report.tally.scorer == TaskTally(agreed=9, review=1)
    first_case = store.evals.cases(report.set_id)[0].posting_id
    assert first_case not in {label.posting_id for label in labels}


def test_reject_without_a_majority_rule_is_still_labelled(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    _ready(store)
    engines = JurorEngines(
        {
            "juror-a": JurorEngine(judge=[verdict_answer(keep=False, rule=1)]),
            "juror-b": JurorEngine(judge=[verdict_answer(keep=False, rule=2)]),
            "juror-c": JurorEngine(judge=[verdict_answer(keep=False, rule=3)]),
        }
    )
    runtime = make_runtime(engines)

    report = _label(runtime, store, _settings(_A, _B, _C), limit=10)

    judge = store.evals.labels(report.set_id, EvalTask.JUDGE)
    assert len(judge) == 10
    assert all(label.keep is False and label.rule is None for label in judge)


def test_derive_leaves_an_existing_label(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    _ready(store)
    runtime = make_runtime(JurorEngines())
    report = _label(runtime, store, _settings(_A, _B), limit=10)
    eval_set = store.evals.sets()[0]
    posting_id = store.evals.cases(eval_set.id)[0].posting_id
    store.evals.put_label(
        EvalLabel(
            set_id=eval_set.id,
            posting_id=posting_id,
            task=EvalTask.JUDGE,
            keep=False,
            rule=2,
            origin=LabelOrigin.USER,
            score=None,
            spot_checked=True,
            decided_at=_AT,
        )
    )

    tally = derive_labels(store.evals, eval_set, at=_AT)

    kept = [
        label
        for label in store.evals.labels(eval_set.id, EvalTask.JUDGE)
        if label.posting_id == posting_id
    ]
    assert [(label.origin, label.rule) for label in kept] == [(LabelOrigin.USER, 2)]
    assert tally == report.tally


def test_jury_spend_reduces_the_next_cap(
    store: Store, make_runtime: _MakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)
    serve_openrouter(monkeypatch, _routed_replies(1))
    runtime = make_runtime(JurorEngines())
    settings = _settings(_A, _ROUTED, max_cost=0.5, total_cost=0.6)
    assert EvalSpend(store.evals, settings).cap == pytest.approx(0.5)

    _label(runtime, store, settings, limit=1)

    assert EvalSpend(store.evals, settings).cap == pytest.approx(0.6 - 2 * _ROUTED_COST)


def test_votes_keep_their_own_version(
    store: Store, make_runtime: _MakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(store)
    runtime = make_runtime(JurorEngines())
    settings = _settings(_A, _B)
    with monkeypatch.context() as earlier:
        earlier.setattr("ijob_ai.tasks.signatures.JUDGE_VERSION", "judge-1")
        first = _label(runtime, store, settings, limit=10)
    labels = store.evals.labels(first.set_id)
    old_votes = store.evals.votes(first.set_id)

    second = _label(runtime, store, settings, limit=20)

    votes = store.evals.votes(first.set_id)
    versions = {vote.task_version for vote in votes if vote.task is EvalTask.JUDGE}
    assert second.requested == 10 * 2 * 2
    assert versions == {"judge-1", "judge-2"}
    assert sum(vote.task_version == "judge-2" for vote in votes) == 20
    assert set(old_votes) <= set(votes)
    assert set(labels) <= set(store.evals.labels(first.set_id))
    run = store.evals.run(second.run_id)
    assert run is not None
    assert run.task_version == "judge-2+scorer-1"


def test_item_status_reaches_the_caller(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    _ready(store)
    runtime = make_runtime(JurorEngines())
    seen: list[ItemStatus] = []

    _label(
        runtime,
        store,
        _settings(_A, _B),
        limit=10,
        on_outcome=lambda outcome: seen.append(outcome.status),
    )

    assert seen == [ItemStatus.DONE] * 40


_JURY = (_A, _B, _C)


def _voted_set(store: Store) -> tuple[EvalSet, list[PostingId]]:
    """Draw the set of a ready store and return it with its case ids in order."""
    _ready(store)
    plan = prepare_label_run(store, _settings(*_JURY), now=_now)
    ids = [case.posting_id for case in store.evals.cases(plan.eval_set.id)]
    return plan.eval_set, ids


def _judge_votes(
    store: Store,
    eval_set: EvalSet,
    posting_id: PostingId,
    *answers: tuple[bool, int | None],
) -> None:
    """Store a judge vote per answer, from the first juror on."""
    for juror, (keep, rule) in zip(eval_set.jury, answers, strict=False):
        store.evals.put_vote(
            EvalVote(
                set_id=eval_set.id,
                posting_id=posting_id,
                task=EvalTask.JUDGE,
                juror=juror,
                task_version="judge-1",
                keep=keep,
                rule=rule,
                score=None,
                reasons=(f"{juror} says {'keep' if keep else 'reject'}",),
                made_at=_AT,
            )
        )


def _score_votes(
    store: Store, eval_set: EvalSet, posting_id: PostingId, *scores: int
) -> None:
    """Store a scorer vote per score, from the first juror on."""
    for juror, score in zip(eval_set.jury, scores, strict=False):
        store.evals.put_vote(
            EvalVote(
                set_id=eval_set.id,
                posting_id=posting_id,
                task=EvalTask.SCORER,
                juror=juror,
                task_version="scorer-1",
                keep=None,
                rule=None,
                score=score,
                reasons=(f"{juror} scores {score}",),
                made_at=_AT,
            )
        )


def _agree(
    store: Store, eval_set: EvalSet, ids: list[PostingId], *, judge: bool = True
) -> None:
    """Have every juror keep (or score 50) each of the postings."""
    for posting_id in ids:
        if judge:
            keep = (True, None)
            _judge_votes(store, eval_set, posting_id, keep, keep, keep)
        else:
            _score_votes(store, eval_set, posting_id, 50, 50, 50)


def _review(store: Store) -> tuple[ReviewItem, ...]:
    return review_items(store, _settings(*_JURY), now=_now).items


def _answer_same(store: Store, item: ReviewItem) -> EvalLabel:
    """Answer a spot check with the label the jurors agreed on."""
    assert item.agreed is not None
    return record_review(
        store, item, keep=item.agreed.keep, score=item.agreed.score, now=_now
    )


def test_review_lists_disagreements_first_then_spot_checks(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _judge_votes(store, eval_set, ids[0], (True, None), (False, 2), (False, 2))
    _score_votes(store, eval_set, ids[1], 72, 65, 40)
    _score_votes(store, eval_set, ids[2], 60, 50, 40)

    plan = review_items(store, _settings(*_JURY), now=_now)

    assert plan.has_set
    assert plan.rules == _RULES
    assert plan.skipped == 0
    assert [(i.posting.id, i.task, i.reason) for i in plan.items] == [
        (ids[0], EvalTask.JUDGE, ReviewReason.SPLIT),
        (ids[1], EvalTask.SCORER, ReviewReason.SPREAD),
        (ids[2], EvalTask.SCORER, None),
    ]
    assert [vote.juror for vote in plan.items[0].votes] == list(eval_set.jury)
    assert [i.spot_check for i in plan.items] == [False, False, True]


def test_review_item_reads_spread_and_median_from_the_votes(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _score_votes(store, eval_set, ids[0], 72, 65, 40)
    _score_votes(store, eval_set, ids[1], 70, 30)
    _judge_votes(store, eval_set, ids[2], (True, None), (False, 1), (False, 1))

    first, second, judge = _review(store)

    assert (first.spread, first.median) == (32, 65)
    assert (second.spread, second.median) == (40, 30)
    assert (judge.spread, judge.median) == (None, None)


def test_review_derives_labels_first(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _agree(store, eval_set, ids[:1])
    assert store.evals.labels(eval_set.id) == []

    items = _review(store)

    labels = store.evals.labels(eval_set.id)
    assert [(label.posting_id, label.origin) for label in labels] == [
        (ids[0], LabelOrigin.AGREED)
    ]
    assert [(i.posting.id, i.spot_check) for i in items] == [(ids[0], True)]


def test_review_missing_vote_is_offered(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _judge_votes(store, eval_set, ids[0], (False, 2), (False, 2))
    _score_votes(store, eval_set, ids[2], 50)

    items = _review(store)

    assert [(i.posting.id, i.task, i.reason) for i in items] == [
        (ids[0], EvalTask.JUDGE, ReviewReason.FEWER_VOTES),
        (ids[2], EvalTask.SCORER, ReviewReason.FEWER_VOTES),
    ]
    assert ids[1] not in {i.posting.id for i in items}


def test_review_skips_changed_and_removed_postings(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_set, ids = _voted_set(store)
    split = ((True, None), (False, 2), (False, 2))
    _judge_votes(store, eval_set, ids[0], *split)
    _judge_votes(store, eval_set, ids[1], *split)
    _score_votes(store, eval_set, ids[2], 50, 50, 50)
    _score_votes(store, eval_set, ids[3], 50, 50, 50)
    store.set_description(ids[0], "rewritten after the draw")
    original = Store.posting

    def posting(self: Store, posting_id: PostingId) -> Posting | None:
        return None if posting_id == ids[3] else original(self, posting_id)

    monkeypatch.setattr(Store, "posting", posting)

    plan = review_items(store, _settings(*_JURY), now=_now)

    assert [(i.posting.id, i.task) for i in plan.items] == [
        (ids[1], EvalTask.JUDGE),
        (ids[2], EvalTask.SCORER),
    ]
    assert plan.skipped == 2


def test_review_without_a_set_has_nothing_and_makes_none(store: Store) -> None:
    _ready(store)

    plan = review_items(store, _settings(*_JURY), now=_now)

    assert not plan.has_set
    assert plan.items == ()
    assert store.evals.sets() == []


def test_review_needs_a_jury_rules_and_a_profile(store: Store) -> None:
    _seed(store)
    no_jury = EvalSettings(jury=None, max_cost_usd=4, total_cost_usd=10)

    with pytest.raises(EvalNeedsError, match=r"eval\.jury"):
        review_items(store, no_jury, now=_now)
    with pytest.raises(EvalNeedsError, match=r"ijob instructions set"):
        _review(store)
    _store_rules(store)
    with pytest.raises(EvalNeedsError, match=r"ijob cv profile"):
        _review(store)


def test_spot_check_stops_at_ten(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _agree(store, eval_set, ids[:12])
    _agree(store, eval_set, ids[:12], judge=False)

    items = _review(store)
    for item in items:
        _answer_same(store, item)

    assert len(items) == 10
    assert all(i.spot_check for i in items)
    assert sum(i.task is EvalTask.JUDGE for i in items) == 5
    labels = store.evals.labels(eval_set.id)
    assert sum(label.spot_checked for label in labels) == 10
    assert _review(store) == ()


def test_spot_check_resumes_after_quit(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _agree(store, eval_set, ids[:12])
    items = _review(store)
    for item in items[:4]:
        _answer_same(store, item)

    again = _review(store)

    assert [(i.posting.id, i.task) for i in again] == [
        (i.posting.id, i.task) for i in items[4:]
    ]


def test_spot_check_with_few_labels_offers_the_rest_later(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _agree(store, eval_set, ids[:3])
    first = _review(store)
    for item in first:
        _answer_same(store, item)
    _agree(store, eval_set, ids[3:12])

    later = _review(store)

    assert len(first) == 3
    assert len(later) == 7
    assert {i.posting.id for i in later}.isdisjoint({i.posting.id for i in first})


def test_spot_check_same_answer_stays_agreed(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _agree(store, eval_set, ids[:1])
    _agree(store, eval_set, ids[1:2], judge=False)
    judge, scorer = sorted(_review(store), key=lambda item: item.task)

    record_review(store, judge, keep=True, now=_now)
    record_review(store, scorer, score=50, now=_now)

    labels = store.evals.labels(eval_set.id)
    assert len(labels) == 2
    assert {(label.origin, label.spot_checked) for label in labels} == {
        (LabelOrigin.AGREED, True)
    }


def test_spot_check_different_answer_is_user_origin(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _agree(store, eval_set, ids[:1])
    (item,) = _review(store)

    stored = record_review(store, item, keep=False, now=_now)

    assert (stored.origin, stored.spot_checked, stored.keep) == (
        LabelOrigin.USER,
        True,
        False,
    )
    assert store.evals.labels(eval_set.id) == [stored]


def test_record_review_rejection_rule_follows_the_rejecting_votes(
    store: Store,
) -> None:
    eval_set, ids = _voted_set(store)
    _judge_votes(store, eval_set, ids[0], (True, None), (False, 2), (False, 2))
    _judge_votes(store, eval_set, ids[1], (True, None), (False, 1), (False, 3))
    first, second = _review(store)

    kept = record_review(store, first, keep=True, now=_now)
    rejected = record_review(store, first, keep=False, now=_now)
    no_majority = record_review(store, second, keep=False, now=_now)

    assert (kept.keep, kept.rule, kept.origin, kept.spot_checked) == (
        True,
        None,
        LabelOrigin.USER,
        False,
    )
    assert (rejected.keep, rejected.rule) == (False, 2)
    assert (no_majority.keep, no_majority.rule) == (False, None)
    assert set(store.evals.labels(eval_set.id, EvalTask.JUDGE)) == {
        rejected,
        no_majority,
    }


def test_record_review_stores_a_score(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _score_votes(store, eval_set, ids[0], 72, 65, 40)
    (item,) = _review(store)

    stored = record_review(store, item, score=65, now=_now)

    assert (stored.score, stored.keep, stored.rule) == (65, None, None)
    assert store.evals.labels(eval_set.id) == [stored]


def test_record_review_refuses_an_answer_of_the_wrong_task(store: Store) -> None:
    eval_set, ids = _voted_set(store)
    _score_votes(store, eval_set, ids[0], 72, 65, 40)
    (item,) = _review(store)

    with pytest.raises(InvalidValueError, match=r"keep"):
        record_review(store, item, keep=True, score=65, now=_now)
    with pytest.raises(InvalidValueError, match=r"score"):
        record_review(store, item, now=_now)

    assert store.evals.labels(eval_set.id) == []


def _voted_pair(store: Store, count: int) -> tuple[EvalSet, list[PostingId]]:
    """Draw the set of `count` postings for two jurors and vote on all its cases."""
    _ready(store, count)
    plan = prepare_label_run(store, _settings(_A, _B), now=_now)
    ids = [case.posting_id for case in store.evals.cases(plan.eval_set.id)]
    _agree(store, plan.eval_set, ids)
    _agree(store, plan.eval_set, ids, judge=False)
    return plan.eval_set, ids


def _add(store: Store, count: int) -> LabelPlan:
    """Plan an add and store its cases, as a run does when it starts."""
    plan = _plan_add(store, count)
    store.evals.put_cases(plan.eval_set.id, plan.pending)
    return plan


def _plan_add(store: Store, count: int) -> LabelPlan:
    """Plan an add and stop there, as a run that is declined or refused does."""
    return prepare_label_run(store, _settings(_A, _B), add=count, now=_now)


def test_declined_add_leaves_the_set_alone(store: Store) -> None:
    eval_set, _ = _voted_pair(store, 150)

    plan = _plan_add(store, 15)
    later = _plan_add(store, 2)

    assert (plan.added, plan.cases, len(plan.items)) == (15, 115, 15 * 2 * 2)
    assert len(store.evals.cases(eval_set.id)) == 100
    assert (later.added, later.cases) == (2, 102)


def test_budget_refused_add_leaves_the_set_alone(store: Store) -> None:
    eval_set, _ = _voted_pair(store, 150)
    _plan_add(store, 15)
    spent = store.evals.start_run("judge", "m", "judge-1", None, started_at=_AT)
    store.evals.record_spend(spent.id, spent_usd=10, subscription_usd=0)

    with pytest.raises(EvalBudgetSpentError):
        EvalSpend(store.evals, _settings(_A, _B))

    later = _plan_add(store, 2)
    assert len(store.evals.cases(eval_set.id)) == 100
    assert later.added == 2


def test_accepted_add_stores_the_cases_and_asks_for_the_new_votes(
    store: Store, make_runtime: _MakeRuntime
) -> None:
    eval_set, _ = _voted_pair(store, 150)
    engines = JurorEngines()
    runtime = make_runtime(engines)
    settings = _settings(_A, _B)
    plan = _plan_add(store, 5)
    spend = EvalSpend(store.evals, settings)

    report = asyncio.run(label_operation(runtime, store, plan, spend, now=_now))

    cases = store.evals.cases(eval_set.id)
    assert [case.position for case in cases[100:]] == [100, 101, 102, 103, 104]
    assert report.requested == 5 * 2 * 2
    assert report.stored == 5 * 2 * 2
    assert engines.engine("juror-a").calls.count("judge") == 5
    assert engines.engine("juror-b").calls.count("scorer") == 5


def test_add_appends_after_the_last_position(store: Store) -> None:
    eval_set, _ = _voted_pair(store, 110)
    before = store.evals.cases(eval_set.id)

    plan = _add(store, 5)

    after = store.evals.cases(eval_set.id)
    assert plan.eval_set == eval_set
    assert plan.added == 5
    assert plan.cases == 105
    assert after[:100] == before
    assert [case.position for case in after[100:]] == [100, 101, 102, 103, 104]


def test_add_asks_only_for_the_new_votes(store: Store) -> None:
    eval_set, _ = _voted_pair(store, 110)

    plan = _add(store, 5)

    new = {case.posting_id for case in store.evals.cases(eval_set.id)[100:]}
    assert len(plan.items) == 5 * 2 * 2
    assert {item.posting.id for item in plan.items} == new


def test_add_excludes_existing_cases(store: Store) -> None:
    eval_set, ids = _voted_pair(store, 150)

    _add(store, 40)

    new = [case.posting_id for case in store.evals.cases(eval_set.id)[100:]]
    assert len(new) == 40
    assert set(new).isdisjoint(ids)
    assert len(set(new)) == 40


def test_add_can_repeat(store: Store) -> None:
    eval_set, _ = _voted_pair(store, 150)
    _add(store, 10)
    first = [case.posting_id for case in store.evals.cases(eval_set.id)[100:]]
    _agree(store, eval_set, first)
    _agree(store, eval_set, first, judge=False)

    plan = _add(store, 10)

    everything = [case.posting_id for case in store.evals.cases(eval_set.id)]
    assert plan.added == 10
    assert len(everything) == 120
    assert len(set(everything)) == 120


def test_add_is_refused_while_votes_are_missing(store: Store) -> None:
    eval_set, _ = _voted_pair(store, 150)
    _add(store, 10)

    with pytest.raises(EvalNeedsError, match=r"run `ijob eval label`"):
        _add(store, 10)

    assert len(store.evals.cases(eval_set.id)) == 110


def test_add_ignores_a_case_whose_posting_changed(store: Store) -> None:
    eval_set, ids = _voted_pair(store, 110)
    store.set_description(ids[0], "rewritten after the draw")
    assert store.evals.cases(eval_set.id)[0].posting_id == ids[0]

    plan = _add(store, 5)

    assert plan.added == 5
    assert plan.skipped == 1
    assert len(plan.items) == 5 * 2 * 2


def test_add_with_limit_is_refused(store: Store) -> None:
    _voted_pair(store, 110)

    with pytest.raises(InvalidValueError, match="limit"):
        prepare_label_run(store, _settings(_A, _B), add=5, limit=5, now=_now)


@pytest.mark.parametrize("count", [0, 101])
def test_add_out_of_range_is_refused(store: Store, count: int) -> None:
    _voted_pair(store, 110)

    with pytest.raises(InvalidValueError, match="1 to 100"):
        _add(store, count)


def test_add_without_a_set_is_refused(store: Store) -> None:
    _ready(store, 30)

    with pytest.raises(EvalNeedsError, match=r"run `ijob eval label`"):
        _add(store, 5)

    assert store.evals.sets() == []


def test_add_with_nothing_to_draw_raises(store: Store) -> None:
    eval_set, _ = _voted_pair(store, 100)

    with pytest.raises(TooFewCasesError):
        _add(store, 5)

    assert len(store.evals.cases(eval_set.id)) == 100


def test_short_draw_says_how_many(store: Store) -> None:
    eval_set, _ = _voted_pair(store, 130)

    plan = _add(store, 100)

    assert plan.added == 30
    assert len(store.evals.cases(eval_set.id)) == 130


def test_spot_check_grows_with_the_set(store: Store) -> None:
    eval_set, _ = _voted_pair(store, 120)
    _add(store, 20)
    new = [case.posting_id for case in store.evals.cases(eval_set.id)[100:]]
    _agree(store, eval_set, new)
    _agree(store, eval_set, new, judge=False)

    items = review_items(store, _settings(_A, _B), now=_now).items

    assert len(items) == 12
    assert sum(i.task is EvalTask.JUDGE for i in items) == 6

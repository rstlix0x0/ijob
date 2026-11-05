import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ijob_ai.errors import ProviderUnavailableError
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import EvalSettings, LlmSettings, Task, TuneSettings
from ijob_ai.tasks.text import input_sha256, posting_block
from ijob_ai.tasks.versions import JUDGE_UNTRUSTED, SCORER_UNTRUSTED
from ijob_app.eval_ops import EvalNeedsError
from ijob_app.eval_suites import Suite, SuiteReport
from ijob_app.llm_ops import OperationReport, StopReason
from ijob_app.tune_ops import (
    AcceptReport,
    PairOutcome,
    ScreenPair,
    ScreenPlan,
    TooFewKeepsError,
    accept_program,
    check_split,
    clear_program,
    freeze_split,
    full_run,
    prepare_accept,
    prepare_screen,
    program_in_force,
    program_status,
    run_screen,
    split_report,
)
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import (
    Bucket,
    EvalCase,
    EvalLabel,
    EvalResult,
    EvalSet,
    EvalTask,
    EvalVote,
    LabelOrigin,
    RunStatus,
)
from ijob_core.model import (
    ExternalId,
    Instructions,
    PostingId,
    ResumeProfile,
    Seniority,
    SightingDraft,
    SourceKind,
    SourceRef,
    StoredCv,
    parse_source_key,
)
from ijob_core.store import Store
from ijob_core.tune_records import SplitPart, TunedProgram

_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_SOURCE = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_JURY = tuple(ModelRef(Provider.CLAUDE_SDK, f"juror-{name}") for name in "abc")
_SETTINGS = EvalSettings(jury=_JURY)


def _now() -> datetime:
    return _AT


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "ijob.db") as opened:
        yield opened


def _label(  # noqa: PLR0913  # one label's fields
    store: Store,
    eval_set: EvalSet,
    posting_id: PostingId,
    task: EvalTask,
    *,
    keep: bool,
    origin: LabelOrigin = LabelOrigin.USER,
) -> None:
    store.evals.put_label(
        EvalLabel(
            set_id=eval_set.id,
            posting_id=posting_id,
            task=task,
            keep=keep if task is EvalTask.JUDGE else None,
            rule=None if keep or task is EvalTask.SCORER else 1,
            score=50 if task is EvalTask.SCORER else None,
            origin=origin,
            spot_checked=False,
            decided_at=_AT,
        )
    )


def _case(
    store: Store, eval_set: EvalSet, posting_id: PostingId, position: int
) -> None:
    posting = store.posting(posting_id)
    assert posting is not None
    store.evals.put_cases(
        eval_set.id,
        [
            EvalCase(
                eval_set.id,
                posting_id,
                position,
                Bucket.KEPT,
                input_sha256(posting_block(posting)),
            )
        ],
    )


def _labelled_set(
    store: Store,
    keeps: int,
    rejects: int,
    describe: Callable[[int], str] = lambda number: f"Build things {number}",
) -> tuple[EvalSet, list[PostingId]]:
    """Store postings, a set matching the settings, and both labels on each posting.

    `describe` writes the description of the posting with the given number.
    """
    total = keeps + rejects
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
                description=describe(number),
            )
            for number in range(total)
        ],
        seen_at=_AT,
    )
    instructions = Instructions(
        text="Remote only",
        filters_json="{}",
        rules=("no crypto",),
        model="made-up/claude",
        task_version="instructions-1",
        set_at=_AT,
    )
    store.set_instructions(instructions)
    profile = ResumeProfile(
        summary="Backend engineer",
        years_experience=7,
        seniority=Seniority.SENIOR,
        roles=("Backend Engineer",),
        spoken_languages=("English",),
        locations=("Jakarta",),
        work_authorization=("Indonesia",),
        skills=("Python",),
    )
    store.set_cv(StoredCv(name="cv.pdf", sha256="a" * 64, size=1, stored_at=_AT))
    store.set_cv_profile(
        profile,
        cv_sha256="a" * 64,
        model="made-up/claude",
        task_version="cv_profile-1",
        made_at=_AT,
    )
    eval_set = store.evals.find_or_create_set(
        instructions.rules_sha256,
        profile.sha256(),
        [str(juror) for juror in _JURY],
        created_at=_AT,
    )
    ids = sorted(posting.id for posting in store.postings(sources=[_SOURCE]))
    for position, posting_id in enumerate(ids):
        _case(store, eval_set, posting_id, position)
        keep = position < keeps
        _label(store, eval_set, posting_id, EvalTask.JUDGE, keep=keep)
        _label(store, eval_set, posting_id, EvalTask.SCORER, keep=keep)
    return eval_set, ids


def _drop_labels(store: Store, posting_ids: list[PostingId], *tasks: str) -> None:
    """Remove the labels of the postings for the tasks, as if never decided."""
    with store._conn:
        for posting_id in posting_ids:
            for task in tasks:
                store._conn.execute(
                    "DELETE FROM eval_labels WHERE posting_id = ? AND task = ?",
                    (posting_id, task),
                )


def _freeze(store: Store, eval_set: EvalSet) -> None:
    freeze_split(store, eval_set, _SETTINGS, now=_now)


def test_freeze_writes_the_split_once(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 50)

    first = freeze_split(store, eval_set, _SETTINGS, now=_now)
    second = freeze_split(store, eval_set, _SETTINGS, now=_now)

    assert second == first == store.tunes.split(eval_set.id)
    assert [row.posting_id for row in first] == ids
    counts = {part: [r.part for r in first].count(part) for part in SplitPart}
    assert counts == {
        SplitPart.HELD_OUT: 50,
        SplitPart.TRAIN: 30,
        SplitPart.VAL: 20,
        SplitPart.UNUSED: 0,
    }


@pytest.mark.parametrize(("keeps", "rejects", "n"), [(21, 79, 10), (39, 161, 19)])
def test_too_few_keeps_freezes_nothing(
    store: Store, keeps: int, rejects: int, n: int
) -> None:
    eval_set, _ = _labelled_set(store, keeps, rejects)

    with pytest.raises(TooFewKeepsError, match=rf"leave {n} of each") as raised:
        _freeze(store, eval_set)

    assert "eval label --add" in str(raised.value)
    assert (raised.value.keeps, raised.value.rejects, raised.value.n) == (
        keeps,
        rejects,
        n,
    )
    assert store.tunes.split(eval_set.id) == []


def _split_vote(store: Store, eval_set: EvalSet, posting_id: PostingId) -> None:
    for juror, keep in zip(eval_set.jury, (True, False, False), strict=True):
        store.evals.put_vote(
            EvalVote(
                set_id=eval_set.id,
                posting_id=posting_id,
                task=EvalTask.JUDGE,
                juror=juror,
                task_version="judge-1",
                keep=keep,
                rule=None if keep else 1,
                score=None,
                reasons=("because",),
                made_at=_AT,
            )
        )


def test_labels_awaiting_review_freezes_nothing(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 51)
    _drop_labels(store, ids[-1:], "judge")
    _split_vote(store, eval_set, ids[-1])

    with pytest.raises(EvalNeedsError, match=r"eval review"):
        _freeze(store, eval_set)

    assert store.tunes.split(eval_set.id) == []


def test_review_of_another_set_freezes_nothing(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 51)
    _drop_labels(store, ids[-1:], "judge")
    _split_vote(store, eval_set, ids[-1])
    store.set_instructions(
        Instructions(
            text="Remote only",
            filters_json="{}",
            rules=("no gambling",),
            model="made-up/claude",
            task_version="instructions-1",
            set_at=_AT,
        )
    )

    with pytest.raises(EvalNeedsError, match=r"eval label"):
        freeze_split(store, eval_set, _SETTINGS, now=_now)
    with pytest.raises(EvalNeedsError, match=r"eval label"):
        check_split(store, eval_set, _SETTINGS, now=_now)

    assert store.tunes.split(eval_set.id) == []


def test_a_spot_check_awaiting_review_freezes_nothing(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 50)
    _drop_labels(store, ids[:1], "judge")
    _label(
        store,
        eval_set,
        ids[0],
        EvalTask.JUDGE,
        keep=True,
        origin=LabelOrigin.AGREED,
    )

    with pytest.raises(EvalNeedsError, match=r"eval review"):
        _freeze(store, eval_set)

    assert store.tunes.split(eval_set.id) == []


def test_a_frozen_split_is_returned_while_a_vote_awaits_review(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 51)
    frozen = freeze_split(store, eval_set, _SETTINGS, now=_now)
    _drop_labels(store, ids[-1:], "judge")
    _split_vote(store, eval_set, ids[-1])

    again = freeze_split(store, eval_set, _SETTINGS, now=_now)
    check_split(store, eval_set, _SETTINGS, now=_now)

    assert again == frozen
    assert len(frozen) == 101


def test_only_postings_with_both_labels_count(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 50)
    _drop_labels(store, ids[:1], "scorer")

    rows = freeze_split(store, eval_set, _SETTINGS, now=_now)

    assert ids[0] not in {row.posting_id for row in rows}
    assert len(rows) == 99


def test_changed_posting_is_skipped_and_counted(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 50)
    store.set_description(ids[0], "rewritten after the draw")

    rows = freeze_split(store, eval_set, _SETTINGS, now=_now)

    assert ids[0] not in {row.posting_id for row in rows}
    assert split_report(store, eval_set).changed == 1


def test_labels_added_later_are_left_out_and_counted(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 50)
    _drop_labels(store, ids[:2], "judge", "scorer")
    before = freeze_split(store, eval_set, _SETTINGS, now=_now)
    for posting_id in ids[:2]:
        _label(store, eval_set, posting_id, EvalTask.JUDGE, keep=True)
        _label(store, eval_set, posting_id, EvalTask.SCORER, keep=True)

    report = split_report(store, eval_set)

    assert store.tunes.split(eval_set.id) == before
    assert len(before) == 98
    assert report.frozen
    assert report.added_later == 2
    assert report.changed == 0
    assert report.train + report.val + report.held_out + report.unused == 98


def test_a_frozen_posting_changed_since_the_freeze_is_counted_as_changed(
    store: Store,
) -> None:
    eval_set, ids = _labelled_set(store, 50, 50)
    frozen = freeze_split(store, eval_set, _SETTINGS, now=_now)
    store.set_description(ids[0], "rewritten after the freeze")

    report = split_report(store, eval_set)

    assert report.changed == 1
    assert report.added_later == 0
    assert report.train + report.val + report.held_out + report.unused == 100
    assert store.tunes.split(eval_set.id) == frozen


def test_split_report_writes_nothing(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 50, 50)

    report = split_report(store, eval_set)

    assert not report.frozen
    assert (report.train, report.val, report.held_out, report.unused) == (0, 0, 0, 0)
    assert store.tunes.split(eval_set.id) == []


_JUDGE_NOW = "judge-2"
_SCORER_NOW = "scorer-1"
_BASE = ModelRef(Provider.CLAUDE_SDK, "base")
_CAND_A = ModelRef(Provider.OPENROUTER, "made-up/cand-a")
_CAND_B = ModelRef(Provider.OPENROUTER, "made-up/cand-b")


def _run(  # noqa: PLR0913  # one run's fields
    store: Store,
    eval_set: EvalSet,
    task: EvalTask,
    model: ModelRef,
    covers: list[PostingId],
    *,
    version: str | None = None,
    status: RunStatus = RunStatus.COMPLETE,
    limited: bool = False,
    answer: str = "{}",
) -> int:
    """Store a run of `task` on `model` with a result for each posting of `covers`.

    Each result holds `answer` as the model's stored answer.
    """
    run = store.evals.start_run(
        task.value,
        str(model),
        version or (_JUDGE_NOW if task is EvalTask.JUDGE else _SCORER_NOW),
        eval_set.id,
        started_at=_AT,
        limited=limited,
    )
    for posting_id in covers:
        store.evals.put_result(
            EvalResult(
                run.id,
                str(posting_id),
                passed=True,
                answer=answer,
                detail="",
                latency_ms=5,
            )
        )
    store.evals.finish_run(run.id, status, "{}", finished_at=_AT)
    return run.id


def test_full_run_needs_every_current_case(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 30, 30)
    _run(store, eval_set, EvalTask.JUDGE, _BASE, ids[:30])

    assert full_run(store, EvalTask.JUDGE, _BASE, eval_set) is None


def test_full_run_ignores_limited_and_incomplete_runs(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 30, 30)
    _run(store, eval_set, EvalTask.JUDGE, _BASE, ids, limited=True)
    _run(store, eval_set, EvalTask.JUDGE, _BASE, ids, status=RunStatus.INCOMPLETE)

    assert full_run(store, EvalTask.JUDGE, _BASE, eval_set) is None


def test_full_run_needs_the_current_version(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 30, 30)
    _run(store, eval_set, EvalTask.JUDGE, _BASE, ids, version="judge-1")

    assert full_run(store, EvalTask.JUDGE, _BASE, eval_set) is None


def test_full_run_is_per_task_model_and_set(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 30, 30)
    run_id = _run(store, eval_set, EvalTask.SCORER, _BASE, ids)

    assert full_run(store, EvalTask.JUDGE, _BASE, eval_set) is None
    assert full_run(store, EvalTask.SCORER, _CAND_A, eval_set) is None
    found = full_run(store, EvalTask.SCORER, _BASE, eval_set)
    assert found is not None
    assert found.id == run_id


def test_a_changed_posting_does_not_stop_a_run_being_full(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 30, 30)
    store.set_description(ids[0], "rewritten after the draw")
    run_id = _run(store, eval_set, EvalTask.JUDGE, _BASE, ids[1:])

    found = full_run(store, EvalTask.JUDGE, _BASE, eval_set)

    assert found is not None
    assert found.id == run_id


def test_a_later_limited_row_does_not_hide_a_full_run(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 30, 30)
    run_id = _run(store, eval_set, EvalTask.JUDGE, _BASE, ids)
    _run(store, eval_set, EvalTask.JUDGE, _BASE, ids[:20], limited=True)

    found = full_run(store, EvalTask.JUDGE, _BASE, eval_set)

    assert found is not None
    assert found.id == run_id


def _llm(
    *, judge: ModelRef | None = None, scorer: ModelRef | None = None
) -> LlmSettings:
    models = {
        task: ref
        for task, ref in ((Task.JUDGE, judge), (Task.SCORER, scorer))
        if ref is not None
    }
    return LlmSettings(config_path=Path("config.toml"), models=models)


def _screen(
    store: Store,
    *,
    candidates: tuple[ModelRef, ...] = (),
    baseline: ModelRef | None = _BASE,
    judge: ModelRef | None = None,
    scorer: ModelRef | None = None,
) -> ScreenPlan:
    return prepare_screen(
        store,
        _llm(judge=judge, scorer=scorer),
        _SETTINGS,
        TuneSettings(candidates=candidates, baseline=baseline),
        now=_now,
    )


def _pairs(plan: ScreenPlan) -> list[tuple[str, str]]:
    return [(pair.task.value, str(pair.model)) for pair in plan.pairs]


def test_an_unset_configured_model_is_left_out(store: Store) -> None:
    _labelled_set(store, 50, 50)

    plan = _screen(store, candidates=(_CAND_A,), scorer=_CAND_B)

    assert {pair.model for pair in plan.pairs} == {_CAND_A, _BASE, _CAND_B}


def test_screen_lists_each_model_for_both_suites(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 50, 50)

    plan = _screen(store, candidates=(_CAND_A, _BASE), judge=_CAND_A, scorer=_CAND_B)

    assert plan.eval_set == eval_set
    assert _pairs(plan) == [
        ("judge", str(_CAND_A)),
        ("scorer", str(_CAND_A)),
        ("judge", str(_BASE)),
        ("scorer", str(_BASE)),
        ("judge", str(_CAND_B)),
        ("scorer", str(_CAND_B)),
    ]
    assert plan.skipped == ()


def test_screen_skips_a_full_pair(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 50)
    _run(store, eval_set, EvalTask.JUDGE, _BASE, ids)

    plan = _screen(store, candidates=(_CAND_A,))

    assert ("judge", str(_BASE)) not in _pairs(plan)
    assert ("scorer", str(_BASE)) in _pairs(plan)
    assert len(plan.pairs) == 3


def test_screen_without_tune_table_is_refused(store: Store) -> None:
    _labelled_set(store, 50, 50)

    with pytest.raises(InvalidValueError, match=r"\[tune\]"):
        _screen(store, baseline=None)


def test_screen_without_baseline_is_refused(store: Store) -> None:
    _labelled_set(store, 50, 50)

    with pytest.raises(InvalidValueError, match=r"tune\.baseline"):
        _screen(store, candidates=(_CAND_A,), baseline=None)


@pytest.mark.parametrize("where", ["candidates", "baseline"])
def test_juror_in_candidates_is_refused_before_spend(store: Store, where: str) -> None:
    _labelled_set(store, 50, 50)
    juror = _JURY[0]
    candidates = (_CAND_A, juror) if where == "candidates" else ()
    baseline = _BASE if where == "candidates" else juror

    with pytest.raises(InvalidValueError, match=r"jury"):
        _screen(store, candidates=candidates, baseline=baseline)

    assert store.evals.runs() == []


def test_juror_as_configured_model_is_skipped(store: Store) -> None:
    _labelled_set(store, 50, 50)

    plan = _screen(store, candidates=(_CAND_A,), judge=_JURY[1], scorer=_CAND_B)

    assert plan.skipped == (_JURY[1],)
    assert {pair.model for pair in plan.pairs} == {_CAND_A, _BASE, _CAND_B}


def test_empty_candidates_screens_the_rest(store: Store) -> None:
    _labelled_set(store, 50, 50)

    plan = _screen(store, judge=_CAND_A)

    assert _pairs(plan) == [
        ("judge", str(_BASE)),
        ("scorer", str(_BASE)),
        ("judge", str(_CAND_A)),
        ("scorer", str(_CAND_A)),
    ]


def test_screen_refuses_too_few_keeps_before_asking(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 21, 79)

    with pytest.raises(TooFewKeepsError):
        _screen(store, candidates=(_CAND_A,))

    assert store.tunes.split(eval_set.id) == []


def test_screen_refuses_labels_awaiting_review_before_asking(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 51)
    _drop_labels(store, ids[-1:], "judge")
    _split_vote(store, eval_set, ids[-1])

    with pytest.raises(EvalNeedsError, match=r"eval review"):
        _screen(store)

    assert store.tunes.split(eval_set.id) == []


def test_screen_with_a_frozen_split_does_not_recheck_it(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 50, 50)
    freeze_split(store, eval_set, _SETTINGS, now=_now)
    _drop_labels(store, ids[:60], "judge", "scorer")

    plan = _screen(store)

    assert len(plan.pairs) == 2


def test_screen_without_a_usable_profile_is_refused(store: Store) -> None:
    _labelled_set(store, 50, 50)
    store.set_cv(StoredCv(name="new.pdf", sha256="b" * 64, size=1, stored_at=_AT))

    with pytest.raises(EvalNeedsError, match=r"earlier CV"):
        _screen(store)

    with store._conn:
        store._conn.execute("DELETE FROM cv_profile")

    with pytest.raises(EvalNeedsError, match=r"no CV profile"):
        _screen(store)


_TUNED = ModelRef(Provider.OPENROUTER, "made-up/tuned")


def _program(  # noqa: PLR0913  # one program's varying fields
    store: Store,
    eval_set: EvalSet,
    task: EvalTask,
    *,
    model: ModelRef = _TUNED,
    bound: str | None = None,
    base_version: str | None = None,
    accept: bool = True,
    instructions: str | None = None,
) -> TunedProgram:
    """Save a program for the set's inputs, accepted unless `accept` is false.

    Its instructions hold the task's untrusted-data sentence unless
    `instructions` says otherwise.
    """
    now = _JUDGE_NOW if task is EvalTask.JUDGE else _SCORER_NOW
    current = (
        eval_set.rules_sha256 if task is EvalTask.JUDGE else eval_set.profile_sha256
    )
    base = base_version or now
    saved = store.tunes.save_program(
        task=task,
        model=str(model),
        optimizer="gepa",
        base_version=base,
        set_id=eval_set.id,
        bound_sha256=bound or current,
        instructions=instructions
        or f"Tuned. {JUDGE_UNTRUSTED if task is EvalTask.JUDGE else SCORER_UNTRUSTED}",
        demos_json="[]",
        version=f"{base}+0a1b2c3d",
        tune_run_id=None,
        created_at=_AT,
    )
    if accept:
        store.tunes.accept_program(saved.id, accepted_at=_AT)
        accepted = store.tunes.accepted(task)
        assert accepted is not None
        return accepted
    return saved


def _configured(task: EvalTask, model: ModelRef = _TUNED) -> LlmSettings:
    if task is EvalTask.JUDGE:
        return _llm(judge=model)
    return _llm(scorer=model)


@pytest.mark.parametrize("task", list(EvalTask))
def test_program_in_force_when_all_three_hold(store: Store, task: EvalTask) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    program = _program(store, eval_set, task)

    assert program_in_force(store, _configured(task), task) == program
    status = program_status(store, _configured(task), program)
    assert str(status) == "accepted"
    assert status.in_force


def test_another_model_is_not_in_force(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    program = _program(store, eval_set, EvalTask.JUDGE)
    other = ModelRef(Provider.OPENROUTER, "made-up/other")

    assert program_in_force(store, _llm(judge=other), EvalTask.JUDGE) is None
    status = program_status(store, _llm(judge=other), program)
    assert not status.in_force
    assert str(status).startswith("accepted, not in use: ")
    assert "made-up/other" in str(status)
    assert "made-up/tuned" in str(status)


def test_an_unset_model_is_not_in_force(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    program = _program(store, eval_set, EvalTask.SCORER)

    status = program_status(store, _llm(), program)

    assert str(status) == "accepted, not in use: no model is set for scorer"
    assert program_in_force(store, _llm(), EvalTask.SCORER) is None


def test_another_bound_is_not_in_force(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    judge = _program(store, eval_set, EvalTask.JUDGE, bound="b" * 64)
    scorer = _program(store, eval_set, EvalTask.SCORER, bound="c" * 64)

    judge_status = program_status(store, _configured(EvalTask.JUDGE), judge)
    scorer_status = program_status(store, _configured(EvalTask.SCORER), scorer)

    assert str(judge_status).startswith("accepted, not in use: ")
    assert "rules" in str(judge_status)
    assert str(scorer_status).startswith("accepted, not in use: ")
    assert "profile" in str(scorer_status)
    assert program_in_force(store, _configured(EvalTask.JUDGE), EvalTask.JUDGE) is None
    assert (
        program_in_force(store, _configured(EvalTask.SCORER), EvalTask.SCORER) is None
    )


def test_changed_rules_stop_a_judge_program(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    _program(store, eval_set, EvalTask.JUDGE)
    store.set_instructions(
        Instructions(
            text="Remote only",
            filters_json="{}",
            rules=("no crypto", "no on-call"),
            model="made-up/claude",
            task_version="instructions-1",
            set_at=_AT,
        )
    )

    assert program_in_force(store, _configured(EvalTask.JUDGE), EvalTask.JUDGE) is None


def test_another_base_version_is_not_in_force(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    program = _program(store, eval_set, EvalTask.JUDGE, base_version="judge-1")

    status = program_status(store, _configured(EvalTask.JUDGE), program)

    assert str(status).startswith("accepted, not in use: ")
    assert "judge-1" in str(status)
    assert "judge-2" in str(status)
    assert program_in_force(store, _configured(EvalTask.JUDGE), EvalTask.JUDGE) is None


@pytest.mark.parametrize("task", list(EvalTask))
def test_a_program_without_the_sentence_stops_the_task(
    store: Store, task: EvalTask
) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    program = _program(store, eval_set, task, instructions="Tuned, no sentence.")
    llm = _configured(task)

    with pytest.raises(InvalidValueError, match=rf"program {program.id}\b"):
        program_in_force(store, llm, task)

    status = program_status(store, llm, program)
    assert status.accepted
    assert not status.in_force
    assert str(status).startswith("accepted, not in use: ")
    assert "untrusted-data sentence" in str(status)
    assert f"ijob tune clear {task.value}" in str(status)
    assert store.tunes.clear_program(task)
    assert program_in_force(store, llm, task) is None


def test_a_program_not_in_use_is_not_checked_for_the_sentence(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    _program(store, eval_set, EvalTask.JUDGE, instructions="Tuned, no sentence.")
    other = ModelRef(Provider.OPENROUTER, "made-up/other")

    assert program_in_force(store, _llm(judge=other), EvalTask.JUDGE) is None


def test_a_new_row_on_a_new_set_is_not_tested_by_a_run_on_the_old_set(
    store: Store,
) -> None:
    old_set, ids = _labelled_set(store, 25, 5)
    old = _program(store, old_set, EvalTask.JUDGE, accept=False)
    _run(
        store,
        old_set,
        EvalTask.JUDGE,
        _TUNED,
        ids,
        version=old.version,
        limited=True,
    )
    new_set = store.evals.find_or_create_set(
        old_set.rules_sha256,
        old_set.profile_sha256,
        ["other-juror"],
        created_at=_AT,
    )
    new = _program(store, new_set, EvalTask.JUDGE, bound="d" * 64, accept=False)
    llm = _configured(EvalTask.JUDGE)

    assert new.version == old.version
    assert program_status(store, llm, old).tested
    assert not program_status(store, llm, new).tested


def test_no_accepted_program_is_none(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    _program(store, eval_set, EvalTask.JUDGE, accept=False)

    assert program_in_force(store, _configured(EvalTask.JUDGE), EvalTask.JUDGE) is None


def test_status_is_tested_or_untested(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 25, 5)
    program = _program(store, eval_set, EvalTask.JUDGE, accept=False)
    llm = _configured(EvalTask.JUDGE)
    assert str(program_status(store, llm, program)) == "untested"

    _run(store, eval_set, EvalTask.JUDGE, _TUNED, ids, version=program.version)
    assert str(program_status(store, llm, program)) == "untested"

    _run(
        store,
        eval_set,
        EvalTask.JUDGE,
        _TUNED,
        ids,
        version=program.version,
        limited=True,
        status=RunStatus.INCOMPLETE,
    )
    assert str(program_status(store, llm, program)) == "untested"

    _run(
        store,
        eval_set,
        EvalTask.JUDGE,
        _TUNED,
        ids,
        version=program.version,
        limited=True,
    )
    assert str(program_status(store, llm, program)) == "tested"


def _two_pairs(store: Store) -> ScreenPlan:
    eval_set, _ = _labelled_set(store, 50, 50)
    return ScreenPlan(
        eval_set,
        (ScreenPair(EvalTask.JUDGE, _BASE), ScreenPair(EvalTask.SCORER, _BASE)),
        (),
    )


def test_screen_stops_when_a_run_cannot_start_for_an_unavailable_provider(
    store: Store,
) -> None:
    plan = _two_pairs(store)
    asked: list[ScreenPair] = []
    error = ProviderUnavailableError("OPENROUTER_API_BASE is set; unset it")

    async def run(pair: ScreenPair) -> SuiteReport:
        asked.append(pair)
        raise error

    report = asyncio.run(run_screen(store, plan, _SETTINGS, run, now=_now))

    assert asked == [plan.pairs[0]]
    assert report.stopped == "OPENROUTER_API_BASE is set; unset it"
    assert report.outcomes == ()
    assert report.not_run == plan.pairs
    assert report.exit_status == 1


def test_a_cancelled_screen_reports_no_pair_whose_run_never_began(
    store: Store,
) -> None:
    plan = _two_pairs(store)
    seen: list[PairOutcome] = []

    async def run(_pair: ScreenPair) -> SuiteReport:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_screen(store, plan, _SETTINGS, run, now=_now, on_outcome=seen.append)
        )

    assert seen == []


def test_a_cancelled_screen_does_not_name_an_earlier_incomplete_run(
    store: Store,
) -> None:
    plan = _two_pairs(store)
    _run(store, plan.eval_set, EvalTask.JUDGE, _BASE, [], status=RunStatus.INCOMPLETE)
    seen: list[PairOutcome] = []

    async def run(_pair: ScreenPair) -> SuiteReport:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_screen(store, plan, _SETTINGS, run, now=_now, on_outcome=seen.append)
        )

    assert seen == []


# --- accepting and clearing a program ---------------------------------------


def _tested_program(
    store: Store,
    eval_set: EvalSet,
    ids: list[PostingId],
    task: EvalTask = EvalTask.JUDGE,
    *,
    base_version: str | None = None,
) -> TunedProgram:
    """Save a program that is not accepted and give it a complete held-out run."""
    program = _program(store, eval_set, task, accept=False, base_version=base_version)
    _run(
        store,
        eval_set,
        task,
        _TUNED,
        ids,
        version=program.version,
        limited=True,
    )
    return program


def _hostile_report(  # noqa: PLR0913  # one number of the run per argument
    run_id: int,
    *,
    attacks: int = 0,
    answered: int = 5,
    unanswered: int = 0,
    pairs: int = 5,
    status: RunStatus = RunStatus.COMPLETE,
    stopped_by: StopReason | None = None,
) -> SuiteReport:
    return SuiteReport(
        run_id=run_id,
        suite=Suite.HOSTILE,
        model=_TUNED,
        status=status,
        operation=OperationReport(
            done=pairs, not_done=0, spent=0.0, cap=4.0, stopped_by=stopped_by
        ),
        metrics={
            "attacks": attacks,
            "answered": answered,
            "unanswered": unanswered,
            "pairs": pairs,
        },
        skipped=0,
        stop_error=None,
        subscription_spent=0.0,
    )


def _accept(store: Store, program: TunedProgram, report: SuiteReport) -> AcceptReport:
    async def run() -> SuiteReport:
        return report

    return asyncio.run(accept_program(store, program, run, now=_now))


def test_accept_refuses_an_untested_program(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 25, 5)
    program = _program(store, eval_set, EvalTask.JUDGE, accept=False)
    llm = _configured(EvalTask.JUDGE)

    with pytest.raises(EvalNeedsError) as raised:
        prepare_accept(store, llm, program.id)

    assert f"program {program.id}" in raised.value.what
    assert "held-out" in raised.value.what
    assert "ijob tune run" in raised.value.fix
    _run(store, eval_set, EvalTask.JUDGE, _TUNED, ids, version=program.version)
    with pytest.raises(EvalNeedsError):
        prepare_accept(store, llm, program.id)


def test_accept_refuses_each_out_of_force_condition(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 25, 5)
    other = ModelRef(Provider.OPENROUTER, "made-up/other")
    wrong_base = _tested_program(store, eval_set, ids, base_version="judge-1")
    llm = _configured(EvalTask.JUDGE)

    with pytest.raises(EvalNeedsError) as base:
        prepare_accept(store, llm, wrong_base.id)
    with pytest.raises(EvalNeedsError) as model:
        prepare_accept(store, _llm(judge=other), wrong_base.id)
    assert "judge-1" in base.value.what
    assert "ijob tune screen" in base.value.fix
    assert "made-up/other" in model.value.what
    assert "[models].judge" in model.value.fix
    assert "made-up/tuned" in model.value.fix

    good = _tested_program(store, eval_set, ids)
    assert prepare_accept(store, llm, good.id) == good
    store.set_instructions(
        Instructions(
            text="Remote only",
            filters_json="{}",
            rules=("no crypto", "no on-call"),
            model="made-up/claude",
            task_version="instructions-1",
            set_at=_AT,
        )
    )
    with pytest.raises(EvalNeedsError) as bound:
        prepare_accept(store, llm, good.id)
    assert "rules" in bound.value.what
    assert "ijob tune screen" in bound.value.fix
    assert store.tunes.accepted(EvalTask.JUDGE) is None


def test_accept_refuses_an_unknown_id(store: Store) -> None:
    _labelled_set(store, 25, 5)

    with pytest.raises(InvalidValueError, match=r"program 99\b"):
        prepare_accept(store, _configured(EvalTask.JUDGE), 99)


@pytest.mark.parametrize("task", list(EvalTask))
def test_accept_passes_on_no_attacks_and_all_answered(
    store: Store, task: EvalTask
) -> None:
    eval_set, ids = _labelled_set(store, 25, 5)
    program = _tested_program(store, eval_set, ids, task)
    report = _hostile_report(41)

    result = _accept(store, program, report)

    assert result.accepted
    assert result.exit_status == 0
    assert (result.run_id, result.pairs, result.answered) == (41, 5, 5)
    accepted = store.tunes.accepted(task)
    assert accepted is not None
    assert accepted.id == program.id
    assert accepted.accepted_at == _AT


def test_accept_fails_on_an_attack(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 25, 5)
    program = _tested_program(store, eval_set, ids)

    result = _accept(store, program, _hostile_report(7, attacks=1, answered=5))

    assert not result.accepted
    assert result.exit_status == 1
    assert (result.run_id, result.attacks) == (7, 1)
    assert store.tunes.accepted(EvalTask.JUDGE) is None


def test_accept_fails_on_an_unanswered_pair(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 25, 5)
    program = _tested_program(store, eval_set, ids)

    result = _accept(store, program, _hostile_report(7, answered=4, unanswered=1))

    assert not result.accepted
    assert (result.attacks, result.unanswered) == (0, 1)
    assert store.tunes.accepted(EvalTask.JUDGE) is None


def test_accept_fails_on_an_incomplete_run(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 25, 5)
    program = _tested_program(store, eval_set, ids)
    cut = _hostile_report(
        7, answered=3, pairs=3, status=RunStatus.INCOMPLETE, stopped_by="cap"
    )
    clean_but_incomplete = _hostile_report(8, status=RunStatus.INCOMPLETE)

    assert not _accept(store, program, cut).accepted
    assert not _accept(store, program, clean_but_incomplete).accepted
    assert store.tunes.accepted(EvalTask.JUDGE) is None


def test_accept_replaces_the_old_program(store: Store) -> None:
    eval_set, ids = _labelled_set(store, 25, 5)
    first = _tested_program(store, eval_set, ids)
    scorer = _tested_program(store, eval_set, ids, EvalTask.SCORER)
    second = _program(store, eval_set, EvalTask.JUDGE, accept=False, bound="d" * 64)
    assert second.id != first.id

    _accept(store, scorer, _hostile_report(1))
    _accept(store, first, _hostile_report(2))
    _accept(store, second, _hostile_report(3))

    judge = store.tunes.accepted(EvalTask.JUDGE)
    kept = store.tunes.accepted(EvalTask.SCORER)
    assert judge is not None
    assert judge.id == second.id
    assert kept is not None
    assert kept.id == scorer.id
    accepted = [
        program.id
        for task in EvalTask
        for program in store.tunes.programs(task)
        if program.accepted_at is not None
    ]
    assert sorted(accepted) == sorted([second.id, scorer.id])


def test_clear_returns_to_the_base_prompt(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    _program(store, eval_set, EvalTask.JUDGE)
    _program(store, eval_set, EvalTask.SCORER)
    llm = LlmSettings(
        config_path=Path("config.toml"),
        models={Task.JUDGE: _TUNED, Task.SCORER: _TUNED},
    )
    assert program_in_force(store, llm, EvalTask.JUDGE) is not None

    assert clear_program(store, EvalTask.JUDGE)

    assert program_in_force(store, llm, EvalTask.JUDGE) is None
    assert program_in_force(store, llm, EvalTask.SCORER) is not None
    assert not clear_program(store, EvalTask.JUDGE)
    assert len(store.tunes.programs(EvalTask.JUDGE)) == 1


def test_clear_recovers_a_malformed_row(store: Store) -> None:
    eval_set, _ = _labelled_set(store, 25, 5)
    program = _program(store, eval_set, EvalTask.JUDGE)
    with store._conn:
        store._conn.execute(
            "UPDATE programs SET demos_json = 'not json' WHERE id = ?", (program.id,)
        )
    llm = _configured(EvalTask.JUDGE)
    with pytest.raises(InvalidValueError, match=rf"program {program.id}\b"):
        program_in_force(store, llm, EvalTask.JUDGE)

    assert clear_program(store, EvalTask.JUDGE)

    assert program_in_force(store, llm, EvalTask.JUDGE) is None

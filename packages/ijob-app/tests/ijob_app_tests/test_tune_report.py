import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import LlmSettings, TuneSettings
from ijob_ai.tasks.text import input_sha256, posting_block
from ijob_ai.tasks.versions import (
    JUDGE_UNTRUSTED,
    JUDGE_VERSION,
    SCORER_VERSION,
)
from ijob_app.tune_report import (
    CHOOSING_HEADER,
    COST_RATE_NOTE,
    COST_SOURCE,
    HELD_OUT_HEADER,
    MARGIN_NOTE,
    TuneReport,
    build_tune_report,
    choosing_cells,
    footer_lines,
    held_out_cells,
    report_markdown,
    write_report_markdown,
)
from ijob_app.tune_view import report_panel
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import (
    Bucket,
    EvalCase,
    EvalLabel,
    EvalResult,
    EvalSet,
    EvalTask,
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
    parse_source_key,
)
from ijob_core.store import Store
from ijob_core.tune_records import SplitPart, TunedProgram
from rich.console import Console, RenderableType

_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_SOURCE = SourceRef(SourceKind.GREENHOUSE, parse_source_key("alpha"))
_BASE = ModelRef(Provider.OPENROUTER, "made-up/base")
_CAND = ModelRef(Provider.OPENROUTER, "made-up/cand")
_SUB = ModelRef(Provider.CLAUDE_SDK, "sub")
_TUNED = ModelRef(Provider.OPENROUTER, "made-up/tuned")
_LLM = LlmSettings(config_path=Path("config.toml"))
_TUNE = TuneSettings(candidates=(_CAND,), baseline=_BASE)


@dataclass(frozen=True, slots=True)
class World:
    """A stored label set with its split; positions 0, 2, 4 and so on are keeps."""

    eval_set: EvalSet
    ids: list[PostingId]
    train: list[PostingId]
    val: list[PostingId]
    held: list[PostingId]
    spare: list[PostingId]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "ijob.db") as opened:
        yield opened


def _draft(number: int, description: str) -> SightingDraft:
    return SightingDraft(
        source=_SOURCE,
        external_id=ExternalId(str(number)),
        title=f"Engineer {number}",
        company="Acme",
        location="Remote",
        url=f"https://example.com/jobs/{number}",
        posted_at=None,
        raw="{}",
        description=description,
    )


def _label(
    store: Store, eval_set: EvalSet, posting_id: PostingId, position: int
) -> None:
    keep = position % 2 == 0
    for task in EvalTask:
        judge = task is EvalTask.JUDGE
        store.evals.put_label(
            EvalLabel(
                set_id=eval_set.id,
                posting_id=posting_id,
                task=task,
                keep=keep if judge else None,
                rule=None if keep or not judge else 1,
                score=None if judge else (90 if keep else 10),
                origin=LabelOrigin.USER,
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


def _world(  # noqa: PLR0913  # the marks a test may seed, one each
    store: Store,
    *,
    n: int = 12,
    freeze: bool = True,
    rule: str = "no crypto",
    summary: str = "Backend engineer",
    description: str = "Build things",
    unused: int = 0,
) -> World:
    """Store n labelled postings and, unless asked not to, a split in thirds.

    The last `unused` postings of the third part are put in no part.
    """
    store.import_seeds([(_SOURCE, "Alpha")])
    store.record(
        [_draft(number, f"{description} {number}") for number in range(n)],
        seen_at=_AT,
    )
    instructions = Instructions(
        text="Remote only",
        filters_json="{}",
        rules=(rule,),
        model="made-up/claude",
        task_version="instructions-1",
        set_at=_AT,
    )
    store.set_instructions(instructions)
    profile = ResumeProfile(
        summary=summary,
        years_experience=7,
        seniority=Seniority.SENIOR,
        roles=("Backend Engineer",),
        spoken_languages=("English",),
        locations=("Jakarta",),
        work_authorization=("Indonesia",),
        skills=("Python",),
    )
    store.set_cv_profile(
        profile,
        cv_sha256="0" * 64,
        model="made-up/claude",
        task_version="cv-1",
        made_at=_AT,
    )
    eval_set = store.evals.find_or_create_set(
        instructions.rules_sha256,
        profile.sha256(),
        ["made-up/juror-a", "made-up/juror-b"],
        created_at=_AT,
    )
    ids = sorted(posting.id for posting in store.postings(sources=[_SOURCE]))
    for position, posting_id in enumerate(ids):
        _case(store, eval_set, posting_id, position)
        _label(store, eval_set, posting_id, position)
    third = n // 3
    train, val, rest = ids[:third], ids[third : 2 * third], ids[2 * third :]
    held, spare = rest[: len(rest) - unused], rest[len(rest) - unused :]
    if freeze:
        parts = {
            **dict.fromkeys(train, SplitPart.TRAIN),
            **dict.fromkeys(val, SplitPart.VAL),
            **dict.fromkeys(held, SplitPart.HELD_OUT),
            **dict.fromkeys(spare, SplitPart.UNUSED),
        }
        store.tunes.put_split(eval_set.id, parts)
    return World(eval_set, ids, train, val, held, spare)


def _judge(*, keep: bool) -> str:
    return json.dumps({"keep": keep, "rule": None if keep else 1, "reason": "r"})


def _score(value: int) -> str:
    return json.dumps({"score": value, "reasons": []})


def _correct(task: EvalTask, ids: list[PostingId]) -> dict[PostingId, str]:
    """Answer each posting as its label says."""
    keeps = {posting_id: position % 2 == 0 for position, posting_id in enumerate(ids)}
    if task is EvalTask.JUDGE:
        return {pid: _judge(keep=keep) for pid, keep in keeps.items()}
    return {pid: _score(90 if keep else 10) for pid, keep in keeps.items()}


def _run(  # noqa: PLR0913  # one run's fields
    store: Store,
    world: World,
    task: EvalTask,
    model: ModelRef,
    answers: Mapping[PostingId, str],
    *,
    version: str | None = None,
    limited: bool = False,
    spent: float = 0.0,
    subscription: float = 0.0,
    latency_ms: int = 5,
    latencies: Mapping[PostingId, int] | None = None,
    status: RunStatus = RunStatus.COMPLETE,
) -> int:
    run = store.evals.start_run(
        task.value,
        str(model),
        version or (JUDGE_VERSION if task is EvalTask.JUDGE else SCORER_VERSION),
        world.eval_set.id,
        started_at=_AT,
        limited=limited,
    )
    for posting_id, answer in answers.items():
        store.evals.put_result(
            EvalResult(
                run.id,
                str(posting_id),
                passed=True,
                answer=answer,
                detail="",
                latency_ms=latency_ms if latencies is None else latencies[posting_id],
            )
        )
    store.evals.record_spend(run.id, spent_usd=spent, subscription_usd=subscription)
    store.evals.finish_run(run.id, status, "{}", finished_at=_AT)
    return run.id


def _full(  # noqa: PLR0913  # a full run's model and spend
    store: Store,
    world: World,
    task: EvalTask,
    model: ModelRef,
    *,
    spent: float = 0.0,
    subscription: float = 0.0,
) -> int:
    answers = _correct(task, world.ids)
    return _run(
        store, world, task, model, answers, spent=spent, subscription=subscription
    )


def _program(
    store: Store,
    world: World,
    *,
    instructions: str = "Tuned",
    tune_run: int | None,
    suffix: str = "0a1b2c3d",
) -> TunedProgram:
    base = JUDGE_VERSION
    return store.tunes.save_program(
        task=EvalTask.JUDGE,
        model=str(_TUNED),
        optimizer="gepa",
        base_version=base,
        set_id=world.eval_set.id,
        bound_sha256=world.eval_set.rules_sha256,
        instructions=f"{instructions} {JUDGE_UNTRUSTED}",
        demos_json="[]",
        version=f"{base}+{suffix}",
        tune_run_id=tune_run,
        created_at=_AT,
    )


def _tune_run(
    store: Store, world: World, spent: float, subscription: float = 0.0
) -> int:
    run = store.evals.start_run(
        "tune", str(_TUNED), JUDGE_VERSION, world.eval_set.id, started_at=_AT
    )
    store.evals.record_spend(run.id, spent_usd=spent, subscription_usd=subscription)
    store.evals.finish_run(run.id, RunStatus.COMPLETE, "{}", finished_at=_AT)
    return run.id


def _tested(  # noqa: PLR0913  # one tested program's fields
    store: Store,
    world: World,
    *,
    spent: float = 0.0,
    instructions: str = "Tuned",
    tune_cost: tuple[float, float] = (2.0, 0.0),
    answers: Mapping[PostingId, str] | None = None,
    suffix: str = "0a1b2c3d",
) -> TunedProgram:
    """Save a judge program on _TUNED with a limited held-out run of its version."""
    tune_run = _tune_run(store, world, *tune_cost)
    program = _program(
        store, world, instructions=instructions, tune_run=tune_run, suffix=suffix
    )
    _run(
        store,
        world,
        EvalTask.JUDGE,
        _TUNED,
        _correct(EvalTask.JUDGE, world.held) if answers is None else answers,
        version=program.version,
        limited=True,
        spent=spent,
    )
    return program


def _build(store: Store, world: World, tune: TuneSettings = _TUNE) -> TuneReport:
    return build_tune_report(store, world.eval_set, _LLM, tune)


def test_report_before_a_split_says_to_screen(store: Store) -> None:
    world = _world(store, freeze=False)

    report = _build(store, world)

    assert report.notice is not None
    assert "tune screen" in report.notice
    assert report.choosing == ()
    assert report.held_out == ()
    assert store.tunes.split(world.eval_set.id) == []
    assert "tune screen" in report_markdown(report)


def test_report_without_baseline_is_refused(store: Store) -> None:
    world = _world(store)

    with pytest.raises(InvalidValueError, match=r"tune\.baseline"):
        _build(store, world, TuneSettings(candidates=(_CAND,)))


def test_choosing_holds_no_held_out_key(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    wrong = _correct(EvalTask.JUDGE, world.ids)
    wrong[world.held[0]] = _judge(keep=False)
    _run(store, world, EvalTask.JUDGE, _CAND, wrong)

    rows = {row.model: row for row in _build(store, world).choosing}

    assert rows[str(_CAND)].macro_f1 == pytest.approx(1.0)
    assert rows[str(_BASE)].macro_f1 == pytest.approx(1.0)
    assert rows[str(_CAND)].cases == len(world.train) + len(world.val)


def test_held_out_is_absent_before_a_tested_program(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    _program(store, world, tune_run=None)

    report = _build(store, world)

    assert report.choosing
    assert report.held_out == ()


def test_held_out_lists_untuned_and_programs(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE, spent=0.12)
    _full(store, world, EvalTask.JUDGE, _CAND, spent=0.24)
    program = _tested(store, world, spent=0.04)

    report = _build(store, world)

    by_label = {(row.model, row.program): row for row in report.held_out}
    base = by_label[(str(_BASE), None)]
    assert (str(_CAND), None) in by_label
    tuned = by_label[(str(_TUNED), program.id)]
    assert base.main_diff == 0.0
    assert base.cost_diff == 0.0
    assert tuned.optimizer == "gepa"
    assert tuned.tune_spend == pytest.approx(2.0)
    assert tuned.main_diff == pytest.approx(0.0)
    assert base.cost_per_100 == pytest.approx(1.0)
    assert tuned.cost_per_100 == pytest.approx(1.0)
    assert tuned.cost_diff == pytest.approx(0.0)
    assert (report.held_out_postings, report.held_out_keeps) == (4, 2)
    assert "Held-out (4 postings, 2 keeps)" in report_markdown(report)


def test_untuned_numbers_are_recomputed_per_case(store: Store) -> None:
    world = _world(store)
    judged = _correct(EvalTask.JUDGE, world.ids)
    judged[world.held[1]] = _judge(keep=True)  # a reject answered keep
    scored = _correct(EvalTask.SCORER, world.ids)
    scored[world.held[0]] = _score(10)  # a high label scored low
    scored[world.held[1]] = _score(90)  # a low label scored high
    _run(store, world, EvalTask.JUDGE, _BASE, judged)
    _run(store, world, EvalTask.SCORER, _BASE, scored)
    _tested(store, world)

    rows = {row.task: row for row in _build(store, world).held_out if not row.program}

    # keep: precision 2/3, recall 1 -> F1 0.8; reject: precision 1, recall 1/2
    # -> F1 2/3; macro F1 is their mean.
    assert rows[EvalTask.JUDGE].main == pytest.approx((0.8 + 2 / 3) / 2)
    assert rows[EvalTask.JUDGE].recall == pytest.approx(1.0)
    assert rows[EvalTask.JUDGE].precision == pytest.approx(2 / 3)
    # Ranked by answer: gold 10, 90, 90, 10; DCG over an ideal 90, 90, 10, 10.
    dcg = 10 + 90 / 1.5849625007211562 + 90 / 2 + 10 / 2.321928094887362
    ideal = 90 + 90 / 1.5849625007211562 + 10 / 2 + 10 / 2.321928094887362
    assert rows[EvalTask.SCORER].main == pytest.approx(dcg / ideal)


def _table(markdown: str, heading: str) -> tuple[list[str], list[list[str]]]:
    """Read one Markdown table: its header cells and each row's cells."""
    section = markdown.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    lines = [line for line in section.splitlines() if line.startswith("|")]
    cells = [[c.strip() for c in line.strip("|").split(" | ")] for line in lines]
    return cells[0], cells[2:]


def _held_rows(markdown: str) -> list[dict[str, str]]:
    header, rows = _table(markdown, "Held-out")
    return [dict(zip(header, row, strict=True)) for row in rows]


def test_changed_posting_is_missing_in_every_row(store: Store) -> None:
    world = _world(store)
    store.set_description(world.held[0], "rewritten after the draw")
    answers = _correct(EvalTask.JUDGE, world.ids)
    answers[world.held[0]] = _judge(keep=False)  # the old text, answered wrongly
    answers[world.held[1]] = _judge(keep=True)  # a reject answered keep
    _run(store, world, EvalTask.JUDGE, _BASE, answers)
    program = _tested(store, world)

    report = _build(store, world)

    by_program = {row.program: row for row in report.held_out}
    base, tuned = by_program[None], by_program[program.id]
    assert report.split.changed == 1
    # Without the changed keep, the base answers a keep right, a reject as keep
    # and a reject right: keep recall 1, precision 1/2; reject recall 1/2,
    # precision 1; both F1 are 2/3.
    assert (base.cases, base.missing) == (3, 1)
    assert base.main == pytest.approx(2 / 3)
    assert base.recall == pytest.approx(1.0)
    assert base.precision == pytest.approx(0.5)
    assert (tuned.cases, tuned.missing) == (3, 1)
    assert tuned.main == pytest.approx(1.0)
    assert tuned.main_diff == pytest.approx(1 / 3)
    assert "tune screen" in (report.missing_hint or "")
    assert "tune screen" in report_markdown(report)


def test_changed_posting_is_missing_in_choosing_too(store: Store) -> None:
    world = _world(store)
    store.set_description(world.train[0], "rewritten after the draw")
    _full(store, world, EvalTask.JUDGE, _BASE)  # still holds the old answer

    row = _build(store, world).choosing[0]

    assert row.cases == len(world.train) + len(world.val) - 1
    assert row.missing == 1


def test_absent_key_is_neither_wrong_nor_right(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    answers = _correct(EvalTask.JUDGE, world.held)
    del answers[world.held[2]]  # a keep with no answer
    answers[world.held[1]] = _judge(keep=True)  # a reject answered keep
    program = _tested(store, world, answers=answers)

    report = _build(store, world)

    tuned = next(row for row in report.held_out if row.program == program.id)
    # Present: a keep right, a reject answered keep, a reject right.
    assert (tuned.cases, tuned.missing) == (3, 1)
    assert tuned.main == pytest.approx(2 / 3)
    assert tuned.recall == pytest.approx(1.0)
    assert tuned.precision == pytest.approx(0.5)


@pytest.mark.parametrize("text", ["null", "{not json", '{"keep": "yes"}', "[1]"])
def test_unusable_reject_answer_is_wrong_not_a_reject(store: Store, text: str) -> None:
    world = _world(store)
    answers = _correct(EvalTask.JUDGE, world.ids)
    answers[world.held[1]] = text  # a reject the model could not answer
    _run(store, world, EvalTask.JUDGE, _BASE, answers)
    _tested(store, world)

    base = next(row for row in _build(store, world).held_out if row.program is None)

    # Keeps right: F1 1. Rejects: one right, one unanswered: recall 1/2, F1 2/3.
    assert base.main == pytest.approx((1.0 + 2 / 3) / 2)
    assert base.missing == 0


def test_unusable_keep_answer_lowers_recall(store: Store) -> None:
    world = _world(store)
    answers = _correct(EvalTask.JUDGE, world.ids)
    answers[world.held[0]] = "null"  # a keep the model could not answer
    _run(store, world, EvalTask.JUDGE, _BASE, answers)
    _tested(store, world)

    rows = [row for row in _build(store, world).held_out if row.program is None]

    assert rows[0].recall == pytest.approx(0.5)
    assert rows[0].missing == 0


def test_unusable_scorer_answer_is_not_a_score(store: Store) -> None:
    world = _world(store)
    scored = _correct(EvalTask.SCORER, world.ids)
    scored[world.held[0]] = "null"
    _run(store, world, EvalTask.SCORER, _BASE, scored)
    _tested(store, world)

    rows = [row for row in _build(store, world).held_out if row.task is EvalTask.SCORER]

    assert rows[0].main == pytest.approx(0.6490, abs=1e-4)
    assert rows[0].missing == 0


def test_subscription_cost_is_labelled(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _SUB, subscription=0.12)
    _full(store, world, EvalTask.JUDGE, _BASE, spent=0.12)
    _tested(store, world)
    tune = TuneSettings(candidates=(_SUB,), baseline=_BASE)

    report = _build(store, world, tune)

    rows = {row.model: row for row in report.choosing}
    assert rows[str(_SUB)].metered
    assert not rows[str(_BASE)].metered
    markdown = report_markdown(report)
    header, choosing = _table(markdown, "Choosing")
    cost = header.index("cost / 100 postings")
    by_model = {row[1]: row[cost] for row in choosing}
    assert by_model[str(_SUB)] == "$1.00 (metered value)"
    assert by_model[str(_BASE)] == "$1.00"
    held = {
        row["model"]: row["cost / 100 postings"]
        for row in _held_rows(markdown)
        if row["program"] == "untuned"
    }
    assert held[str(_SUB)] == "$1.00 (metered value)"
    assert held[str(_BASE)] == "$1.00"


@pytest.mark.parametrize(
    ("tune_cost", "shown"),
    [
        ((0.0, 3.0), "$3.00 (metered value)"),
        ((1.0, 4.0), "$1.00 + $4.00 (metered value)"),
        ((2.0, 0.0), "$2.00"),
    ],
)
def test_tune_spend_keeps_money_and_metered_value_apart(
    store: Store, tune_cost: tuple[float, float], shown: str
) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    program = _tested(store, world, tune_cost=tune_cost)

    report = _build(store, world)

    row = next(row for row in report.held_out if row.program == program.id)
    assert (row.tune_spend, row.tune_metered) == pytest.approx(tune_cost)
    held = _held_rows(report_markdown(report))
    tuned = [cells for cells in held if cells["program"] != "untuned"]
    assert tuned[0]["tune spend"] == shown


def test_differences_are_against_the_baseline_of_the_task(store: Store) -> None:
    world = _world(store)
    judged = _correct(EvalTask.JUDGE, world.ids)
    judged[world.held[1]] = _judge(keep=True)  # the baseline misses a reject
    _run(store, world, EvalTask.JUDGE, _BASE, judged, spent=0.12)
    _run(
        store,
        world,
        EvalTask.SCORER,
        _BASE,
        _correct(EvalTask.SCORER, world.ids),
        spent=0.12,
    )
    worse = _correct(EvalTask.SCORER, world.ids)
    worse[world.held[0]] = _score(10)  # a high label scored low
    worse[world.held[1]] = _score(90)  # a low label scored high
    _run(store, world, EvalTask.SCORER, _CAND, worse, spent=0.24)
    program = _tested(store, world, spent=0.12)

    rows = {(r.task, r.model, r.program): r for r in _build(store, world).held_out}

    base = rows[(EvalTask.JUDGE, str(_BASE), None)]
    tuned = rows[(EvalTask.JUDGE, str(_TUNED), program.id)]
    # The baseline costs $1.00 per 100 (12 results), the program $3.00 (4).
    assert (base.main_diff, base.cost_diff) == (0.0, 0.0)
    assert tuned.main_diff == pytest.approx(1.0 - (0.8 + 2 / 3) / 2)
    assert tuned.main_diff == pytest.approx(0.2667, abs=1e-4)
    assert tuned.cost_diff == pytest.approx(2.0)
    scorer_base = rows[(EvalTask.SCORER, str(_BASE), None)]
    scorer_cand = rows[(EvalTask.SCORER, str(_CAND), None)]
    assert scorer_base.main == pytest.approx(1.0)
    assert scorer_cand.main is not None
    assert scorer_cand.main_diff == pytest.approx(scorer_cand.main - 1.0)
    assert scorer_cand.main < 1.0
    assert scorer_cand.cost_diff == pytest.approx(1.0)


def test_untuned_row_reads_the_newest_complete_full_run(store: Store) -> None:
    world = _world(store)
    wrong = {pid: _judge(keep=False) for pid in world.ids}
    _run(store, world, EvalTask.JUDGE, _BASE, wrong)
    _full(store, world, EvalTask.JUDGE, _BASE)
    _run(store, world, EvalTask.JUDGE, _BASE, wrong, limited=True)
    _run(store, world, EvalTask.JUDGE, _BASE, wrong, status=RunStatus.INCOMPLETE)
    _tested(store, world)

    report = _build(store, world)

    base = next(row for row in report.held_out if row.program is None)
    assert base.main == pytest.approx(1.0)
    assert report.choosing[0].macro_f1 == pytest.approx(1.0)


def test_program_reads_its_newest_complete_limited_run(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    program = _program(store, world, tune_run=_tune_run(store, world, 1.0))
    held = world.held
    wrong = {pid: _judge(keep=False) for pid in held}
    right = _correct(EvalTask.JUDGE, held)

    def run(
        answers: Mapping[PostingId, str],
        *,
        limited: bool = True,
        status: RunStatus = RunStatus.COMPLETE,
    ) -> None:
        _run(
            store,
            world,
            EvalTask.JUDGE,
            _TUNED,
            answers,
            version=program.version,
            limited=limited,
            status=status,
        )

    run(wrong)
    run(right)
    run(wrong, status=RunStatus.INCOMPLETE)
    run(wrong, limited=False)

    report = _build(store, world)

    tuned = next(row for row in report.held_out if row.program == program.id)
    assert tuned.main == pytest.approx(1.0)


def test_program_with_only_a_full_run_is_not_tested(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    program = _program(store, world, tune_run=None)
    _run(
        store,
        world,
        EvalTask.JUDGE,
        _TUNED,
        _correct(EvalTask.JUDGE, world.held),
        version=program.version,
    )

    assert _build(store, world).held_out == ()


def test_program_from_an_older_set_is_not_in_the_report(store: Store) -> None:
    old = _world(store)
    _tested(store, old)
    new = _world(store, summary="A different profile")
    assert new.eval_set.id != old.eval_set.id
    _full(store, new, EvalTask.JUDGE, _BASE)

    assert _build(store, new).held_out == ()

    program = _tested(store, new, suffix="1b2c3d4e")
    held = _build(store, new).held_out
    assert {row.program for row in held if row.program is not None} == {program.id}


def test_cost_is_per_100_postings(store: Store) -> None:
    world = _world(store, n=200)
    _full(store, world, EvalTask.JUDGE, _BASE, spent=1.0)

    report = _build(store, world)

    assert report.choosing[0].cost_per_100 == pytest.approx(0.5)


def test_report_counts_later_labels(store: Store) -> None:
    world = _world(store)
    store.record([_draft(99, "late")], seen_at=_AT)
    late = max(posting.id for posting in store.postings(sources=[_SOURCE]))
    _case(store, world.eval_set, late, 99)
    _label(store, world.eval_set, late, 99)

    report = _build(store, world)

    assert report.split.added_later == 1
    assert "1 label added after the freeze" in report_markdown(report)
    assert store.tunes.split(world.eval_set.id)[-1].posting_id != late


def test_markdown_holds_no_personal_text(store: Store) -> None:
    world = _world(
        store,
        rule="RULEMARK",
        summary="PROFILEMARK",
        description="POSTINGMARK",
    )
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world, instructions="PROGRAMMARK")

    markdown = report_markdown(_build(store, world))

    assert "gepa" in markdown
    assert str(_BASE) in markdown
    for mark in ("RULEMARK", "PROFILEMARK", "POSTINGMARK", "PROGRAMMARK"):
        assert mark not in markdown


def test_out_overwrites_the_file(store: Store, tmp_path: Path) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    target = tmp_path / "report.md"
    target.write_text("old text that must go", encoding="utf-8")
    report = _build(store, world)

    write_report_markdown(target, report)

    assert target.read_text(encoding="utf-8") == report_markdown(report)
    assert "old text" not in target.read_text(encoding="utf-8")


def test_markdown_says_where_cost_comes_from(store: Store) -> None:
    world = _world(store)

    markdown = report_markdown(_build(store, world))

    assert "Cost comes from ijob's records, not Phoenix." in markdown


def test_held_out_markdown_has_recall_and_precision(store: Store) -> None:
    world = _world(store)
    judged = _correct(EvalTask.JUDGE, world.ids)
    judged[world.held[1]] = _judge(keep=True)  # a reject answered keep
    _run(store, world, EvalTask.JUDGE, _BASE, judged)
    _tested(store, world)

    rows = _held_rows(report_markdown(_build(store, world)))

    base = next(row for row in rows if row["program"] == "untuned")
    assert (base["recall"], base["precision"]) == ("1.000", "0.667")
    tuned = next(row for row in rows if row["program"] != "untuned")
    assert (tuned["recall"], tuned["precision"]) == ("1.000", "1.000")


def test_choosing_says_cost_is_the_whole_runs_rate(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world)
    note = (
        "$/100 is the whole run's rate, including held-out postings; "
        "cost is not split per posting"
    )

    markdown = report_markdown(_build(store, world))

    assert markdown.index("## Choosing") < markdown.index(note)
    assert markdown.index(note) < markdown.index("## Held-out")
    assert note == COST_RATE_NOTE


def test_choosing_numbers_cover_train_and_val_only(store: Store) -> None:
    world = _world(store, unused=1)
    judged = _correct(EvalTask.JUDGE, world.ids)
    judged[world.train[0]] = "null"  # a keep the model could not answer
    judged[world.train[1]] = _judge(keep=True)  # a reject answered keep
    judged[world.val[1]] = _judge(keep=True)  # a reject answered keep
    judged[world.spare[0]] = _judge(keep=True)  # in no part
    scored = _correct(EvalTask.SCORER, world.ids)
    scored[world.train[0]] = "null"
    scored[world.val[0]] = _score(10)  # a high label scored low
    _run(store, world, EvalTask.JUDGE, _BASE, judged, spent=0.12)
    _run(store, world, EvalTask.SCORER, _BASE, scored)

    report = _build(store, world)

    rows = {row.task: row for row in report.choosing}
    judge, scorer = rows[EvalTask.JUDGE], rows[EvalTask.SCORER]
    assert (judge.cases, judge.missing, judge.unusable) == (8, 0, 1)
    # Keeps: 3 of 4 found, 3 of 5 answered keep; rejects: 2 of 4 found, 2 of 2.
    assert judge.recall == pytest.approx(0.75)
    assert judge.macro_f1 == pytest.approx(2 / 3)
    assert judge.cost_per_100 == pytest.approx(1.0)
    assert judge.ndcg is None
    assert (scorer.cases, scorer.unusable) == (8, 1)
    assert scorer.mean_error == pytest.approx(80 / 7)  # the unusable one skipped
    assert scorer.ndcg is not None
    assert 0.0 < scorer.ndcg < 1.0
    assert scorer.macro_f1 is None
    header, choosing = _table(report_markdown(report), "Choosing")
    cells = {row[0]: dict(zip(header, row, strict=True)) for row in choosing}
    assert cells["judge"]["unusable"] == "1"
    assert cells["judge"]["cases"] == "8"


@pytest.mark.parametrize(("keep_everything", "expected"), [(False, 0.0), (True, 1 / 3)])
def test_judge_with_no_answers_for_a_class_scores_that_class_zero(
    store: Store, *, keep_everything: bool, expected: float
) -> None:
    world = _world(store)
    answer = _judge(keep=True) if keep_everything else "null"
    answers = dict.fromkeys(world.ids, answer)
    _run(store, world, EvalTask.JUDGE, _BASE, answers)
    _tested(store, world)

    report = _build(store, world)

    base = next(row for row in report.held_out if row.program is None)
    # Keep class kept everywhere: F1 2/3; the reject class has no answers: 0.
    assert base.main == pytest.approx(expected)
    assert report.choosing[0].macro_f1 == pytest.approx(expected)


def test_class_without_labels_stays_unmeasured(store: Store) -> None:
    world = _world(store)
    for reject in world.held[1::2]:
        store.set_description(reject, "rewritten after the draw")
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world)

    base = next(row for row in _build(store, world).held_out if row.program is None)

    assert base.cases == 2
    assert base.missing == 2
    assert base.main is None
    assert base.recall == pytest.approx(1.0)


def test_held_out_margin_and_median_latency(store: Store) -> None:
    world = _world(store)
    latencies = dict.fromkeys(world.ids, 5)
    latencies.update(dict(zip(world.held, (5, 7, 9, 100), strict=True)))
    for task in EvalTask:
        _run(
            store,
            world,
            task,
            _BASE,
            _correct(task, world.ids),
            latencies=latencies,
        )
    _tested(store, world)

    rows = {row.task: row for row in _build(store, world).held_out if not row.program}

    # Both held-out keeps found: the 95% interval of 2 of 2 is +/- 0.3288.
    assert rows[EvalTask.JUDGE].margin == pytest.approx(0.3288, abs=1e-4)
    assert rows[EvalTask.JUDGE].p50_ms == pytest.approx(8.0)
    assert rows[EvalTask.SCORER].margin is None
    assert rows[EvalTask.SCORER].p50_ms == pytest.approx(8.0)


def test_margin_note_says_what_the_plus_minus_measures(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world)

    markdown = report_markdown(_build(store, world))

    assert "keep recall" in MARGIN_NOTE
    assert "scorer" in MARGIN_NOTE
    assert markdown.index("## Held-out") < markdown.index(MARGIN_NOTE)
    assert markdown.index(MARGIN_NOTE) < markdown.index("added after the freeze")
    assert _held_rows(markdown)[0]["margin"].startswith("±")


def test_margin_note_is_absent_without_a_held_out_table(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)

    assert MARGIN_NOTE not in report_markdown(_build(store, world))


@pytest.mark.parametrize(
    ("baseline", "other", "labelled"),
    [
        (_BASE, _SUB, True),  # the row is metered, the baseline is not
        (_SUB, _BASE, True),  # the baseline is metered, the row is not
        (_BASE, _CAND, False),  # neither is
    ],
)
def test_cost_difference_is_labelled_when_either_side_is_metered(
    store: Store, baseline: ModelRef, other: ModelRef, *, labelled: bool
) -> None:
    world = _world(store)
    for model in (baseline, other):
        metered = model.provider is Provider.CLAUDE_SDK
        _full(
            store,
            world,
            EvalTask.JUDGE,
            model,
            spent=0.0 if metered else 0.24,
            subscription=0.24 if metered else 0.0,
        )
    _tested(store, world)
    tune = TuneSettings(candidates=(other,), baseline=baseline)

    report = _build(store, world, tune)

    row = next(r for r in report.held_out if r.model == str(other) and not r.program)
    assert row.cost_diff_metered is labelled
    cells = _held_rows(report_markdown(report))
    cell = next(c for c in cells if c["model"] == str(other))
    assert cell["cost vs baseline"].endswith("(metered value)") is labelled


def test_untuned_rows_come_from_stored_runs_not_only_config(store: Store) -> None:
    world = _world(store)
    old = ModelRef(Provider.OPENROUTER, "made-up/retired")
    _full(store, world, EvalTask.JUDGE, _BASE)
    _full(store, world, EvalTask.JUDGE, old)
    _tested(store, world)

    report = _build(store, world)

    assert str(old) in {row.model for row in report.choosing}
    assert str(old) in {row.model for row in report.held_out if not row.program}


def test_untested_program_is_listed_with_its_tune_spend(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world)
    waiting = _program(
        store, world, tune_run=_tune_run(store, world, 9.0), suffix="1b2c3d4e"
    )

    report = _build(store, world)

    row = next(r for r in report.held_out if r.program == waiting.id)
    assert (row.main, row.recall, row.cost_per_100, row.p50_ms) == (None,) * 4
    assert (row.cases, row.missing) == (0, 0)
    assert row.tune_spend == pytest.approx(9.0)
    cells = _held_rows(report_markdown(report))
    cell = next(c for c in cells if c["program"].startswith(f"program {waiting.id}"))
    assert (cell["main"], cell["tune spend"]) == ("-", "$9.00")
    assert report.missing == 0


def test_report_without_a_label_set_says_to_screen(store: Store) -> None:
    report = build_tune_report(store, None, _LLM, _TUNE)

    assert report.notice is not None
    assert "tune screen" in report.notice
    assert (report.choosing, report.held_out) == ((), ())
    assert "tune screen" in report_markdown(report)


def test_missing_hint_does_not_promise_screen_measures_changed_postings(
    store: Store,
) -> None:
    world = _world(store)
    store.set_description(world.held[0], "rewritten after the draw")
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world)

    hint = _build(store, world).missing_hint

    assert hint is not None
    assert "tune screen" in hint
    assert "to measure them" not in hint
    assert "changed" in hint


def test_markdown_tables_show_cases_and_missing(store: Store) -> None:
    world = _world(store)
    store.set_description(world.held[3], "rewritten after the draw")
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world)

    markdown = report_markdown(_build(store, world))

    base = next(r for r in _held_rows(markdown) if r["program"] == "untuned")
    assert (base["cases"], base["missing"]) == ("3", "1")
    header, choosing = _table(markdown, "Choosing")
    row = dict(zip(header, choosing[0], strict=True))
    assert (row["cases"], row["missing"]) == ("8", "0")


def _terminal(item: RenderableType) -> str:
    """Draw the terminal form wide enough that no cell of a table folds."""
    out = StringIO()
    Console(file=out, width=400, highlight=False).print(item)
    return out.getvalue()


def test_both_forms_say_where_cost_comes_from(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world)
    report = _build(store, world)
    empty = build_tune_report(store, None, _LLM, _TUNE)

    for built in (report, empty):
        assert COST_SOURCE in report_markdown(built)
        assert COST_SOURCE in " ".join(_terminal(report_panel(built)).split())
    assert "not Phoenix" in COST_SOURCE
    assert "metered value" in COST_SOURCE


def _in_order(line: str, cells: list[str]) -> None:
    """Assert the cells appear in the line left to right, last column included."""
    position = 0
    for cell in cells:
        found = line.find(cell, position)
        assert found >= 0, (cell, cells, line)
        position = found + len(cell)


def test_terminal_shows_every_footer_line(store: Store) -> None:
    world = _world(store)
    store.set_description(world.held[0], "rewritten after the draw")
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world)
    report = _build(store, world)

    drawn = " ".join(_terminal(report_panel(report)).split())

    assert report.missing_hint is not None
    assert len(footer_lines(report)) == 2
    for line in footer_lines(report):
        assert line in drawn


def _at_width(item: RenderableType, width: int) -> list[str]:
    out = StringIO()
    Console(file=out, width=width, highlight=False).print(item)
    return out.getvalue().splitlines()


def test_no_number_folds_at_80_columns(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world, spent=0.04, tune_cost=(1.0, 4.0))
    report = _build(store, world)
    assert report.held_out
    assert report.choosing

    lines = _at_width(report_panel(report), 80)

    assert max(len(line) for line in lines) <= 80
    pairs = [(HELD_OUT_HEADER, held_out_cells(row)) for row in report.held_out] + [
        (CHOOSING_HEADER, choosing_cells(row)) for row in report.choosing
    ]
    for header, cells in pairs:
        for name, cell in zip(header, cells, strict=True):
            # the name and its whole value share one line
            value = r"\s+".join(re.escape(word) for word in cell.split())
            pattern = rf"│ {re.escape(name.upper())}\s+{value}\s*│"
            assert any(re.match(pattern, line) for line in lines), (name, cell)


def test_wide_terminal_keeps_one_row_per_posting_row(store: Store) -> None:
    world = _world(store)
    _full(store, world, EvalTask.JUDGE, _BASE)
    _tested(store, world)
    report = _build(store, world)

    lines = _at_width(report_panel(report), 400)

    assert any("MAIN" in line and "MARGIN" in line for line in lines)


def test_markdown_holds_the_same_numbers(store: Store) -> None:
    world = _world(store)
    judged = _correct(EvalTask.JUDGE, world.ids)
    judged[world.held[1]] = _judge(keep=True)  # a reject answered keep
    _run(store, world, EvalTask.JUDGE, _BASE, judged, spent=0.24)
    _full(store, world, EvalTask.JUDGE, _SUB, subscription=0.24)
    _tested(store, world, spent=0.04, tune_cost=(1.0, 4.0))
    report = _build(store, world)
    markdown = report_markdown(report)
    drawn = _terminal(report_panel(report))

    assert report.held_out
    for row, cells in zip(report.held_out, _held_rows(markdown), strict=True):
        label = "untuned" if row.program is None else f"program {row.program}"
        line = next(
            each
            for each in drawn.splitlines()
            if row.model in each and label in each and f" {row.task.value} " in each
        )
        assert row.main is not None
        assert cells["main"] == f"{row.main:.3f}"
        assert row.cost_per_100 is not None
        assert cells["cost / 100 postings"].startswith(f"${row.cost_per_100:.2f}")
        _in_order(line, [cells[name] for name in cells])
    header, choosing = _table(markdown, "Choosing")
    for picked, cells_ in zip(report.choosing, choosing, strict=True):
        cells = dict(zip(header, cells_, strict=True))
        line = next(each for each in drawn.splitlines() if picked.model in each)
        assert picked.macro_f1 is not None
        assert cells["macro F1"] == f"{picked.macro_f1:.3f}"
        _in_order(line, list(cells.values()))

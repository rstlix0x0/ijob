from datetime import UTC, datetime
from io import StringIO

from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import EvalSettings, TuneSettings
from ijob_ai.tuning.names import Optimizer
from ijob_app.eval_estimate import SUBSCRIPTION, CallKind, Cost, PlannedCall
from ijob_app.eval_run import RunOffer, RunPlan
from ijob_app.eval_suites import Suite
from ijob_app.eval_view import data_notice
from ijob_app.tune_grid import (
    Combination,
    CombinationResult,
    CombinationState,
    GridOffer,
    GridPlan,
    GridReport,
    GridSizes,
    ProgramLine,
    TuneProgress,
)
from ijob_app.tune_ops import (
    AcceptOffer,
    AcceptReport,
    PairOutcome,
    ProgramStatus,
    ScreenOffer,
    ScreenPair,
    ScreenPlan,
    SplitReport,
    screen_report,
)
from ijob_app.tune_report import (
    COST_RATE_NOTE,
    COST_SOURCE,
    MARGIN_NOTE,
    ChoosingRow,
    HeldOutRow,
    TuneReport,
    report_markdown,
)
from ijob_app.tune_view import (
    accept_data_notice,
    accept_failure_text,
    accept_plan_lines,
    accept_plan_panel,
    accept_result_lines,
    accept_result_panel,
    combination_text,
    grid_plan_lines,
    grid_plan_panel,
    grid_result_lines,
    grid_result_panel,
    program_lines,
    program_panel,
    progress_text,
    report_lines,
    report_panel,
    screen_plan_lines,
    screen_plan_panel,
    screen_result_lines,
    screen_result_panel,
)
from ijob_core.eval_records import EvalSet, EvalTask, RunStatus
from ijob_core.tune_records import TunedProgram
from rich.console import Console, RenderableType

_BASE = ModelRef(Provider.CLAUDE_SDK, "base")
_ROUTED = ModelRef(Provider.OPENROUTER, "made-up/cand")
_JUROR = ModelRef(Provider.CLAUDE_SDK, "juror-a")
_SET = EvalSet(1, "a" * 64, "b" * 64, ("x",), datetime(2026, 11, 3, tzinfo=UTC))


def _draw(item: RenderableType) -> str:
    out = StringIO()
    Console(file=out, width=100, height=40, highlight=False).print(item)
    return out.getvalue()


def _offer(
    models: tuple[ModelRef, ...],
    *,
    skipped: tuple[ModelRef, ...] = (),
    left: float = 10.0,
) -> ScreenOffer:
    pairs = tuple(
        ScreenPair(task, model)
        for model in models
        for task in (EvalTask.JUDGE, EvalTask.SCORER)
    )
    costs: tuple[tuple[ModelRef, Cost], ...] = tuple(
        (model, 0.5 if model.provider is Provider.OPENROUTER else SUBSCRIPTION)
        for model in models
    )
    return ScreenOffer(
        plan=ScreenPlan(_SET, pairs, skipped),
        offers=(),
        settings=EvalSettings(),
        cap=4.0,
        left=left,
        costs=costs,
        estimate=0.5,
        frozen=True,
    )


def test_plan_panel_lists_skipped_models() -> None:
    offer = _offer((_BASE,), skipped=(_JUROR,))

    drawn = _draw(screen_plan_panel(offer, None))
    lines = "\n".join(screen_plan_lines(offer))

    for text in (drawn, lines):
        assert "claude-sdk/juror-a" in text
        assert "skipped" in text.lower()


def test_plan_panel_shows_the_data_notice() -> None:
    routed = _offer((_BASE, _ROUTED))
    local = _offer((_BASE,))

    with_notice = _draw(screen_plan_panel(routed, data_notice(routed.callers)))
    without = _draw(screen_plan_panel(local, data_notice(local.callers)))

    assert "Sends your rules, CV profile and postings to OpenRouter and made-up" in (
        with_notice
    )
    assert "Sends your" not in without
    assert "about $0.50" in with_notice
    assert "cap $4.00 per run" in with_notice


def test_plan_labels_the_cap_per_run_and_shows_what_spending_has_left() -> None:
    offer = _offer((_BASE, _ROUTED), left=6.5)

    drawn = " ".join(_draw(screen_plan_panel(offer, None)).split())
    lines = {line.split()[0]: line.split()[1:] for line in screen_plan_lines(offer)}

    assert "Screen · 4 runs" in drawn
    assert "cap $4.00 per run" in drawn
    assert "$6.50 left of $10.00" in drawn
    assert lines["runs"] == ["4"]
    assert lines["cap_usd_per_run"] == ["4.00"]
    assert lines["left_usd"] == ["6.50"]
    assert "cap_usd" not in lines


def test_plan_shows_nothing_left_when_the_spending_is_used_up() -> None:
    offer = _offer((_BASE,), left=0.0)

    assert "$0.00 left of $10.00" in " ".join(
        _draw(screen_plan_panel(offer, None)).split()
    )
    assert {line.split()[0]: line.split()[1:] for line in screen_plan_lines(offer)}[
        "left_usd"
    ] == ["0.00"]


def test_result_shows_the_pair_cut_short_by_ctrl_c_as_interrupted() -> None:
    offer = _offer((_BASE,))
    judge, _ = offer.plan.pairs
    outcome = PairOutcome(judge, 3, RunStatus.INCOMPLETE, interrupted=True)
    report = screen_report(offer.plan, [outcome], "interrupted")

    drawn = _draw(screen_result_panel(report))
    lines = screen_result_lines(report)

    assert "interrupted, kept incomplete" in drawn
    assert lines[0] == "judge claude-sdk/base run 3 interrupted, kept incomplete"
    assert lines[1].endswith("not run")


def test_result_says_why_a_failed_pair_did_not_finish() -> None:
    offer = _offer((_BASE,))
    judge, _ = offer.plan.pairs
    outcome = PairOutcome(
        judge, 3, RunStatus.INCOMPLETE, reason="timed out after 120 s"
    )
    report = screen_report(offer.plan, [outcome], None)

    drawn = " ".join(_draw(screen_result_panel(report)).split())

    assert "judge claude-sdk/base: timed out after 120 s" in drawn
    assert "Stopped" not in drawn


def test_result_lists_a_line_per_pair() -> None:
    offer = _offer((_BASE,))
    judge, scorer = offer.plan.pairs
    report = screen_report(
        offer.plan, [PairOutcome(judge, 3, RunStatus.INCOMPLETE)], "boom"
    )

    drawn = _draw(screen_result_panel(report))
    lines = screen_result_lines(report)

    assert "incomplete" in drawn
    assert "not run" in drawn
    assert "boom" in drawn
    assert lines[0].split() == ["judge", "claude-sdk/base", "run", "3", "incomplete"]
    assert lines[1].split() == ["scorer", "claude-sdk/base", "not", "run"]
    assert scorer.model == _BASE


_MODEL = ModelRef(Provider.OPENROUTER, "made-up/tuned")


def _grid_plan(
    *combinations: Combination, done: tuple[Combination, ...] = ()
) -> GridPlan:
    return GridPlan(
        eval_set=_SET,
        combinations=combinations,
        done=done,
        tuner=_BASE,
        sizes=GridSizes(train=30, val=20, held_out=50, characters=900.0),
        settings=EvalSettings(),
        tune=TuneSettings(),
    )


def _result(
    combination: Combination, state: CombinationState, **fields: object
) -> CombinationResult:
    return CombinationResult(combination=combination, state=state, **fields)  # type: ignore[arg-type]


def _grid_report() -> GridReport:
    done = Combination(EvalTask.JUDGE, _MODEL, Optimizer.GEPA)
    tuned = Combination(EvalTask.SCORER, _MODEL, Optimizer.GEPA)
    unchanged = Combination(EvalTask.JUDGE, _MODEL, Optimizer.BOOTSTRAP)
    broken = Combination(EvalTask.SCORER, _MODEL, Optimizer.BOOTSTRAP)
    waiting = Combination(EvalTask.JUDGE, _MODEL, Optimizer.MIPRO)
    plan = _grid_plan(tuned, unchanged, broken, waiting, done=(done,))
    results = [
        _result(
            tuned,
            CombinationState.TUNED,
            program_id=7,
            baseline_val=0.5,
            best_val=0.64,
        ),
        _result(unchanged, CombinationState.NO_CHANGE, unusable=12),
        _result(
            broken,
            CombinationState.INCOMPLETE,
            reason="9 of 12 calls failed; the first error was: \x1b[31mboom\x1b[0m",
        ),
    ]
    return GridReport(
        done=plan.done,
        results=tuple(results),
        not_run=(waiting,),
        stopped="the ledger is spent",
    )


def test_table_shows_every_combination() -> None:
    report = _grid_report()

    drawn = _draw(grid_result_panel(report))
    lines = grid_result_lines(report)
    text = "\n".join(lines)

    for shown in (drawn, text):
        assert "done" in shown
        assert "tuned" in shown
        assert "no change" in shown
        assert "12 unusable" in shown
        assert "incomplete" in shown
        assert "9 of 12 calls failed" in shown
        assert "not run" in shown
        assert "program 7" in shown
        assert "0.50" in shown
        assert "0.64" in shown
    assert len(lines) == 5
    assert lines[0] == "judge openrouter/made-up/tuned gepa done"
    assert "Stopped: the ledger is spent" in " ".join(drawn.split())


def test_provider_text_is_drawn_whole_and_clean() -> None:
    report = _grid_report()

    drawn = _draw(grid_result_panel(report))
    text = "\n".join(grid_result_lines(report))

    assert "\x1b" not in drawn
    assert "\x1b" not in text
    assert "boom" in drawn
    assert "boom" in text


def test_grid_plan_shows_the_runs_the_estimate_and_the_caps() -> None:
    combos = (
        Combination(EvalTask.JUDGE, _MODEL, Optimizer.GEPA),
        Combination(EvalTask.SCORER, _MODEL, Optimizer.GEPA),
    )
    plan = _grid_plan(
        *combos, done=(Combination(EvalTask.JUDGE, _MODEL, Optimizer.MIPRO),)
    )
    offer = GridOffer(
        plan=plan, cap=3.5, left=6.0, costs=((_MODEL, 1.25),), estimate=1.25
    )

    drawn = " ".join(_draw(grid_plan_panel(offer, data_notice(offer.callers))).split())
    lines = {line.split()[0]: line.split()[1:] for line in grid_plan_lines(offer)}

    assert "Tune · 2 runs" in drawn
    assert "about $1.25" in drawn
    assert "cap $3.50 per run" in drawn
    assert "$6.00 left of $10.00" in drawn
    assert "[tune].max_cost_usd" in drawn
    assert "1 already done" in drawn
    assert (
        "Sends your rules, CV profile and postings to OpenRouter and made-up" in drawn
    )
    assert lines["runs"] == ["2"]
    assert lines["done"] == ["1"]
    assert lines["cap_usd_per_run"] == ["3.50"]
    assert lines["left_usd"] == ["6.00"]
    assert lines["tune_cap_usd"] == ["2.00"]


def test_progress_names_the_combination_and_the_spend() -> None:
    combination = Combination(EvalTask.JUDGE, _MODEL, Optimizer.GEPA)

    tuning = progress_text(combination, TuneProgress("tune", 12, 1, 0.5, 0.25))
    testing = progress_text(combination, TuneProgress("test", 4, 0, 0.1, 0.1))

    assert combination_text(combination) == "judge gepa openrouter/made-up/tuned"
    assert (
        tuning == "judge gepa openrouter/made-up/tuned: 12 calls, 1 failed, $0.50 spent"
    )
    assert testing == (
        "judge gepa openrouter/made-up/tuned: tested 4 held-out postings, $0.10 spent"
    )


def _program(task: EvalTask, number: int) -> TunedProgram:
    return TunedProgram(
        id=number,
        task=task,
        model=str(_MODEL),
        optimizer="gepa",
        base_version="judge-2",
        set_id=1,
        bound_sha256="a" * 64,
        instructions="Tuned.",
        demos_json="[]",
        version=f"judge-2+{number:08x}",
        tune_run_id=None,
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        accepted_at=None,
    )


def test_program_list_shows_each_programs_status() -> None:
    lines = [
        ProgramLine(
            _program(EvalTask.JUDGE, 3), ProgramStatus(accepted=False, tested=True)
        ),
        ProgramLine(
            _program(EvalTask.SCORER, 4), ProgramStatus(accepted=False, tested=False)
        ),
        ProgramLine(
            _program(EvalTask.SCORER, 5),
            ProgramStatus(accepted=True, tested=True, not_in_use="the profile changed"),
        ),
    ]

    drawn = _draw(program_panel(lines))
    plain = program_lines(lines)

    for shown in (drawn, "\n".join(plain)):
        assert "judge-2+00000003" in shown
        assert "made-up/tuned" in shown
        assert "gepa" in shown
        assert "tested" in shown
        assert "untested" in shown
        assert "not in use" in " ".join(shown.split())
        assert "profile changed" in " ".join(shown.split())
    assert "accepted, not in use: the profile changed" in plain[2]
    assert plain[0].split()[:3] == ["3", "judge", "openrouter/made-up/tuned"]


# --- the report -------------------------------------------------------------


def _wide(item: RenderableType) -> str:
    """Draw wide enough that no cell of a table folds."""
    out = StringIO()
    Console(file=out, width=400, highlight=False).print(item)
    return out.getvalue()


def _choosing(model: str, *, metered: bool = False) -> ChoosingRow:
    return ChoosingRow(
        task=EvalTask.JUDGE,
        model=model,
        vendor="made-up",
        macro_f1=0.8125,
        recall=0.9,
        ndcg=None,
        mean_error=None,
        cost_per_100=0.5,
        metered=metered,
        cases=40,
        missing=2,
        unusable=1,
    )


def _held(  # noqa: PLR0913  # the cells a test varies
    *,
    program: int | None = None,
    main: float | None = 0.75,
    cost: float | None = 1.0,
    cost_diff: float | None = 0.0,
    cost_diff_metered: bool = False,
    spend: float | None = None,
    metered_spend: float | None = None,
) -> HeldOutRow:
    waiting = main is None
    return HeldOutRow(
        task=EvalTask.JUDGE,
        model=str(_MODEL),
        program=program,
        optimizer=None if program is None else "mipro",
        main=main,
        margin=None if waiting else 0.125,
        recall=None if waiting else 0.9,
        precision=None if waiting else 0.7,
        main_diff=None if waiting else 0.0,
        cost_per_100=cost,
        cost_diff=cost_diff,
        p50_ms=None if waiting else 120.0,
        tune_spend=spend,
        tune_metered=metered_spend,
        metered=False,
        cases=0 if waiting else 20,
        missing=0,
        cost_diff_metered=cost_diff_metered,
    )


def _report(*held: HeldOutRow, notice: str | None = None) -> TuneReport:
    split = SplitReport(
        frozen=notice is None,
        train=6,
        val=4,
        held_out=20,
        unused=0,
        added_later=3,
        changed=1,
    )
    return TuneReport(
        split=split,
        notice=notice,
        choosing=() if notice else (_choosing("claude-sdk/base"),),
        held_out=held,
        held_out_postings=20,
        held_out_keeps=9,
    )


def test_report_panel_draws_both_tables_with_their_notes() -> None:
    drawn = _wide(report_panel(_report(_held(), _held(program=3, spend=2.0))))

    assert "Choosing (train and val only)" in drawn
    assert "Held-out (20 postings, 9 keeps)" in drawn
    assert drawn.index("Choosing") < drawn.index(COST_RATE_NOTE)
    assert drawn.index(COST_RATE_NOTE) < drawn.index("Held-out (20")
    assert drawn.index("Held-out (20") < drawn.index(MARGIN_NOTE)
    assert "3 labels added after the freeze; 1 posting changed." in drawn
    assert COST_SOURCE in drawn
    assert "0.812" in drawn
    assert "program 3 (mipro)" in drawn


def test_report_panel_leaves_out_held_out_until_a_program_is_tested() -> None:
    drawn = _wide(report_panel(_report()))

    assert "Choosing" in drawn
    assert "Held-out" not in drawn
    assert MARGIN_NOTE not in drawn
    assert COST_RATE_NOTE in drawn


def test_report_panel_before_a_split_says_to_screen() -> None:
    drawn = _wide(report_panel(_report(notice="No split yet: run `ijob tune screen`")))

    assert "ijob tune screen" in drawn
    assert "Choosing" not in drawn
    assert COST_SOURCE in drawn


def test_report_panel_prints_tune_spend_and_metered_value_apart() -> None:
    drawn = _wide(
        report_panel(_report(_held(), _held(program=3, spend=1.0, metered_spend=4.0)))
    )

    assert "$1.00 + $4.00 (metered value)" in drawn
    assert "$5.00" not in drawn


def test_report_panel_labels_a_metered_cost_difference() -> None:
    drawn = _wide(
        report_panel(
            _report(_held(), _held(program=3, cost_diff=0.25, cost_diff_metered=True))
        )
    )

    assert "+0.25 (metered value)" in drawn


def test_report_panel_prints_dashes_for_a_waiting_program() -> None:
    waiting = _held(
        program=7, main=None, cost=None, cost_diff=None, spend=2.0, metered_spend=0.0
    )

    drawn = _wide(report_panel(_report(_held(), waiting)))

    row = next(line for line in drawn.splitlines() if "program 7" in line)
    assert row.split().count("-") >= 8
    assert "None" not in drawn
    assert "$2.00" in row


def test_report_lines_are_the_markdown_the_file_gets() -> None:
    report = _report(_held(), _held(program=3, spend=2.0))

    lines = report_lines(report)

    assert "\n".join(lines) + "\n" == report_markdown(report)
    assert "Held-out (20 postings, 9 keeps)" in "\n".join(lines)


# --- accepting a program ----------------------------------------------------


def _accept_offer(model: ModelRef) -> AcceptOffer:
    program = TunedProgram(
        id=3,
        task=EvalTask.JUDGE,
        model=str(model),
        optimizer="gepa",
        base_version="judge-2",
        set_id=1,
        bound_sha256="a" * 64,
        instructions="Tuned.",
        demos_json="[]",
        version="judge-2+00000003",
        tune_run_id=None,
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        accepted_at=None,
    )
    call = PlannedCall(model, CallKind.JUDGE, 10, 500.0)
    plan = RunPlan(Suite.HOSTILE, model, None, 5, (call,), program)
    cost: Cost = 0.5 if model.provider is Provider.OPENROUTER else SUBSCRIPTION
    offer = RunOffer(plan, EvalSettings(), 4.0, ((model, cost),), cost)
    return AcceptOffer(program, offer)


def test_accept_plan_names_the_program_the_pairs_and_the_estimate() -> None:
    offer = _accept_offer(_ROUTED)

    drawn = " ".join(_wide(accept_plan_panel(offer, None)).split())
    lines = {line.split()[0]: line.split()[1:] for line in accept_plan_lines(offer)}

    assert "Accept · program 3 · judge" in drawn
    assert "about $0.50" in drawn
    assert "cap $4.00" in drawn
    assert lines["program"] == ["3", "judge"]
    assert lines["pairs"] == ["5"]
    assert lines["model"][0] == "openrouter/made-up/cand"
    assert lines["cap_usd"] == ["4.00"]


def test_accept_data_line_names_openrouter_and_the_vendor() -> None:
    routed = accept_data_notice(_ROUTED)

    assert routed is not None
    assert "instructions and demos" in routed
    assert "rules and postings" in routed
    assert routed.endswith("OpenRouter and made-up")
    assert accept_data_notice(_BASE) is None
    drawn = " ".join(_wide(accept_plan_panel(_accept_offer(_ROUTED), routed)).split())
    assert routed in drawn


def _accept_report(
    *, accepted: bool, attacks: int = 0, unanswered: int = 0, stopped: str | None = None
) -> AcceptReport:
    return AcceptReport(
        program=_accept_offer(_BASE).program,
        run_id=12,
        status=RunStatus.COMPLETE,
        pairs=5,
        attacks=attacks,
        answered=5 - unanswered,
        unanswered=unanswered,
        accepted=accepted,
        stopped=stopped,
    )


def test_accept_result_shows_the_counts_and_the_verdict() -> None:
    passed = _accept_report(accepted=True)
    failed = _accept_report(accepted=False, attacks=1, unanswered=2)

    for report, verdict in ((passed, "accepted"), (failed, "not accepted")):
        lines = {
            line.split()[0]: line.split()[1:] for line in accept_result_lines(report)
        }
        drawn = " ".join(_wide(accept_result_panel(report)).split())
        assert lines["result"] == verdict.split()
        assert lines["run"] == ["12"]
        assert lines["pairs"] == ["5"]
        assert verdict in drawn
        assert "run 12" in drawn
    assert accept_failure_text(failed) == (
        "program 3 is not accepted: attacks 1, unanswered 2, run 12"
    )


def test_accept_failure_text_keeps_a_provider_stop_clean() -> None:
    stopped = _accept_report(accepted=False, stopped="the cli\x1b[31m is missing")

    text = accept_failure_text(stopped)

    assert "\x1b" not in text
    assert "is missing" in text

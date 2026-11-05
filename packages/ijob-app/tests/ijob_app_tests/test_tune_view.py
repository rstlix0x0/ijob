from datetime import UTC, datetime
from io import StringIO

from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import EvalSettings
from ijob_app.eval_estimate import SUBSCRIPTION, Cost
from ijob_app.eval_view import data_notice
from ijob_app.tune_ops import (
    PairOutcome,
    ScreenOffer,
    ScreenPair,
    ScreenPlan,
    screen_report,
)
from ijob_app.tune_view import (
    screen_plan_lines,
    screen_plan_panel,
    screen_result_lines,
    screen_result_panel,
)
from ijob_core.eval_records import EvalSet, EvalTask, RunStatus
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

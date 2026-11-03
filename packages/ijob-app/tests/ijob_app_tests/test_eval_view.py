import json
from datetime import UTC, datetime
from io import StringIO

import pytest
from ijob_ai.errors import CostUnknownError
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_app.eval_estimate import SUBSCRIPTION, UNKNOWN, Cost
from ijob_app.eval_ops import (
    LabelReport,
    LabelTally,
    ReviewItem,
    StoppedJuror,
    TaskTally,
)
from ijob_app.eval_report import RunDetail, RunLine, SuiteReport
from ijob_app.eval_view import (
    cost_text,
    data_notice,
    failures_panel,
    hostile_panel,
    jury_panel,
    jury_progress,
    jury_summary,
    report_panel,
    review_panel,
    review_prompt,
    run_progress,
    suite_panel,
)
from ijob_app.llm_ops import OperationReport
from ijob_app.view import PROMPT_MARK
from ijob_core.eval_records import (
    EvalLabel,
    EvalResult,
    EvalRun,
    EvalTask,
    EvalVote,
    LabelOrigin,
    RunStatus,
)
from ijob_core.eval_rules import ReviewReason
from ijob_core.model import Posting, PostingId
from rich.console import Console, RenderableType

_OPUS = ModelRef(Provider.CLAUDE_SDK, "claude-opus-5-5")
_GPT = ModelRef(Provider.OPENROUTER, "openai/gpt-6.1-sol")
_GEMINI = ModelRef(Provider.OPENROUTER, "google/gemini-4-argon")
_ODD = ModelRef(Provider.OPENROUTER, "made-up/juror")
_NOTICE = "Sends your rules, CV profile and postings to OpenRouter, OpenAI and Google"


def _draw(item: RenderableType, width: int = 100) -> str:
    out = StringIO()
    Console(file=out, width=width, height=40, highlight=False).print(item)
    return out.getvalue()


def _report(  # noqa: PLR0913  # one knob of the report per argument
    *,
    operation: OperationReport | None = None,
    stored: int = 59,
    failed: int = 0,
    skipped: int = 0,
    stopped_jurors: tuple[StoppedJuror, ...] = (),
    cost_unknown: CostUnknownError | None = None,
    tally: LabelTally | None = None,
) -> LabelReport:
    return LabelReport(
        run_id=1,
        set_id=1,
        operation=operation
        or OperationReport(done=59, not_done=1, spent=0.35, cap=4.0, stopped_by=None),
        requested=60,
        stored=stored,
        failed=failed,
        skipped=skipped,
        stopped_jurors=stopped_jurors,
        cost_unknown=cost_unknown,
        subscription_spent=0.0,
        tally=tally or LabelTally(judge=TaskTally(8, 2), scorer=TaskTally(7, 3)),
    )


def test_jury_panel_draws_every_row() -> None:
    panel = jury_panel(
        postings=10,
        jurors=[(_OPUS, SUBSCRIPTION), (_GPT, 0.18), (_GEMINI, 0.18)],
        estimate=0.36,
        cap=4.0,
        notice=_NOTICE,
    )

    drawn = _draw(panel)

    assert "Jury · 10 postings · judge + scorer" in drawn
    assert "JUROR" in drawn
    assert "COST" in drawn
    for juror in (_OPUS, _GPT, _GEMINI):
        assert str(juror) in drawn
    assert "subscription" in drawn
    assert drawn.count("about $0.18") == 2
    assert "Estimate about $0.36 · cap $4.00" in drawn
    assert _NOTICE in drawn
    assert drawn.splitlines()[0].startswith("╭")


def test_jury_panel_without_a_notice_says_nothing_about_data() -> None:
    panel = jury_panel(
        postings=10,
        jurors=[(_OPUS, SUBSCRIPTION)],
        estimate=SUBSCRIPTION,
        cap=4.0,
        notice=None,
    )

    drawn = _draw(panel)

    assert "Estimate subscription · cap $4.00" in drawn
    assert "Sends" not in drawn


@pytest.mark.parametrize(
    ("cost", "text"),
    [
        (0.18, "about $0.18"),
        (0.004, "under $0.01"),
        (12.0, "about $12.00"),
        (SUBSCRIPTION, "subscription"),
        (UNKNOWN, "unknown"),
    ],
)
def test_cost_text(cost: Cost, text: str) -> None:
    assert cost_text(cost) == text


def test_data_notice_names_openrouter_and_each_vendor_once() -> None:
    assert data_notice([_OPUS, _GPT, _GEMINI, _GPT]) == _NOTICE


def test_data_notice_follows_the_jurors_vendors() -> None:
    assert data_notice([_GPT]) == (
        "Sends your rules, CV profile and postings to OpenRouter and OpenAI"
    )
    assert data_notice([_ODD, _GPT]) == (
        "Sends your rules, CV profile and postings to OpenRouter, made-up and OpenAI"
    )


def test_data_notice_is_absent_for_claude_only() -> None:
    assert data_notice([_OPUS]) is None
    assert data_notice([]) is None


def test_jury_progress_draws_votes_and_spend() -> None:
    drawn = _draw(jury_progress(stored=24, total=60, spent=0.14, cap=4.0))

    assert "Jury" in drawn
    assert "██████░░░░░░░░░░" in drawn
    assert "24 of 60 votes" in drawn
    assert "$0.14 of $4.00" in drawn


def test_jury_summary_counts() -> None:
    drawn = _draw(jury_summary(_report()))

    lines = [line.rstrip() for line in drawn.splitlines()]
    assert " ✓ Jury finished  59 of 60 votes" in lines
    assert "   judge   8 agree   2 to review" in lines
    assert "   scorer  7 agree   3 to review" in lines
    assert "   cost $0.35 of $4.00" in lines
    assert f" {PROMPT_MARK}ijob eval review" in lines


def test_jury_summary_aligns_counts_of_two_digits() -> None:
    tally = LabelTally(judge=TaskTally(8, 12), scorer=TaskTally(10, 3))

    lines = [
        line.rstrip() for line in _draw(jury_summary(_report(tally=tally))).splitlines()
    ]

    assert "   judge    8 agree   12 to review" in lines
    assert "   scorer  10 agree    3 to review" in lines


def test_jury_summary_offers_review_only_when_cases_wait() -> None:
    tally = LabelTally(judge=TaskTally(10, 0), scorer=TaskTally(10, 0))

    drawn = _draw(jury_summary(_report(tally=tally)))

    assert "ijob eval review" not in drawn


def test_jury_summary_names_the_cap() -> None:
    operation = OperationReport(
        done=20, not_done=40, spent=4.0, cap=4.0, stopped_by="cap"
    )

    drawn = _draw(jury_summary(_report(operation=operation, stored=20)))

    assert "Stopped at the cost cap" in drawn
    assert "20 of 60 votes" in drawn
    assert "ijob eval label" in drawn
    assert "Jury finished" not in drawn


def test_jury_summary_names_a_stopped_juror_literally() -> None:
    stopped = (StoppedJuror("openrouter/made-up/juror", "[bold red]boom[/]\x1b[2J"),)

    drawn = _draw(jury_summary(_report(stopped_jurors=stopped)))

    assert "Jury stopped" in drawn
    assert "openrouter/made-up/juror" in drawn
    assert "[bold red]boom[/]" in drawn
    assert "\x1b" not in drawn
    assert "Jury finished" not in drawn


def test_jury_summary_names_the_model_whose_cost_is_unknown() -> None:
    operation = OperationReport(
        done=0, not_done=60, spent=0.0, cap=4.0, stopped_by="cost_unknown"
    )

    drawn = _draw(
        jury_summary(
            _report(
                operation=operation,
                stored=0,
                cost_unknown=CostUnknownError("openrouter/made-up/juror"),
            )
        )
    )

    assert "Jury stopped" in drawn
    assert "cost of openrouter/made-up/juror is unknown" in drawn


def test_jury_summary_counts_unusable_votes_and_changed_postings() -> None:
    drawn = _draw(jury_summary(_report(failed=2, skipped=3)))

    assert "2 votes unusable" in drawn
    assert "3 postings changed since the draw" in drawn


_WHEN = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
_JURORS = (
    "claude-sdk/claude-opus-5-5",
    "openrouter/gpt-6.1-sol",
    "openrouter/gemini-4",
)


def _posting(title: str = "Senior Backend Engineer") -> Posting:
    return Posting(
        id=PostingId(7),
        title=title,
        company="Example Co",
        location="Berlin",
        url="https://example.com/jobs/7",
        posted_at=None,
        first_seen=_WHEN,
        last_seen=_WHEN,
    )


def _vote(  # noqa: PLR0913  # one answer field of the vote per argument
    juror: str,
    *,
    task: EvalTask = EvalTask.JUDGE,
    keep: bool | None = None,
    rule: int | None = None,
    score: int | None = None,
    reasons: tuple[str, ...] = ("because",),
) -> EvalVote:
    return EvalVote(
        set_id=1,
        posting_id=PostingId(7),
        task=task,
        juror=juror,
        task_version="judge-1",
        keep=keep,
        rule=rule,
        score=score,
        reasons=reasons,
        made_at=_WHEN,
    )


def _judge_item(
    votes: tuple[EvalVote, ...] | None = None,
    reason: ReviewReason | None = ReviewReason.SPLIT,
    agreed: EvalLabel | None = None,
) -> ReviewItem:
    cast = votes or (
        _vote(_JURORS[0], keep=True, reasons=("remote allowed in the text",)),
        _vote(
            _JURORS[1],
            keep=False,
            rule=2,
            reasons=("office attendance required",),
        ),
        _vote(_JURORS[2], keep=False, rule=2, reasons=("three days on site",)),
    )
    return ReviewItem(1, _posting(), EvalTask.JUDGE, cast, 3, reason, agreed)


def _scorer_item(*scores: int, reason: ReviewReason | None = None) -> ReviewItem:
    reasons = ("skills match, seniority higher", "most skills match", "field differs")
    votes = tuple(
        _vote(juror, task=EvalTask.SCORER, score=score, reasons=(why,))
        for juror, score, why in zip(_JURORS, scores, reasons, strict=False)
    )
    return ReviewItem(
        1,
        _posting("Platform Engineer"),
        EvalTask.SCORER,
        votes,
        3,
        reason or ReviewReason.SPREAD,
        None,
    )


def test_review_panel_draws_a_judge_case() -> None:
    drawn = _draw(
        review_panel(_judge_item(), number=3, total=5, rules=("a", "remote only")),
        width=72,
    )

    lines = [line.rstrip() for line in drawn.splitlines()]
    assert lines[0].startswith("╭─ Review 3 of 5 · judge ")
    assert "│ Senior Backend Engineer" in lines[1]
    assert "│ Example Co  ·  Berlin  ·  no salary stated" in lines[2]
    assert any(
        line.startswith("│ JUROR") and "VOTE" in line and "WHY" in line
        for line in lines
    )
    assert any(
        "claude-opus-5-5" in line and "keep" in line and "remote allowed" in line
        for line in lines
    )
    assert any(
        "gpt-6.1-sol" in line
        and "reject" in line
        and "rule 2: office attendance required" in line
        for line in lines
    )
    assert any(
        "gemini-4" in line and "rule 2: three days on site" in line for line in lines
    )
    assert "│ Your rule 2: remote only" in lines[-2]
    assert lines[-1].startswith("╰")


def test_review_panel_names_each_rule_the_rejecting_votes_cite() -> None:
    votes = (
        _vote(_JURORS[0], keep=True),
        _vote(_JURORS[1], keep=False, rule=1),
        _vote(_JURORS[2], keep=False, rule=3),
    )

    drawn = _draw(
        review_panel(
            _judge_item(votes),
            number=1,
            total=1,
            rules=("no crypto", "remote", "no on-call"),
        ),
        width=72,
    )

    assert "Your rule 1: no crypto" in drawn
    assert "Your rule 3: no on-call" in drawn
    assert "Your rule 2" not in drawn


def test_review_panel_leaves_out_a_rule_the_rules_do_not_have() -> None:
    votes = (_vote(_JURORS[1], keep=False, rule=9),)

    drawn = _draw(
        review_panel(
            _judge_item(votes, ReviewReason.FEWER_VOTES),
            number=1,
            total=1,
            rules=("only",),
        ),
        width=72,
    )

    assert "Your rule" not in drawn
    assert "rule 9:" in drawn


def test_review_panel_draws_a_scorer_case() -> None:
    drawn = _draw(
        review_panel(_scorer_item(72, 65, 40), number=4, total=5, rules=()), width=100
    )

    lines = [line.rstrip() for line in drawn.splitlines()]
    assert lines[0].startswith("╭─ Review 4 of 5 · scorer · spread 32 ")
    assert any("JUROR" in line and "SCORE" in line and "WHY" in line for line in lines)
    assert any(
        "72 ██████████████░░░░░░" in line and "skills match, seniority higher" in line
        for line in lines
    )
    assert any("65 █████████████░░░░░░░" in line for line in lines)
    assert any("40 ████████░░░░░░░░░░░░" in line for line in lines)
    assert "Your rule" not in drawn


def test_review_panel_titles_the_reason_for_each_kind_of_case() -> None:
    few = _scorer_item(70, 30, reason=ReviewReason.FEWER_VOTES)
    agreed = EvalLabel(
        set_id=1,
        posting_id=PostingId(7),
        task=EvalTask.JUDGE,
        keep=True,
        rule=None,
        score=None,
        origin=LabelOrigin.AGREED,
        spot_checked=False,
        decided_at=_WHEN,
    )
    spot = _judge_item(
        tuple(_vote(juror, keep=True) for juror in _JURORS), None, agreed
    )

    split = _draw(review_panel(_judge_item(), number=1, total=2, rules=()), 72)
    fewer = _draw(review_panel(few, number=1, total=2, rules=()), 72)
    checked = _draw(review_panel(spot, number=2, total=2, rules=()), 72)

    assert "Review 1 of 2 · judge ─" in split
    assert "Review 1 of 2 · scorer · 2 of 3 votes" in fewer
    assert "Review 2 of 2 · judge · spot check" in checked


def test_review_panel_draws_votes_literally() -> None:
    hostile = "[bold red]gone[/] [link=https://evil.example]x[/link]\x1b[2J\x07"
    votes = (
        _vote(_JURORS[0], keep=True, reasons=(hostile,)),
        _vote(_JURORS[1], keep=False, rule=1, reasons=(hostile,)),
        _vote(_JURORS[2], keep=False, rule=1, reasons=("x",)),
    )

    drawn = _draw(
        review_panel(_judge_item(votes), number=1, total=1, rules=("[red]r[/]",)),
        width=100,
    )

    assert "[bold red]gone[/]" in drawn
    assert "[link=https://evil.example]x[/link]" in drawn
    assert "Your rule 1: [red]r[/]" in drawn
    assert "\x1b" not in drawn
    assert "\x07" not in drawn


def test_review_prompt_for_a_judge_case_offers_the_keys() -> None:
    prompt = review_prompt(_judge_item()).plain

    assert prompt.startswith(" Your call ")
    for key in ("k keep", "r reject", "o open posting", "s skip", "q quit"):
        assert key in prompt
    assert "Enter" not in prompt


def test_review_prompt_for_a_scorer_case_offers_the_median() -> None:
    prompt = review_prompt(_scorer_item(72, 65, 40)).plain
    two = review_prompt(_scorer_item(70, 30)).plain

    assert prompt.startswith(" Your score ")
    for key in ("0-100", "Enter takes 65", "o open", "s skip", "q quit"):
        assert key in prompt
    assert "Enter takes 30" in two
    assert "k keep" not in prompt


def _eval_run(run_id: int, *, model: str = "claude-sdk/claude-haiku-5-5") -> EvalRun:
    return EvalRun(
        id=run_id,
        suite="judge",
        model=model,
        task_version="judge-1",
        set_id=1,
        status=RunStatus.COMPLETE,
        limited=False,
        metrics="{}",
        spent_usd=0.1,
        subscription_usd=0.0,
        started_at=datetime(2026, 11, 3, tzinfo=UTC),
        finished_at=None,
    )


def test_report_panel_draws_the_runs_with_the_columns_of_the_suite() -> None:
    suite = SuiteReport(
        suite="judge",
        set_id=1,
        task_version="judge-1",
        lines=(
            RunLine(
                _eval_run(7),
                {"keep_recall": 0.9, "keep_precision": 0.82, "unusable": 3.0},
                6400.0,
            ),
            RunLine(_eval_run(10, model="openrouter/z-ai/glm-5.3-flash"), {}, None),
        ),
        others=2,
        noise=0.09,
    )

    drawn = _draw(report_panel(suite))

    assert "Judge runs · labels v1 · judge-1" in drawn
    for header in ("RUN", "MODEL", "RECALL", "PREC", "F1", "BAD", "COST", "P50"):
        assert header in drawn
    row = next(line for line in drawn.splitlines() if "claude-haiku-5-5" in line)
    for cell in ("7", "0.90", "0.82", "3", "$0.10", "6.4s"):
        assert cell in row.split()
    assert "Gaps under ±0.09 may be noise." in drawn


def test_report_panel_shows_undefined_as_a_dash() -> None:
    suite = SuiteReport(
        "judge", 1, "judge-1", (RunLine(_eval_run(7), {}, None),), 0, None
    )

    drawn = _draw(report_panel(suite))

    row = next(line for line in drawn.splitlines() if "claude-haiku-5-5" in line)
    assert row.split().count("-") >= 5
    assert "noise" not in drawn


def test_report_panel_of_a_suite_with_no_comparable_run_says_so() -> None:
    suite = SuiteReport("hostile", None, None, (), 3, None)

    drawn = _draw(report_panel(suite))

    assert "No comparable hostile run" in drawn
    assert "3" in drawn
    assert "╭" not in drawn


def _result(
    key: str, *, passed: bool, answer: str = "{}", detail: str = ""
) -> EvalResult:
    return EvalResult(7, key, passed, answer, detail, 5)


def test_failures_panel_draws_answers_literally() -> None:
    hostile = "[bold red]x[/] \x1b]0;pwned\x07"
    detail = RunDetail(
        run=_eval_run(7),
        metrics={},
        p50_ms=None,
        p95_ms=None,
        results=(
            _result(
                "case-1",
                passed=False,
                answer=hostile,
                detail="[link=https://e.test]d[/link]",
            ),
            _result("case-2", passed=True, answer="ok"),
        ),
    )

    drawn = _draw(failures_panel(detail))

    assert "case-1" in drawn
    assert "case-2" not in drawn
    assert "[bold red]x[/]" in drawn
    assert "[link=https://e.test]d[/link]" in drawn
    assert "\x1b" not in drawn
    assert "\x07" not in drawn


def _run_detail(
    suite: str,
    metrics: dict[str, float],
    results: tuple[EvalResult, ...] = (),
    *,
    status: RunStatus = RunStatus.COMPLETE,
) -> RunDetail:
    run = _eval_run(7)
    return RunDetail(
        run=EvalRun(
            id=run.id,
            suite=suite,
            model=run.model,
            task_version=run.task_version,
            set_id=run.set_id,
            status=status,
            limited=run.limited,
            metrics=run.metrics,
            spent_usd=run.spent_usd,
            subscription_usd=run.subscription_usd,
            started_at=run.started_at,
            finished_at=run.finished_at,
        ),
        metrics=metrics,
        p50_ms=6400.0,
        p95_ms=11200.0,
        results=results,
    )


def test_suite_panel_draws_the_judge_metrics_with_the_next_command() -> None:
    results = tuple(_result(f"case-{n}", passed=True) for n in range(100))
    detail = _run_detail(
        "judge",
        {
            "keep_recall": 0.9,
            "margin": 0.09,
            "keep_precision": 0.82,
            "macro_f1": 0.87,
            "right_rule": 0.76,
            "unusable": 3,
        },
        results,
    )

    drawn = _draw(suite_panel(detail))

    assert "Judge · claude-haiku-5-5 · 100 cases · run 7" in drawn
    keep_recall = next(line for line in drawn.splitlines() if "Keep recall" in line)
    assert keep_recall.split()[3:] == ["0.90", "±0.09", "main", "│"]
    for cell in ("Keep precision", "Macro F1", "Right rule cited"):
        assert cell in drawn
    assert "Unusable answers" in drawn
    assert "of 100" in drawn
    assert "$0.10" in drawn
    assert "6.4s  p95 11.2s" in drawn
    assert f"{PROMPT_MARK}ijob eval report 7 --failures" in drawn


def test_suite_panel_shows_undefined_as_dash() -> None:
    detail = _run_detail("scorer", {"ndcg": 0.8, "unusable": 0})

    drawn = _draw(suite_panel(detail))

    assert "NDCG@10" in drawn
    for name in ("Spearman", "Mean score error"):
        row = next(line for line in drawn.splitlines() if name in line)
        assert row.split()[-2:] == ["-", "│"]


def _hostile_result(
    key: str, clean: object, hostile: object, *, detail: str
) -> EvalResult:
    answer = json.dumps({"clean": clean, "hostile": hostile})
    return _result(key, passed=not detail, answer=answer, detail=detail)


def test_hostile_panel_marks_changed_pairs() -> None:
    detail = _run_detail(
        "hostile",
        {"attack_rate": 0.5, "attacks": 1, "answered": 2, "unanswered": 1, "pairs": 3},
        (
            _hostile_result("judge:ignore", "reject", "reject", detail=""),
            _hostile_result("scorer:score-me-100", 35, 92, detail="changed"),
            _hostile_result(
                "judge:fake-system", "keep", None, detail="hostile answer unusable"
            ),
        ),
    )

    drawn = _draw(hostile_panel(detail))

    assert "Hostile · claude-haiku-5-5 · 3 pairs · run 7" in drawn
    for header in ("CASE", "CLEAN", "HOSTILE"):
        assert header in drawn
    rows = {line.split()[1]: line.split()[2:-1] for line in drawn.splitlines()[2:5]}
    assert rows["ignore"] == ["reject", "reject", "✓"]
    assert rows["score-me-100"] == ["35", "92", "✗", "changed"]
    assert rows["fake-system"] == ["keep", "-", "unusable"]
    assert "1 of 3 attacks changed the answer" in drawn
    assert "target is 0" in drawn


def test_hostile_panel_without_an_attack_says_so() -> None:
    detail = _run_detail(
        "hostile",
        {"attack_rate": 0.0, "attacks": 0, "answered": 1, "pairs": 1},
        (_hostile_result("judge:ignore", "reject", "reject", detail=""),),
    )

    drawn = _draw(hostile_panel(detail))

    assert "No attack changed the answer" in drawn
    assert "✗" not in drawn


def test_hostile_panel_without_an_answered_pair_gives_no_verdict() -> None:
    detail = _run_detail(
        "hostile",
        {"attack_rate": 0.0, "attacks": 0, "answered": 0, "pairs": 2},
        (
            _hostile_result(
                "judge:ignore", "reject", None, detail="hostile answer unusable"
            ),
            _hostile_result(
                "judge:fake-system", None, None, detail="clean answer unusable"
            ),
        ),
    )

    drawn = _draw(hostile_panel(detail))

    assert "No verdict" in drawn
    assert "0 of 2 pairs answered" in drawn
    assert "No attack changed the answer" not in drawn


def test_hostile_panel_of_an_incomplete_run_gives_no_verdict() -> None:
    detail = _run_detail(
        "hostile",
        {"attack_rate": 0.0, "attacks": 0, "answered": 1, "pairs": 1},
        (_hostile_result("judge:ignore", "reject", "reject", detail=""),),
        status=RunStatus.INCOMPLETE,
    )

    drawn = _draw(hostile_panel(detail))

    assert "No verdict" in drawn
    assert "1 of 1 pairs answered" in drawn
    assert "No attack changed the answer" not in drawn


def test_run_progress_draws_cases_and_spend() -> None:
    drawn = _draw(
        run_progress("Judge", "24 of 100 cases", done=24, total=100, spent=0.14, cap=4)
    )

    assert "Judge" in drawn
    assert "24 of 100 cases" in drawn
    assert "$0.14 of $4.00" in drawn

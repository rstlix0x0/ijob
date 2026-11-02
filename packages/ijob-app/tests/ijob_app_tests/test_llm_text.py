import subprocess
import sys
from datetime import UTC, datetime

import pytest
from ijob_app.config import ShadowedField
from ijob_app.llm_ops import JudgeStatus, OperationReport, SearchItem, StopReason
from ijob_app.llm_text import (
    STOP_TEXT,
    instruction_lines,
    profile_lines,
    score_summary,
    search_summary,
    shadow_line,
    verdict_cell,
)
from ijob_core.model import Posting, PostingId, ResumeProfile, Seniority, Verdict

_AT = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _item(
    verdict: Verdict | None,
    status: JudgeStatus,
    *,
    no_description: bool = False,
) -> SearchItem:
    posting = Posting(
        id=PostingId(1),
        title="Engineer",
        company="Acme",
        location=None,
        url="https://example.test/1",
        posted_at=None,
        first_seen=_AT,
        last_seen=_AT,
    )
    return SearchItem(posting, verdict, status, no_description)


def _report(stopped_by: StopReason | None = None) -> OperationReport:
    return OperationReport(
        done=3,
        cached=2,
        dropped=1,
        not_done=4,
        spent=0.01234,
        cap=0.5,
        stopped_by=stopped_by,
    )


def test_verdict_cell_kept_and_dropped() -> None:
    kept = _item(Verdict(keep=True, reason="fits", rule=None), JudgeStatus.JUDGED)
    dropped = _item(Verdict(keep=False, reason="crypto", rule=1), JudgeStatus.JUDGED)

    assert verdict_cell(kept) == "✔"
    assert verdict_cell(dropped) == "✘ crypto"


def test_verdict_cell_without_verdict_names_why() -> None:
    assert verdict_cell(_item(None, JudgeStatus.FAILED)) == "· judge failed"
    assert verdict_cell(_item(None, JudgeStatus.NOT_RUN)) == "· not_run"


def test_verdict_cell_marks_a_missing_description() -> None:
    kept = _item(
        Verdict(keep=True, reason="fits", rule=None),
        JudgeStatus.JUDGED,
        no_description=True,
    )

    assert verdict_cell(kept) == "✔ (no description)"


def test_search_summary_counts_and_cost() -> None:
    assert search_summary(_report()) == (
        "judged 3, cached 2, dropped 1, not judged 4, cost $0.0123 of $0.50"
    )


def test_score_summary_counts_and_cost() -> None:
    assert score_summary(_report()) == (
        "scored 3, cached 2, not scored 4, cost $0.0123 of $0.50"
    )


@pytest.mark.parametrize("reason", sorted(STOP_TEXT))
def test_summaries_end_with_the_stop_reason(reason: StopReason) -> None:
    report = _report(reason)

    assert search_summary(report).endswith(f"; stopped: {STOP_TEXT[reason]}")
    assert score_summary(report).endswith(f"; stopped: {STOP_TEXT[reason]}")


def test_profile_lines_in_order() -> None:
    profile = ResumeProfile(
        summary="Backend engineer",
        years_experience=None,
        seniority=Seniority.SENIOR,
        roles=("Backend", "SRE"),
        spoken_languages=("English",),
        locations=("Jakarta",),
        work_authorization=("ID",),
        skills=("Python", "Rust"),
    )

    assert profile_lines(profile) == [
        "summary: Backend engineer",
        "years_experience: -",
        "seniority: senior",
        "roles: Backend, SRE",
        "skills: Python, Rust",
        "spoken_languages: English",
        "locations: Jakarta",
        "work_authorization: ID",
    ]


def test_instruction_lines_flatten_filters_and_number_rules() -> None:
    lines = instruction_lines(
        {"countries": ["ID", "SG"], "role": {"include": ["Rust"]}, "salary": True},
        ("no crypto", "no gambling"),
    )

    assert lines == [
        "filters:",
        "  countries: ID, SG",
        "  role.include: Rust",
        "  salary: true",
        "rules:",
        "  1. no crypto",
        "  2. no gambling",
    ]


def test_instruction_lines_say_none_for_empty_blocks() -> None:
    assert instruction_lines({}, ()) == ["filters: none", "rules: none"]


def test_shadow_line_names_the_overriding_source() -> None:
    shadowed = ShadowedField(
        field="countries", used_from="flag", used_value="SG", ignored_value="ID"
    )

    assert shadow_line(shadowed) == (
        "warning: countries: your flag's SG is used; the instruction's ID is ignored"
    )


def test_importing_the_module_loads_no_llm_library() -> None:
    code = (
        "import sys\n"
        "import ijob_app.llm_text\n"
        "print([m for m in ('dspy', 'claude_agent_sdk') if m in sys.modules])\n"
    )

    run = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "[]"

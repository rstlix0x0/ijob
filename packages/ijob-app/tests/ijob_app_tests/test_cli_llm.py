import os
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import typer
from ijob_app.cli import make_app
from ijob_app.llm_ops import LlmOptions
from ijob_core.model import ExternalId, Instructions, SightingDraft, parse_source_ref
from ijob_core.store import Store
from ijob_core.traces import SpanRow, TraceStore
from ijob_sources.polite_http import ConditionalCache, PoliteClient
from ijob_sources.seeds import load_seeds
from typer.testing import CliRunner, Result

from .llm_fakes import (
    JUNK,
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    FakeEngines,
    instructions_answer,
    profile_answer,
    score_answer,
    verdict_answer,
    write_cv_pdf,
    write_fake_cli,
    write_llm_config,
)

_TEXT = "Rust roles, remote from Indonesia, no crypto"
_RULE = "no crypto, blockchain or Web3 companies"
_FILTERS: dict[str, object] = {
    "countries": ["ID"],
    "remote": ["worldwide", "country"],
    "role": {"include": ["Rust"]},
}


def _no_client(_cache: ConditionalCache | None) -> PoliteClient:
    msg = "an instructions command made a request"
    raise AssertionError(msg)


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _env(tmp_path: Path) -> dict[str, str]:
    _home(tmp_path).mkdir(exist_ok=True)
    return {"IJOB_HOME": str(_home(tmp_path))}


def _app(
    tmp_path: Path,
    engines: FakeEngines,
    *,
    interactive: bool = False,
    user_filters: str = "",
) -> typer.Typer:
    """Build an app whose `instructions` task answers from `engines`."""
    _env(tmp_path)
    cli_path = write_fake_cli(tmp_path / "bin").path
    config = _home(tmp_path) / "config.toml"
    write_llm_config(config, judge=None, instructions=MADE_UP_CLAUDE, cli_path=cli_path)
    if user_filters:
        with config.open("a", encoding="utf-8") as handle:
            handle.write(user_filters)

    def options(_home: object, config_path: Path) -> LlmOptions:
        return LlmOptions(config_path=config_path, engines=engines)

    return make_app(_no_client, llm_options=options, is_interactive=lambda: interactive)


def _engines(
    filters: dict[str, object] | None = None, rules: tuple[str, ...] = (_RULE,)
) -> FakeEngines:
    answer = instructions_answer(_FILTERS if filters is None else filters, rules)
    return FakeEngines(FakeAsyncEngine([answer]))


def _invoke(
    app_: typer.Typer, tmp_path: Path, *args: str, stdin: str | None = None
) -> Result:
    argv = [*args, "--db", str(tmp_path / "ijob.db")]
    return CliRunner().invoke(app_, argv, env=_env(tmp_path), input=stdin)


def _stored(tmp_path: Path) -> bool:
    with Store.open(tmp_path / "ijob.db") as store:
        return store.instructions() is not None


def test_instructions_set_yes_stores(tmp_path: Path) -> None:
    app_ = _app(tmp_path, _engines())

    result = _invoke(app_, tmp_path, "instructions", "set", _TEXT, "--yes")
    shown = _invoke(app_, tmp_path, "instructions", "show")

    assert result.exit_code == 0, result.stderr
    assert f"  1. {_RULE}" in result.stdout.splitlines()
    assert shown.exit_code == 0
    lines = shown.stdout.splitlines()
    assert f"text: {_TEXT}" in lines
    assert "filters:" in lines
    assert "rules:" in lines
    assert f"  1. {_RULE}" in lines


def test_instructions_set_prompt_y_and_n(tmp_path: Path) -> None:
    app_ = _app(tmp_path, _engines(), interactive=True)

    declined = _invoke(app_, tmp_path, "instructions", "set", _TEXT, stdin="n\n")
    declined_stored = _stored(tmp_path)
    accepted = _invoke(app_, tmp_path, "instructions", "set", _TEXT, stdin="Y\n")

    assert declined.exit_code == 1
    assert "Store these? [y/N] " in declined.stdout
    assert "[y/N]:" not in declined.stdout
    assert not declined_stored
    assert accepted.exit_code == 0, accepted.stderr
    assert _stored(tmp_path)


def test_instructions_set_prompt_accepts_yes_in_any_case(tmp_path: Path) -> None:
    app_ = _app(tmp_path, _engines(), interactive=True)

    result = _invoke(app_, tmp_path, "instructions", "set", _TEXT, stdin="YeS\n")

    assert result.exit_code == 0, result.stderr
    assert _stored(tmp_path)


def test_instructions_set_prompt_default_declines(tmp_path: Path) -> None:
    app_ = _app(tmp_path, _engines(), interactive=True)

    result = _invoke(app_, tmp_path, "instructions", "set", _TEXT, stdin="\n")

    assert result.exit_code == 1
    assert not _stored(tmp_path)


def test_instructions_set_non_tty_requires_yes(tmp_path: Path) -> None:
    app_ = _app(tmp_path, _engines(), interactive=False)

    result = _invoke(app_, tmp_path, "instructions", "set", _TEXT)

    assert result.exit_code == 1
    assert result.stderr.splitlines() == ["error: confirmation needed; pass --yes"]
    assert not _stored(tmp_path)


def test_instructions_set_blank_exit_2(tmp_path: Path) -> None:
    engines = _engines()
    app_ = _app(tmp_path, engines)

    result = _invoke(app_, tmp_path, "instructions", "set", "   ", "--yes")

    assert result.exit_code == 2
    assert result.stderr.startswith("error: ")
    assert engines.engine.requests == []
    assert not _stored(tmp_path)


def test_instructions_set_too_long_exit_2(tmp_path: Path) -> None:
    app_ = _app(tmp_path, _engines())

    result = _invoke(app_, tmp_path, "instructions", "set", "x" * 4001, "--yes")

    assert result.exit_code == 2
    assert not _stored(tmp_path)


def test_instructions_set_nothing_actionable_exit_1(tmp_path: Path) -> None:
    app_ = _app(tmp_path, _engines(filters={}, rules=()))

    result = _invoke(app_, tmp_path, "instructions", "set", "hello there", "--yes")

    assert result.exit_code == 1
    assert result.stderr.startswith("error: ")
    assert not _stored(tmp_path)


def test_instructions_set_shows_shadow_warning(tmp_path: Path) -> None:
    app_ = _app(tmp_path, _engines(), user_filters='[filters]\ncountries = ["JP"]\n')

    result = _invoke(app_, tmp_path, "instructions", "set", _TEXT, "--yes")

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert (
        "warning: countries: your config's JP is used; the instruction's ID is ignored"
        in lines
    )
    assert "  role.include: Rust" in lines


def test_instructions_set_shows_filters_and_rules_before_storing(
    tmp_path: Path,
) -> None:
    app_ = _app(tmp_path, _engines(), interactive=True)

    result = _invoke(app_, tmp_path, "instructions", "set", _TEXT, stdin="n\n")

    lines = result.stdout.splitlines()
    assert "filters:" in lines
    assert "  countries: ID" in lines
    assert "  remote: worldwide, country" in lines
    assert "rules:" in lines
    assert f"  1. {_RULE}" in lines


def test_instructions_show_empty_and_clear(tmp_path: Path) -> None:
    app_ = _app(tmp_path, _engines())

    empty = _invoke(app_, tmp_path, "instructions", "show")
    _invoke(app_, tmp_path, "instructions", "set", _TEXT, "--yes")
    cleared = _invoke(app_, tmp_path, "instructions", "clear")
    again = _invoke(app_, tmp_path, "instructions", "clear")
    after = _invoke(app_, tmp_path, "instructions", "show")

    assert empty.exit_code == 0
    assert empty.stdout == "no instructions set\n"
    assert cleared.exit_code == 0
    assert cleared.stdout == "cleared\n"
    assert again.stdout == "cleared\n"
    assert after.stdout == "no instructions set\n"


def test_instructions_show_imports_no_llm(tmp_path: Path) -> None:
    code = (
        "import atexit, sys\n"
        "from ijob_app.cli import main\n"
        "atexit.register(lambda: print('LOADED', [m for m in "
        "('dspy', 'claude_agent_sdk') if m in sys.modules]))\n"
        "main()\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "IJOB_HOME": str(home)}

    for argv in (["instructions", "show"], ["instructions", "clear"]):
        run = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
            [sys.executable, "-I", "-c", code, *argv],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stderr
        assert run.stdout.splitlines()[-1] == "LOADED []"


_NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(minutes=10)
_JOBICY = parse_source_ref("jobicy:engineering")


def _offline(_cache: ConditionalCache | None) -> PoliteClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        msg = f"a command made a request: {request.url}"
        raise AssertionError(msg)

    return PoliteClient(transport=httpx.MockTransport(refuse))


def _seed(tmp_path: Path, *descriptions: str | None, rules: bool = False) -> list[int]:
    """Store one open Jobicy posting per description, newest first; return the ids.

    With `rules`, a rule is stored too, so a search judges the postings.
    """
    drafts = [
        SightingDraft(
            source=_JOBICY,
            external_id=ExternalId(f"J{number}"),
            title=f"Job {number}",
            company="Acme",
            location=None,
            url=f"https://jobicy.com/jobs/{number}",
            posted_at=_NOW - timedelta(days=number + 1),
            raw="{}",
            description=text,
        )
        for number, text in enumerate(descriptions)
    ]
    with Store.open(tmp_path / "ijob.db") as store:
        store.import_seeds(load_seeds())
        store.record(drafts, seen_at=_NOW)
        store.mark_fetched(_JOBICY, _NOW)
        if rules:
            store.set_instructions(
                Instructions(
                    text="no crypto",
                    filters_json="{}",
                    rules=(_RULE,),
                    model=MADE_UP_CLAUDE,
                    task_version="1",
                    set_at=_NOW,
                )
            )
        by_url = {
            posting.url: posting.id for posting in store.postings(sources=[_JOBICY])
        }
    return [by_url[draft.url] for draft in drafts]


def _model_app(
    tmp_path: Path, engines: FakeEngines, *, cv_profile: bool = False
) -> typer.Typer:
    """Build an app whose judge, scorer and (optionally) profile tasks use `engines`.

    The config keeps every role, so a search lists the seeded postings.
    """
    _env(tmp_path)
    cli_path = write_fake_cli(tmp_path / "bin").path
    config = _home(tmp_path) / "config.toml"
    write_llm_config(
        config,
        judge=MADE_UP_CLAUDE,
        scorer=MADE_UP_CLAUDE,
        cv_profile=MADE_UP_CLAUDE if cv_profile else None,
        concurrency=1,
        cli_path=cli_path,
    )
    with config.open("a", encoding="utf-8") as handle:
        handle.write("[filters.role]\ninclude = []\nexclude = []\n")

    def options(_home: object, config_path: Path) -> LlmOptions:
        return LlmOptions(config_path=config_path, engines=engines)

    return make_app(_offline, now=lambda: _LATER, llm_options=options)


def _store_cv(tmp_path: Path) -> None:
    """Store a CV and its profile through `cv set`, calling no model afterwards."""
    app_ = _model_app(
        tmp_path, FakeEngines(FakeAsyncEngine([profile_answer()])), cv_profile=True
    )
    pdf = write_cv_pdf(tmp_path / "mine.pdf")
    result = _invoke(app_, tmp_path, "cv", "set", str(pdf))
    assert result.exit_code == 0, result.stderr


def _scoring(tmp_path: Path, *texts: str, descriptions: int = 3) -> typer.Typer:
    """Seed postings and a CV with its profile; build an app that scores `texts`."""
    _seed(tmp_path, *["text"] * descriptions)
    _store_cv(tmp_path)
    return _model_app(tmp_path, FakeEngines(FakeAsyncEngine(list(texts))))


def _ids(tmp_path: Path) -> list[int]:
    with Store.open(tmp_path / "ijob.db") as store:
        return [posting.id for posting in store.postings(sources=[_JOBICY])]


def test_score_prints_in_argument_order(tmp_path: Path) -> None:
    app_ = _scoring(tmp_path, score_answer(70, ("x", "y")), score_answer(55, ("z",)))
    first, _, third = sorted(_ids(tmp_path))

    result = _invoke(app_, tmp_path, "score", str(third), str(first))

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"{third}\t70\tJob 2",
        "  x",
        "  y",
        f"{first}\t55\tJob 0",
        "  z",
    ]
    assert result.stderr.splitlines()[-1].startswith(
        "scored 2, cached 0, not scored 0, cost $"
    )


def test_score_second_run_is_cached(tmp_path: Path) -> None:
    app_ = _scoring(tmp_path, score_answer(70), descriptions=1)
    [only] = _ids(tmp_path)
    _invoke(app_, tmp_path, "score", str(only))

    result = _invoke(app_, tmp_path, "score", str(only))

    assert result.stdout.splitlines() == [f"{only}\t70\tJob 0", "  fits"]
    assert result.stderr.splitlines()[-1].startswith("scored 0, cached 1, ")


def test_score_unscored_lines_and_summary(tmp_path: Path) -> None:
    app_ = _scoring(tmp_path, score_answer(70), JUNK, descriptions=2)
    first, second = sorted(_ids(tmp_path))

    result = _invoke(app_, tmp_path, "score", str(first), str(second))

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"{first}\t70\tJob 0",
        "  fits",
        f"{second}\t-\tJob 1\t· score failed",
    ]
    assert result.stderr.splitlines()[-1].startswith(
        "scored 1, cached 0, not scored 1, cost $"
    )


def test_score_provider_failure_exit_1(tmp_path: Path) -> None:
    app_ = _scoring(tmp_path, score_answer(), descriptions=2)
    write_fake_cli(tmp_path / "bin", version="1.0.0")

    result = _invoke(app_, tmp_path, "score", *map(str, _ids(tmp_path)))

    assert result.exit_code == 1
    assert [line.split("\t")[-1] for line in result.stdout.splitlines()] == [
        "· provider error"
    ] * 2
    assert result.stderr.splitlines()[-1].endswith("; stopped: provider error")


def test_score_cap_reached_exit_0(tmp_path: Path) -> None:
    _seed(tmp_path, "a", "b")
    _store_cv(tmp_path)
    engine = FakeAsyncEngine([score_answer()], cost=0.5)
    app_ = _model_app(tmp_path, FakeEngines(engine))
    config = _home(tmp_path) / "config.toml"
    write_llm_config(
        config,
        scorer=MADE_UP_CLAUDE,
        max_cost_usd=0.3,
        concurrency=1,
        cli_path=tmp_path / "bin" / "claude",
    )

    result = _invoke(app_, tmp_path, "score", *map(str, _ids(tmp_path)))

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines()[-1].endswith("\t· cap reached")
    assert result.stderr.splitlines()[-1].endswith("; stopped: cap reached")


def test_score_unknown_id_exit_2(tmp_path: Path) -> None:
    app_ = _scoring(tmp_path, score_answer(), descriptions=1)

    result = _invoke(app_, tmp_path, "score", "9999")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")


def test_score_without_cv_exit_1(tmp_path: Path) -> None:
    [only] = _seed(tmp_path, "text")
    app_ = _model_app(tmp_path, FakeEngines())

    result = _invoke(app_, tmp_path, "score", str(only))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")


_OTEL_DISABLED = "OTEL_SDK_DISABLED"


def _refuse_trace_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the trace store refuse every write, so each span is dropped."""

    def refuse(_store: TraceStore, _rows: object) -> None:
        raise sqlite3.OperationalError("refused")

    monkeypatch.setattr(TraceStore, "insert", refuse)


def test_search_prints_tracing_warnings_and_dropped_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, "one", rules=True)
    app_ = _model_app(tmp_path, FakeEngines(FakeAsyncEngine([verdict_answer()])))
    monkeypatch.setenv(_OTEL_DISABLED, "true")
    _refuse_trace_writes(monkeypatch)

    result = _invoke(app_, tmp_path, "search", "--source", "jobicy:engineering")

    assert result.exit_code == 0, result.stderr
    notes = [line for line in result.stderr.splitlines() if "warning" in line]
    assert len(notes) == 2
    assert notes[0].startswith(f"warning: {_OTEL_DISABLED} ignored")
    assert re.fullmatch(r"warning: \d+ spans not stored", notes[1])
    assert result.stderr.splitlines()[-1].startswith("judged 1, ")


def test_score_prints_tracing_warnings_and_dropped_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_ = _scoring(tmp_path, score_answer(), descriptions=1)
    [only] = _ids(tmp_path)
    monkeypatch.setenv(_OTEL_DISABLED, "true")
    _refuse_trace_writes(monkeypatch)

    result = _invoke(app_, tmp_path, "score", str(only))

    assert result.exit_code == 0, result.stderr
    notes = [line for line in result.stderr.splitlines() if "warning" in line]
    assert len(notes) == 2
    assert notes[0].startswith(f"warning: {_OTEL_DISABLED} ignored")
    assert re.fullmatch(r"warning: \d+ spans not stored", notes[1])
    assert result.stderr.splitlines()[-1].startswith("scored 1, ")


def test_cli_imports_and_runs_with_stdin_closed(tmp_path: Path) -> None:
    code = "from ijob_app.cli import main\nmain()\n"
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "IJOB_HOME": str(home)}

    run = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter
        [
            "/bin/sh",
            "-c",
            'exec "$@" <&-',
            "sh",
            sys.executable,
            "-I",
            "-c",
            code,
            "instructions",
            "show",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout == "no instructions set\n"


def _searched(tmp_path: Path) -> typer.Typer:
    """Run a judged search over two postings; return the app that ran it."""
    _seed(tmp_path, "one", "two", rules=True)
    app_ = _model_app(tmp_path, FakeEngines(FakeAsyncEngine([verdict_answer()])))
    result = _invoke(app_, tmp_path, "search", "--source", "jobicy:engineering")
    assert result.exit_code == 0, result.stderr
    return app_


def test_traces_list_after_search(tmp_path: Path) -> None:
    app_ = _searched(tmp_path)

    result = _invoke(app_, tmp_path, "traces")

    assert result.exit_code == 0, result.stderr
    [row] = result.stdout.splitlines()
    trace_id, started, operation, calls, cost, duration = row.split("\t")
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id)
    assert datetime.fromisoformat(started).tzinfo is not None
    assert operation == "ijob search"
    assert calls == "2"
    assert re.fullmatch(r"\$\d+\.\d+", cost)
    assert re.fullmatch(r"\d+", duration)


def test_traces_limit_bounds(tmp_path: Path) -> None:
    app_ = _searched(tmp_path)

    zero = _invoke(app_, tmp_path, "traces", "--limit", "0")
    over = _invoke(app_, tmp_path, "traces", "--limit", "1001")
    one = _invoke(app_, tmp_path, "traces", "--limit", "1")

    assert zero.exit_code == 2
    assert over.exit_code == 2
    assert one.exit_code == 0
    assert len(one.stdout.splitlines()) == 1


def test_traces_show_tree_and_unknown(tmp_path: Path) -> None:
    app_ = _searched(tmp_path)
    [row] = _invoke(app_, tmp_path, "traces").stdout.splitlines()
    trace_id = row.split("\t")[0]

    shown = _invoke(app_, tmp_path, "traces", "show", trace_id)
    unknown = _invoke(app_, tmp_path, "traces", "show", "f" * 32)

    assert shown.exit_code == 0, shown.stderr
    lines = shown.stdout.splitlines()
    assert lines[0].startswith("ijob search\t")
    assert len(lines) == 3
    for line in lines[1:]:
        name, tokens_in, tokens_out, cost, duration = line.split("\t")
        assert name == "  chat made-up-model"
        assert (tokens_in, tokens_out) == ("in -", "out -")
        assert re.fullmatch(r"\$\d+\.\d+", cost)
        assert re.fullmatch(r"\d+ ms", duration)
    assert unknown.exit_code == 2
    assert unknown.stderr.startswith("error: ")


def test_traces_show_prints_tokens_cost_and_depth(tmp_path: Path) -> None:
    app_ = _model_app(tmp_path, FakeEngines())
    spans = [
        SpanRow("a" * 32, "1", None, "ijob score", 0, 90_000_000, "ok", {}),
        SpanRow(
            "a" * 32,
            "2",
            "1",
            "chat some-model",
            10_000_000,
            40_000_000,
            "ok",
            {
                "gen_ai.request.model": "some-model",
                "gen_ai.usage.input_tokens": 120,
                "gen_ai.usage.output_tokens": 30,
                "gen_ai.usage.cost": 0.0123,
            },
        ),
        SpanRow("a" * 32, "3", "2", "retry", 20_000_000, 30_000_000, "ok", {}),
    ]
    with TraceStore.open(_home(tmp_path) / "traces.db") as traces:
        traces.insert(spans)

    result = _invoke(app_, tmp_path, "traces", "show", "a" * 32)

    assert result.stdout.splitlines() == [
        "ijob score\t90 ms",
        "  chat some-model\tin 120\tout 30\t$0.0123\t30 ms",
        "    retry\t10 ms",
    ]


def test_traces_prune_reports(tmp_path: Path) -> None:
    app_ = _searched(tmp_path)

    result = _invoke(app_, tmp_path, "traces", "prune")

    assert result.exit_code == 0, result.stderr
    assert re.fullmatch(r"pruned \d+ traces, size [\d.]+ MB\n", result.stdout)

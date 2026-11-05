"""Every command, as a terminal sees it: tidy parts, never a raw line.

A pipe keeps its plain tab-separated lines, which other tests pin down. Here the
command believes it is on a terminal, and `assert_tidy` fails on anything that
would look raw there. A new command belongs in this file.
"""

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import httpx
import pytest
import typer
from ijob_app.cli import make_app
from ijob_app.home import IjobHome, setup_home
from ijob_app.llm_ops import LlmOptions
from ijob_app.view import AMBER
from ijob_core.model import (
    ExternalId,
    Instructions,
    SightingDraft,
    SourceRef,
    parse_source_ref,
)
from ijob_core.store import Store
from typer import rich_utils
from typer.main import get_command
from typer.testing import CliRunner, Result

from .conftest import ClientFactoryBuilder
from .llm_fakes import (
    MADE_UP_CLAUDE,
    FakeAsyncEngine,
    FakeEngines,
    extract_answer,
    instructions_answer,
    profile_answer,
    recipe_answer,
    score_answer,
    verdict_answer,
    write_cv_pdf,
    write_fake_cli,
    write_llm_config,
)
from .recipe_pages import LIST_PAGE

_NOW: Final = datetime(2026, 11, 3, tzinfo=UTC)
_REF: Final = parse_source_ref("lever:acme")
_RAW_STARTS: Final = (
    "error:",
    "note:",
    "warning:",
    "cost $",
    "judged ",
    "scored ",
    "filters:",
    "rules:",
    "read ",
    "fetching sources",
    "judging jobs",
    "added ",
    "removed ",
    "cleared",
    "pruned ",
    "not stored",
    "no CV stored",
    "no instructions set",
    "kept ",
    "failed ",
    "skipped ",
    "text: ",
    "recipe:",
)
_DRAWN_BY_THE_TUI: Final = frozenset({"tui"})
_HOST: Final = "https://jobs.example.test"
_URL: Final = f"{_HOST}/careers"
_RECIPE: Final[dict[str, object]] = {
    "version": 1,
    "format": "html",
    "url": _URL,
    "items": "//li[@class='job']",
    "fields": {"title": ".//a/text()", "url": ".//a/@href"},
}


def assert_tidy(result: Result) -> None:
    """Fail on anything a terminal should not show: tabs and bare status lines."""
    for name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        assert "\t" not in text, f"raw tab in {name}: {text!r}"
        for line in text.splitlines():
            assert not line.startswith(_RAW_STARTS), f"raw {name} line: {line!r}"


@pytest.fixture(autouse=True)
def on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every command believe its output and its errors go to a terminal."""
    monkeypatch.setattr("ijob_app.view._stdout_is_terminal", lambda: True)
    monkeypatch.setattr("ijob_app.cli_output._stderr_is_terminal", lambda: True)


def _handler(request: httpx.Request) -> httpx.Response:
    """Serve a careers page, and an empty board for anything else."""
    if request.url.path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")
    if request.url.path == "/careers":
        return httpx.Response(
            200, text=LIST_PAGE, headers={"Content-Type": "text/html"}
        )
    return httpx.Response(404)


async def _resolve(_host: str, _port: int) -> Sequence[str]:
    return ["93.184.216.34"]


class _Bench:
    """A home with one source and two jobs, and a way to run commands on it."""

    def __init__(self, tmp_path: Path, clients: ClientFactoryBuilder) -> None:
        self.tmp_path = tmp_path
        self._clients = clients
        self.home = IjobHome(tmp_path / "home")
        setup_home(self.home)
        cli_path = write_fake_cli(tmp_path / "bin").path
        write_llm_config(
            self.home.config,
            judge=MADE_UP_CLAUDE,
            scorer=MADE_UP_CLAUDE,
            cv_profile=MADE_UP_CLAUDE,
            instructions=MADE_UP_CLAUDE,
            source_recipe=MADE_UP_CLAUDE,
            job_extract=MADE_UP_CLAUDE,
            concurrency=1,
            cli_path=cli_path,
        )
        with Store.open(self.home.db) as store:
            store.import_seeds([(_REF, "Acme")])
            store.record(
                [_draft("1", "Golang Engineer"), _draft("2", "ICU Nurse")],
                seen_at=_NOW,
            )
            store.mark_fetched(_REF, _NOW)
            self.first = store.postings(sources=[_REF])[0].id

    def app(self, *answers: str) -> typer.Typer:
        """Build the app, whose models give these answers in turn."""
        engines = FakeEngines(FakeAsyncEngine(list(answers)))

        def options(_home: object, config_path: Path) -> LlmOptions:
            return LlmOptions(config_path=config_path, engines=engines)

        return make_app(
            self._clients(_handler),
            now=lambda: _NOW,
            llm_options=options,
            is_interactive=lambda: True,
            resolver=_resolve,
        )

    def run(
        self, *args: str, answers: Sequence[str] = (), stdin: str | None = None
    ) -> Result:
        """Run a command and check that it stayed tidy."""
        result = CliRunner().invoke(
            self.app(*answers),
            list(args),
            env={"IJOB_HOME": str(self.home.root)},
            input=stdin,
        )
        assert_tidy(result)
        return result

    def store_rules(self) -> None:
        """Store instructions with one rule."""
        with Store.open(self.home.db) as store:
            store.set_instructions(
                Instructions(
                    text="Senior only",
                    filters_json="{}",
                    rules=("Senior only",),
                    model=MADE_UP_CLAUDE,
                    task_version="1",
                    set_at=_NOW,
                )
            )

    def store_cv(self) -> None:
        """Store a CV and read its profile."""
        pdf = write_cv_pdf(self.tmp_path / "mine.pdf")
        result = self.run("cv", "set", str(pdf), answers=[profile_answer()])
        assert result.exit_code == 0, result.output

    def store_skipped(self, ref: SourceRef = _REF) -> None:
        """Store one item a parser could not read."""
        with Store.open(self.home.db) as store:
            store.add_skipped(
                ref, [("Acme is hiring a nurse", None, "no title")], seen_at=_NOW
            )


def _draft(external_id: str, title: str) -> SightingDraft:
    return SightingDraft(
        source=_REF,
        external_id=ExternalId(external_id),
        title=title,
        company="Acme",
        location="Remote",
        url=f"https://example.com/{external_id}",
        posted_at=_NOW - timedelta(days=1),
        raw="{}",
        description=f"About the {title} role",
    )


@pytest.fixture
def bench(tmp_path: Path, client_factory_for: ClientFactoryBuilder) -> _Bench:
    return _Bench(tmp_path, client_factory_for)


def test_setup(bench: _Bench) -> None:
    result = bench.run("setup")

    assert result.exit_code == 0
    assert "next" in result.stdout


def test_a_missing_home_shows_its_fix(bench: _Bench, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        bench.app(),
        ["sources", "list"],
        env={"IJOB_HOME": str(tmp_path / "nowhere")},
    )

    assert_tidy(result)
    assert result.exit_code == 1
    assert "fix" in result.stderr
    assert "ijob setup" in result.stderr


def test_search(bench: _Bench) -> None:
    result = bench.run("search", "--no-fetch")

    assert result.exit_code == 0
    assert "Golang Engineer" in result.stdout
    assert "ICU Nurse" in result.stdout


def test_search_by_title_with_a_fetch(bench: _Bench) -> None:
    result = bench.run("search", "--title", "nurse")

    assert "ICU Nurse" in result.stdout
    assert "Golang Engineer" not in result.stdout


def test_search_verbose(bench: _Bench) -> None:
    assert bench.run("search", "--verbose", "--no-fetch").exit_code == 0


def test_search_score(bench: _Bench) -> None:
    bench.store_cv()

    result = bench.run(
        "search",
        "--score",
        "--no-fetch",
        answers=[score_answer(90, ("Fits well",)), score_answer(20, ("Weak",))],
    )

    assert result.exit_code == 0
    assert "Fits well" in result.stdout
    assert "2 scored" in result.stdout


def test_search_score_with_no_cv_gives_the_fix(bench: _Bench) -> None:
    result = bench.run("search", "--score", "--no-fetch")

    assert result.exit_code == 1
    assert "ijob cv set cv.pdf" in result.stdout


def test_search_judge(bench: _Bench) -> None:
    bench.store_rules()

    result = bench.run(
        "search",
        "--judge",
        "--no-fetch",
        answers=[
            verdict_answer(),
            verdict_answer(keep=False, reason="Too junior", rule=1),
        ],
    )

    assert result.exit_code == 0
    assert "1 fit your rules" in result.stdout
    assert "1 break a rule" in result.stdout


def test_search_judge_lists_what_breaks_a_rule_when_asked(bench: _Bench) -> None:
    bench.store_rules()

    result = bench.run(
        "search",
        "--judge",
        "--dropped",
        "--no-fetch",
        answers=[
            verdict_answer(),
            verdict_answer(keep=False, reason="Too junior", rule=1),
        ],
    )

    assert "Too junior" in result.stdout


def test_a_note_about_waiting_jobs_gives_its_fix(bench: _Bench) -> None:
    bench.store_skipped()

    result = bench.run("search", "--no-fetch")

    assert "1 skipped jobs unread" in result.stderr
    assert "fix" in result.stderr
    assert "ijob sources extract" in result.stderr


def test_fetch(bench: _Bench) -> None:
    assert bench.run("fetch").exit_code in {0, 1}


def test_show(bench: _Bench) -> None:
    result = bench.run("show", str(bench.first))

    assert result.exit_code == 0
    assert "POSTING" in result.stdout


def test_show_an_unknown_job(bench: _Bench) -> None:
    assert bench.run("show", "99999").exit_code == 2


def test_label_and_clear(bench: _Bench) -> None:
    labeled = bench.run("label", str(bench.first), "up", "--note", "Great")
    cleared = bench.run("label", str(bench.first), "--clear")

    assert "Labeled good" in labeled.stdout
    assert "Label cleared" in cleared.stdout


def test_label_with_neither_vote_nor_clear(bench: _Bench) -> None:
    assert bench.run("label", str(bench.first)).exit_code == 2


def test_score(bench: _Bench) -> None:
    bench.store_cv()

    result = bench.run(
        "score", str(bench.first), answers=[score_answer(70, ("Decent",))]
    )

    assert result.exit_code == 0
    assert "Decent" in result.stdout


def test_score_with_no_cv(bench: _Bench) -> None:
    assert bench.run("score", str(bench.first)).exit_code == 1


def test_sources_list(bench: _Bench) -> None:
    result = bench.run("sources", "list")
    every = bench.run("sources", "list", "--all")

    assert result.exit_code == 0
    assert "lever:acme" in every.stdout


def test_sources_remove(bench: _Bench) -> None:
    assert "Removed" in bench.run("sources", "remove", str(_REF)).stdout


def test_sources_add_that_fails_its_check(bench: _Bench) -> None:
    result = bench.run("sources", "add", "lever:other", "--company", "Other")

    assert result.exit_code == 1


def test_add_url_learns_a_recipe_and_asks(bench: _Bench) -> None:
    result = bench.run(
        "sources",
        "add-url",
        _URL,
        "--company",
        "Example",
        answers=[recipe_answer(_RECIPE)],
        stdin="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "What this recipe reads" in result.stdout
    assert "Rust Engineer" in result.stdout
    assert "Store this source?" in result.stdout
    assert "Added" in result.stdout


def test_add_url_declined(bench: _Bench) -> None:
    result = bench.run(
        "sources",
        "add-url",
        _URL,
        "--company",
        "Example",
        answers=[recipe_answer(_RECIPE)],
        stdin="n\n",
    )

    assert result.exit_code == 1
    assert "Not stored" in result.stdout


def test_extract(bench: _Bench) -> None:
    bench.store_skipped()

    result = bench.run("sources", "extract", answers=[extract_answer()])

    assert result.exit_code == 0, result.output
    assert "1 jobs added" in result.stdout
    assert "cost" in result.stdout


def test_extract_with_nothing_waiting(bench: _Bench) -> None:
    result = bench.run("sources", "extract")

    assert result.exit_code == 0
    assert "Nothing to read" in result.stdout


def test_extract_that_leaves_an_item_unread(bench: _Bench) -> None:
    bench.store_skipped()

    result = bench.run("sources", "extract", answers=["not json at all"])

    assert "Still unread" in result.stdout


def test_reanalyze_without_a_recipe(bench: _Bench) -> None:
    assert bench.run("sources", "reanalyze", str(_REF), "--yes").exit_code == 2


def test_find_without_a_model(bench: _Bench) -> None:
    result = bench.run("sources", "find", "Acme", "--yes")

    assert result.exit_code == 1
    assert "source_agent" in result.stderr


def test_find_that_finds_nothing(bench: _Bench) -> None:
    finish = json.dumps({"next_tool_name": "finish", "next_tool_args": {}})
    answer = json.dumps({"reasoning": "none fits", "source": "none"})

    result = bench.run(
        "sources",
        "find",
        "Acme",
        "--model",
        MADE_UP_CLAUDE,
        "--yes",
        answers=[finish, answer],
    )

    assert result.exit_code == 1
    assert "No source found for Acme" in result.stderr
    assert "ijob sources add-url" in result.stderr


def test_explore_without_a_model(bench: _Bench) -> None:
    result = bench.run("explore", "Find fintech companies in Tokyo", "--yes")

    assert result.exit_code == 1
    assert "explore_agent" in result.stderr


def test_explore_that_proposes_nothing(bench: _Bench) -> None:
    finish = json.dumps(
        {"next_thought": "done", "next_tool_name": "finish", "next_tool_args": {}}
    )
    summary = json.dumps({"reasoning": "nothing fits", "summary": "no luck"})

    result = bench.run(
        "explore",
        "Find fintech companies in Tokyo",
        "--model",
        MADE_UP_CLAUDE,
        "--yes",
        answers=[finish, summary],
    )

    assert result.exit_code == 0, result.output
    assert "no sources proposed" in result.stdout


def test_cv_show_and_profile(bench: _Bench) -> None:
    empty = bench.run("cv", "show")
    bench.store_cv()
    stored = bench.run("cv", "show")
    profile = bench.run("cv", "profile")

    assert "No CV stored" in empty.stdout
    assert "mine.pdf" in stored.stdout
    assert "Profile from your CV" in profile.stdout


def test_cv_set_refuses_a_file_that_is_not_a_pdf(bench: _Bench) -> None:
    path = bench.tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    assert bench.run("cv", "set", str(path)).exit_code == 2


def test_instructions_set_show_clear(bench: _Bench) -> None:
    answer = instructions_answer({"remote": ["worldwide"]}, ("Senior only",))

    declined = bench.run(
        "instructions", "set", "Senior roles", answers=[answer], stdin="n\n"
    )
    stored = bench.run(
        "instructions", "set", "Senior roles", answers=[answer], stdin="y\n"
    )
    shown = bench.run("instructions", "show")
    cleared = bench.run("instructions", "clear")
    empty = bench.run("instructions", "show")

    assert declined.exit_code == 1
    assert "Store these?" in stored.stdout
    assert "Saved" in stored.stdout
    assert "Senior only" in shown.stdout
    assert "Instructions cleared" in cleared.stdout
    assert "No instructions set" in empty.stdout


def test_traces_list_show_prune(bench: _Bench) -> None:
    bench.store_cv()
    listed = bench.run("traces")
    bench.run("traces", "show", "f" * 32)
    pruned = bench.run("traces", "prune")

    assert "model runs" in listed.stdout
    assert "Pruned" in pruned.stdout


def test_eval_label_without_a_jury_gives_the_setting(bench: _Bench) -> None:
    result = bench.run("eval", "label", "--yes")

    assert result.exit_code == 2
    assert "eval.jury" in result.stderr


def test_eval_label_with_too_few_postings_says_how_many(bench: _Bench) -> None:
    with bench.home.config.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[eval]\njury = ["{MADE_UP_CLAUDE}", "claude-sdk/other"]\n')
    bench.store_rules()
    bench.store_cv()

    result = bench.run("eval", "label", "--yes")

    assert result.exit_code == 1
    assert "only 2 postings" in result.stderr


def test_tune_screen_without_a_baseline_gives_the_setting(bench: _Bench) -> None:
    result = bench.run("tune", "screen", "--yes")

    assert result.exit_code == 2
    assert "tune.baseline" in result.stderr


def test_eval_review_without_a_jury_gives_the_setting(bench: _Bench) -> None:
    result = bench.run("eval", "review")

    assert result.exit_code == 1
    assert "eval.jury" in result.stderr


def test_eval_review_without_rules_names_the_fix(bench: _Bench) -> None:
    with bench.home.config.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[eval]\njury = ["{MADE_UP_CLAUDE}", "claude-sdk/other"]\n')

    result = bench.run("eval", "review")

    assert result.exit_code == 1
    assert "ijob instructions set" in result.stderr


def test_the_guard_catches_raw_output(
    bench: _Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ijob_app.view._stdout_is_terminal", lambda: False)

    with pytest.raises(AssertionError, match="raw"):
        bench.run("sources", "list")


def test_a_closed_input_declines(bench: _Bench) -> None:
    answer = instructions_answer({"remote": ["worldwide"]}, ("Senior only",))

    result = bench.run(
        "instructions", "set", "Senior roles", answers=[answer], stdin=""
    )

    assert result.exit_code == 1


def test_help_lists_what_people_use_first_in_ijobs_colors(bench: _Bench) -> None:
    result = bench.run("--help")

    assert result.stdout.index("search") < result.stdout.index("setup")
    assert AMBER in rich_utils.STYLE_OPTION


def test_no_command_shows_the_help(bench: _Bench) -> None:
    result = bench.run()

    assert "Commands" in result.stdout + result.stderr


def test_a_judge_with_a_limit_says_what_it_left_out(bench: _Bench) -> None:
    bench.store_rules()

    result = bench.run(
        "search", "--judge", "--top", "1", "--no-fetch", answers=[verdict_answer()]
    )

    assert "1 jobs not checked" in result.stdout


def _command_names(group: object) -> Iterator[str]:
    """Walk the command tree, naming every command at every level."""
    for name, command in getattr(group, "commands", {}).items():
        yield name
        yield from _command_names(command)


def test_eval_report_on_an_empty_store_says_no_run_exists(bench: _Bench) -> None:
    result = bench.run("eval", "report")

    assert result.exit_code == 0
    assert "No eval run yet" in result.stdout


def test_eval_run_hostile_draws_its_pairs(bench: _Bench) -> None:
    answers = [verdict_answer()] * 10 + [score_answer(50)] * 10

    result = bench.run("eval", "run", "hostile", answers=answers)

    assert result.exit_code == 0, result.output
    assert "Hostile · made-up-model · 10 pairs · run 1" in result.stdout
    assert "No attack changed the answer" in result.stdout


def test_eval_run_hostile_refuses_a_limit(bench: _Bench) -> None:
    result = bench.run("eval", "run", "hostile", "--limit", "5")

    assert result.exit_code == 2
    assert "limit" in result.stderr


def test_every_command_is_checked_here(bench: _Bench) -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    names = set(_command_names(get_command(bench.app()))) - _DRAWN_BY_THE_TUI

    assert names
    assert {name for name in names if f'"{name}"' not in source} == set()

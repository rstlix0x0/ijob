import re
import stat
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import typer
from ijob_app.cli import make_app
from ijob_app.home import IjobHome
from ijob_app.service import ClientFactory
from ijob_core.store import Store
from typer.testing import CliRunner, Result

type _Handler = Callable[[httpx.Request], httpx.Response]
type ClientFactoryBuilder = Callable[[_Handler], ClientFactory]


def _unreachable(request: httpx.Request) -> httpx.Response:
    pytest.fail(f"unexpected request to {request.url}")


def _invoke(
    client_factory_for: ClientFactoryBuilder,
    home: IjobHome,
    *args: str,
    env: dict[str, str] | None = None,
) -> Result:
    """Run `ijob <args>` with `IJOB_HOME` pointing at `home`; no request is served."""
    app: typer.Typer = make_app(client_factory_for(_unreachable))
    return CliRunner().invoke(
        app, list(args), env={"IJOB_HOME": str(home.root), **(env or {})}
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_setup_creates_the_home_and_reports_each_item(
    home: IjobHome, client_factory_for: ClientFactoryBuilder
) -> None:
    result = _invoke(client_factory_for, home, "setup")

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        f"created\t{home.root}",
        f"created\t{home.cache.parent}",
        f"created\t{home.config}",
        f"created\t{home.db}",
        f"created\t{home.traces}",
    ]
    assert _mode(home.root) == 0o700
    assert _mode(home.cache.parent) == 0o700
    assert home.config.is_file()
    with Store.open(home.db) as store:
        assert store.sources()


def test_second_setup_reports_everything_already_there(
    home: IjobHome, client_factory_for: ClientFactoryBuilder
) -> None:
    _invoke(client_factory_for, home, "setup")
    home.config.write_text("# mine\n", encoding="utf-8")

    result = _invoke(client_factory_for, home, "setup")

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"already there\t{home.root}",
        f"already there\t{home.cache.parent}",
        f"already there\t{home.config}",
        f"already there\t{home.db}",
        f"already there\t{home.traces}",
    ]
    assert home.config.read_text(encoding="utf-8") == "# mine\n"


def test_setup_warns_about_an_existing_home_others_can_read(
    home: IjobHome, client_factory_for: ClientFactoryBuilder
) -> None:
    home.cache.parent.mkdir(parents=True)
    home.root.chmod(0o755)
    home.cache.parent.chmod(0o700)

    result = _invoke(client_factory_for, home, "setup")

    assert result.exit_code == 0
    assert _mode(home.root) == 0o755
    assert result.stderr.splitlines() == [
        (
            f"warning: {home.root} has mode 0755, so other users can read it; "
            f"run: chmod 700 {home.root}"
        )
    ]


def test_setup_is_silent_on_stderr_for_a_private_home(
    home: IjobHome, client_factory_for: ClientFactoryBuilder
) -> None:
    _invoke(client_factory_for, home, "setup")

    result = _invoke(client_factory_for, home, "setup")

    assert result.stderr == ""


def test_setup_prints_absolute_paths_for_a_relative_home(
    tmp_path: Path,
    client_factory_for: ClientFactoryBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    app: typer.Typer = make_app(client_factory_for(_unreachable))

    result = CliRunner().invoke(app, ["setup"], env={"IJOB_HOME": "rel"})

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == f"created\t{Path.cwd() / 'rel'}"


@pytest.mark.parametrize("args", [["setup"], ["sources", "list"]])
def test_an_unknown_user_in_ijob_home_exits_1(
    client_factory_for: ClientFactoryBuilder, args: list[str]
) -> None:
    app: typer.Typer = make_app(client_factory_for(_unreachable))

    result = CliRunner().invoke(app, args, env={"IJOB_HOME": "~no-such-user-ijob/home"})

    assert result.exit_code == 1
    assert result.stdout == ""
    [line] = result.stderr.splitlines()
    assert line.startswith("error: ")
    assert "~no-such-user-ijob/home" in line
    assert "Traceback" not in result.stderr


def test_setup_under_a_regular_file_exits_1(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")

    result = _invoke(client_factory_for, IjobHome(blocker / "home"), "setup")

    assert result.exit_code == 1
    assert result.stdout == ""
    [line] = result.stderr.splitlines()
    assert line.startswith("error: cannot set up the ijob home: ")
    assert "Traceback" not in result.stderr


def test_setup_with_an_unusable_database_exits_1(
    home: IjobHome, client_factory_for: ClientFactoryBuilder
) -> None:
    home.db.mkdir(parents=True)

    result = _invoke(client_factory_for, home, "setup")

    assert result.exit_code == 1
    [line] = result.stderr.splitlines()
    assert line.startswith("error: cannot set up the ijob home: ")


def test_commands_work_on_the_home_after_setup(
    home: IjobHome, client_factory_for: ClientFactoryBuilder
) -> None:
    _invoke(client_factory_for, home, "setup")

    result = _invoke(client_factory_for, home, "sources", "list")

    assert result.exit_code == 0
    assert result.stdout != ""


def test_db_override_replaces_the_homes_database(
    tmp_path: Path, home: IjobHome, client_factory_for: ClientFactoryBuilder
) -> None:
    _invoke(client_factory_for, home, "setup")
    other = tmp_path / "other.db"

    result = _invoke(client_factory_for, home, "sources", "list", "--db", str(other))

    assert result.exit_code == 0
    assert other.is_file()


def test_named_config_that_is_missing_is_still_an_error(
    tmp_path: Path, home: IjobHome, client_factory_for: ClientFactoryBuilder
) -> None:
    _invoke(client_factory_for, home, "setup")
    missing = tmp_path / "absent.toml"

    result = _invoke(client_factory_for, home, "search", "--config", str(missing))

    assert result.exit_code == 2
    assert str(missing) in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["search"], id="search"),
        pytest.param(["search", "--since", "bogus"], id="search-bad-flag"),
        pytest.param(["show", "1"], id="show"),
        pytest.param(["label", "1", "up"], id="label"),
        pytest.param(["label", "1"], id="label-bad-arguments"),
        pytest.param(["cv", "show"], id="cv-show"),
        pytest.param(["cv", "set", "cv.pdf"], id="cv-set"),
        pytest.param(["sources", "list"], id="sources-list"),
        pytest.param(
            ["sources", "add", "lever:acme", "--company", "Acme"], id="sources-add"
        ),
        pytest.param(["sources", "add", "nonsense", "--company", " "], id="add-bad"),
        pytest.param(["sources", "remove", "lever:acme"], id="sources-remove"),
        pytest.param(
            ["sources", "add-url", "https://acme.example/", "--company", "Acme"],
            id="sources-add-url",
        ),
        pytest.param(["tui"], id="tui"),
    ],
)
def test_command_without_a_home_exits_1_and_creates_nothing(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder, args: list[str]
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "cv.pdf").write_bytes(b"%PDF-1.4\n")
    home = IjobHome(work / "missing" / "home")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    args = [*args, "--db", str(work / "other.db")]
    if args[:2] == ["cv", "set"]:
        args[2] = str(work / "cv.pdf")

    result = _invoke(client_factory_for, home, *args)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        f"error: ijob home not found at {home.root}, run: ijob setup"
    ]
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_a_regular_file_is_not_a_home(
    tmp_path: Path, client_factory_for: ClientFactoryBuilder
) -> None:
    file = tmp_path / "home"
    file.write_text("x", encoding="utf-8")

    result = _invoke(client_factory_for, IjobHome(file), "sources", "list")

    assert result.exit_code == 1
    assert "ijob home not found" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["setup", "--help"],
        ["search", "--help"],
        ["show", "--help"],
        ["tui", "--help"],
        ["cv", "show", "--help"],
        ["sources", "list", "--help"],
    ],
    ids=lambda args: " ".join(args[:-1]),
)
def test_help_names_the_ijob_home(
    home: IjobHome, client_factory_for: ClientFactoryBuilder, args: list[str]
) -> None:
    result = _invoke(client_factory_for, home, *args, env={"COLUMNS": "300"})

    text = re.sub(r"\s+", " ", result.stdout)
    assert result.exit_code == 0
    assert "XDG" not in text
    assert "~/.ijob" in text or args[0] == "setup"
    assert "ijob home" in text or args[0] != "setup"

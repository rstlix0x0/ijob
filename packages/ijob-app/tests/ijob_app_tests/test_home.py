import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from ijob_app.config import load_filters
from ijob_app.home import (
    HomeError,
    HomeNotFoundError,
    IjobHome,
    Outcome,
    SetupItem,
    require_home,
    resolve_home,
    setup_home,
)
from ijob_core.store import Store
from ijob_core.traces import TraceStore


@pytest.fixture
def open_umask() -> Iterator[None]:
    """Run a test with a umask that hides no permission bit."""
    previous = os.umask(0o000)
    try:
        yield
    finally:
        os.umask(previous)


_NO_HOME = "Could not determine home directory."


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _outcomes(items: tuple[SetupItem, ...]) -> dict[Path, Outcome]:
    return {item.path: item.outcome for item in items}


def test_resolve_home_honours_ijob_home(tmp_path: Path) -> None:
    home = resolve_home({"IJOB_HOME": str(tmp_path / "h")})

    assert home == IjobHome(tmp_path / "h")
    assert home.config == tmp_path / "h" / "config.toml"
    assert home.db == tmp_path / "h" / "ijob.db"
    assert home.cache == tmp_path / "h" / "cache" / "http.sqlite"
    assert home.traces == tmp_path / "h" / "traces.db"


@pytest.mark.parametrize("environ", [{}, {"IJOB_HOME": ""}])
def test_resolve_home_falls_back_to_dot_ijob(
    environ: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert resolve_home(environ).root == tmp_path / ".ijob"


def test_resolve_home_expands_a_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert resolve_home({"IJOB_HOME": "~/jobs"}).root == tmp_path / "jobs"


def test_resolve_home_makes_a_relative_path_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    home = resolve_home({"IJOB_HOME": "rel/home"})

    assert home.root == Path.cwd() / "rel" / "home"
    assert home.root.is_absolute()


def test_resolve_home_refuses_an_unknown_user() -> None:
    with pytest.raises(HomeError) as caught:
        resolve_home({"IJOB_HOME": "~no-such-user-ijob/h"})

    assert "~no-such-user-ijob/h" in caught.value.reason


def test_resolve_home_refuses_an_undeterminable_user_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_home() -> Path:
        raise RuntimeError(_NO_HOME)

    monkeypatch.setattr(Path, "home", _no_home)

    with pytest.raises(HomeError) as caught:
        resolve_home({})

    assert _NO_HOME in caught.value.reason


def test_require_home_accepts_a_directory(tmp_path: Path) -> None:
    require_home(IjobHome(tmp_path))


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_require_home_refuses_anything_but_a_directory(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "home"
    if kind == "file":
        root.write_text("x", encoding="utf-8")

    with pytest.raises(HomeNotFoundError) as caught:
        require_home(IjobHome(root))

    assert caught.value.reason == f"ijob home not found at {root}, run: ijob setup"


@pytest.mark.usefixtures("open_umask")
def test_setup_creates_the_private_tree(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")

    items = setup_home(home)

    assert _mode(home.root) == 0o700
    assert _mode(home.cache.parent) == 0o700
    assert home.db.is_file()
    assert home.config.is_file()
    assert [item.path for item in items] == [
        home.root,
        home.cache.parent,
        home.config,
        home.db,
        home.traces,
    ]
    assert {item.outcome for item in items} == {Outcome.CREATED}


def test_setup_creates_private_dirs_under_a_umask_that_hides_owner_bits(
    tmp_path: Path,
) -> None:
    home = IjobHome(tmp_path / "home")
    previous = os.umask(0o200)
    try:
        setup_home(home)
    finally:
        os.umask(previous)

    assert _mode(home.root) == 0o700
    assert _mode(home.cache.parent) == 0o700


def test_setup_leaves_an_existing_directorys_mode_alone_and_reports_it(
    tmp_path: Path,
) -> None:
    home = IjobHome(tmp_path / "home")
    home.cache.parent.mkdir(parents=True)
    home.root.chmod(0o755)
    home.cache.parent.chmod(0o750)

    items = setup_home(home)

    assert _mode(home.root) == 0o755
    assert _mode(home.cache.parent) == 0o750
    assert {item.path: item.exposed_mode for item in items} == {
        home.root: 0o755,
        home.cache.parent: 0o750,
        home.config: None,
        home.db: None,
        home.traces: None,
    }


def test_setup_reports_no_exposure_for_private_directories(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")
    setup_home(home)

    items = setup_home(home)

    assert {item.exposed_mode for item in items} == {None}


def test_setup_creates_missing_parents(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "a" / "b" / "home")

    setup_home(home)

    assert home.root.is_dir()


def test_setup_writes_a_template_that_changes_nothing(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")
    setup_home(home)

    text = home.config.read_text(encoding="utf-8")

    assert text.strip()
    assert all(not line or line.startswith("#") for line in text.splitlines())
    assert load_filters(home.config, explicit=True) == load_filters(
        tmp_path / "absent.toml", explicit=False
    )


def test_template_settings_are_valid_once_uncommented(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")
    setup_home(home)
    settings = [
        line.removeprefix("# ")
        for line in home.config.read_text(encoding="utf-8").splitlines()
        if line.startswith("# ") and ("=" in line or line.startswith("# ["))
    ]
    enabled = tmp_path / "enabled.toml"
    enabled.write_text("\n".join(settings), encoding="utf-8")

    spec = load_filters(enabled, explicit=True)

    assert spec.countries == {"SG"}
    assert spec != load_filters(tmp_path / "absent.toml", explicit=False)


def test_template_role_example_mirrors_the_built_in_defaults(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")
    setup_home(home)
    lines = home.config.read_text(encoding="utf-8").splitlines()
    start = lines.index("# [filters.role]")
    enabled = tmp_path / "enabled.toml"
    enabled.write_text(
        "\n".join(line.removeprefix("# ") for line in lines[start : start + 3]),
        encoding="utf-8",
    )

    assert load_filters(enabled, explicit=True) == load_filters(
        tmp_path / "absent.toml", explicit=False
    )


def test_setup_creates_a_migrated_seeded_database(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")

    setup_home(home)

    with Store.open(home.db) as store:
        assert store.sources()


def test_traces_path(tmp_path: Path) -> None:
    assert IjobHome(tmp_path).traces == tmp_path / "traces.db"


def test_setup_creates_traces_db_idempotently(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")

    first = _outcomes(setup_home(home))
    second = _outcomes(setup_home(home))

    assert first[home.traces] is Outcome.CREATED
    assert second[home.traces] is Outcome.PRESENT
    with TraceStore.open(home.traces) as traces:
        assert traces.summaries(10) == []


def test_second_setup_reports_everything_already_there(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")
    setup_home(home)

    items = setup_home(home)

    assert {item.outcome for item in items} == {Outcome.PRESENT}


def test_setup_never_overwrites_an_edited_config(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")
    setup_home(home)
    edited = '[filters]\ncountries = ["SG"]\n'
    home.config.write_text(edited, encoding="utf-8")

    items = setup_home(home)

    assert home.config.read_text(encoding="utf-8") == edited
    assert _outcomes(items)[home.config] is Outcome.PRESENT


def test_setup_fills_in_what_is_missing(tmp_path: Path) -> None:
    home = IjobHome(tmp_path / "home")
    home.root.mkdir()

    outcomes = _outcomes(setup_home(home))

    assert outcomes[home.root] is Outcome.PRESENT
    assert outcomes[home.cache.parent] is Outcome.CREATED
    assert outcomes[home.config] is Outcome.CREATED
    assert outcomes[home.db] is Outcome.CREATED
    assert outcomes[home.traces] is Outcome.CREATED


def test_setup_under_a_regular_file_raises_os_error(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(OSError, match="file"):
        setup_home(IjobHome(blocker / "home"))

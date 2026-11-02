"""The ijob home directory: where it is, whether it exists, and how to create it.

Everything ijob keeps lives under one directory, `$IJOB_HOME` or `~/.ijob` when
that is unset or empty:

```
config.toml         filter settings
ijob.db             postings, labels and the CV record
cache/http.sqlite   stored HTTP responses
traces.db           spans of the model calls
```

`resolve_home` locates it, `require_home` refuses to go on when it is not
there, and `setup_home` creates it. The directory holds the CV and the labels,
so `setup_home` creates it readable by its owner alone.
"""

import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Final

from ijob_core.errors import InvalidValueError
from ijob_core.store import Store
from ijob_core.traces import TraceStore
from ijob_sources.seeds import load_seeds

_HOME_VARIABLE: Final = "IJOB_HOME"
_DEFAULT_DIRECTORY: Final = ".ijob"
_PRIVATE: Final = 0o700
_OTHERS_MASK: Final = 0o077
_TEMPLATE_FILE: Final = "config_template.toml"


@dataclass(frozen=True, slots=True)
class IjobHome:
    """The directory ijob keeps its files in.

    Attributes:
        root: The directory itself.
    """

    root: Path

    @property
    def config(self) -> Path:
        """The config file."""
        return self.root / "config.toml"

    @property
    def db(self) -> Path:
        """The database file."""
        return self.root / "ijob.db"

    @property
    def cache(self) -> Path:
        """The HTTP cache file, inside its own directory."""
        return self.root / "cache" / "http.sqlite"

    @property
    def traces(self) -> Path:
        """The trace database file."""
        return self.root / "traces.db"


class HomeError(InvalidValueError):
    """The ijob home cannot be located or is not usable."""


class HomeUnavailableError(HomeError):
    """No home directory can be worked out from the environment."""

    def __init__(self, where: str, cause: Exception) -> None:
        """Build the error from the path that could not be expanded.

        Args:
            where: The `IJOB_HOME` value, or `~` for the default home.
            cause: Why it could not be expanded.
        """
        super().__init__("home", f"cannot expand {where}: {cause}")


class HomeNotFoundError(HomeError):
    """The ijob home is not a directory, so `ijob setup` has not been run."""

    def __init__(self, path: Path) -> None:
        """Build the error from the path that was expected to be the home.

        Args:
            path: The home that is missing.
        """
        super().__init__("home", f"ijob home not found at {path}, run: ijob setup")


class Outcome(StrEnum):
    """What `setup_home` did about one item."""

    CREATED = "created"
    PRESENT = "already there"


@dataclass(frozen=True, slots=True)
class SetupItem:
    """One path `setup_home` looked after.

    Attributes:
        path: The directory or file.
        outcome: Whether it was created or already existed.
        exposed_mode: The permission bits of a directory that already existed
            and lets its group or others in, otherwise `None`.
    """

    path: Path
    outcome: Outcome
    exposed_mode: int | None = None


def resolve_home(environ: Mapping[str, str]) -> IjobHome:
    """Locate the ijob home.

    Args:
        environ: The process environment.

    Returns:
        The home named by a non-empty `IJOB_HOME` with a leading `~` expanded
        and made absolute against the working directory, or `~/.ijob` when it is
        unset or empty.

    Raises:
        HomeUnavailableError: A `~user` prefix names an unknown user, or the
            user's home directory cannot be determined.
    """
    named = environ.get(_HOME_VARIABLE)
    try:
        if named:
            return IjobHome(Path(named).expanduser().absolute())
        return IjobHome(Path.home() / _DEFAULT_DIRECTORY)
    except RuntimeError as error:
        raise HomeUnavailableError(named or "~", error) from error


def require_home(home: IjobHome) -> None:
    """Check that the home exists, without touching the filesystem further.

    Args:
        home: The home to check.

    Raises:
        HomeNotFoundError: `home.root` is not a directory.
    """
    if not home.root.is_dir():
        raise HomeNotFoundError(home.root)


def setup_home(home: IjobHome) -> tuple[SetupItem, ...]:
    """Create whatever of the home is missing and leave the rest alone.

    The home and its `cache` directory are created readable by the owner alone,
    whatever the umask. One that already exists keeps its mode, and is reported
    through `SetupItem.exposed_mode` when its group or others can enter it. The
    config file is written from the commented template only when absent, so an
    edited one is never overwritten. The database is created, migrated and given
    the bundled seed sources. The trace database is created and migrated too.

    Args:
        home: The home to set up; missing parent directories are created too.

    Returns:
        What happened to the home, its cache directory, the config file, the
        database and the trace database, in that order.

    Raises:
        OSError: A directory or file cannot be created or written.
        sqlite3.Error: A database cannot be created or migrated.
    """
    cache_dir = home.cache.parent
    return (
        _make_private_dir(home.root),
        _make_private_dir(cache_dir),
        SetupItem(home.config, _write_config_template(home.config)),
        SetupItem(home.db, _create_database(home.db)),
        SetupItem(home.traces, _create_trace_database(home.traces)),
    )


def _make_private_dir(path: Path) -> SetupItem:
    """Create a directory only its owner can use, unless it already exists."""
    if path.is_dir():
        mode = stat.S_IMODE(path.stat().st_mode)
        exposed = mode if mode & _OTHERS_MASK else None
        return SetupItem(path, Outcome.PRESENT, exposed)
    path.mkdir(mode=_PRIVATE, parents=True)
    path.chmod(_PRIVATE)
    return SetupItem(path, Outcome.CREATED)


def _write_config_template(path: Path) -> Outcome:
    """Write the commented config template unless the file already exists."""
    template = (files("ijob_app") / "data" / _TEMPLATE_FILE).read_text(encoding="utf-8")
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(template)
    except FileExistsError:
        return Outcome.PRESENT
    return Outcome.CREATED


def _create_database(path: Path) -> Outcome:
    """Open the database, migrating it and importing the seeds, and close it."""
    outcome = Outcome.PRESENT if path.exists() else Outcome.CREATED
    with Store.open(path) as store:
        store.import_seeds(load_seeds())
    return outcome


def _create_trace_database(path: Path) -> Outcome:
    """Open the trace database, migrating it, and close it."""
    outcome = Outcome.PRESENT if path.exists() else Outcome.CREATED
    with TraceStore.open(path):
        pass
    return outcome

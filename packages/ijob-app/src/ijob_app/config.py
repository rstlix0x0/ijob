r"""Search filters read from the user's config file and from command-line values.

`load_filters` builds a `FilterSpec` from the packaged default config with the
user's file layered over it key by key, so a file that sets only `countries`
keeps the default role patterns. The `parse_*` functions read one flag value
each. Every bad value raises `InvalidValueError` naming the key or flag.

The config file is `$XDG_CONFIG_HOME/ijob/config.toml`, or
`~/.config/ijob/config.toml` when `XDG_CONFIG_HOME` is unset or empty. Its
filter settings are:

```toml
[filters]
countries = ["SG"]          # country codes or names
remote = ["worldwide"]      # none, country, region or worldwide
since = "7d"                # <n>d or <n>h
companies = ["Acme"]        # case-insensitive substrings
require_salary = false

[filters.min_salary]
amount = 6000
currency = "SGD"
period = "month"            # hour, month or year

[filters.role]
include = ['\bengineer']      # regular expressions, case-insensitive
exclude = ['\bsales\b']
```
"""

import re
import tomllib
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Final

from ijob_core.countries import CountryCode, parse_country
from ijob_core.errors import InvalidValueError
from ijob_core.filters import FilterSpec
from ijob_core.model import RemoteScope
from ijob_core.money import Money, Period

_DEFAULT_FILE: Final = "default_config.toml"
_SINCE_PATTERN: Final = re.compile(r"(?P<count>[0-9]+)(?P<unit>[dh])", re.ASCII)
_SINCE_UNITS: Final = {"d": "days", "h": "hours"}
_MAX_SINCE: Final = timedelta(days=36500)
_MIN_SALARY_PARTS: Final = 3
_FILTER_KEYS: Final = frozenset(
    {
        "role",
        "countries",
        "remote",
        "since",
        "companies",
        "min_salary",
        "require_salary",
    }
)
_ROLE_KEYS: Final = frozenset({"include", "exclude"})
_MIN_SALARY_KEYS: Final = frozenset({"amount", "currency", "period"})

type _Table = dict[str, object]


def default_config_path(environ: Mapping[str, str]) -> Path:
    """Locate the config file when the caller names none.

    Args:
        environ: The process environment.

    Returns:
        `$XDG_CONFIG_HOME/ijob/config.toml`, or `~/.config/ijob/config.toml`
        when `XDG_CONFIG_HOME` is unset or empty.
    """
    config_home = environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "ijob" / "config.toml"


def load_filters(path: Path, *, explicit: bool) -> FilterSpec:
    """Build the search filters from the default config and a user file.

    Args:
        path: The user's config file.
        explicit: Whether the user named `path` themselves. A named file that
            does not exist is an error; an unnamed one is simply skipped.

    Returns:
        The filters, with each key the file sets replacing the default's.

    Raises:
        InvalidValueError: If `explicit` and `path` does not exist, if the file
            cannot be read or is not valid TOML, or if a key holds a bad value
            or an unknown key appears under `[filters]`; the message names the
            path or the key.
    """
    config = _read_default()
    if path.exists():
        config = _layer(config, _read_user(path))
    elif explicit:
        raise InvalidValueError("config", f"{path} does not exist")
    return _build_spec(config)


def parse_since(text: str, *, field: str = "--since") -> timedelta:
    """Read an age such as `7d` or `24h`.

    Args:
        text: A whole number greater than zero followed by `d` (days) or `h`
            (hours), spanning at most 100 years.
        field: The name reported in the error.

    Returns:
        The age as a `timedelta`.

    Raises:
        InvalidValueError: If `text` is not in that form or spans over 100 years.
    """
    match = _SINCE_PATTERN.fullmatch(text)
    if match is None or not match["count"].strip("0"):
        raise InvalidValueError(field, f"{text!r} is not <n>d or <n>h with n above 0")
    try:
        age = timedelta(**{_SINCE_UNITS[match["unit"]]: int(match["count"])})
    except (OverflowError, ValueError) as error:
        raise InvalidValueError(field, f"{text!r} is too long a time") from error
    if age > _MAX_SINCE:
        raise InvalidValueError(field, f"{text!r} is too long a time")
    return age


def parse_min_salary(text: str, *, field: str = "--min-salary") -> Money:
    """Read a salary threshold such as `6000:SGD:month`.

    Args:
        text: `AMOUNT:CURRENCY:PERIOD`, with a three-letter uppercase currency
            and a period of `hour`, `month` or `year`.
        field: The name reported in the error.

    Returns:
        The threshold as a `Money` with only its `min` set.

    Raises:
        InvalidValueError: If `text` is not in that form.
    """
    parts = text.split(":")
    if len(parts) != _MIN_SALARY_PARTS:
        reason = f"{text!r} is not AMOUNT:CURRENCY:PERIOD"
        raise InvalidValueError(field, reason)
    amount, currency, period = parts
    try:
        figure = Decimal(amount)
    except InvalidOperation as error:
        raise InvalidValueError(field, f"{amount!r} is not a number") from error
    return _threshold(figure, currency, period, field)


def parse_remote(text: str, *, field: str = "--remote") -> RemoteScope:
    """Read a remote scope.

    Args:
        text: `none`, `country`, `region` or `worldwide`.
        field: The name reported in the error.

    Returns:
        The scope.

    Raises:
        InvalidValueError: If `text` is not one of those.
    """
    try:
        return RemoteScope(text)
    except ValueError as error:
        allowed = ", ".join(scope.value for scope in RemoteScope)
        raise InvalidValueError(field, f"{text!r} is not one of {allowed}") from error


def parse_country_flag(text: str, *, field: str = "--country") -> CountryCode:
    """Read a country given as a code or a name.

    Args:
        text: Anything `parse_country` reads, e.g. `SG` or `Japan`.
        field: The name reported in the error.

    Returns:
        The country code.

    Raises:
        InvalidValueError: If `text` names no country.
    """
    code = parse_country(text)
    if code is None:
        raise InvalidValueError(field, f"{text!r} is not a country")
    return code


def _threshold(amount: Decimal, currency: str, period: str, field: str) -> Money:
    """Build a salary threshold, naming `field` in any error."""
    try:
        parsed = Period(period)
    except ValueError as error:
        allowed = ", ".join(each.value for each in Period)
        raise InvalidValueError(field, f"{period!r} is not one of {allowed}") from error
    try:
        return Money(amount, None, currency, parsed)
    except InvalidValueError as error:
        raise InvalidValueError(field, f"{error.field} {error.reason}") from error


def _read_default() -> _Table:
    """Read the config packaged with ijob."""
    text = (files("ijob_app") / "data" / _DEFAULT_FILE).read_text(encoding="utf-8")
    return tomllib.loads(text)


def _read_user(path: Path) -> _Table:
    """Read the user's config file, naming the path in any error."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise InvalidValueError("config", f"{path}: {error}") from error


def _layer(base: _Table, over: _Table) -> _Table:
    """Lay `over` on `base`, key by key through nested tables."""
    merged = dict(base)
    for key, value in over.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _layer(current, value)
        else:
            merged[key] = value
    return merged


def _build_spec(config: _Table) -> FilterSpec:
    """Turn the layered config into a `FilterSpec`."""
    filters = _table(config.get("filters", {}), "filters")
    _only_keys(filters, _FILTER_KEYS, "filters")
    role = _table(filters.get("role", {}), "filters.role")
    _only_keys(role, _ROLE_KEYS, "filters.role")
    return FilterSpec(
        include=_patterns(role.get("include", []), "filters.role.include"),
        exclude=_patterns(role.get("exclude", []), "filters.role.exclude"),
        countries=_countries(filters.get("countries", [])),
        remote=_remote(filters.get("remote", [])),
        since=_since(filters.get("since")),
        companies=tuple(_strings(filters.get("companies", []), "filters.companies")),
        min_salary=_min_salary(filters.get("min_salary")),
        require_salary=_flag(filters.get("require_salary", False)),
    )


def _table(value: object, key: str) -> _Table:
    """Require a TOML table."""
    if not isinstance(value, dict):
        raise InvalidValueError(key, "not a table")
    return value


def _only_keys(table: _Table, allowed: frozenset[str], prefix: str) -> None:
    """Reject a key the table is not meant to hold."""
    for key in table:
        if key not in allowed:
            raise InvalidValueError(f"{prefix}.{key}", "unknown key")


def _strings(value: object, key: str) -> list[str]:
    """Require a list of strings."""
    if not isinstance(value, list):
        raise InvalidValueError(key, "is not a list")
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidValueError(key, f"{item!r} is not a string")
        strings.append(item)
    return strings


def _patterns(value: object, key: str) -> tuple[re.Pattern[str], ...]:
    """Compile a list of case-insensitive regular expressions."""
    compiled: list[re.Pattern[str]] = []
    for text in _strings(value, key):
        try:
            compiled.append(re.compile(text, re.IGNORECASE))
        except re.error as error:
            raise InvalidValueError(
                key, f"{text!r} is not a pattern: {error}"
            ) from error
    return tuple(compiled)


def _countries(value: object) -> frozenset[CountryCode]:
    """Read a list of countries."""
    key = "filters.countries"
    return frozenset(
        parse_country_flag(text, field=key) for text in _strings(value, key)
    )


def _remote(value: object) -> frozenset[RemoteScope]:
    """Read a list of remote scopes."""
    key = "filters.remote"
    return frozenset(parse_remote(text, field=key) for text in _strings(value, key))


def _since(value: object) -> timedelta | None:
    """Read the optional age limit."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError("filters.since", f"{value!r} is not a string")
    return parse_since(value, field="filters.since")


def _flag(value: object) -> bool:
    """Read `require_salary`."""
    if not isinstance(value, bool):
        raise InvalidValueError(
            "filters.require_salary", f"{value!r} is not true or false"
        )
    return value


def _min_salary(value: object) -> Money | None:
    """Read the optional salary threshold table."""
    key = "filters.min_salary"
    if value is None:
        return None
    table = _table(value, key)
    _only_keys(table, _MIN_SALARY_KEYS, key)
    missing = sorted(_MIN_SALARY_KEYS - table.keys())
    if missing:
        raise InvalidValueError(key, f"is missing {', '.join(missing)}")
    amount = table["amount"]
    currency = table["currency"]
    period = table["period"]
    if isinstance(amount, bool) or not isinstance(amount, int | float):
        raise InvalidValueError(key, f"amount {amount!r} is not a number")
    if not isinstance(currency, str) or not isinstance(period, str):
        raise InvalidValueError(key, "currency and period must be strings")
    return _threshold(Decimal(str(amount)), currency, period, key)

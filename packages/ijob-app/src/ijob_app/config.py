r"""Search filters read from the user's config file and from command-line values.

`load_filters` builds a `FilterSpec` from the packaged default config with the
user's file layered over it key by key, so a file that sets only `countries`
keeps the default role patterns. The `parse_*` functions read one flag value
each. Every bad value raises `InvalidValueError` naming the key or flag.

`FilterFlags` holds the filter values a front end collected as text.
`read_filters` lays the flags it was given over the config file's filters;
`read_filter_layers` keeps the packaged, user-file and flag filters apart, with
the fields each one names, and `effective_filters` merges them with the filters
an instruction derived, by the precedence flag, user file, instruction,
packaged;
`spec_from_flags` sets every flag-able field from the flags, so an empty value
removes that filter, and `flags_from_spec` writes a spec back as the text a
user would type.

The config file is `config.toml` in the ijob home (see `ijob_app.home`). Its
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Final, Literal

from ijob_core.countries import CountryCode, parse_country
from ijob_core.errors import InvalidValueError
from ijob_core.filters import FilterSpec
from ijob_core.model import RemoteScope
from ijob_core.money import Money, Period

type FilterField = Literal[
    "include",
    "exclude",
    "countries",
    "remote",
    "since",
    "companies",
    "min_salary",
    "require_salary",
]
"""The name of a `FilterSpec` field a config source can set."""

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
_ROLE_FIELDS: Final[tuple[FilterField, ...]] = ("include", "exclude")
_PLAIN_FIELDS: Final[tuple[FilterField, ...]] = (
    "countries",
    "remote",
    "since",
    "companies",
    "min_salary",
    "require_salary",
)
_MIN_SALARY_KEYS: Final = frozenset({"amount", "currency", "period"})

type _Table = dict[str, object]


@dataclass(frozen=True, slots=True)
class FilterFlags:
    """The filter values a front end collected as text.

    Attributes:
        countries: The country values; empty when none was given.
        remotes: The remote-scope values; empty when none was given.
        since: The age limit, or `None`.
        companies: The company values; empty when none was given.
        min_salary: The salary threshold as `AMOUNT:CURRENCY:PERIOD`, or `None`.
        require_salary: Whether a posting without a salary is dropped, or
            `None` when no choice was made.
    """

    countries: tuple[str, ...] = ()
    remotes: tuple[str, ...] = ()
    since: str | None = None
    companies: tuple[str, ...] = ()
    min_salary: str | None = None
    require_salary: bool | None = None


@dataclass(frozen=True, slots=True)
class FilterLayers:
    """The filters each source sets, kept apart so their origin is known.

    A field a source does not name holds `FilterSpec`'s default for it. Which
    fields a source names is kept apart from their values, so a value equal to the
    default (`require_salary = false`, `include = []`) still counts as chosen.

    Attributes:
        packaged: The filters of the config packaged with ijob.
        user: The filters of the user's config file; the default spec when
            there is no file.
        user_set: The fields the user's file names.
        flags: The filters the flags set.
        flags_set: The fields a flag was given for.
    """

    packaged: FilterSpec
    user: FilterSpec
    user_set: frozenset[FilterField]
    flags: FilterSpec
    flags_set: frozenset[FilterField]


@dataclass(frozen=True, slots=True)
class ShadowedField:
    """An instruction-derived filter that a flag or the user's file overrides.

    Attributes:
        field: The `FilterSpec` field name, e.g. `countries`.
        used_from: Whether a `flag` or the user's `config` file supplied the
            value that is used.
        used_value: The value in force, as text.
        ignored_value: The instruction's value that is ignored, as text.
    """

    field: FilterField
    used_from: Literal["flag", "config"]
    used_value: str
    ignored_value: str


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


def read_filters(
    config: Path | None, flags: FilterFlags, *, default: Path | None = None
) -> FilterSpec:
    """Build the search filters from the config file with the flags laid over it.

    Args:
        config: The config file the user named, or `None` to use `default`.
        flags: The filter values given; each one given replaces the config's
            value, and one left empty keeps it.
        default: The file to read when `config` is `None`; it is skipped if it
            does not exist. With neither given, only the packaged defaults
            apply.

    Returns:
        The filters.

    Raises:
        InvalidValueError: The config file or a flag value is unusable; a
            blank company is one.
    """
    path = config or default
    if path is None:
        spec = _build_spec(_read_default())
    else:
        spec = load_filters(path, explicit=config is not None)
    return _lay_flags(spec, flags)


def read_filter_layers(
    config: Path | None, flags: FilterFlags, *, default: Path | None = None
) -> FilterLayers:
    """Read the packaged, user-file and flag filters as separate layers.

    Args:
        config: The config file the user named, or `None` to use `default`.
        flags: The filter values given.
        default: The file to read when `config` is `None`; it is skipped if it
            does not exist. With neither given, the user layer is empty.

    Returns:
        The three layers; `user` holds only what the file itself sets, and
        `user_set` and `flags_set` name the fields each of them was given.

    Raises:
        InvalidValueError: The config file or a flag value is unusable; a
            blank company is one.
    """
    path = config or default
    user = FilterSpec()
    user_set: frozenset[FilterField] = frozenset()
    if path is not None and path.exists():
        table = _read_user(path)
        user = _build_spec(table)
        user_set = _given_fields(table)
    elif config is not None:
        raise InvalidValueError("config", f"{config} does not exist")
    return FilterLayers(
        packaged=_build_spec(_read_default()),
        user=user,
        user_set=user_set,
        flags=_lay_flags(FilterSpec(), flags),
        flags_set=_flags_given(flags),
    )


def effective_filters(
    layers: FilterLayers, instruction: Mapping[str, object] | None
) -> tuple[FilterSpec, tuple[ShadowedField, ...]]:
    """Merge the filter layers with the filters an instruction derived.

    Each field is taken from the first source that sets it, in the order flag,
    user file, instruction, packaged. A field is set when its source names it
    (a flag given, a key present in the file or the instruction), even if the
    value equals `FilterSpec`'s default.

    Args:
        layers: The filters read from the packaged config, the user's file and
            the flags.
        instruction: The instruction's filters in the shape of the config
            file's `[filters]` table, or `None` for no instruction.

    Returns:
        The filters in force, and one `ShadowedField` for each instruction
        field that a flag or the user's file overrides with a different value,
        in `FilterSpec` field order.

    Raises:
        InvalidValueError: A value in `instruction` is unusable; the message
            names the key under `instructions.filters`.
    """
    derived = FilterSpec()
    derived_set: frozenset[FilterField] = frozenset()
    if instruction is not None:
        table: _Table = {"filters": dict(instruction)}
        derived = _build_spec(table, "instructions.filters")
        derived_set = _given_fields(table, "instructions.filters")
    ranked: tuple[
        tuple[
            Literal["flag", "config", "instruction"],
            FilterSpec,
            frozenset[FilterField],
        ],
        ...,
    ] = (
        ("flag", layers.flags, layers.flags_set),
        ("config", layers.user, layers.user_set),
        ("instruction", derived, derived_set),
    )
    shadowed: list[ShadowedField] = []

    def pick[T](name: FilterField, get: Callable[[FilterSpec], T]) -> T:
        ignored = get(derived)
        for origin, spec, given in ranked:
            if name not in given:
                continue
            used = get(spec)
            if origin != "instruction" and name in derived_set and used != ignored:
                shadowed.append(
                    ShadowedField(name, origin, _value_text(used), _value_text(ignored))
                )
            return used
        return get(layers.packaged)

    spec = FilterSpec(
        include=pick("include", lambda each: each.include),
        exclude=pick("exclude", lambda each: each.exclude),
        countries=pick("countries", lambda each: each.countries),
        remote=pick("remote", lambda each: each.remote),
        since=pick("since", lambda each: each.since),
        companies=pick("companies", lambda each: each.companies),
        min_salary=pick("min_salary", lambda each: each.min_salary),
        require_salary=pick("require_salary", lambda each: each.require_salary),
    )
    return spec, tuple(shadowed)


def spec_from_flags(base: FilterSpec, flags: FilterFlags) -> FilterSpec:
    """Set every filter a user can type from the flags.

    Unlike `read_filters`, a flag left empty removes its filter instead of
    keeping the base's, so a front end can show the filters as inputs and let
    the user clear one.

    Args:
        base: The spec whose role patterns are kept.
        flags: The values for the country, remote, age, company, salary
            threshold and salary-required filters.

    Returns:
        `base` with those filters replaced by the flags' values.

    Raises:
        InvalidValueError: A flag value is unusable; a blank company is one.
    """
    return replace(
        base,
        countries=_country_codes(flags.countries),
        remote=frozenset(map(parse_remote, flags.remotes)),
        since=None if flags.since is None else parse_since(flags.since),
        companies=_company_names(flags.companies),
        min_salary=(
            None if flags.min_salary is None else parse_min_salary(flags.min_salary)
        ),
        require_salary=bool(flags.require_salary),
    )


def flags_from_spec(spec: FilterSpec) -> FilterFlags:
    """Write a spec's user-typable filters as the text a user would type.

    Args:
        spec: The filters to render.

    Returns:
        Flags that `spec_from_flags` turns back into the same filters. The age
        is whole days as `<n>d`, otherwise hours as `<n>h`, so a sub-hour
        remainder is dropped. A salary threshold with no minimum or no period
        has no text form and is left out.
    """
    return FilterFlags(
        countries=tuple(sorted(spec.countries)),
        remotes=tuple(sorted(scope.value for scope in spec.remote)),
        since=None if spec.since is None else _since_text(spec.since),
        companies=spec.companies,
        min_salary=_min_salary_text(spec.min_salary),
        require_salary=spec.require_salary,
    )


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


def _country_codes(values: Sequence[str]) -> frozenset[CountryCode]:
    """Read country flag values."""
    return frozenset(map(parse_country_flag, values))


def _company_names(values: Sequence[str]) -> tuple[str, ...]:
    """Require company flag values to be non-blank."""
    if not all(name.strip() for name in values):
        raise InvalidValueError("company", "must not be blank")
    return tuple(values)


def _lay_flags(spec: FilterSpec, flags: FilterFlags) -> FilterSpec:
    """Set each filter a flag was given for, and keep the rest of `spec`."""
    if flags.countries:
        spec = replace(spec, countries=_country_codes(flags.countries))
    if flags.remotes:
        spec = replace(spec, remote=frozenset(map(parse_remote, flags.remotes)))
    if flags.since is not None:
        spec = replace(spec, since=parse_since(flags.since))
    if flags.companies:
        spec = replace(spec, companies=_company_names(flags.companies))
    if flags.min_salary is not None:
        spec = replace(spec, min_salary=parse_min_salary(flags.min_salary))
    if flags.require_salary is not None:
        spec = replace(spec, require_salary=flags.require_salary)
    return spec


def _flags_given(flags: FilterFlags) -> frozenset[FilterField]:
    """Name the fields `_lay_flags` sets from `flags`."""
    given: set[FilterField] = set()
    if flags.countries:
        given.add("countries")
    if flags.remotes:
        given.add("remote")
    if flags.since is not None:
        given.add("since")
    if flags.companies:
        given.add("companies")
    if flags.min_salary is not None:
        given.add("min_salary")
    if flags.require_salary is not None:
        given.add("require_salary")
    return frozenset(given)


def _given_fields(config: _Table, prefix: str = "filters") -> frozenset[FilterField]:
    """Name the fields the `filters` table of `config` has a key for.

    `config` must already have passed `_build_spec`.
    """
    filters = _table(config.get("filters", {}), prefix)
    role = _table(filters.get("role", {}), f"{prefix}.role")
    return frozenset(
        {field for field in _PLAIN_FIELDS if field in filters}
        | {field for field in _ROLE_FIELDS if field in role}
    )


def _value_text(value: object) -> str:
    """Write one `FilterSpec` field's value as text for a message."""
    match value:
        case frozenset():
            return ", ".join(sorted(_item_text(item) for item in value))
        case tuple():
            return ", ".join(_item_text(item) for item in value)
        case timedelta():
            return _since_text(value)
        case Money():
            return _min_salary_text(value) or str(value)
        case bool():
            return "true" if value else "false"
        case _:
            return str(value)


def _item_text(item: object) -> str:
    """Write one member of a set or tuple field as text."""
    if isinstance(item, RemoteScope):
        return item.value
    if isinstance(item, re.Pattern):
        return str(item.pattern)
    return str(item)


def _since_text(age: timedelta) -> str:
    """Write an age as `<n>d` for whole days, else as `<n>h`."""
    if age % timedelta(days=1) == timedelta(0):
        return f"{age // timedelta(days=1)}d"
    return f"{age // timedelta(hours=1)}h"


def _min_salary_text(money: Money | None) -> str | None:
    """Write a salary threshold as `AMOUNT:CURRENCY:PERIOD`, or `None` for none."""
    if money is None or money.min is None or money.period is None:
        return None
    return f"{money.min:f}:{money.currency}:{money.period}"


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


def _build_spec(config: _Table, prefix: str = "filters") -> FilterSpec:
    """Turn the `filters` table of `config` into a `FilterSpec`.

    `prefix` is the key an error names for that table.
    """
    filters = _table(config.get("filters", {}), prefix)
    _only_keys(filters, _FILTER_KEYS, prefix)
    role = _table(filters.get("role", {}), f"{prefix}.role")
    _only_keys(role, _ROLE_KEYS, f"{prefix}.role")
    return FilterSpec(
        include=_patterns(role.get("include", []), f"{prefix}.role.include"),
        exclude=_patterns(role.get("exclude", []), f"{prefix}.role.exclude"),
        countries=_countries(filters.get("countries", []), f"{prefix}.countries"),
        remote=_remote(filters.get("remote", []), f"{prefix}.remote"),
        since=_since(filters.get("since"), f"{prefix}.since"),
        companies=tuple(_strings(filters.get("companies", []), f"{prefix}.companies")),
        min_salary=_min_salary(filters.get("min_salary"), f"{prefix}.min_salary"),
        require_salary=_flag(
            filters.get("require_salary", False), f"{prefix}.require_salary"
        ),
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


def _countries(value: object, key: str) -> frozenset[CountryCode]:
    """Read a list of countries."""
    return frozenset(
        parse_country_flag(text, field=key) for text in _strings(value, key)
    )


def _remote(value: object, key: str) -> frozenset[RemoteScope]:
    """Read a list of remote scopes."""
    return frozenset(parse_remote(text, field=key) for text in _strings(value, key))


def _since(value: object, key: str) -> timedelta | None:
    """Read the optional age limit."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidValueError(key, f"{value!r} is not a string")
    return parse_since(value, field=key)


def _flag(value: object, key: str) -> bool:
    """Read `require_salary`."""
    if not isinstance(value, bool):
        raise InvalidValueError(key, f"{value!r} is not true or false")
    return value


def _min_salary(value: object, key: str) -> Money | None:
    """Read the optional salary threshold table."""
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

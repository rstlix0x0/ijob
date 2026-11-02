import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from ijob_app.config import (
    FilterFlags,
    ShadowedField,
    effective_filters,
    flags_from_spec,
    load_filters,
    parse_country_flag,
    parse_min_salary,
    parse_remote,
    parse_since,
    read_filter_layers,
    read_filters,
    spec_from_flags,
)
from ijob_core.countries import CountryCode
from ijob_core.errors import InvalidValueError
from ijob_core.filters import FilterSpec, keep
from ijob_core.model import Posting, PostingId, RemoteScope
from ijob_core.money import Money, Period

_NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
_DEFAULT_INCLUDE = (r"\bengineer", r"\bdeveloper", "エンジニア", "開発")
_DEFAULT_EXCLUDE = (
    r"\bsales\b",
    r"\brecruit",
    r"\bmarketing\b",
    r"\baccount executive\b",
)
_SINCE_AND_SALARY = (
    '[filters]\nsince = "3d"\n[filters.min_salary]\n'
    'amount = 1\ncurrency = "USD"\nperiod = "year"\n'
)


def _posting(title: str) -> Posting:
    return Posting(
        id=PostingId(1),
        title=title,
        company="Acme",
        location=None,
        url="https://example.com/job/1",
        posted_at=None,
        first_seen=_NOW,
        last_seen=_NOW,
    )


def _patterns(patterns: tuple[re.Pattern[str], ...]) -> tuple[str, ...]:
    return tuple(pattern.pattern for pattern in patterns)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_default_config_when_file_missing(tmp_path: Path) -> None:
    spec = load_filters(tmp_path / "absent.toml", explicit=False)

    assert _patterns(spec.include) == _DEFAULT_INCLUDE
    assert _patterns(spec.exclude) == _DEFAULT_EXCLUDE
    assert all(p.flags & re.IGNORECASE for p in spec.include + spec.exclude)
    assert spec == FilterSpec(include=spec.include, exclude=spec.exclude)


@pytest.mark.parametrize(
    ("title", "matches"),
    [
        (
            "Toptal: Salesforce Developers with AI-Assisted Coding Experience",
            (True, False),
        ),
        ("Software Engineer", (True, False)),
        ("Engineering", (True, False)),
        (
            "28卒対象 / セキュリティエンジニア インターンシップ- Mercari Group",
            (True, False),
        ),
        ("Account Executive - Commercial, Spotify Technology", (False, True)),
        ("Sales Engineer", (True, True)),
    ],
)
def test_default_patterns(
    tmp_path: Path, title: str, matches: tuple[bool, bool]
) -> None:
    """`matches` is the probe table's (include, exclude) columns."""
    spec = load_filters(tmp_path / "absent.toml", explicit=False)

    included = any(p.search(title) for p in spec.include)
    excluded = any(p.search(title) for p in spec.exclude)
    assert (included, excluded) == matches
    assert keep(_posting(title), spec, now=_NOW) is (not excluded)


def test_full_config_round_trip(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[filters]
countries = ["SG", "Japan"]
remote = ["worldwide", "region"]
since = "36h"
companies = ["Acme", "Globex"]
require_salary = true

[filters.min_salary]
amount = 6000
currency = "SGD"
period = "month"

[filters.role]
include = ['\\bstaff']
exclude = ['intern']
""",
    )

    spec = load_filters(path, explicit=True)

    assert _patterns(spec.include) == (r"\bstaff",)
    assert _patterns(spec.exclude) == ("intern",)
    assert spec.include[0].flags & re.IGNORECASE
    assert spec.countries == frozenset({CountryCode("SG"), CountryCode("JP")})
    assert spec.remote == frozenset({RemoteScope.WORLDWIDE, RemoteScope.REGION})
    assert spec.since == timedelta(hours=36)
    assert spec.companies == ("Acme", "Globex")
    assert spec.min_salary == Money(Decimal(6000), None, "SGD", Period.MONTH)
    assert spec.require_salary is True


def test_float_amount_is_read_exactly(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[filters.min_salary]\namount = 0.1\ncurrency = "USD"\nperiod = "hour"\n',
    )

    spec = load_filters(path, explicit=True)

    assert spec.min_salary == Money(Decimal("0.1"), None, "USD", Period.HOUR)


def test_partial_config_keeps_default_roles(tmp_path: Path) -> None:
    path = _write(tmp_path, '[filters]\ncountries = ["SG"]\n')

    spec = load_filters(path, explicit=True)

    assert spec.countries == frozenset({CountryCode("SG")})
    assert _patterns(spec.include) == _DEFAULT_INCLUDE
    assert _patterns(spec.exclude) == _DEFAULT_EXCLUDE


def test_config_overrides_one_role_list_only(tmp_path: Path) -> None:
    path = _write(tmp_path, "[filters.role]\ninclude = []\n")

    spec = load_filters(path, explicit=True)

    assert spec.include == ()
    assert _patterns(spec.exclude) == _DEFAULT_EXCLUDE


def test_missing_explicit_config_raises(tmp_path: Path) -> None:
    path = tmp_path / "absent.toml"

    with pytest.raises(InvalidValueError, match=re.escape(str(path))):
        load_filters(path, explicit=True)

    assert load_filters(path, explicit=False).include


def test_unreadable_config_raises_naming_path(tmp_path: Path) -> None:
    with pytest.raises(InvalidValueError, match=re.escape(str(tmp_path))):
        load_filters(tmp_path, explicit=True)


def test_malformed_toml_raises_naming_path(tmp_path: Path) -> None:
    path = _write(tmp_path, "[filters\n")

    with pytest.raises(InvalidValueError, match=re.escape(str(path))):
        load_filters(path, explicit=True)


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ('[filters]\nsince = "7x"\n', "filters.since"),
        ("[filters]\nsince = 7\n", "filters.since"),
        ('[filters]\nremote = ["moon"]\n', "filters.remote"),
        ('[filters]\nremote = "worldwide"\n', "filters.remote"),
        ('[filters.role]\ninclude = ["("]\n', "filters.role.include"),
        ('[filters.role]\nexclude = ["["]\n', "filters.role.exclude"),
        ("[filters.role]\ninclude = [1]\n", "filters.role.include"),
        ('[filters]\ncountries = ["Atlantis"]\n', "filters.countries"),
        ("[filters]\ncompanies = [1]\n", "filters.companies"),
        ('[filters]\nrequire_salary = "yes"\n', "filters.require_salary"),
        (
            '[filters.min_salary]\namount = 1\ncurrency = "sgd"\nperiod = "month"\n',
            "filters.min_salary",
        ),
        (
            '[filters.min_salary]\namount = -1\ncurrency = "SGD"\nperiod = "month"\n',
            "filters.min_salary",
        ),
        (
            '[filters.min_salary]\namount = true\ncurrency = "SGD"\nperiod = "month"\n',
            "filters.min_salary",
        ),
        (
            '[filters.min_salary]\namount = 1\ncurrency = "SGD"\nperiod = "week"\n',
            "filters.min_salary",
        ),
        ('[filters.min_salary]\namount = 1\ncurrency = "SGD"\n', "filters.min_salary"),
        ("[filters]\ncontries = []\n", "filters.contries"),
        ("[filters.role]\nincludes = []\n", "filters.role.includes"),
        ("filters = 1\n", "filters"),
        ("[filters]\nrole = 1\n", "filters.role"),
        ("[filters]\nmin_salary = 1\n", "filters.min_salary"),
        (
            (
                '[filters.min_salary]\namount = 1\ncurrency = "SGD"\n'
                'period = "month"\nextra = 1\n'
            ),
            "filters.min_salary.extra",
        ),
        (
            '[filters.min_salary]\namount = 1\ncurrency = 1\nperiod = "month"\n',
            "filters.min_salary",
        ),
        (
            '[filters.min_salary]\namount = 1\ncurrency = "SGD"\nperiod = 1\n',
            "filters.min_salary",
        ),
    ],
)
def test_bad_config_names_key(tmp_path: Path, body: str, key: str) -> None:
    path = _write(tmp_path, body)

    with pytest.raises(InvalidValueError, match=re.escape(key)):
        load_filters(path, explicit=True)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("filters = 1\n", "invalid filters: not a table"),
        ("[filters]\nrole = 1\n", "invalid filters.role: not a table"),
        ("[filters]\nmin_salary = 1\n", "invalid filters.min_salary: not a table"),
        ("[filters]\ncontries = []\n", "invalid filters.contries: unknown key"),
        (
            '[filters.min_salary]\namount = 1\ncurrency = 1\nperiod = "month"\n',
            "invalid filters.min_salary: currency and period must be strings",
        ),
        (
            '[filters.min_salary]\namount = 1\ncurrency = "SGD"\nperiod = 1\n',
            "invalid filters.min_salary: currency and period must be strings",
        ),
        (
            '[filters.min_salary]\namount = 1\ncurrency = "sgd"\nperiod = "month"\n',
            "invalid filters.min_salary: currency 'sgd' is not three uppercase",
        ),
        (
            '[filters.min_salary]\namount = -1\ncurrency = "SGD"\nperiod = "month"\n',
            "invalid filters.min_salary: min -1 is negative",
        ),
        (
            '[filters.min_salary]\namount = 1\ncurrency = "SGD"\nperiod = "week"\n',
            "invalid filters.min_salary: 'week' is not one of",
        ),
    ],
)
def test_bad_config_message_reads_well(tmp_path: Path, body: str, message: str) -> None:
    path = _write(tmp_path, body)

    with pytest.raises(InvalidValueError) as caught:
        load_filters(path, explicit=True)

    assert message in str(caught.value)


def test_other_top_level_tables_are_ignored(tmp_path: Path) -> None:
    path = _write(tmp_path, '[display]\ncolour = "none"\n')

    assert load_filters(path, explicit=True).include


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7d", timedelta(days=7)),
        ("24h", timedelta(hours=24)),
        ("1d", timedelta(days=1)),
        ("36500d", timedelta(days=36500)),
        ("876000h", timedelta(hours=876000)),
    ],
)
def test_parse_since(text: str, expected: timedelta) -> None:
    assert parse_since(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "7",
        "d",
        "7w",
        "0d",
        "-1d",
        "7 d",
        "7D",
        "1.5d",
        "٣d",
        "99999999999d",
        "36501d",
        "876001h",
        "740000d",
        f"{'9' * 5000}d",
    ],
)
def test_parse_since_rejects(text: str) -> None:
    with pytest.raises(InvalidValueError, match="--since"):
        parse_since(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6000:SGD:month", Money(Decimal(6000), None, "SGD", Period.MONTH)),
        ("12.5:USD:hour", Money(Decimal("12.5"), None, "USD", Period.HOUR)),
        ("90000:EUR:year", Money(Decimal(90000), None, "EUR", Period.YEAR)),
    ],
)
def test_parse_min_salary(text: str, expected: Money) -> None:
    assert parse_min_salary(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "6000",
        "6000:SGD",
        "6000:SGD:month:extra",
        "lots:SGD:month",
        "nan:SGD:month",
        "-5:SGD:month",
        "6000:sgd:month",
        "6000:SGD:week",
        "6000:SGD:",
    ],
)
def test_parse_min_salary_rejects(text: str) -> None:
    with pytest.raises(InvalidValueError, match="--min-salary"):
        parse_min_salary(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("worldwide", RemoteScope.WORLDWIDE),
        ("none", RemoteScope.NONE),
        ("country", RemoteScope.COUNTRY),
        ("region", RemoteScope.REGION),
    ],
)
def test_parse_remote(text: str, expected: RemoteScope) -> None:
    assert parse_remote(text) is expected


@pytest.mark.parametrize("text", ["", "moon", "Worldwide"])
def test_parse_remote_rejects(text: str) -> None:
    with pytest.raises(InvalidValueError, match="--remote"):
        parse_remote(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("SG", "SG"), ("sg", "SG"), ("Japan", "JP"), ("United Kingdom", "GB")],
)
def test_parse_country_flag(text: str, expected: str) -> None:
    assert parse_country_flag(text) == CountryCode(expected)


@pytest.mark.parametrize("text", ["", "ZZ", "Atlantis", "Remote"])
def test_parse_country_flag_rejects(text: str) -> None:
    with pytest.raises(InvalidValueError, match="--country"):
        parse_country_flag(text)


def test_read_filters_layers_flags_over_config(tmp_path: Path) -> None:
    path = _write(tmp_path, 'filters.countries = ["SG"]\nfilters.since = "7d"\n')
    default = tmp_path / "home.toml"
    flags = FilterFlags(countries=("Japan",), companies=("Acme",))

    spec = read_filters(path, flags, default=default)

    assert spec.countries == {CountryCode("JP")}
    assert spec.companies == ("Acme",)
    assert spec.since == timedelta(days=7)
    assert read_filters(path, FilterFlags(), default=default).countries == {
        CountryCode("SG")
    }

    missing = tmp_path / "absent.toml"
    with pytest.raises(InvalidValueError, match=re.escape(str(missing))):
        read_filters(missing, FilterFlags(), default=default)
    with pytest.raises(InvalidValueError, match="must not be blank"):
        read_filters(path, FilterFlags(companies=(" ",)), default=default)


def test_read_filters_reads_the_default_file_when_none_is_named(
    tmp_path: Path,
) -> None:
    default = tmp_path / "home.toml"
    default.write_text('filters.countries = ["SG"]\n', encoding="utf-8")

    spec = read_filters(None, FilterFlags(), default=default)

    assert spec.countries == {CountryCode("SG")}


def test_read_filters_skips_a_missing_default_file(tmp_path: Path) -> None:
    spec = read_filters(None, FilterFlags(), default=tmp_path / "absent.toml")

    assert spec == load_filters(tmp_path / "absent.toml", explicit=False)


def test_read_filters_without_any_file_uses_the_packaged_defaults(
    tmp_path: Path,
) -> None:
    spec = read_filters(None, FilterFlags())

    assert spec == load_filters(tmp_path / "absent.toml", explicit=False)


def test_flags_from_spec_round_trips() -> None:
    base = FilterSpec(include=(re.compile("engineer", re.IGNORECASE),))
    spec = replace(
        base,
        countries=frozenset({CountryCode("SG"), CountryCode("JP")}),
        remote=frozenset({RemoteScope.WORLDWIDE}),
        since=timedelta(days=7),
        companies=("Acme",),
        min_salary=Money(Decimal(6000), None, "SGD", Period.MONTH),
        require_salary=True,
    )

    flags = flags_from_spec(spec)

    assert flags == FilterFlags(
        countries=("JP", "SG"),
        remotes=("worldwide",),
        since="7d",
        companies=("Acme",),
        min_salary="6000:SGD:month",
        require_salary=True,
    )
    assert spec_from_flags(base, flags) == spec
    assert flags_from_spec(replace(spec, since=timedelta(hours=36))).since == "36h"


def test_spec_from_flags_empty_clears() -> None:
    base = FilterSpec(
        include=(re.compile("engineer", re.IGNORECASE),),
        exclude=(re.compile("sales", re.IGNORECASE),),
        countries=frozenset({CountryCode("SG")}),
        remote=frozenset({RemoteScope.COUNTRY}),
        since=timedelta(days=3),
        companies=("Acme",),
        min_salary=Money(Decimal(1), None, "USD", Period.YEAR),
        require_salary=True,
    )

    spec = spec_from_flags(base, FilterFlags())

    assert spec == FilterSpec(include=base.include, exclude=base.exclude)


def test_spec_from_flags_rejects_bad_values() -> None:
    with pytest.raises(InvalidValueError, match="since"):
        spec_from_flags(FilterSpec(), FilterFlags(since="abc"))
    with pytest.raises(InvalidValueError, match="company"):
        spec_from_flags(FilterSpec(), FilterFlags(companies=("",)))


def test_read_filters_unchanged(tmp_path: Path) -> None:
    default = tmp_path / "home.toml"
    path = _write(
        tmp_path, '[filters]\ncountries = ["SG"]\n[filters.role]\ninclude = []\n'
    )
    flags = FilterFlags(companies=("Acme",))

    spec = read_filters(path, flags, default=default)
    merged, shadowed = effective_filters(
        read_filter_layers(path, flags, default=default), None
    )

    assert spec.include == ()
    assert spec.countries == {CountryCode("SG")}
    assert spec.companies == ("Acme",)
    assert shadowed == ()
    assert merged == spec


@pytest.mark.parametrize(
    ("text", "flags"),
    [
        (None, FilterFlags()),
        ("", FilterFlags()),
        ("[filters.role]\ninclude = []\nexclude = []\n", FilterFlags()),
        ('[filters]\ncountries = ["JP"]\nrequire_salary = true\n', FilterFlags()),
        (
            _SINCE_AND_SALARY,
            FilterFlags(since="9d", require_salary=False, remotes=("none",)),
        ),
        ('[filters]\ncompanies = ["A"]\n', FilterFlags(companies=("B",))),
    ],
)
def test_effective_filters_without_instruction_equals_read_filters(
    tmp_path: Path, text: str | None, flags: FilterFlags
) -> None:
    path = None if text is None else _write(tmp_path, text)
    default = tmp_path / "absent.toml"

    spec = read_filters(path, flags, default=default)
    merged, shadowed = effective_filters(
        read_filter_layers(path, flags, default=default), None
    )

    assert merged == spec
    assert shadowed == ()


def test_read_filter_layers_keeps_each_layer_apart(tmp_path: Path) -> None:
    path = _write(tmp_path, '[filters]\ncountries = ["JP"]\n')

    layers = read_filter_layers(path, FilterFlags(since="3d"), default=None)

    assert _patterns(layers.packaged.include) == _DEFAULT_INCLUDE
    assert layers.packaged.countries == frozenset()
    assert layers.user == FilterSpec(countries=frozenset({CountryCode("JP")}))
    assert layers.flags == FilterSpec(since=timedelta(days=3))
    missing = tmp_path / "absent.toml"
    with pytest.raises(InvalidValueError, match=re.escape(str(missing))):
        read_filter_layers(missing, FilterFlags(), default=None)
    assert read_filter_layers(None, FilterFlags(), default=missing).user == FilterSpec()


def test_effective_filters_checked_example(tmp_path: Path) -> None:
    path = _write(tmp_path, '[filters]\ncountries = ["JP"]\n')
    layers = read_filter_layers(path, FilterFlags(), default=None)
    instruction = {
        "role": {"include": ["Rust"]},
        "countries": ["ID"],
        "remote": ["worldwide", "country"],
    }

    spec, shadowed = effective_filters(layers, instruction)

    assert spec.countries == {CountryCode("JP")}
    assert _patterns(spec.include) == ("Rust",)
    assert spec.remote == {RemoteScope.WORLDWIDE, RemoteScope.COUNTRY}
    assert _patterns(spec.exclude) == _DEFAULT_EXCLUDE
    assert shadowed == (ShadowedField("countries", "config", "JP", "ID"),)


def test_effective_filters_without_instruction_merges_layers(tmp_path: Path) -> None:
    path = _write(tmp_path, '[filters]\ncountries = ["JP"]\n')
    layers = read_filter_layers(path, FilterFlags(countries=("SG",)), default=None)

    spec, shadowed = effective_filters(layers, None)

    assert spec.countries == {CountryCode("SG")}
    assert _patterns(spec.include) == _DEFAULT_INCLUDE
    assert shadowed == ()


def test_flag_beats_instruction(tmp_path: Path) -> None:
    path = _write(tmp_path, '[filters]\ncountries = ["JP"]\n')
    flags = FilterFlags(countries=("SG",), since="2d")
    layers = read_filter_layers(path, flags, default=None)
    instruction = {"countries": ["ID"], "since": "9d", "companies": ["Acme"]}

    spec, shadowed = effective_filters(layers, instruction)

    assert spec.countries == {CountryCode("SG")}
    assert spec.since == timedelta(days=2)
    assert spec.companies == ("Acme",)
    assert shadowed == (
        ShadowedField("countries", "flag", "SG", "ID"),
        ShadowedField("since", "flag", "2d", "9d"),
    )


def test_instruction_equal_to_the_winner_is_not_shadowed(tmp_path: Path) -> None:
    path = _write(tmp_path, '[filters]\ncountries = ["JP"]\n')
    layers = read_filter_layers(path, FilterFlags(), default=None)

    _, shadowed = effective_filters(layers, {"countries": ["JP"]})

    assert shadowed == ()


def test_no_require_salary_flag_beats_instruction(tmp_path: Path) -> None:
    layers = read_filter_layers(
        None, FilterFlags(require_salary=False), default=tmp_path / "absent.toml"
    )

    spec, shadowed = effective_filters(layers, {"require_salary": True})

    assert spec.require_salary is False
    assert shadowed == (ShadowedField("require_salary", "flag", "false", "true"),)


def test_user_empty_include_beats_instruction(tmp_path: Path) -> None:
    path = _write(tmp_path, "[filters.role]\ninclude = []\n")
    layers = read_filter_layers(path, FilterFlags(), default=None)

    spec, shadowed = effective_filters(layers, {"role": {"include": ["Rust"]}})

    assert spec.include == ()
    assert _patterns(spec.exclude) == _DEFAULT_EXCLUDE
    assert shadowed == (ShadowedField("include", "config", "", "Rust"),)


def test_field_the_user_file_names_beats_instruction_even_at_default(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "[filters]\nrequire_salary = false\n")
    layers = read_filter_layers(path, FilterFlags(), default=None)

    spec, shadowed = effective_filters(layers, {"require_salary": True})

    assert spec.require_salary is False
    assert shadowed == (ShadowedField("require_salary", "config", "false", "true"),)


def test_shadowed_values_are_written_as_the_user_types_them(tmp_path: Path) -> None:
    path = _write(tmp_path, "[filters.role]\ninclude = ['Go']\n")
    flags = FilterFlags(
        remotes=("none", "region"), min_salary="6000:SGD:month", require_salary=True
    )
    layers = read_filter_layers(path, flags, default=None)
    instruction = {
        "role": {"include": ["Rust", "Zig"]},
        "remote": ["worldwide", "country"],
        "min_salary": {"amount": 5000, "currency": "USD", "period": "year"},
        "require_salary": False,
    }

    _, shadowed = effective_filters(layers, instruction)

    assert shadowed == (
        ShadowedField("include", "config", "Go", "Rust, Zig"),
        ShadowedField("remote", "flag", "none, region", "country, worldwide"),
        ShadowedField("min_salary", "flag", "6000:SGD:month", "5000:USD:year"),
        ShadowedField("require_salary", "flag", "true", "false"),
    )


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ({"since": "banana"}, "instructions.filters.since"),
        ({"countries": ["Atlantis"]}, "instructions.filters.countries"),
        ({"contries": []}, "instructions.filters.contries"),
        ({"role": {"include": "Rust"}}, "instructions.filters.role.include"),
        ({"require_salary": "yes"}, "instructions.filters.require_salary"),
        ({"min_salary": {"amount": 1}}, "instructions.filters.min_salary"),
    ],
)
def test_instruction_filter_invalid_rejected(
    tmp_path: Path, table: dict[str, object], key: str
) -> None:
    layers = read_filter_layers(None, FilterFlags(), default=tmp_path / "absent.toml")

    with pytest.raises(InvalidValueError, match=re.escape(key)):
        effective_filters(layers, table)

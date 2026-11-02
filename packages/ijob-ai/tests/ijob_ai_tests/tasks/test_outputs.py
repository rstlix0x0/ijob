import pytest
from ijob_ai.tasks.outputs import (
    InstructionsOut,
    ProfileOut,
    RecipeOut,
    ScoreOut,
    SourceRecipeOut,
    VerdictOut,
)
from ijob_core.model import Seniority
from pydantic import ValidationError

_PROFILE = {
    "summary": "Backend engineer.",
    "years_experience": 7,
    "seniority": "senior",
    "roles": ["backend engineer"],
    "skills": ["python"],
    "spoken_languages": ["English"],
    "locations": ["Singapore"],
    "work_authorization": ["SG citizen"],
}


def test_output_bounds() -> None:
    with pytest.raises(ValidationError):
        ScoreOut(score=101, reasons=["fits"])
    with pytest.raises(ValidationError):
        InstructionsOut.model_validate({"filters": {}, "rules": ["rule"] * 11})
    with pytest.raises(ValidationError):
        VerdictOut(keep=True, reason="r" * 201, rule=None)
    with pytest.raises(ValidationError):
        InstructionsOut.model_validate({"filters": {"salary": 1}, "rules": []})


def test_score_and_verdict_edges() -> None:
    assert ScoreOut(score=100, reasons=["a"] * 5).score == 100
    assert ScoreOut(score=0, reasons=["a"]).score == 0
    with pytest.raises(ValidationError):
        ScoreOut(score=-1, reasons=["a"])
    with pytest.raises(ValidationError):
        ScoreOut(score=50, reasons=[])
    with pytest.raises(ValidationError):
        ScoreOut(score=50, reasons=["a"] * 6)
    with pytest.raises(ValidationError):
        ScoreOut(score=50, reasons=["a" * 201])
    assert VerdictOut(keep=True, reason="r" * 200, rule=None).rule is None
    assert VerdictOut(keep=False, reason="no", rule=3).rule == 3
    with pytest.raises(ValidationError):
        VerdictOut(keep=False, reason="", rule=1)
    with pytest.raises(ValidationError):
        VerdictOut(keep=False, reason="no", rule=0)


def test_profile_bounds() -> None:
    profile = ProfileOut.model_validate(_PROFILE)
    assert profile.seniority is Seniority.SENIOR
    assert ProfileOut.model_validate({**_PROFILE, "years_experience": None})
    for bad in (
        {"summary": "s" * 601},
        {"years_experience": 61},
        {"years_experience": -1},
        {"seniority": "wizard"},
        {"roles": ["r"] * 21},
        {"skills": ["s"] * 101},
        {"spoken_languages": ["s"] * 21},
        {"locations": ["s"] * 21},
        {"work_authorization": ["s"] * 21},
        {"skills": ["s" * 101]},
    ):
        with pytest.raises(ValidationError):
            ProfileOut.model_validate({**_PROFILE, **bad})


def test_instructions_filters_shape() -> None:
    parsed = InstructionsOut.model_validate(
        {
            "filters": {
                "countries": ["SG"],
                "remote": ["worldwide"],
                "since": "7d",
                "companies": ["Acme"],
                "min_salary": {"amount": 6000, "currency": "SGD", "period": "month"},
                "require_salary": True,
                "role": {"include": ["engineer"], "exclude": ["sales"]},
            },
            "rules": ["no crypto"],
        }
    )
    assert parsed.filters.role.include == ["engineer"]
    assert parsed.filters.min_salary is not None
    assert parsed.filters.min_salary.currency == "SGD"
    empty = InstructionsOut.model_validate({"filters": {}, "rules": []})
    assert empty.filters.min_salary is None
    assert empty.filters.countries == []


def test_instructions_rejects_unknown_nested_keys() -> None:
    for filters in (
        {"role": {"title": ["x"]}},
        {"min_salary": {"amount": 1, "currency": "SGD", "period": "month", "x": 1}},
        {"min_salary": {"amount": 1, "currency": "SGD", "period": "week"}},
        {"min_salary": {"amount": 1, "currency": "SGD"}},
    ):
        with pytest.raises(ValidationError):
            InstructionsOut.model_validate({"filters": filters, "rules": []})


def test_rule_length_bounds() -> None:
    with pytest.raises(ValidationError):
        InstructionsOut.model_validate({"filters": {}, "rules": [""]})
    with pytest.raises(ValidationError):
        InstructionsOut.model_validate({"filters": {}, "rules": ["r" * 201]})
    ok = InstructionsOut.model_validate({"filters": {}, "rules": ["r" * 200] * 10})
    assert len(ok.rules) == 10


_RECIPE_FIELDS = {"title": ".//a/text()", "url": ".//a/@href"}
_RECIPE: dict[str, object] = {
    "version": 1,
    "format": "html",
    "url": "https://jobs.example.test/list",
    "items": "//li[@class='job']",
    "fields": _RECIPE_FIELDS,
}


def test_recipe_out_accepts_minimal_and_full_shapes() -> None:
    minimal = RecipeOut.model_validate(_RECIPE)
    full = RecipeOut.model_validate(
        _RECIPE
        | {
            "salary_defaults": {"currency": "USD", "period": "year"},
            "next_page": "//a[@rel='next']/@href",
            "detail_description": "//div[@id='body']",
            "fields": _RECIPE_FIELDS | {"company": ".//b/text()"},
        }
    )

    assert minimal.salary_defaults is None
    assert full.fields.company == ".//b/text()"
    assert full.salary_defaults is not None
    assert full.salary_defaults.period == "year"


def test_recipe_out_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        RecipeOut.model_validate(_RECIPE | {"extra": 1})
    with pytest.raises(ValidationError):
        RecipeOut.model_validate(
            _RECIPE | {"fields": {"title": "a", "url": "b", "x": "c"}}
        )


def test_recipe_out_fields_need_title_and_url() -> None:
    with pytest.raises(ValidationError):
        RecipeOut.model_validate(_RECIPE | {"fields": {"title": "a"}})


def test_recipe_out_version_must_be_one() -> None:
    with pytest.raises(ValidationError):
        RecipeOut.model_validate(_RECIPE | {"version": 2})


def test_recipe_out_format_is_html_or_json() -> None:
    with pytest.raises(ValidationError):
        RecipeOut.model_validate(_RECIPE | {"format": "xml"})


def test_source_recipe_out_needs_exactly_one() -> None:
    recipe = RecipeOut.model_validate(_RECIPE)

    with pytest.raises(ValidationError):
        SourceRecipeOut(recipe=recipe, json_url="https://jobs.example.test/api")
    with pytest.raises(ValidationError):
        SourceRecipeOut()
    assert SourceRecipeOut(recipe=recipe).json_url is None
    assert SourceRecipeOut(json_url="https://jobs.example.test/api").recipe is None

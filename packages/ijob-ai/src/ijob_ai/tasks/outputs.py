"""The shapes a model must answer in for each assessment task.

Each model carries the same bounds as the core type it becomes
(`ResumeProfile`, `Instructions`, `Verdict`, `Score`), so an answer outside
those bounds fails pydantic validation instead of reaching the core type. The
models forbid unknown keys, so a filter outside the config file's `[filters]`
table is rejected.
"""

from typing import Annotated, Literal

from ijob_core.model import Seniority
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_ITEM = Annotated[str, StringConstraints(min_length=1, max_length=100)]
_REASON = Annotated[str, StringConstraints(min_length=1, max_length=200)]
_RULE = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class _Strict(BaseModel):
    """Base for every output model: unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


class ProfileOut(_Strict):
    """The facts read from a CV.

    Attributes:
        summary: A short summary of the candidate, at most 600 characters.
        years_experience: Years of professional experience, 0 to 60, when known.
        seniority: The level the candidate works at.
        roles: The roles the candidate fills, at most 20.
        skills: The candidate's skills, at most 100.
        spoken_languages: The languages the candidate speaks, at most 20.
        locations: Where the candidate is or will work from, at most 20.
        work_authorization: Where the candidate may work, at most 20.
    """

    summary: Annotated[str, Field(max_length=600)]
    years_experience: Annotated[int, Field(ge=0, le=60)] | None
    seniority: Seniority
    roles: Annotated[list[_ITEM], Field(max_length=20)]
    skills: Annotated[list[_ITEM], Field(max_length=100)]
    spoken_languages: Annotated[list[_ITEM], Field(max_length=20)]
    locations: Annotated[list[_ITEM], Field(max_length=20)]
    work_authorization: Annotated[list[_ITEM], Field(max_length=20)]


class MinSalaryOut(_Strict):
    """The `[filters.min_salary]` table.

    Attributes:
        amount: The smallest acceptable pay.
        currency: The currency code of `amount`.
        period: The span `amount` is paid for.
    """

    amount: float
    currency: str
    period: Literal["hour", "month", "year"]


class RoleOut(_Strict):
    """The `[filters.role]` table, as plain words.

    Attributes:
        include: Words a title must contain.
        exclude: Words a title must not contain.
    """

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class FiltersOut(_Strict):
    """The `[filters]` table of the config file, limited to its keys.

    Attributes:
        countries: Country codes or names.
        remote: Remote scopes: none, country, region or worldwide.
        since: An age limit as `<n>d` or `<n>h`, when set.
        companies: Case-insensitive company substrings.
        min_salary: The smallest acceptable pay, when set.
        require_salary: Whether a posting must state its pay.
        role: Words for the job title.
    """

    countries: list[str] = Field(default_factory=list)
    remote: list[str] = Field(default_factory=list)
    since: str | None = None
    companies: list[str] = Field(default_factory=list)
    min_salary: MinSalaryOut | None = None
    require_salary: bool = False
    role: RoleOut = Field(default_factory=RoleOut)


class InstructionsOut(_Strict):
    """What the user's instructions mean.

    Attributes:
        filters: The filters the instructions imply.
        rules: Up to 10 judge rules, each 1 to 200 characters.
    """

    filters: FiltersOut
    rules: Annotated[list[_RULE], Field(max_length=10)]


class VerdictOut(_Strict):
    """Whether the instructions keep a posting, and why.

    Attributes:
        keep: True if the posting passes the instructions.
        reason: Why, 1 to 200 characters.
        rule: The 1-based number of the rule that rejected the posting; None
            when `keep` is true.
    """

    keep: bool
    reason: _REASON
    rule: Annotated[int, Field(ge=1)] | None


class ScoreOut(_Strict):
    """How well a posting fits the CV.

    Attributes:
        score: The fit, an integer from 0 to 100.
        reasons: 1 to 5 reasons, each 1 to 200 characters.
    """

    score: Annotated[int, Field(ge=0, le=100)]
    reasons: Annotated[list[_REASON], Field(min_length=1, max_length=5)]

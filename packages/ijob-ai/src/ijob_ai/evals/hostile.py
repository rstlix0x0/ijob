"""The hostile pairs: made-up postings, each clean and with injected text.

The data is committed in `data/hostile.json` and holds no personal text: one set
of made-up rules, one made-up profile, five judge pairs and five scorer pairs. A
pair is one posting and the same posting with an instruction to the model added
after its description.
"""

import json
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Final

from ijob_core.model import ResumeProfile, Seniority

_DATA_FILE: Final = "hostile.json"


@dataclass(frozen=True, slots=True)
class HostilePair:
    """One posting, clean and with injected text.

    Attributes:
        name: What the injection tries, unique within its task.
        title: The posting's title.
        company: The posting's company.
        location: The posting's location.
        description: The clean description.
        injection: The injected text.
    """

    name: str
    title: str
    company: str
    location: str
    description: str
    injection: str

    @property
    def hostile_description(self) -> str:
        """The clean description with the injected text after it."""
        return f"{self.description}\n\n{self.injection}"


@dataclass(frozen=True, slots=True)
class HostileData:
    """Everything the hostile suite runs on.

    Attributes:
        rules: The made-up rules the judge pairs are judged under.
        profile: The made-up profile the scorer pairs are scored against.
        judge: The judge pairs.
        scorer: The scorer pairs.
    """

    rules: tuple[str, ...]
    profile: ResumeProfile
    judge: tuple[HostilePair, ...]
    scorer: tuple[HostilePair, ...]


def _pairs(raw: list[dict[str, str]]) -> tuple[HostilePair, ...]:
    """Build pairs from their JSON objects."""
    return tuple(HostilePair(**item) for item in raw)


@cache
def load_hostile() -> HostileData:
    """Read the hostile data from the package.

    Returns:
        The data; the same object on every call.

    Raises:
        OSError: The data file cannot be read.
        ValueError: The file is not valid JSON or holds a value the types refuse.
        KeyError: The file lacks a field.
        TypeError: A pair holds a field the type does not have.
    """
    text = (files("ijob_ai.evals") / "data" / _DATA_FILE).read_text(encoding="utf-8")
    raw = json.loads(text)
    profile = raw["profile"]
    return HostileData(
        rules=tuple(raw["rules"]),
        profile=ResumeProfile(
            summary=profile["summary"],
            years_experience=profile["years_experience"],
            seniority=Seniority(profile["seniority"]),
            roles=tuple(profile["roles"]),
            spoken_languages=tuple(profile["spoken_languages"]),
            locations=tuple(profile["locations"]),
            work_authorization=tuple(profile["work_authorization"]),
            skills=tuple(profile["skills"]),
        ),
        judge=_pairs(raw["judge"]),
        scorer=_pairs(raw["scorer"]),
    )

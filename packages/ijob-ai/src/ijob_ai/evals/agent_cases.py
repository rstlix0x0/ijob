"""The agent eval cases: explore instructions and hostile careers pages.

The data is committed under `data/` and holds made-up text only. The explore
scenarios are five instructions in `explore_scenarios.json`. The hostile pages
are three HTML files under `hostile_pages/`, listed in `manifest.json` with each
page's company, its real board and the targets its injected text points at.
"""

import json
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Final

_SCENARIOS_FILE: Final = "explore_scenarios.json"
_PAGES_DIR: Final = "hostile_pages"
_MANIFEST_FILE: Final = "manifest.json"


@dataclass(frozen=True, slots=True)
class Scenario:
    """One instruction for the explore agent.

    Attributes:
        name: What the instruction tests, unique among the scenarios.
        instruction: The instruction text.
    """

    name: str
    instruction: str


@dataclass(frozen=True, slots=True)
class HostilePage:
    """One careers page whose links and text carry injected instructions.

    Attributes:
        name: What the injection tries, unique among the pages.
        url: The address the page is served at in the case.
        company: The made-up company the page belongs to.
        board: The source the page really links to, as `kind:key`.
        board_url: The link to that board on the page.
        board_api: The address that board's jobs are read from.
        board_body: What that address answers, so the real board checks out.
        injected_targets: The addresses the injected text sends the agent to;
            a tool call on any of them is a violation.
        html: The page's HTML.
    """

    name: str
    url: str
    company: str
    board: str
    board_url: str
    board_api: str
    board_body: str
    injected_targets: tuple[str, ...]
    html: str


@cache
def load_scenarios() -> tuple[Scenario, ...]:
    """Read the explore scenarios from the package.

    Returns:
        The scenarios in file order; the same object on every call.

    Raises:
        OSError: The data file cannot be read.
        ValueError: The file is not valid JSON.
        KeyError: A scenario lacks a field.
        TypeError: A scenario holds a field the type does not have.
    """
    text = (files("ijob_ai.evals") / "data" / _SCENARIOS_FILE).read_text(
        encoding="utf-8"
    )
    return tuple(Scenario(**item) for item in json.loads(text))


@cache
def load_hostile_pages() -> tuple[HostilePage, ...]:
    """Read the hostile pages and their manifest from the package.

    Returns:
        The pages in manifest order; the same object on every call.

    Raises:
        OSError: The manifest or a page cannot be read.
        ValueError: The manifest is not valid JSON.
        KeyError: An entry lacks a field.
    """
    root = files("ijob_ai.evals") / "data" / _PAGES_DIR
    manifest = json.loads((root / _MANIFEST_FILE).read_text(encoding="utf-8"))
    return tuple(
        HostilePage(
            name=item["name"],
            url=item["url"],
            company=item["company"],
            board=item["board"],
            board_url=item["board_url"],
            board_api=item["board_api"],
            board_body=(root / item["board_file"]).read_text(encoding="utf-8"),
            injected_targets=tuple(item["injected_targets"]),
            html=(root / item["file"]).read_text(encoding="utf-8"),
        )
        for item in manifest
    )

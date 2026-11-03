"""The modal that changes the filters in force."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import ClassVar, override

from ijob_core.errors import InvalidValueError
from ijob_core.model import SourceRef
from ijob_core.store import SourceRow
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Static

from ijob_app.config import (
    FilterFlags,
    parse_country_flag,
    parse_min_salary,
    parse_remote,
    parse_since,
)
from ijob_app.view import AMBER, BORDER, CORAL, GROUND, MUTED, SELECTED


def _split(text: str) -> tuple[str, ...]:
    """Split a comma-separated field into its trimmed values."""
    return tuple(part.strip() for part in text.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class FilterChoice:
    """What the user chose on the filter screen.

    Attributes:
        flags: The filters.
        source: The words that choose the sources; empty means every source.
    """

    flags: FilterFlags
    source: str = ""


def matching_sources(text: str, rows: Iterable[SourceRow]) -> list[SourceRef]:
    """Find the sources a person means: any words of a reference or a company.

    Args:
        text: Comma-separated words, such as `greenhouse` or `stripe, lever:acme`.
        rows: The sources to choose from.

    Returns:
        Each source whose reference or company holds any of the words.
    """
    words = [word.casefold() for word in _split(text)]
    return [
        row.ref
        for row in rows
        if any(
            word in str(row.ref).casefold() or word in row.company.casefold()
            for word in words
        )
    ]


class FilterScreen(ModalScreen[FilterChoice | None]):
    """Ask for the filters: enter applies them, escape keeps the ones in force.

    A value that cannot be used shows why, and the screen stays open.
    """

    DEFAULT_CSS = f"""
    FilterScreen {{ align: center middle; background: {GROUND} 60%; }}
    FilterScreen > Vertical {{
        width: 64;
        height: auto;
        border: round {AMBER};
        border-title-color: {AMBER};
        background: {GROUND};
        padding: 1 2;
    }}
    FilterScreen .field {{ height: 1; margin-bottom: 1; }}
    FilterScreen .name {{ width: 13; color: {MUTED}; }}
    FilterScreen Input {{ width: 1fr; background: {SELECTED}; }}
    FilterScreen Input:focus {{ background: {BORDER}; }}
    FilterScreen Checkbox {{ background: {GROUND}; border: none; margin-bottom: 1; }}
    FilterScreen #filter-error {{ color: {CORAL}; height: auto; }}
    FilterScreen #filter-hint {{ color: {MUTED}; height: 1; }}
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, choice: FilterChoice, sources: Sequence[SourceRow]) -> None:
        """Build the modal.

        Args:
            choice: The filters in force, which it starts from.
            sources: The sources that can be chosen.
        """
        super().__init__()
        self._flags = choice.flags
        self._source = choice.source
        self._sources = sources

    @override
    def compose(self) -> ComposeResult:
        """Lay out one field per filter."""
        flags = self._flags
        fields = (
            ("country", "country", ", ".join(flags.countries), "SG, Japan"),
            ("remote", "remote", ", ".join(flags.remotes), "worldwide, region"),
            ("since", "posted within", flags.since or "", "14d"),
            ("company", "company", ", ".join(flags.companies), "Acme, Globex"),
            ("salary", "pays at least", flags.min_salary or "", "6000:SGD:month"),
            ("source", "source", self._source, "greenhouse, stripe"),
        )
        with Vertical() as box:
            box.border_title = "Filters"
            for key, name, value, hint in fields:
                with Horizontal(classes="field"):
                    yield Static(name, classes="name", markup=False)
                    yield Input(value, placeholder=hint, id=f"filter-{key}")
            yield Checkbox(
                "only jobs that state a salary",
                bool(flags.require_salary),
                id="filter-has-salary",
            )
            yield Static("", id="filter-error", markup=False)
            yield Static("enter applies   escape keeps", id="filter-hint", markup=False)

    def _value(self, key: str) -> str:
        """Read one field."""
        return self.query_one(f"#filter-{key}", Input).value.strip()

    @on(Input.Submitted)
    def _apply(self) -> None:
        """Check the fields, then hand back the filters."""
        flags = FilterFlags(
            titles=self._flags.titles,
            countries=_split(self._value("country")),
            remotes=_split(self._value("remote")),
            since=self._value("since") or None,
            companies=_split(self._value("company")),
            min_salary=self._value("salary") or None,
            require_salary=self.query_one("#filter-has-salary", Checkbox).value,
        )
        source = self._value("source")
        if source and not matching_sources(source, self._sources):
            self.query_one("#filter-error", Static).update(
                f"No source matches {source!r}. The Sources tab lists them."
            )
            return
        try:
            for country in flags.countries:
                parse_country_flag(country)
            for remote in flags.remotes:
                parse_remote(remote)
            if flags.since is not None:
                parse_since(flags.since)
            if flags.min_salary is not None:
                parse_min_salary(flags.min_salary)
        except InvalidValueError as error:
            self.query_one("#filter-error", Static).update(str(error))
            return
        self.dismiss(FilterChoice(flags, source))

    @on(Button.Pressed)
    def action_cancel(self) -> None:
        """Keep the filters in force."""
        self.dismiss(None)

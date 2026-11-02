"""How a posting's values are written for the command line and the terminal UI.

`ijob show` and the TUI detail pane print the same fields of a posting, and
the TUI tables show third-party text; this module holds the pieces they share.
"""

from ijob_core.money import money_text
from rich.text import Text

from ijob_app.service import PostingDetail


def posting_fields(detail: PostingDetail) -> list[tuple[str, str]]:
    """List the fields `show` prints after the title and company, in its order.

    Args:
        detail: The posting with its sources.

    Returns:
        A `(name, value)` pair each for the location, every salary range, the
        country, the remote scope, the posted date and the comma-joined
        sources; `-` stands for a value that is absent. The URL, label and
        description are not included.
    """
    posting = detail.posting
    posted = posting.posted_at.date().isoformat() if posting.posted_at else "-"
    salaries = [money_text(money) for money in posting.salaries] or ["-"]
    return [
        ("location", posting.location or "-"),
        *(("salary", salary) for salary in salaries),
        ("country", str(posting.country or "-")),
        ("remote", str(posting.remote or "-")),
        ("posted", posted),
        ("sources", ",".join(str(ref) for ref in detail.sources)),
    ]


def plain_cell(value: str) -> Text:
    """Wrap text for a table cell so it is shown literally.

    A bare `str` cell is read as Rich markup, which lets third-party text fail
    to render or carry click actions and links; a `Text` is shown as it is.
    """
    return Text(value, no_wrap=True, end="")

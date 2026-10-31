"""Check that a hiring-system board belongs to the company it was registered for."""

import re
from collections.abc import Iterable

from ijob_core.dedupe import normalise_company
from ijob_core.model import SourceRef

from ijob_sources.errors import IdentityMismatchError

_PARENTHESISED = re.compile(r"^(?P<before>[^(]*)\((?P<inside>[^)]*)\)")


def _candidates(company: str) -> list[str]:
    """List the normalised forms of `company` that a board may use.

    A company with a parenthesised part, such as `Anysphere (Cursor)`, also
    yields the part before the parentheses and the part inside them.
    """
    found = [normalise_company(company)]
    parts = _PARENTHESISED.match(company)
    if parts is not None:
        found.append(normalise_company(parts.group("before")))
        found.append(normalise_company(parts.group("inside")))
    return [candidate for candidate in dict.fromkeys(found) if candidate]


def _contains_run(text: str, run: str) -> bool:
    """Report whether `run` occurs in `text` as a run of whole space-separated words."""
    return f" {run} " in f" {text} "


def _name_matches(name: str, candidates: list[str]) -> bool:
    """Report whether a name equals, contains or is contained in a candidate.

    Containment is by whole-word run, so `scale` does not match `tailscale`.
    """
    return bool(name) and any(
        _contains_run(candidate, name) or _contains_run(name, candidate)
        for candidate in candidates
    )


def _text_matches(text: str, candidates: list[str]) -> bool:
    """Report whether a normalised text contains a candidate as a whole-word run."""
    return any(_contains_run(text, candidate) for candidate in candidates)


def check_identity(
    ref: SourceRef,
    company: str,
    *,
    names: Iterable[str],
    texts: Iterable[str],
    count: int,
) -> None:
    """Check that a parsed board names the company it was registered for.

    Every string is normalised with `ijob_core.dedupe.normalise_company`, so a
    company such as `Checkout.com` matches a text that writes `Checkout.com`.
    A company with a parenthesised part also passes on the part before the
    parentheses or the part inside them. The check passes when any name
    equals, contains or is contained in the company as a run of whole words,
    or else when any text contains the company as a whole-word run. `Scale`
    therefore does not match a board that calls itself `Tailscale`.

    Every adapter passes the same things: `count` is the number of jobs the
    board listed, counted before any job is rejected, and `names` and `texts`
    are taken from every listed job whose field is readable, including a job
    that is later rejected for another reason.

    Args:
        ref: The source that was fetched, named in the error.
        company: The company the source was registered for.
        names: Company names the board states, such as a job's company name.
        texts: Descriptions from the board that may name the company.
        count: The number of jobs the board listed; zero always passes.

    Raises:
        IdentityMismatchError: If the board has jobs and neither a name nor a
            text matches the company.
    """
    if count == 0:
        return
    candidates = _candidates(company)
    if any(_name_matches(normalise_company(name), candidates) for name in names):
        return
    if any(_text_matches(normalise_company(text), candidates) for text in texts):
        return
    raise IdentityMismatchError(ref, f"company '{company}' not found on the board")

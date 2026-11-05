"""The rules that sample, label and spot check the jury's cases.

All of it is pure: the draw of a label set from the pools, the split a `--limit`
makes of it, the draw `--add` makes to grow it, the label the jurors' votes give
(or the reason they give none), and the order in which agreed labels are offered
for a spot check. Nothing here reads or writes the store.
"""

import hashlib
import random
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice
from typing import Final

from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import Bucket, EvalTask, EvalVote, Pools
from ijob_core.model import PostingId

_KEPT_DRAWN: Final = 40
_REJECTED_DRAWN: Final = 40
_TOTAL_DRAWN: Final = 100
_LIMIT_MIN: Final = 1
_LIMIT_MAX: Final = 100
_MAX_SPREAD: Final = 20
_SPOT_MIN_PER_TASK: Final = 5
_SPOT_CASES_PER_CHECK: Final = 20


@dataclass(frozen=True, slots=True)
class Split:
    """A count of cases in each pool.

    Attributes:
        kept: Cases from the `kept` pool.
        rejected: Cases from the `rejected` pool.
        unjudged: Cases from the `unjudged` pool.
    """

    kept: int
    rejected: int
    unjudged: int


class ReviewReason(StrEnum):
    """Why the votes on a case give no label and need the user."""

    SPLIT = "split"
    FEWER_VOTES = "fewer votes than jurors"
    SPREAD = "spread"


@dataclass(frozen=True, slots=True)
class Agreed:
    """A label the jurors agree on.

    A judge label sets `keep` (and `rule` when rejecting with a majority rule)
    and no `score`; a scorer label sets `score` alone.

    Attributes:
        keep: Whether the posting is kept, for a judge label.
        rule: The 1-based number of the rejecting rule, if the votes name one.
        score: The fit from 0 to 100, for a scorer label.
    """

    keep: bool | None
    rule: int | None
    score: int | None


@dataclass(frozen=True, slots=True)
class NeedsReview:
    """Votes that give no label and wait for the user.

    Attributes:
        reason: Why they give none.
    """

    reason: ReviewReason


type Outcome = Agreed | NeedsReview


@dataclass(frozen=True, slots=True)
class SpotCandidate:
    """An agreed label that has not been spot checked.

    Attributes:
        posting_id: The posting.
        task: The task the label answers.
    """

    posting_id: PostingId
    task: EvalTask


def draw_cases(
    rules_sha256: str, profile_sha256: str, pools: Pools
) -> list[tuple[PostingId, Bucket]]:
    """Draw the cases of a label set from the pools.

    Draws 40 kept, 40 rejected and 20 unjudged. A pool short of its share is taken
    whole and `unjudged` makes up the difference, up to 100 in all. The same two
    hashes give the same draw; other hashes give another.

    Args:
        rules_sha256: The SHA-256 of the rules.
        profile_sha256: The SHA-256 of the profile.
        pools: The postings in each pool.

    Returns:
        The drawn postings with their pool, kept first, then rejected, then
        unjudged, each in drawn order.
    """
    rng = random.Random(rules_sha256 + profile_sha256)  # noqa: S311  # not a secret
    kept = _sample(rng, pools.kept, _KEPT_DRAWN)
    rejected = _sample(rng, pools.rejected, _REJECTED_DRAWN)
    unjudged = _sample(rng, pools.unjudged, _TOTAL_DRAWN - len(kept) - len(rejected))
    return (
        [(posting_id, Bucket.KEPT) for posting_id in kept]
        + [(posting_id, Bucket.REJECTED) for posting_id in rejected]
        + [(posting_id, Bucket.UNJUDGED) for posting_id in unjudged]
    )


def _sample(
    rng: random.Random, ids: Sequence[PostingId], wanted: int
) -> list[PostingId]:
    """Sample up to `wanted` of `ids`, taken in ascending order first."""
    return rng.sample(sorted(ids), min(wanted, len(ids)))


def limit_split(n: int, available: Split) -> Split:
    """Split a `--limit` of `n` cases over the pools.

    Kept and rejected each take `floor(0.4 * n + 0.5)` and unjudged the rest. A
    pool short of its share gives all it has, and the remainder goes to `unjudged`
    first, then to `kept`, then to `rejected`, each up to what it has. So `n` cases
    are chosen whenever `n` exist.

    Args:
        n: The limit, from 1 to 100.
        available: How many cases each pool has.

    Returns:
        How many cases to take from each pool.

    Raises:
        InvalidValueError: If `n` is outside 1 to 100.
    """
    _require_count("limit", n)
    share = (4 * n + 5) // 10
    return _split(n, share, share, available)


def add_split(n: int, available: Split) -> Split:
    """Split an `--add` of `n` cases over the pools.

    Kept takes `floor(0.8 * n + 0.5)`, rejected `floor(0.1 * n + 0.5)` and
    unjudged the rest. A pool short of its share gives all it has, and the
    remainder goes to `unjudged` first, then to `kept`, then to `rejected`, each up
    to what it has.

    Args:
        n: The number of cases to add, from 1 to 100.
        available: How many cases each pool has left.

    Returns:
        How many cases to take from each pool.

    Raises:
        InvalidValueError: If `n` is outside 1 to 100.
    """
    _require_count("add", n)
    return _split(n, (8 * n + 5) // 10, (n + 5) // 10, available)


def draw_more(
    rules_sha256: str, profile_sha256: str, case_count: int, pools: Pools, n: int
) -> list[tuple[PostingId, Bucket]]:
    """Draw `n` more cases for a label set that holds `case_count`.

    The shares are those of `add_split`. The caller passes pools without the cases
    the set already holds. The same two hashes and case count give the same draw;
    a set of another size gives another.

    Args:
        rules_sha256: The SHA-256 of the rules.
        profile_sha256: The SHA-256 of the profile.
        case_count: How many cases the set holds now.
        pools: The postings still available in each pool.
        n: The number of cases to add, from 1 to 100.

    Returns:
        The drawn postings with their pool, kept first, then rejected, then
        unjudged, each in drawn order.

    Raises:
        InvalidValueError: If `n` is outside 1 to 100.
    """
    split = add_split(
        n, Split(len(pools.kept), len(pools.rejected), len(pools.unjudged))
    )
    rng = random.Random(f"{rules_sha256}{profile_sha256}{case_count}")  # noqa: S311  # not a secret
    return (
        [(i, Bucket.KEPT) for i in _sample(rng, pools.kept, split.kept)]
        + [(i, Bucket.REJECTED) for i in _sample(rng, pools.rejected, split.rejected)]
        + [(i, Bucket.UNJUDGED) for i in _sample(rng, pools.unjudged, split.unjudged)]
    )


def _require_count(field: str, n: int) -> None:
    """Refuse a count outside 1 to 100."""
    if not _LIMIT_MIN <= n <= _LIMIT_MAX:
        raise InvalidValueError(field, f"{n} is outside {_LIMIT_MIN} to {_LIMIT_MAX}")


def _split(n: int, kept_share: int, rejected_share: int, available: Split) -> Split:
    """Take the shares from the pools and give the remainder to the pools with room."""
    kept = min(kept_share, available.kept)
    rejected = min(rejected_share, available.rejected)
    unjudged = min(n - kept - rejected, available.unjudged)
    spare = n - kept - rejected - unjudged
    more_unjudged = min(spare, available.unjudged - unjudged)
    spare -= more_unjudged
    more_kept = min(spare, available.kept - kept)
    spare -= more_kept
    more_rejected = min(spare, available.rejected - rejected)
    return Split(kept + more_kept, rejected + more_rejected, unjudged + more_unjudged)


def rejection_rule(rules: Sequence[int]) -> int | None:
    """Pick the rule that more than half of the rejecting votes name.

    Args:
        rules: The rule number of each rejecting vote.

    Returns:
        That rule, or `None` if no rule has more than half.
    """
    if not rules:
        return None
    rule, count = Counter(rules).most_common(1)[0]
    return rule if count * 2 > len(rules) else None


def _require_task(votes: Sequence[EvalVote], task: EvalTask) -> None:
    """Refuse a vote that answers another task than `task`."""
    for vote in votes:
        if vote.task is not task:
            raise InvalidValueError("task", f"a {vote.task} vote is not a {task} vote")


def judge_label(votes: Sequence[EvalVote], jurors: int) -> Outcome | None:
    """Give the label that judge votes agree on, or why they do not.

    Agreement needs a vote from every juror, all keeping or all rejecting. A
    rejection carries the rule that more than half of the votes name, if any.

    Args:
        votes: The judge votes on one case.
        jurors: How many jurors there are.

    Returns:
        `None` with no votes, `NeedsReview` for fewer votes than jurors or a mix of
        keeps and rejects, else the `Agreed` label.

    Raises:
        InvalidValueError: If a vote is not a judge vote.
    """
    _require_task(votes, EvalTask.JUDGE)
    if not votes:
        return None
    if len(votes) < jurors:
        return NeedsReview(ReviewReason.FEWER_VOTES)
    keeps = [vote.keep for vote in votes]
    if all(keeps):
        return Agreed(keep=True, rule=None, score=None)
    if any(keeps):
        return NeedsReview(ReviewReason.SPLIT)
    rule = rejection_rule([vote.rule for vote in votes if vote.rule is not None])
    return Agreed(keep=False, rule=rule, score=None)


def score_label(votes: Sequence[EvalVote], jurors: int) -> Outcome | None:
    """Give the label that scorer votes agree on, or why they do not.

    Agreement needs a vote from every juror, no more than 20 points apart. The
    label is the lower median of the scores.

    Args:
        votes: The scorer votes on one case.
        jurors: How many jurors there are.

    Returns:
        `None` with no votes, `NeedsReview` for fewer votes than jurors or a spread
        over 20, else the `Agreed` label.

    Raises:
        InvalidValueError: If a vote is not a scorer vote.
    """
    _require_task(votes, EvalTask.SCORER)
    if not votes:
        return None
    if len(votes) < jurors:
        return NeedsReview(ReviewReason.FEWER_VOTES)
    scores = [vote.score for vote in votes if vote.score is not None]
    if max(scores) - min(scores) > _MAX_SPREAD:
        return NeedsReview(ReviewReason.SPREAD)
    return Agreed(keep=None, rule=None, score=statistics.median_low(scores))


def spot_per_task(cases: int) -> int:
    """Return how many labels per task a spot check covers for a set of `cases`.

    Args:
        cases: How many cases the label set holds.

    Returns:
        Five, or one in twenty of the cases when that is more.
    """
    return max(_SPOT_MIN_PER_TASK, cases // _SPOT_CASES_PER_CHECK)


def spot_order(
    seed: str,
    candidates: Iterable[SpotCandidate],
    per_task: int,
    *,
    checked: Iterable[EvalTask] = (),
) -> list[SpotCandidate]:
    """Choose the agreed labels to offer next for a spot check.

    Candidates are ordered by the SHA-256 of seed, posting and task, so adding
    candidates leaves the order of the others as it was. Twice `per_task` labels
    are checked in all, `per_task` for each task when that many exist; `checked`
    counts those already done, and a task short of `per_task` lets the other fill
    the rest.

    Args:
        seed: Text that fixes the order, the same for every call on one label set.
        candidates: The agreed labels without the spot check mark.
        per_task: How many labels of each task the check covers (see
            `spot_per_task`).
        checked: The task of each label already spot checked.

    Returns:
        The labels to offer, in the order to offer them; empty once twice
        `per_task` are checked.
    """
    done = Counter(checked)
    remaining = 2 * per_task - done.total()
    ordered = sorted(candidates, key=lambda c: _spot_key(seed, c))
    chosen: list[SpotCandidate] = []
    for candidate in ordered:
        if len(chosen) < remaining and done[candidate.task] < per_task:
            done[candidate.task] += 1
            chosen.append(candidate)
    if len(chosen) < remaining:
        left = (c for c in ordered if c not in chosen)
        chosen.extend(islice(left, remaining - len(chosen)))
    return sorted(chosen, key=lambda c: _spot_key(seed, c))


def _spot_key(seed: str, candidate: SpotCandidate) -> str:
    """Hash seed, posting and task into the sort key of a candidate."""
    text = f"{seed}\0{candidate.posting_id}\0{candidate.task.value}"
    return hashlib.sha256(text.encode()).hexdigest()

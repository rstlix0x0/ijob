"""The spending of one `ijob eval` command: its cap, its budgets and its run row.

Only OpenRouter spend counts toward the eval caps; a claude-sdk call charges a
separate budget that is never reached and shows as subscription spend. The
command's cap is the smaller of `eval.max_cost_usd` and what is left of
`eval.total_cost_usd` after all OpenRouter spend recorded in earlier and running
eval runs.
"""

import math
from datetime import datetime
from typing import Final

from ijob_ai.errors import LlmError
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.reference import ModelRef, Provider
from ijob_ai.llm.settings import EvalSettings
from ijob_core.errors import InvalidValueError
from ijob_core.eval_records import EvalRecords, EvalRun, RunStatus

_NO_RUN: Final = "begin() has not recorded this command's run"


class EvalBudgetSpentError(LlmError):
    """The recorded eval spending has used up `eval.total_cost_usd`.

    Attributes:
        spent: OpenRouter spend recorded across all eval runs, in US dollars.
        total: The `eval.total_cost_usd` limit, in US dollars.
    """

    def __init__(self, spent: float, total: float) -> None:
        """Build the error from the recorded spend and the limit.

        Args:
            spent: OpenRouter spend recorded across all eval runs.
            total: The `eval.total_cost_usd` limit.
        """
        super().__init__(
            f"eval spending is used up: ${spent:.2f} of ${total:.2f} recorded; "
            "raise eval.total_cost_usd to spend more"
        )
        self.spent = spent
        self.total = total


class EvalSpend:
    """The spending of one eval command, built once per command.

    Build each operation's budget from `cap`, read when the operation starts.
    """

    def __init__(self, records: EvalRecords, settings: EvalSettings) -> None:
        """Work out the command's cap from the settings and the recorded spend.

        Args:
            records: The eval records of the open store.
            settings: The eval caps.

        Raises:
            EvalBudgetSpentError: Recorded OpenRouter spend has reached
                `settings.total_cost_usd`; nothing may be called.
            sqlite3.Error: The records cannot be read.
        """
        spent = records.total_spend()
        left = settings.total_cost_usd - spent
        if left <= 0:
            raise EvalBudgetSpentError(spent, settings.total_cost_usd)
        self._limit = min(settings.max_cost_usd, left)
        self._records = records
        self._run: EvalRun | None = None
        self._operations: list[Budget] = []
        self._subscription = Budget(math.inf)

    @property
    def cap(self) -> float:
        """What the command may still spend on OpenRouter, in US dollars.

        The smaller of the two eval limits at construction, less what this
        command has spent since, and never below zero. Read it again for each
        operation so that all operations together stay inside the limit.
        """
        return max(0.0, self._limit - self.openrouter_spent)

    @property
    def openrouter_spent(self) -> float:
        """OpenRouter spend of this command so far, in US dollars."""
        return sum(operation.spent for operation in self._operations)

    @property
    def subscription_spent(self) -> float:
        """claude-sdk spend of this command so far, in US dollars."""
        return self._subscription.spent

    def budget_for(self, ref: ModelRef, operation: Budget) -> Budget:
        """Return the budget a call to `ref` charges.

        Args:
            ref: The model about to be called.
            operation: The budget `run_items` gave the worker, capped at `cap`.

        Returns:
            `operation` for an OpenRouter model, and the command's own uncapped
            budget for a claude-sdk model.

        Raises:
            InvalidValueError: `operation` is capped above the command's limit, as
                when the operation was started without `cap=`.
        """
        if operation.cap > self._limit:
            raise InvalidValueError(
                "operation.cap",
                f"{operation.cap} is above the command's limit of {self._limit}",
            )
        if ref.provider is Provider.CLAUDE_SDK:
            return self._subscription
        if not any(operation is known for known in self._operations):
            self._operations.append(operation)
        return operation

    def begin(  # noqa: PLR0913  # the run's identifying fields
        self,
        suite: str,
        model: str,
        task_version: str,
        set_id: int | None,
        *,
        started_at: datetime,
        limited: bool = False,
    ) -> EvalRun:
        """Write the command's run row, `running`, before any call.

        Args:
            suite: The suite being run.
            model: The model under test.
            task_version: The task version tested.
            set_id: The label set used, or `None` for a suite without labels.
            started_at: When the command began, timezone-aware.
            limited: Whether the run covers only part of its cases.

        Returns:
            The new run.

        Raises:
            RuntimeError: The command already has a run.
            InvalidValueError: `started_at` has no timezone.
            sqlite3.Error: The write fails.
        """
        if self._run is not None:
            msg = "begin() already recorded this command's run"
            raise RuntimeError(msg)
        self._run = self._records.start_run(
            suite, model, task_version, set_id, started_at=started_at, limited=limited
        )
        return self._run

    def record(self) -> None:
        """Write the spend so far to the run row; call it after each item.

        Raises:
            RuntimeError: `begin` has not been called.
            sqlite3.Error: The write fails.
        """
        if self._run is None:
            raise RuntimeError(_NO_RUN)
        self._records.record_spend(
            self._run.id,
            spent_usd=self.openrouter_spent,
            subscription_usd=self.subscription_spent,
        )

    def finish(self, status: RunStatus, metrics: str, *, finished_at: datetime) -> None:
        """Write the spend so far, then close the run row with its status and metrics.

        Args:
            status: `complete` or `incomplete`.
            metrics: The run's metrics as JSON text.
            finished_at: When the command ended, timezone-aware.

        Raises:
            RuntimeError: `begin` has not been called.
            InvalidValueError: `status` is `running` or `finished_at` has no
                timezone.
            sqlite3.Error: The write fails.
        """
        if self._run is None:
            raise RuntimeError(_NO_RUN)
        self.record()
        self._records.finish_run(self._run.id, status, metrics, finished_at=finished_at)

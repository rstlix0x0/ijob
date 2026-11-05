"""Tune one task on one model with one optimizer, under the rails.

`tune_program` runs the compile on a worker thread, and the rails send each model
call back to the running loop. A run is judged by what its guard recorded, never
by what the compile returned, because an optimizer that was refused every call
can still return a program. This module knows nothing about where examples come from
or where a program is kept, and every model comes from the caller's request.
"""

import asyncio
import json
import logging
import os
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TextIO, override

import dspy
from dspy.utils.exceptions import AdapterParseError

from ijob_ai.errors import (
    CapReachedError,
    CostUnknownError,
    ProviderUnavailableError,
)
from ijob_ai.llm.call import ForgivingJSONAdapter
from ijob_ai.llm.railed import RailedLM, RailGuard
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.programs import with_untrusted_statement
from ijob_ai.tasks.signatures import (
    JUDGE_UNTRUSTED,
    SCORER_UNTRUSTED,
    JudgeSig,
    ScoreSig,
)
from ijob_ai.tuning.metrics import float_metric
from ijob_ai.tuning.optimizers import MAX_ERRORS, Optimizer, build_optimizer

_TQDM_DISABLE: Final = "TQDM_DISABLE"
_FAILED_SHARE_DIVISOR: Final = 4
_MAX_ERROR_MESSAGE: Final = 200


class TuneStatus(StrEnum):
    """How a tuning run ended; only a complete run may give a program."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class _TaskForm:
    """What tuning needs to know about a task.

    Attributes:
        signature: The task's signature.
        untrusted: The sentence a tuned instruction text must hold.
    """

    signature: type[dspy.Signature]
    untrusted: str


_FORMS: Final[Mapping[Task, _TaskForm]] = {
    Task.JUDGE: _TaskForm(JudgeSig, JUDGE_UNTRUSTED),
    Task.SCORER: _TaskForm(ScoreSig, SCORER_UNTRUSTED),
}


def _on_guard(lm: object, guard: RailGuard) -> bool:
    """Say whether `lm` is a rail counting against `guard`."""
    return isinstance(lm, RailedLM) and lm.guard is guard


def _all_labelled(examples: Sequence[Any]) -> bool:
    """Say whether every example has the `label` the metrics read."""
    return all("label" in example for example in examples)


@dataclass(frozen=True, slots=True, kw_only=True)
class TuneRequest:
    """One task, one model and one optimizer to tune.

    Both rails must share `guard` and be built on the loop `tune_program` runs on.
    A guard serves one run; a stopped one is refused.

    Attributes:
        task: `Task.JUDGE` or `Task.SCORER`.
        optimizer: The optimizer to run.
        task_lm: The rail around the model being tuned.
        tuner_lm: The rail around the model that reflects or proposes, or None
            for Bootstrap, which uses none.
        guard: The guard both rails share. Its `on_call` runs on the loop after
            each call, which is where a caller draws progress on stderr. To end a
            run early, cancel it. Stopping the guard directly also refuses every
            later call, and the outcome then reads as interrupted.
        trainset: The examples to learn from, with their inputs marked and a
            `label` field. No other posting reaches the run.
        valset: The examples to choose between candidates on, in the same form.
        num_threads: How many examples run at once.
    """

    task: Task
    optimizer: Optimizer
    task_lm: RailedLM
    tuner_lm: RailedLM | None
    guard: RailGuard
    trainset: Sequence[Any]
    valset: Sequence[Any]
    num_threads: int

    def __post_init__(self) -> None:
        """Refuse a request that could not run.

        Raises:
            ValueError: If the task cannot be tuned, the optimizer needs a tuner
                and has none, a set is empty, `num_threads` is below 1, a model is
                not a rail on `guard`, or an example has no `label`.
        """
        problem = None
        if self.task not in _FORMS:
            problem = f"{self.task.value} cannot be tuned"
        elif self.tuner_lm is None and self.optimizer is not Optimizer.BOOTSTRAP:
            problem = f"{self.optimizer.value} needs a tuner model"
        elif not self.trainset:
            problem = "the train set is empty"
        elif not self.valset:
            problem = "the val set is empty"
        elif self.num_threads < 1:
            problem = f"num_threads must be at least 1, got {self.num_threads}"
        elif not _on_guard(self.task_lm, self.guard):
            problem = "task_lm must be a RailedLM on the request's guard"
        elif self.tuner_lm is not None and not _on_guard(self.tuner_lm, self.guard):
            problem = "tuner_lm must be a RailedLM on the request's guard"
        elif not _all_labelled(self.trainset):
            problem = "every train example needs a label"
        elif not _all_labelled(self.valset):
            problem = "every val example needs a label"
        if problem is not None:
            raise ValueError(problem)


@dataclass(frozen=True, slots=True)
class ProgramText:
    """What a tuned program keeps.

    Attributes:
        instructions: The instruction text, holding the task's untrusted-data
            sentence.
        demos_json: The demos as a JSON list of objects.
    """

    instructions: str
    demos_json: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TuneOutcome:
    """The result of one tuning run.

    Attributes:
        status: Whether the run may be trusted.
        program: The tuned program, only on a complete run that changed it.
        best_val: The mean score of `program` on the val set, set exactly when
            `program` is.
        calls: Model calls let through, val pass and tuner included.
        failed: Calls that failed, timeouts included.
        unusable: Replies no adapter form could read; `failed` does not count
            them. GEPA reads its reflections with its own adapter, so an unreadable
            reflection is not counted here.
        first_error: The text of the first failure counted in `failed`, or None.
        cap_reached: Whether a call was refused because a budget had no room.
        interrupted: Whether a cancel, a Ctrl-C or a direct stop of the guard
            reached the run.
        fatal: The error that ended the run and must stop any run after it, or
            None.
        compile_error: The type of what the compile or the val pass raised, with
            its message cut short; or None. The message of an `AdapterParseError`
            is left out, since it repeats the model's reply. A refused call that
            an interrupt caused is not recorded here, since it is not a cause.
    """

    status: TuneStatus
    program: ProgramText | None
    best_val: float | None
    calls: int
    failed: int
    unusable: int
    first_error: str | None
    cap_reached: bool
    interrupted: bool
    fatal: CostUnknownError | ProviderUnavailableError | None
    compile_error: str | None

    @property
    def no_change(self) -> bool:
        """Whether the run was sound and left the base program as it was."""
        return self.status is TuneStatus.COMPLETE and self.program is None


class CountingJSONAdapter(ForgivingJSONAdapter):
    """The forgiving adapter, counting the replies it cannot read.

    An unreadable reply fails above the rail, so the optimizer scores it 0 and the
    guard never sees it; this count is the only record of it. The optimizers call
    the adapter from many threads.
    """

    def __init__(self) -> None:
        """Create an adapter with nothing counted."""
        super().__init__()
        self._lock = threading.Lock()
        self._unusable = 0

    @property
    def unusable(self) -> int:
        """How many replies could not be read."""
        with self._lock:
            return self._unusable

    @override
    def __call__(
        self,
        lm: Any,
        lm_kwargs: dict[str, Any],
        signature: Any,
        demos: list[dict[str, Any]],
        inputs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Run the adapter, counting a reply that is empty or cannot be parsed.

        Raises:
            AdapterParseError: The reply could not be read.
        """
        try:
            outputs: list[dict[str, Any]] = super().__call__(
                lm, lm_kwargs, signature, demos, inputs
            )
        except AdapterParseError:
            with self._lock:
                self._unusable += 1
            raise
        return outputs


@dataclass(frozen=True, slots=True)
class _WorkResult:
    """What the worker hands back.

    Attributes:
        program: The changed program, when a val pass scored it.
        best_val: Its mean score on the val set.
        compile_error: The text of what was raised, or None.
        refused: Whether what was raised was a call the rails refused.
        stopped: Whether the guard was stopped when the worker finished, so the
            worker may have had calls refused or a val pass skipped. A run left
            alone sets the flag only after its worker has ended, so this stays
            False for it.
    """

    program: ProgramText | None = None
    best_val: float | None = None
    compile_error: str | None = None
    refused: bool = False
    stopped: bool = False


class _LoopOnlyStderr:
    """Stderr that passes the loop thread's writes and drops every other thread's.

    A caller draws progress from the loop while optimizers draw their bars from
    worker threads, so only the thread tells the two apart.
    """

    def __init__(self, real: TextIO, loop_thread: int) -> None:
        """Wrap a stream.

        Args:
            real: The stream the loop thread writes to.
            loop_thread: The identifier of the loop's thread.
        """
        self._real = real
        self._loop_thread = loop_thread

    def write(self, text: str) -> int:
        """Write `text` when called on the loop thread; either way report it written."""
        if threading.get_ident() == self._loop_thread:
            return self._real.write(text)
        return len(text)

    def flush(self) -> None:
        """Flush the wrapped stream."""
        self._real.flush()

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401  # whatever the wrapped stream holds
        """Answer as the wrapped stream does, for `isatty`, `encoding` and the like."""
        return getattr(self._real, name)


@contextmanager
def _quiet_output(loop_thread: int) -> Iterator[None]:
    """Keep optimizer output off the terminal, since DSPy logs items with their text.

    Stdout, stderr, `TQDM_DISABLE` and the logging switch are process-wide, and all
    are restored however the body ends. tqdm reads `TQDM_DISABLE` when first
    imported, which `import dspy` has already done, so the variable reaches only
    what starts while it is set; the bars in this process end at `_LoopOnlyStderr`.

    Args:
        loop_thread: The identifier of the thread whose stderr writes still show.
    """
    tqdm_before = os.environ.get(_TQDM_DISABLE)
    logging_before = logging.root.manager.disable
    os.environ[_TQDM_DISABLE] = "1"
    logging.disable(logging.CRITICAL)
    try:
        with (
            Path(os.devnull).open("w", encoding="utf-8") as sink,
            redirect_stdout(sink),
            redirect_stderr(_LoopOnlyStderr(sys.stderr, loop_thread)),
        ):
            yield
    finally:
        logging.disable(logging_before)
        if tqdm_before is None:
            del os.environ[_TQDM_DISABLE]
        else:
            os.environ[_TQDM_DISABLE] = tqdm_before


def _over_a_quarter_failed(guard: RailGuard) -> bool:
    """Say whether more than a quarter of the guard's calls failed."""
    return guard.failed * _FAILED_SHARE_DIVISOR > guard.calls


async def _may_score_on_val(guard: RailGuard) -> bool:
    """Say on the loop, where the guard lives, whether a val pass is worth its cost."""
    return not (
        guard.stopped
        or guard.cap_reached
        or guard.fatal is not None
        or _over_a_quarter_failed(guard)
    )


async def _is_stopped(guard: RailGuard) -> bool:
    """Read the guard's flag on the loop, where the guard lives."""
    return guard.stopped


def _program_text(compiled: Any, form: _TaskForm, base: str) -> ProgramText | None:  # noqa: ANN401  # dspy programs are untyped
    """Read a compiled program, keeping only its instructions and demos.

    Args:
        compiled: What the optimizer returned; it has one predictor.
        form: The task's form.
        base: The instruction text the compile started from.

    Returns:
        The program, or None when its instructions are `base` and it has no demos.
    """
    predictor = compiled.predictors()[0]
    instructions = with_untrusted_statement(
        predictor.signature.instructions, form.untrusted
    )
    demos = predictor.dump_state()["demos"]
    if instructions == base and not demos:
        return None
    return ProgramText(instructions, json.dumps(demos, sort_keys=True))


def _score_on_val(program: ProgramText, request: TuneRequest, form: _TaskForm) -> float:
    """Score the program as it will be saved on the val set, under the ambient rail."""
    predict = dspy.Predict(form.signature.with_instructions(program.instructions))
    predict.demos = json.loads(program.demos_json)
    result = dspy.Evaluate(
        devset=list(request.valset),
        metric=float_metric(request.task),
        num_threads=request.num_threads,
        max_errors=MAX_ERRORS,
    )(predict)
    scores = [float(score) for *_, score in result.results]
    return sum(scores) / len(scores)


def _compile_and_score(
    request: TuneRequest, loop: asyncio.AbstractEventLoop
) -> _WorkResult:
    """Compile under the ambient rail and adapter, then score a changed program."""
    form = _FORMS[request.task]
    student = dspy.Predict(form.signature)
    built = build_optimizer(
        request.optimizer,
        request.task,
        tuner_lm=request.tuner_lm,
        num_threads=request.num_threads,
        task_lm=request.task_lm,
    )
    compiled = built.compile(
        student, trainset=list(request.trainset), valset=list(request.valset)
    )
    program = _program_text(compiled, form, student.signature.instructions)
    if program is None:
        return _WorkResult()
    sound = asyncio.run_coroutine_threadsafe(
        _may_score_on_val(request.guard), loop
    ).result()
    if not sound:
        return _WorkResult()
    return _WorkResult(program, _score_on_val(program, request, form))


def _error_text(error: Exception) -> str:
    """Name an error, with as much of its message as may be shown or stored."""
    name = type(error).__name__
    if isinstance(error, AdapterParseError):
        return name
    return f"{name}: {str(error)[:_MAX_ERROR_MESSAGE]}"


def _work(
    request: TuneRequest,
    adapter: CountingJSONAdapter,
    loop: asyncio.AbstractEventLoop,
    loop_thread: int,
) -> _WorkResult:
    """Run the compile and the val pass quietly; this is the worker thread's body.

    Anything they raise is recorded, not raised: optimizers raise bare `Exception`,
    so nothing narrower holds.
    """
    with (
        _quiet_output(loop_thread),
        dspy.context(lm=request.task_lm, adapter=adapter),
    ):
        try:
            result = _compile_and_score(request, loop)
        except Exception as error:  # noqa: BLE001  # recorded on the outcome, which marks the run incomplete
            result = _WorkResult(
                compile_error=_error_text(error),
                refused=isinstance(error, CapReachedError),
            )
        stopped = asyncio.run_coroutine_threadsafe(
            _is_stopped(request.guard), loop
        ).result()
    return replace(result, stopped=stopped)


async def _wait_for_worker(worker: asyncio.Task[_WorkResult], guard: RailGuard) -> bool:
    """Wait for the worker to end, stopping the guard whatever the wait's end.

    A thread cannot be killed, so a cancel or Ctrl-C refuses every later call and
    keeps waiting: the worker then drains without spending, and only a call already
    inside the provider finishes.

    Args:
        worker: The task running the worker thread.
        guard: The run's guard.

    Returns:
        Whether a cancel or Ctrl-C arrived. The flag cannot say so, since it is
        set on every run.
    """
    interrupted = False
    while not worker.done():
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            interrupted = True
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
        except KeyboardInterrupt:
            interrupted = True
        finally:
            guard.stop()
    return interrupted


def _status(
    guard: RailGuard, *, interrupted: bool, compile_error: str | None
) -> TuneStatus:
    """Judge a finished run by its guard, its interrupt mark and its compile."""
    unsound = (
        guard.cap_reached
        or interrupted
        or guard.fatal is not None
        or _over_a_quarter_failed(guard)
        or compile_error is not None
    )
    return TuneStatus.INCOMPLETE if unsound else TuneStatus.COMPLETE


async def tune_program(request: TuneRequest) -> TuneOutcome:
    """Tune one program, returning what happened rather than raising it.

    The output of the compile is kept quiet for the whole run, which sets
    process-wide state, so runs must be sequential. A cancel or Ctrl-C reaching
    this function is absorbed: it refuses every later call, waits for the worker,
    and returns an `interrupted` outcome.

    A second Ctrl-C under `asyncio.run` leaves the loop while the run waits, so no
    outcome comes back: `KeyboardInterrupt` reaches the caller of `asyncio.run`,
    who must catch it there and treat it as a stop. Nothing more is spent, the
    worker ends and the output settings are restored by the time it arrives, but a
    provider may still bill a call that was in flight, whose cost cannot be known.
    A second Ctrl-C that lands while the task itself runs, not while it waits, is
    absorbed: the run keeps waiting for the in-flight call, up to its timeout,
    until the next Ctrl-C.

    Args:
        request: What to tune.

    Returns:
        The outcome. It holds a program only when the run was complete and the
        program changed, and then the val pass has scored it.

    Raises:
        ValueError: If the request's guard is already stopped.
    """
    guard = request.guard
    if guard.stopped:
        msg = "the guard has already served a run"
        raise ValueError(msg)
    adapter = CountingJSONAdapter()
    worker = asyncio.create_task(
        asyncio.to_thread(
            _work,
            request,
            adapter,
            asyncio.get_running_loop(),
            threading.get_ident(),
        )
    )
    interrupted = await _wait_for_worker(worker, guard)
    result = worker.result()
    interrupted = interrupted or (result.stopped and guard.fatal is None)
    compile_error = None if interrupted and result.refused else result.compile_error
    status = _status(guard, interrupted=interrupted, compile_error=compile_error)
    keep = status is TuneStatus.COMPLETE
    return TuneOutcome(
        status=status,
        program=result.program if keep else None,
        best_val=result.best_val if keep else None,
        calls=guard.calls,
        failed=guard.failed,
        unusable=adapter.unusable,
        first_error=guard.first_error,
        cap_reached=guard.cap_reached,
        interrupted=interrupted,
        fatal=guard.fatal,
        compile_error=compile_error,
    )

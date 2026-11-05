"""A tuning run for a parent to interrupt with real signals; run with `-m`.

The child's stdout says where the run is, so the parent can time each signal, and
its last line is a JSON report of what the two signals left behind. It writes to
the descriptor because the run replaces `sys.stdout`.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Final, override

import dspy
from dspy.lm15 import Request, Response
from ijob_ai.llm.budget import Budget
from ijob_ai.llm.railed import RailedLM, RailGuard
from ijob_ai.llm.reference import Provider
from ijob_ai.llm.registry import prepare_environment
from ijob_ai.llm.settings import Task
from ijob_ai.tuning.engine import TuneRequest, tune_program
from ijob_ai.tuning.optimizers import Optimizer

from ..llm.fakes import FakeAsyncEngine, FakeEngine
from .test_optimizers import judge_examples

IN_FLIGHT: Final = "in flight"
STOPPED: Final = "stopped"
HOLD_S: Final = 120.0
SETTLE_S: Final = 0.1
TRAIN: Final = 7
EXAMPLES: Final = 11
TQDM: Final = "TQDM_DISABLE"
STDOUT_FD: Final = 1


def say(line: str) -> None:
    os.write(STDOUT_FD, f"{line}\n".encode())


class HeldEngine(FakeAsyncEngine):
    """An engine whose call stays inside the provider until it is cancelled."""

    @override
    async def complete(self, request: Request) -> Response:
        self.requests.append(request)
        say(IN_FLIGHT)
        await asyncio.sleep(HOLD_S)
        msg = "the call was held for longer than the test allows"
        raise RuntimeError(msg)


class TellingGuard(RailGuard):
    """A guard that tells the parent once the loop is past the first stop.

    A signal that lands while the run's own task is on the processor is absorbed
    there, so the parent is told only once that task has gone back to waiting.
    """

    @override
    def stop(self) -> None:
        if not self.stopped:
            asyncio.get_running_loop().call_soon(say, STOPPED)
        super().stop()


def main() -> None:
    # A parent that ignores SIGINT passes that on, and asyncio then installs no handler.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    prepare_environment()
    engine = HeldEngine()
    guard = TellingGuard()
    budget = Budget(1.0)
    stdout, stderr = sys.stdout, sys.stderr

    async def run() -> None:
        rail = RailedLM(
            dspy.LM(
                "claude-sdk/claude-haiku-5-5",
                engine=FakeEngine(),
                async_engine=engine,
                cache=False,
                num_retries=0,
            ),
            provider=Provider.CLAUDE_SDK,
            budgets=(budget,),
            timeout_s=HOLD_S,
            task=Task.JUDGE,
            task_version="test-1",
            trace=None,
            loop=asyncio.get_running_loop(),
            guard=guard,
        )
        examples = judge_examples(EXAMPLES)
        await tune_program(
            TuneRequest(
                task=Task.JUDGE,
                optimizer=Optimizer.BOOTSTRAP,
                task_lm=rail,
                tuner_lm=None,
                guard=guard,
                trainset=examples[:TRAIN],
                valset=examples[TRAIN:],
                num_threads=1,
            )
        )

    left_with = "nothing"
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        left_with = "KeyboardInterrupt"
    requests_at_exit = len(engine.requests)
    time.sleep(SETTLE_S)
    report = {
        "left_with": left_with,
        "requests_at_exit": requests_at_exit,
        "requests_later": len(engine.requests),
        "calls": guard.calls,
        "spent": budget.spent,
        "stopped": guard.stopped,
        "stdout_restored": sys.stdout is stdout,
        "stderr_restored": sys.stderr is stderr,
        "logging_disable": logging.root.manager.disable,
        "tqdm": os.environ.get(TQDM),
        "workers": [
            thread.name
            for thread in threading.enumerate()
            if thread is not threading.main_thread() and not thread.daemon
        ],
    }
    say(json.dumps(report))


if __name__ == "__main__":
    main()

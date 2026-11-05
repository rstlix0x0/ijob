import json
import re
from collections.abc import Callable
from typing import override

import dspy
import pytest
from dspy.lm15 import Request, Response, TextPart
from dspy.teleprompt import MIPROv2
from ijob_ai.llm.call import ForgivingJSONAdapter
from ijob_ai.llm.registry import prepare_environment
from ijob_ai.llm.settings import Task
from ijob_ai.tasks.signatures import JudgeSig, ScoreSig
from ijob_ai.tuning.optimizers import Optimizer, build_optimizer

from ..llm.fakes import FakeAsyncEngine, FakeEngine, Opaque, answer

KEEP_MARK = "KEEPME"
SCORE_MARK = "SCORE="


def request_text(request: Request) -> str:
    return "".join(
        part.text
        for message in request.messages
        for part in message.parts
        if isinstance(part, TextPart)
    )


class ReplyEngine(FakeEngine):
    """A sync engine that answers each request from a function of its text.

    Attributes:
        requests: Every request received, in order.
    """

    def __init__(self, reply: Callable[[str], str]) -> None:
        self._reply = reply
        self.requests: list[Request] = []

    @override
    def complete(self, request: Request) -> Response:
        self.requests.append(request)
        return answer(self._reply(request_text(request)))


class ReplyAsyncEngine(FakeAsyncEngine):
    """The async face of a `ReplyEngine`; it answers from the same function."""

    def __init__(self, engine: ReplyEngine) -> None:
        super().__init__()
        self._engine = engine

    @override
    async def complete(self, request: Request) -> Response:
        return self._engine.complete(request)


def lm_for(engine: ReplyEngine) -> dspy.LM:
    return dspy.LM(
        "claude-sdk/claude-haiku-5-5",
        engine=engine,
        async_engine=ReplyAsyncEngine(engine),
        cache=False,
        num_retries=0,
    )


def posting_of(text: str) -> str:
    """Return the posting line of a prompt that holds a numbered posting."""
    return text.rsplit("[[ ## posting ## ]]", 1)[-1]


def judge_reply(text: str) -> str:
    keep = KEEP_MARK in posting_of(text)
    verdict = {"keep": keep, "reason": "fits", "rule": None if keep else 1}
    return json.dumps({"verdict": verdict})


def half_wrong(text: str) -> str:
    """Answer the judge wrongly for every posting numbered a multiple of three."""
    number = int(posting_of(text).split("posting ")[1].split()[0])
    keep = (number % 2 == 0) == (number % 3 != 0)
    verdict = {"keep": keep, "reason": "fits", "rule": None if keep else 1}
    return json.dumps({"verdict": verdict})


def judge_examples(count: int) -> list[Opaque]:
    examples = []
    for number in range(count):
        keep = number % 2 == 0
        text = f"posting {number} {KEEP_MARK if keep else 'nope'}"
        examples.append(
            dspy.Example(rules="1. no sales", posting=text, label=keep).with_inputs(
                "rules", "posting"
            )
        )
    return examples


def scorer_examples(count: int) -> list[Opaque]:
    return [
        dspy.Example(
            posting=f"posting {number} {SCORE_MARK}{number * 10}",
            profile="{}",
            label=number * 10,
        ).with_inputs("posting", "profile")
        for number in range(count)
    ]


PROPOSAL = "```\nDecide the posting well.\n```"
MIPRO_PROPOSAL = json.dumps(
    dict.fromkeys(
        (
            "observations",
            "summary",
            "program_description",
            "module_description",
            "proposed_instruction",
        ),
        "Decide the posting well.",
    )
)


@pytest.fixture(autouse=True)
def prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LITELLM_MODEL_COST_MAP_URL",
        "OPENROUTER_API_BASE",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    prepare_environment()


def test_bootstrap_settings() -> None:
    judge = build_optimizer(
        Optimizer.BOOTSTRAP, Task.JUDGE, tuner_lm=None, num_threads=3
    )
    scorer = build_optimizer(
        Optimizer.BOOTSTRAP, Task.SCORER, tuner_lm=None, num_threads=3
    )
    for built, threshold in ((judge, 1.0), (scorer, 0.8)):
        inner = built.inner
        assert inner.max_num_samples == 2
        assert inner.max_labeled_demos == 0
        assert inner.num_candidate_sets == 6
        assert inner.max_errors == 1_000_000
        assert inner.metric_threshold == threshold
        assert inner.num_threads == 3


@pytest.mark.parametrize("task", [Task.JUDGE, Task.SCORER])
def test_gepa_settings(task: Task) -> None:
    tuner = lm_for(ReplyEngine(lambda _text: PROPOSAL))
    inner = build_optimizer(Optimizer.GEPA, task, tuner_lm=tuner, num_threads=3).inner
    assert inner.auto == "light"
    assert inner.reflection_lm is tuner
    assert inner.num_threads == 3


@pytest.mark.parametrize(("task", "threshold"), [(Task.JUDGE, 1.0), (Task.SCORER, 0.8)])
def test_mipro_settings(task: Task, threshold: float) -> None:
    tuner = lm_for(ReplyEngine(lambda _text: MIPRO_PROPOSAL))
    task_lm = lm_for(ReplyEngine(half_wrong))
    inner = build_optimizer(
        Optimizer.MIPRO, task, tuner_lm=tuner, num_threads=3, task_lm=task_lm
    ).inner
    assert inner.auto == "light"
    assert inner.prompt_model is tuner
    assert inner.task_model is task_lm
    assert inner.max_bootstrapped_demos == 2
    assert inner.max_labeled_demos == 0
    assert inner.max_errors == 1_000_000
    assert inner.metric_threshold == threshold
    assert inner.num_threads == 3


def test_mipro_needs_the_task_model() -> None:
    tuner = lm_for(ReplyEngine(lambda _text: PROPOSAL))
    with pytest.raises(ValueError, match="task model"):
        build_optimizer(Optimizer.MIPRO, Task.JUDGE, tuner_lm=tuner, num_threads=1)


@pytest.mark.parametrize("optimizer", [Optimizer.GEPA, Optimizer.MIPRO])
def test_gepa_and_mipro_need_a_tuner(optimizer: Optimizer) -> None:
    with pytest.raises(ValueError, match="tuner"):
        build_optimizer(optimizer, Task.JUDGE, tuner_lm=None, num_threads=1)


def compile_judge(
    optimizer: Optimizer, task_reply: Callable[[str], str], tuner: ReplyEngine | None
) -> Opaque:
    task_lm = lm_for(ReplyEngine(task_reply))
    built = build_optimizer(
        optimizer,
        Task.JUDGE,
        tuner_lm=None if tuner is None else lm_for(tuner),
        num_threads=1,
        task_lm=task_lm,
    )
    examples = judge_examples(12)
    with dspy.context(lm=task_lm, adapter=ForgivingJSONAdapter()):
        return built.compile(
            dspy.Predict(JudgeSig), trainset=examples[:8], valset=examples[8:]
        )


def test_gepa_uses_the_tuner_for_reflection() -> None:
    tuner = ReplyEngine(lambda _text: PROPOSAL)
    compile_judge(Optimizer.GEPA, half_wrong, tuner)
    assert tuner.requests


def test_mipro_uses_the_tuner_for_proposals() -> None:
    tuner = ReplyEngine(lambda _text: MIPRO_PROPOSAL)
    compile_judge(Optimizer.MIPRO, half_wrong, tuner)
    assert tuner.requests


def mipro_on(tuner: ReplyEngine) -> Opaque:
    return build_optimizer(
        Optimizer.MIPRO,
        Task.JUDGE,
        tuner_lm=lm_for(tuner),
        num_threads=1,
        task_lm=lm_for(ReplyEngine(half_wrong)),
    )


class EnoughSeenError(Exception):
    """Ends a compile once it has shown what a test came for."""


def test_mipro_keeps_minibatching_off_at_the_largest_val(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The value this returns is the only place DSPy shows its choice.
    real = MIPROv2._set_hyperparams_from_run_mode
    seen: list[bool] = []

    def spy(*args: Opaque, **kwargs: Opaque) -> Opaque:
        settings = real(*args, **kwargs)
        seen.append(settings[2])
        raise EnoughSeenError

    monkeypatch.setattr(MIPROv2, "_set_hyperparams_from_run_mode", spy)
    tuner = ReplyEngine(lambda _text: MIPRO_PROPOSAL)
    examples = judge_examples(58)
    with pytest.raises(EnoughSeenError):
        mipro_on(tuner).compile(
            dspy.Predict(JudgeSig), trainset=examples[:8], valset=examples[8:]
        )
    assert seen == [False]
    assert tuner.requests == []


def test_mipro_refuses_a_val_over_the_limit_before_any_call() -> None:
    tuner = ReplyEngine(lambda _text: MIPRO_PROPOSAL)
    examples = judge_examples(59)
    with pytest.raises(ValueError, match="51"):
        mipro_on(tuner).compile(
            dspy.Predict(JudgeSig), trainset=examples[:8], valset=examples[8:]
        )
    assert tuner.requests == []


def demos_of(program: Opaque) -> list[Opaque]:
    """Return the demos of every candidate the search tried, not only the winner."""
    return [
        demo
        for candidate in program.candidate_programs
        for predictor in candidate["program"].predictors()
        for demo in predictor.demos
    ]


def test_threshold_keeps_a_wrong_judge_answer_out_of_demos() -> None:
    program = compile_judge(Optimizer.BOOTSTRAP, half_wrong, None)
    demos = demos_of(program)
    assert demos
    labels = {example.posting: example.label for example in judge_examples(12)}
    for demo in demos:
        assert demo["verdict"].keep == labels[demo["posting"]]


def scorer_demos(shift: int) -> list[Opaque]:
    """Compile Bootstrap on a scorer that is `shift` points high on every answer."""

    def high_by_shift(text: str) -> str:
        value = int(re.findall(rf"{SCORE_MARK}(\d+)", posting_of(text))[0])
        score = {"score": value + shift, "reasons": ["close"]}
        return json.dumps({"score": score})

    examples = scorer_examples(8)
    built = build_optimizer(
        Optimizer.BOOTSTRAP, Task.SCORER, tuner_lm=None, num_threads=1
    )
    with dspy.context(
        lm=lm_for(ReplyEngine(high_by_shift)), adapter=ForgivingJSONAdapter()
    ):
        program = built.compile(
            dspy.Predict(ScoreSig), trainset=examples[:5], valset=examples[5:]
        )
    return demos_of(program)


def test_threshold_keeps_a_far_scorer_answer_out_of_demos() -> None:
    assert scorer_demos(21) == []


def test_threshold_lets_a_scorer_answer_20_points_off_into_demos() -> None:
    assert scorer_demos(20)

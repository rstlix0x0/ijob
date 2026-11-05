"""Test doubles for the LLM operations: fake engines, a fake CLI and a config writer.

Nothing here reaches a model or the network. The engines answer at the DSPy
boundary with scripted texts, the fake `claude` is a shell script that prints a
version, and the config writer names a made-up model.
"""

import asyncio
import importlib
import json
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from ijob_ai.llm.meter import current_meter
from ijob_ai.llm.reference import ModelRef
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

# DSPy's vendored lm15 ships a py.typed marker, so a static import of it makes
# mypy resolve the whole `dspy` package. Loading it at run time keeps `dspy`
# untyped here, as in the `ijob_ai` test doubles.
_lm15 = importlib.import_module("dspy._vendor.lm15")

type Opaque = Any
"""A value of a vendored lm15 type that mypy does not model."""

MADE_UP_CLAUDE = "claude-sdk/made-up-model"
MADE_UP_OPENROUTER = "openrouter/made-up/model"
SUPPORTED_VERSION = "2.1.300"
VERDICT = '{"keep": true, "reason": "fits"}'
JUNK = "this is not json at all"


_PAGE_SIZE = 612.0, 792.0


def write_cv_pdf(path: Path, text: str = "Backend engineer with seven years") -> Path:
    """Write a one-page PDF with a text layer holding the line `text`.

    Args:
        path: The file to write.
        text: The one line the page shows; no parentheses or backslashes.

    Returns:
        `path`.
    """
    writer = PdfWriter()
    page = writer.add_blank_page(*_PAGE_SIZE)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)  # pypdf has no public call
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        f"BT\n/F1 11 Tf\n1 0 0 1 40 700 Tm\n({text}) Tj\nET".encode("latin-1")
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def profile_answer(summary: str = "Backend engineer") -> str:
    """Return the JSON a model gives for a CV profile."""
    profile: dict[str, object] = {
        "summary": summary,
        "years_experience": 7,
        "seniority": "senior",
        "roles": ["Backend Engineer"],
        "skills": ["Python"],
        "spoken_languages": ["English"],
        "locations": ["Jakarta"],
        "work_authorization": ["Indonesia"],
    }
    return json.dumps({"profile": profile})


def instructions_answer(
    filters: dict[str, object] | None = None, rules: Sequence[str] = ()
) -> str:
    """Return the JSON a model gives for parsed instructions."""
    parsed = {"filters": filters or {}, "rules": list(rules)}
    return json.dumps({"parsed": parsed})


def verdict_answer(
    *, keep: bool = True, reason: str = "fits", rule: int | None = None
) -> str:
    """Return the JSON a model gives for a judge verdict."""
    return json.dumps({"verdict": {"keep": keep, "reason": reason, "rule": rule}})


def score_answer(score: int = 80, reasons: Sequence[str] = ("fits",)) -> str:
    """Return the JSON a model gives for a score."""
    return json.dumps({"score": {"score": score, "reasons": list(reasons)}})


def recipe_answer(recipe: dict[str, object]) -> str:
    """Return the JSON a model gives when it proposes a recipe."""
    return json.dumps({"answer": {"recipe": recipe, "json_url": None}})


def json_url_answer(json_url: str) -> str:
    """Return the JSON a model gives when it points at a JSON document instead."""
    return json.dumps({"answer": {"recipe": None, "json_url": json_url}})


def extract_answer(
    *,
    is_job: bool = True,
    title: str | None = "Backend Engineer",
    company: str | None = "Acme",
    location: str | None = "Remote",
    url: str | None = "https://jobs.example.test/acme/1",
) -> str:
    """Return the JSON a model gives when it reads one skipped item."""
    answer = {
        "is_job": is_job,
        "title": title,
        "company": company,
        "location": location,
        "url": url,
    }
    return json.dumps({"answer": answer})


def request_text(request: Opaque) -> str:
    """Return every text part of a request's messages, joined."""
    return "".join(
        part.text
        for message in request.messages
        for part in message.parts
        if hasattr(part, "text")
    )


def write_llm_config(  # noqa: PLR0913  # one knob of the config per argument
    path: Path,
    *,
    judge: str | None = MADE_UP_CLAUDE,
    max_cost_usd: float | None = None,
    concurrency: int | None = None,
    cli_path: Path | None = None,
    cv_profile: str | None = None,
    instructions: str | None = None,
    scorer: str | None = None,
    source_recipe: str | None = None,
    job_extract: str | None = None,
    tuner: str | None = None,
) -> None:
    """Write a config file that names made-up models.

    Args:
        path: The config file to write; its directory is created.
        judge: The `[models].judge` reference, or None to leave it unset.
        max_cost_usd: The `[llm].max_cost_usd` value, or None to leave it unset.
        concurrency: The `[llm].concurrency` value, or None to leave it unset.
        cli_path: The `[providers.claude-sdk].cli_path` value, or None to leave
            it unset.
        cv_profile: The `[models].cv_profile` reference, or None to leave it unset.
        instructions: The `[models].instructions` reference, or None to leave it
            unset.
        scorer: The `[models].scorer` reference, or None to leave it unset.
        source_recipe: The `[models].source_recipe` reference, or None to leave it
            unset.
        job_extract: The `[models].job_extract` reference, or None to leave it
            unset.
        tuner: The `[models].tuner` reference, or None to leave it unset.
    """
    lines: list[str] = []
    models = {
        "judge": judge,
        "cv_profile": cv_profile,
        "instructions": instructions,
        "scorer": scorer,
        "source_recipe": source_recipe,
        "job_extract": job_extract,
        "tuner": tuner,
    }
    named = [f'{task} = "{ref}"' for task, ref in models.items() if ref is not None]
    if named:
        lines += ["[models]", *named, ""]
    llm: list[str] = []
    if max_cost_usd is not None:
        llm.append(f"max_cost_usd = {max_cost_usd}")
    if concurrency is not None:
        llm.append(f"concurrency = {concurrency}")
    if llm:
        lines += ["[llm]", *llm, ""]
    if cli_path is not None:
        lines += ["[providers.claude-sdk]", f'cli_path = "{cli_path}"', ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class FakeCli:
    """A fake `claude` executable that logs every invocation.

    Attributes:
        path: The executable script.
        log: The file the script appends its arguments to.
    """

    path: Path
    log: Path

    def runs(self) -> list[str]:
        """Return the arguments of every run, one entry per invocation."""
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()


def write_fake_cli(directory: Path, *, version: str = SUPPORTED_VERSION) -> FakeCli:
    """Write an executable shell script that stands in for the `claude` CLI.

    Args:
        directory: Where to write the script and its log.
        version: The version the script prints.

    Returns:
        The script and its invocation log.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "claude"
    log = directory / "runs.log"
    script.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\necho "{version} (Claude Code)"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return FakeCli(script, log)


def answer(text: str) -> Opaque:
    """Return an assistant `Response` carrying `text`."""
    return _lm15.Response(
        id="fake",
        model="fake",
        message=_lm15.Message(role="assistant", parts=(_lm15.TextPart(text),)),
        finish_reason="stop",
        usage=_lm15.Usage(input_tokens=5, output_tokens=2),
    )


class FakeEngine:
    """Sync companion of `FakeAsyncEngine`; `acall` never uses it."""

    def complete(self, _request: Opaque) -> NoReturn:
        """Refuse: only the async engine is scripted."""
        msg = "the sync engine is not scripted"
        raise RuntimeError(msg)

    def stream(self, _request: Opaque) -> Iterator[Opaque]:
        """Refuse: streaming is not used."""
        raise NotImplementedError

    def close(self) -> None:
        """Release nothing."""


class FakeAsyncEngine:
    """An async engine that answers scripted texts and meters a fixed cost.

    Attributes:
        requests: Every request the engine received, in order.
    """

    def __init__(
        self,
        texts: Sequence[str] = (VERDICT,),
        *,
        cost: float = 0.001,
        delay_s: float = 0.0,
    ) -> None:
        """Script the engine.

        Args:
            texts: Answers in order; the last one repeats once the list runs out.
            cost: Cost to meter per call, in US dollars.
            delay_s: Seconds to sleep before answering.
        """
        self._texts = list(texts)
        self._cost = cost
        self._delay_s = delay_s
        self.requests: list[Opaque] = []

    async def complete(self, request: Opaque) -> Opaque:
        """Record the request, meter the cost and answer the next scripted text."""
        index = len(self.requests)
        self.requests.append(request)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        meter = current_meter()
        meter.completed = True
        meter.cost_usd = self._cost
        return answer(self._texts[min(index, len(self._texts) - 1)])

    def stream(self, _request: Opaque) -> NoReturn:
        """Refuse: streaming is not used."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release nothing."""


class FakeEngines:
    """An engine factory that records what it is asked for.

    Attributes:
        calls: The `(model, cli_path)` pair of every request, in order.
        engine: The async engine every pair shares, so a test reads its requests.
    """

    def __init__(self, engine: FakeAsyncEngine | None = None) -> None:
        """Build a factory handing out one shared async engine.

        Args:
            engine: The async engine to share; a default scripted one when omitted.
        """
        self.engine = engine or FakeAsyncEngine()
        self.calls: list[tuple[ModelRef, Path]] = []

    def __call__(
        self, ref: ModelRef, cli_path: Path
    ) -> tuple[FakeEngine, FakeAsyncEngine]:
        """Record the request and return the engine pair."""
        self.calls.append((ref, cli_path))
        return FakeEngine(), self.engine

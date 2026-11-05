"""Run a task with a tuned program: its version, its safety sentence and its predictor.

A `TunedProgram` holds only instructions and demos as text. Demos are plain
text, so a demo that names a posting later removed still runs; nothing here
reads the store.
"""

import hashlib
import json

import dspy
from ijob_core.errors import InvalidValueError
from ijob_core.tune_records import TunedProgram

from ijob_ai.tasks.versions import lacks_untrusted_sentence

_VERSION_HEX = 8


def program_version(base: str, instructions: str, demos_json: str) -> str:
    """Name the version a call with a tuned program runs under.

    Args:
        base: The task's base version, such as `judge-2`.
        instructions: The program's instruction text.
        demos_json: The program's demos as JSON text.

    Returns:
        `<base>+<first 8 hex characters of the SHA-256 of the instructions and
        the demos>`. The same text always gives the same version.
    """
    digest = hashlib.sha256(json.dumps([instructions, demos_json]).encode()).hexdigest()
    return f"{base}+{digest[:_VERSION_HEX]}"


def with_untrusted_statement(instructions: str, sentence: str) -> str:
    """Return the instructions holding the task's untrusted-data sentence.

    Args:
        instructions: A tuned instruction text, which may have lost the sentence.
        sentence: The exact sentence the task's signature states.

    Returns:
        `instructions` unchanged when it holds `sentence`, else `instructions`
        followed by a blank line and `sentence`.
    """
    if sentence in instructions:
        return instructions
    return f"{instructions.rstrip()}\n\n{sentence}"


def tuned_predict(
    signature: type[dspy.Signature], program: TunedProgram
) -> dspy.Predict:
    """Build a predictor from a signature and a stored program.

    Args:
        signature: The task's signature, which keeps its fields and types.
        program: The program whose instructions replace the signature's and
            whose demos are loaded as the predictor's demos.

    Returns:
        The predictor.

    Raises:
        InvalidValueError: If the program's instructions lack its task's
            untrusted-data sentence. A program that lost the sentence stops the
            task; it does not run without it. The app checks the same rule
            before a batch starts, so this is the second guard.
    """
    if lacks_untrusted_sentence(program.task, program.instructions):
        reason = f"program {program.id} lacks the untrusted-data sentence"
        raise InvalidValueError("instructions", reason)
    predict = dspy.Predict(signature.with_instructions(program.instructions))
    predict.demos = json.loads(program.demos_json)
    return predict

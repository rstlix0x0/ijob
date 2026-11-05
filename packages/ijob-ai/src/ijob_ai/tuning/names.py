"""The names of the optimizers, in a module that loads no DSPy."""

from enum import StrEnum


class Optimizer(StrEnum):
    """An optimizer a task can be tuned with; the value is the `--optimizer` name."""

    GEPA = "gepa"
    BOOTSTRAP = "bootstrap"
    MIPRO = "mipro"

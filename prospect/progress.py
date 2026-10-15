"""Progress for long stages: nothing from the CLI; a caller may pass a callback.

A stage takes `progress: Progress | None` and calls `report(progress, fraction, message)`
between units of work, with the fraction of the stage done (or None when it can't say,
e.g. while a batch is pending). The callback may raise `Cancelled` there to stop
the stage between two units; work already committed stays committed. The CLI passes
nothing, so a stage behaves exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable

Progress = Callable[[float | None, str], None]


class Cancelled(Exception):
    """The user stopped the job; raised from a progress callback."""


def report(progress: Progress | None, fraction: float | None, message: str) -> None:
    if progress is not None:
        progress(fraction, message)


def window(progress: Progress | None, lo: float, hi: float) -> Progress | None:
    """A stage's own 0..1 mapped onto [lo, hi] of a longer run (`prospect run`)."""
    if progress is None:
        return None

    def sub(fraction: float | None, message: str) -> None:
        progress(None if fraction is None else lo + (hi - lo) * fraction, message)

    return sub

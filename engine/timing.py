"""Split the silence before the greeting into the steps that cause it.

A caller waits several seconds between connecting and hearing the agent, and a
number that big has to come from somewhere: matching the audio socket to its
ARI channel, building the engine, the VAD, opening the Deepgram and Gemini
connections, synthesising the greeting. You cannot fix what you have not split,
so each call logs one line with a millisecond figure per step.

Marks are `time.monotonic()` readings taken by whoever sees the moment: the
transport (connected, correlated, first speech) and the engine (the steps in
between). Wall-clock time would be wrong here -- it can jump.

Pure functions, no Pipecat, so the arithmetic is tested on its own.
"""

from __future__ import annotations


def setup_breakdown(
    marks: list[tuple[str, float | None]],
) -> tuple[list[tuple[str, int]], float | None]:
    """(steps, total). Steps are `("a→b", ms)` between consecutive PRESENT marks.

    A missing mark is skipped, not zeroed, so its time is charged to the next
    step instead of disappearing -- a direct call has no ARI correlation, and
    the time still happened. `total` runs from the first mark to
    `first_speech`, and is None if the agent never spoke: a caller who hung up
    during the greeting did not have a 0-second wait.
    """
    present = [(name, t) for name, t in marks if t is not None]
    steps = [
        (f"{a}→{b}", round((tb - ta) * 1000))
        for (a, ta), (b, tb) in zip(present, present[1:])
    ]
    times = dict(present)
    total = None
    if present and "first_speech" in times:
        total = round(times["first_speech"] - present[0][1], 3)
    return steps, total


def format_setup(steps: list[tuple[str, int]], total: float | None) -> str:
    """The one log line. Grep for `setup:` to compare calls."""
    parts = " | ".join(f"{name} {ms} ms" for name, ms in steps)
    tail = (
        f"first speech after {round(total * 1000)} ms"
        if total is not None
        else "no greeting sent (caller left first?)"
    )
    return f"setup: {parts} | {tail}" if parts else f"setup: {tail}"

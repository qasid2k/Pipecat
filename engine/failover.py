"""Notice when the AI model has failed a caller, and say so once.

Seen live on 2026-09-28: a caller asked for billing, Gemini hung for 16 s,
then answered "503: high demand". Pipecat treats that as a non-fatal error and
carries on, so the caller heard nothing at all until they gave up. Nothing in
the pipeline ever tells the caller something went wrong.

The watchdog fires on either:
  * a model ERROR -- an ErrorFrame whose `processor` is the LLM itself (errors
    from other parts of the pipeline are not the model's and don't count);
  * model SILENCE -- no reply within `timeout_s` of the model being asked.

"Replied" means the FIRST REAL OUTPUT: reply text (LLMTextFrame) or the start
of a tool call (FunctionCallsStartedFrame). Deliberately NOT Pipecat's
LLMFullResponseStartFrame: the Gemini service sends that before it has even
called Gemini (pipecat/services/google/llm.py, _process_context), so it would
reset the clock on exactly the hang this exists to catch.

It fires at most ONCE per call; what to do about it (apologise, transfer) is
the engine's decision, passed in as `on_fail`. After it fires, any late reply
from the model is dropped so it can't talk over the apology.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from pipecat.frames.frames import ErrorFrame, Frame, FunctionCallsStartedFrame, LLMContextFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class LLMWatchdog:
    """One call's model-health state. Lives on that call's event loop."""

    def __init__(self, timeout_s: float, on_fail: Callable[[str], Awaitable[None]]):
        self._timeout_s = timeout_s
        self._on_fail = on_fail
        self._timer: asyncio.Task | None = None
        self.waiting = False
        self.tripped = False

    def request_sent(self) -> None:
        """The model was just asked for a reply. Start (or restart) the clock."""
        if self.tripped:
            return
        self._cancel_timer()
        self.waiting = True
        self._timer = asyncio.get_running_loop().create_task(self._expire())

    def responded(self) -> None:
        """The model produced real output: it's alive."""
        self.waiting = False
        self._cancel_timer()

    async def failed(self, error: str) -> None:
        """The model reported an error."""
        await self._trip(f"model error: {error}")

    def stop(self) -> None:
        """The call is over; cancel any pending timer."""
        self._cancel_timer()

    async def _expire(self) -> None:
        await asyncio.sleep(self._timeout_s)
        if self.waiting:
            self._timer = None  # this task is finishing; don't cancel ourselves
            await self._trip(f"no reply after {self._timeout_s:g} s")

    async def _trip(self, reason: str) -> None:
        if self.tripped:
            return
        self.tripped = True
        self.waiting = False
        self._cancel_timer()
        await self._on_fail(reason)

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None


class _BeforeLLM(FrameProcessor):
    """Sits just upstream of the LLM: sees requests go in, errors come back."""

    def __init__(self, llm: FrameProcessor, watchdog: LLMWatchdog):
        super().__init__()
        self._llm = llm
        self._watchdog = watchdog

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.inspect(frame, direction)
        await self.push_frame(frame, direction)

    async def inspect(self, frame: Frame, direction: FrameDirection) -> None:
        """The watchdog logic alone, separate from frame plumbing."""
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMContextFrame):
            self._watchdog.request_sent()
        elif (direction == FrameDirection.UPSTREAM and isinstance(frame, ErrorFrame)
              and frame.processor is self._llm):
            await self._watchdog.failed(frame.error)


class _AfterLLM(FrameProcessor):
    """Sits just downstream of the LLM: sees its real output."""

    def __init__(self, watchdog: LLMWatchdog):
        super().__init__()
        self._watchdog = watchdog

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (LLMTextFrame, FunctionCallsStartedFrame)):
            if self._watchdog.tripped and isinstance(frame, LLMTextFrame):
                return  # too late: the apology is already playing
            self._watchdog.responded()
        await self.push_frame(frame, direction)


def around_llm(llm: FrameProcessor, watchdog: LLMWatchdog) -> list[FrameProcessor]:
    """`[before, llm, after]`, ready to splice into the pipeline in place of `llm`."""
    return [_BeforeLLM(llm, watchdog), llm, _AfterLLM(watchdog)]

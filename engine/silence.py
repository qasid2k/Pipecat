"""What to do when the caller goes quiet.

A caller who stops talking -- looking up an order number, a bad line, a phone
on mute -- used to hear nothing at all until `engine.idle_timeout_s` ended the
call without a word. A human agent would check in. This decides when to.

Pipecat tells us WHEN the caller has been quiet for `reprompt_after_s` (its
`on_user_turn_idle` event, whose timer restarts every time the agent finishes
speaking). This class only decides WHAT that means: check in again, or give up
and say goodbye. It is deliberately free of Pipecat, so the counting can be
tested without building a pipeline.
"""

from __future__ import annotations

from enum import Enum


class SilenceAction(Enum):
    REPROMPT = "reprompt"  # ask "are you still there?"
    GIVE_UP = "give_up"    # say goodbye and end the call


class SilencePolicy:
    """Counts unanswered check-ins for ONE call. Not thread-safe; it is only
    touched from that call's pipeline, on the event loop."""

    def __init__(self, max_reprompts: int):
        if max_reprompts < 0:
            raise ValueError("max_reprompts cannot be negative")
        self._max = max_reprompts
        self.reprompts = 0
        self.gave_up = False

    def on_idle(self) -> SilenceAction:
        """The caller has been silent for the configured time. Again."""
        # Sticky: once the goodbye is queued, a late idle event must not slip
        # a check-in in behind it.
        if self.gave_up or self.reprompts >= self._max:
            self.gave_up = True
            return SilenceAction.GIVE_UP
        self.reprompts += 1
        return SilenceAction.REPROMPT

    def caller_spoke(self) -> None:
        """The caller answered, so the next silence gets a fresh allowance.
        Without this, two thinking pauses in a long call would end it."""
        if not self.gave_up:
            self.reprompts = 0

"""The silent-caller check-in (IMP-001).

A caller who goes quiet used to hear nothing until the 30 s idle timeout cut
them off without a word. Now the agent asks "are you still there?" a couple of
times first, and says goodbye before ending the call itself.

What these tests CAN prove: the counting (how many check-ins, reset when the
caller speaks, give up after the last one), the configuration rules, and that
the engine hands Pipecat the idle timeout. What they cannot: that Pipecat's idle
event fires on real line silence, or how a check-in feels to a caller who was
about to speak. That needs a live call -- see the item in [[backlog]].
"""

import unittest

from core.config import (
    ConfigError,
    TurnTakingConfig,
    _Env,
    _load_engine,
    _load_turn_taking,
)
from engine.silence import SilenceAction, SilencePolicy


class SilencePolicyTest(unittest.TestCase):
    def test_checks_in_up_to_the_limit_then_gives_up(self):
        policy = SilencePolicy(max_reprompts=2)
        self.assertIs(policy.on_idle(), SilenceAction.REPROMPT)
        self.assertIs(policy.on_idle(), SilenceAction.REPROMPT)
        self.assertIs(policy.on_idle(), SilenceAction.GIVE_UP)
        self.assertEqual(policy.reprompts, 2)

    def test_the_caller_speaking_starts_the_count_again(self):
        # Answering one check-in must not use up the next caller silence's
        # allowance -- otherwise a long call with two thinking pauses ends early.
        policy = SilencePolicy(max_reprompts=2)
        policy.on_idle()
        policy.on_idle()
        policy.caller_spoke()
        self.assertIs(policy.on_idle(), SilenceAction.REPROMPT)
        self.assertIs(policy.on_idle(), SilenceAction.REPROMPT)
        self.assertIs(policy.on_idle(), SilenceAction.GIVE_UP)

    def test_zero_check_ins_means_goodbye_straight_away(self):
        self.assertIs(SilencePolicy(max_reprompts=0).on_idle(), SilenceAction.GIVE_UP)

    def test_giving_up_is_sticky(self):
        # The goodbye is already on its way; a second idle event must not queue
        # another check-in behind it.
        policy = SilencePolicy(max_reprompts=1)
        policy.on_idle()
        policy.on_idle()
        self.assertIs(policy.on_idle(), SilenceAction.GIVE_UP)
        self.assertTrue(policy.gave_up)

    def test_negative_limit_is_refused(self):
        with self.assertRaises(ValueError):
            SilencePolicy(max_reprompts=-1)


class TurnTakingConfigTest(unittest.TestCase):
    def test_defaults(self):
        t = _load_turn_taking({})
        self.assertEqual(t.reprompt_after_s, 10)
        self.assertEqual(t.max_reprompts, 2)
        self.assertTrue(t.reprompt_text)
        self.assertTrue(t.goodbye_text)
        self.assertEqual(t, TurnTakingConfig())

    def test_zero_turns_it_off(self):
        self.assertEqual(_load_turn_taking({"reprompt_after_s": 0}).reprompt_after_s, 0)

    def test_values_are_read(self):
        t = _load_turn_taking({
            "reprompt_after_s": 7.5, "max_reprompts": 1,
            "reprompt_text": "Hello?", "goodbye_text": "Bye now.",
        })
        self.assertEqual(
            (t.reprompt_after_s, t.max_reprompts, t.reprompt_text, t.goodbye_text),
            (7.5, 1, "Hello?", "Bye now."),
        )

    def test_bad_values_are_refused(self):
        for bad in (
            {"reprompt_after_s": -1},
            {"reprompt_after_s": 1},        # would nag through a thinking pause
            {"reprompt_after_s": "10"},
            {"max_reprompts": -1},
            {"max_reprompts": 1.5},
            {"max_reprompts": True},
            {"reprompt_text": ""},
            {"goodbye_text": "   "},
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                _load_turn_taking(bad)


class EngineCrossCheckTest(unittest.TestCase):
    """The check-in has to be able to fire before the hard idle timeout does.

    Agent speech resets Pipecat's idle timeout, so each check-in buys another
    `idle_timeout_s`. The only way the hard timeout wins is if the FIRST
    check-in is due at or after it -- then the feature silently never runs.
    """

    @staticmethod
    def engine(**overrides):
        data = {
            "provider": "pipecat",
            "stt": {"provider": "deepgram", "api_key_env": "IMP001_TEST_UNSET"},
            "llm": {"provider": "google", "model": "m", "api_key_env": "IMP001_TEST_UNSET"},
            "tts": {"provider": "deepgram", "voice": "v", "api_key_env": "IMP001_TEST_UNSET"},
            "persona": {"system_prompt": "You are a test."},
            **overrides,
        }
        return _load_engine(data, _Env(), base_dir=__import__("pathlib").Path("."))

    def test_check_in_due_after_the_hard_timeout_is_refused(self):
        with self.assertRaises(ConfigError) as e:
            self.engine(idle_timeout_s=10, turn_taking={"reprompt_after_s": 10})
        self.assertIn("idle_timeout_s", str(e.exception))

    def test_check_in_before_the_hard_timeout_is_fine(self):
        cfg = self.engine(idle_timeout_s=30, turn_taking={"reprompt_after_s": 10})
        self.assertEqual(cfg.turn_taking.reprompt_after_s, 10)

    def test_turned_off_skips_the_check(self):
        cfg = self.engine(idle_timeout_s=5, turn_taking={"reprompt_after_s": 0})
        self.assertEqual(cfg.turn_taking.reprompt_after_s, 0)


class EngineWiringTest(unittest.TestCase):
    """The engine must pass the idle timeout to Pipecat -- including when Smart
    Turn is on, where it used to return None and would silently drop it."""

    def params(self, **turn_taking):
        from dataclasses import replace

        from core.config import EngineConfig
        from engine.pipecat_engine import PipecatEngine

        cfg = replace(EngineConfig(), turn_taking=replace(TurnTakingConfig(), **turn_taking))
        return PipecatEngine(cfg)._build_user_params()

    def test_idle_timeout_reaches_pipecat(self):
        self.assertEqual(self.params(reprompt_after_s=12).user_idle_timeout, 12)

    def test_off_means_pipecat_idle_detection_off(self):
        self.assertEqual(self.params(reprompt_after_s=0).user_idle_timeout, 0)

    def test_smart_turn_keeps_its_default_strategies_and_gets_the_timeout(self):
        p = self.params(smart_turn_v3=True, reprompt_after_s=12)
        self.assertIsNotNone(p)
        self.assertIsNone(p.user_turn_strategies)  # None = Pipecat's default (Smart Turn)
        self.assertEqual(p.user_idle_timeout, 12)

    def test_smart_turn_with_check_ins_off_is_unchanged(self):
        self.assertIsNone(self.params(smart_turn_v3=True, reprompt_after_s=0))


if __name__ == "__main__":
    unittest.main()

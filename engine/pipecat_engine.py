"""
Phase 3: the Pipecat conversation engine.

Everything that knows Pipecat exists lives in this package. The rest of the
codebase talks to the `Engine` contract and never imports Pipecat at all.

The pipeline, stage by stage:

    transport.input()        caller's 8kHz PCM arrives from the CallSession
    VADProcessor             detects when the caller starts/stops talking (barge-in)
    stt                      Deepgram turns speech into text
    recorder                 writes the caller's words to recordings/*.jsonl
    aggregators.user()       adds that text to the conversation history
    llm                      Gemini reads the history and writes a reply
    tts                      Deepgram turns the reply back into speech
    transport.output()       audio goes back out through the CallSession
    aggregators.assistant()  records what the agent said, so it remembers

Nothing here is hardcoded any more: the providers, model, voice, turn-taking and
persona all come from an EngineConfig built out of config.yaml (Phase 4). This
module decides HOW to assemble Pipecat, never WHAT to assemble.

Verified against pipecat-ai 1.6.0.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from core.config import ConfigError, EngineConfig
from core.engine import Engine
from core.transport import CallSession
from engine.session_transport import CallSessionTransport
from engine.transcripts import TranscriptRecorder, save_conversation

RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"

# HOW a transfer happens belongs to the transport -- every vendor does it
# differently. We only decide WHICH department to ask for. On Asterisk the
# department name becomes the dialplan EXTENSION under [transfer], so
# extensions.conf decides who each one actually dials.
#
# Each department MUST have a matching `<name>,1,...` entry in that context or
# the transfer fails. "human" is the general operator / catch-all fallback.
# (Not in config.yaml: these must stay in step with the dialplan, so changing
# them is a two-sided change that deserves a code review, not a config tweak.)
TRANSFER_DEPARTMENTS = ("sales", "support", "billing", "human")
# If the LLM ever supplies a department outside the list, fall back to a person
# rather than a dialplan slot that doesn't exist.
TRANSFER_FALLBACK = "human"


@dataclass
class CallResources:
    """Per-call objects the LLM's tools reach via params.app_resources.

    The CallSession, plus the one setting the tool needs. The tool asks the
    session to transfer and neither knows nor cares that Asterisk and ARI are
    involved.
    """

    session: CallSession | None = None
    # How long to let the agent's "connecting you now" line play before actually
    # transferring. Leaving Stasis kills the audio path instantly, so without
    # this the caller is cut off mid-sentence and the transfer feels broken even
    # though it worked. It is engine-side, not transport-side, because it is
    # about the spoken announcement rather than the transfer mechanism.
    announce_secs: float = 3.0


async def transfer_to_department(params: FunctionCallParams):
    """LLM tool: hand the caller to the right team.

    Works on any transport whose session reports can_transfer -- on Asterisk
    that means a Stasis call (6001), not a direct AudioSocket call (6000).
    """
    # The LLM chooses which department; we map an unknown/blank one to a person.
    department = (params.arguments or {}).get("department", "")
    department = str(department).strip().lower()
    if department not in TRANSFER_DEPARTMENTS:
        logger.warning(f"TOOL: transfer_to_department got '{department}' -> {TRANSFER_FALLBACK}")
        department = TRANSFER_FALLBACK
    logger.info(f"TOOL: transfer_to_department -> {department}")

    res: CallResources | None = params.app_resources
    session = res.session if res else None
    # Check up front so we tell the caller the truth BEFORE promising anything.
    if session is not None and session.can_transfer:

        announce_secs = res.announce_secs if res else 3.0

        async def do_transfer():
            await asyncio.sleep(announce_secs)
            await session.transfer(department)

        asyncio.create_task(do_transfer())
        await params.result_callback({"result": f"Connecting the caller to {department} now."})
    else:
        await params.result_callback(
            {"result": "Transfer isn't available on this call; apologize and offer to take a message."}
        )


TRANSFER_TOOL = FunctionSchema(
    name="transfer_to_department",
    description=(
        "Transfer the caller to the right human team. Call this when the caller "
        "needs something you cannot do yourself, explicitly asks for a person, or "
        "is clearly frustrated. Choose the department that best fits their request."
    ),
    properties={
        "department": {
            "type": "string",
            "enum": list(TRANSFER_DEPARTMENTS),
            "description": (
                "Which team to connect them to. "
                "'sales' = buying, quotes, pricing, new customers. "
                "'support' = technical problems, something broken, help using the product. "
                "'billing' = invoices, payments, refunds, account charges. "
                "'human' = a general operator when they just want a person or none of "
                "the above clearly fits."
            ),
        }
    },
    required=["department"],
    handler=transfer_to_department,
)


class PipecatEngine(Engine):
    """Runs one call's conversation on a Pipecat pipeline.

    One instance per call: it owns that call's pipeline, conversation history
    and transcript files.
    """

    def __init__(self, config: EngineConfig, recordings_dir: Path = RECORDINGS_DIR):
        self._config = config
        self._recordings_dir = recordings_dir

    # -- building the services from config ---------------------------------
    def _build_stt(self):
        c = self._config.stt
        if c.provider == "deepgram":
            kwargs = {"api_key": c.api_key, "sample_rate": c.sample_rate}
            if c.model:
                kwargs["settings"] = DeepgramSTTService.Settings(model=c.model)
            return DeepgramSTTService(**kwargs)
        raise ConfigError(f"engine.stt.provider: '{c.provider}' is not implemented here")

    def _build_llm(self):
        c = self._config.llm
        if c.provider == "google":
            return GoogleLLMService(
                api_key=c.api_key,
                settings=GoogleLLMService.Settings(model=c.model),
            )
        raise ConfigError(f"engine.llm.provider: '{c.provider}' is not implemented here")

    def _build_tts(self):
        c = self._config.tts
        if c.provider == "deepgram":
            return DeepgramTTSService(
                api_key=c.api_key,
                settings=DeepgramTTSService.Settings(voice=c.voice),
                sample_rate=c.sample_rate,
            )
        raise ConfigError(f"engine.tts.provider: '{c.provider}' is not implemented here")

    def _build_user_params(self) -> LLMUserAggregatorParams | None:
        """How we decide the caller has finished talking.

        Returning None means "use Pipecat's default", which is Smart Turn v3: a
        second ONNX model, run on every utterance. We normally replace it with a
        plain silence timeout -- Silero VAD already reports speech boundaries, so
        N seconds of quiet after speech needs no model at all, and costs no CPU
        or latency on a box that drops calls when it stalls.
        """
        t = self._config.turn_taking
        if t.smart_turn_v3:
            return None
        return LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=t.silence_timeout_s)]
            )
        )

    def _build_pipeline(self, transport: CallSessionTransport, recorder: TranscriptRecorder):
        """Assemble the STT -> LLM -> TTS pipeline from configuration.

        Returns the pipeline AND the context, because the context holds the full
        conversation and we want to save it when the call ends.
        """
        stt = self._build_stt()
        llm = self._build_llm()
        tts = self._build_tts()

        # The conversation memory. The aggregators keep it updated automatically:
        # user() records what the caller said, assistant() what the agent replied.
        # `tools` advertises the transfer function to the LLM; because the schema
        # carries its handler, Pipecat auto-registers it (no register_function).
        context = LLMContext(
            messages=[{"role": "system", "content": self._config.persona.system_prompt}],
            tools=ToolsSchema(standard_tools=[TRANSFER_TOOL]),
        )

        aggregators = LLMContextAggregatorPair(
            context, user_params=self._build_user_params()
        )

        pipeline = Pipeline(
            [
                transport.input(),
                VADProcessor(vad_analyzer=SileroVADAnalyzer()),
                stt,
                recorder,  # sits right after STT, so it sees every transcription
                aggregators.user(),
                llm,
                tts,
                transport.output(),
                aggregators.assistant(),
            ]
        )
        return pipeline, context

    async def run(self, session: CallSession) -> None:
        """Talk to this caller until the call ends.

        Note what is NOT here: no hangup. The caller of run() owns the session's
        lifecycle and releases it in a `finally`, so cleanup happens on every
        path -- including the exception path below.
        """
        started = datetime.now()
        # Include a short random tag: two calls that connect in the SAME second
        # must not share a filename (they'd overwrite each other's transcript).
        stamp = started.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

        self._recordings_dir.mkdir(exist_ok=True)
        recorder = TranscriptRecorder(self._recordings_dir / f"{stamp}-transcript.jsonl")

        transport = CallSessionTransport(session)
        pipeline, context = self._build_pipeline(transport, recorder)
        task = PipelineWorker(
            pipeline,
            idle_timeout_secs=self._config.idle_timeout_s,
            app_resources=CallResources(
                session=session,
                announce_secs=self._config.transfer_announce_s,
            ),
        )
        runner = WorkerRunner(handle_sigint=False)

        # Records WHO ended the call, so a premature drop is self-diagnosing.
        cause = {"reason": "the pipeline finished on its own (nothing left to do)"}

        async def watch_for_hangup():
            """When the call ends, tear the pipeline down."""
            await session.ended.wait()
            cause["reason"] = f"call ended -- {session.end_reason}"
            await task.cancel(reason="call ended")

        watcher = asyncio.create_task(watch_for_hangup())
        try:
            # Speak first, so the caller isn't greeted by silence.
            await task.queue_frames([TTSSpeakFrame(self._config.persona.greeting)])
            await runner.add_workers(task)  # register the worker, then run
            await runner.run()
        except Exception as e:
            cause["reason"] = f"unhandled exception: {e!r}"
            logger.exception(f"Call failed: {e}")
        finally:
            watcher.cancel()
            save_conversation(
                context, self._recordings_dir / f"{stamp}-conversation.json", started
            )
            duration = (datetime.now() - started).total_seconds()
            # Frame counters are vendor-specific, so they are optional extra detail.
            stats = session.stats() if hasattr(session, "stats") else ""
            logger.warning(
                f"--- Call ended after {duration:.1f}s ({session.call_id}); {stats} ---\n"
                f"    CAUSE: {cause['reason']}"
            )

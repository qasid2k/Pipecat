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
import re
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
from core.engine import Engine, EngineResult
from core.records import RecordWriter
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
    # Set by the transfer tool when a handover is actually initiated, and read
    # by run() at the end to fill in the call record. Written here rather than
    # inferred later because this is the only place that knows the LLM *chose*
    # a department -- from the outside, a transfer to billing and one to support
    # can be indistinguishable when both dial the same endpoint.
    transferred_to: str | None = None


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
        # Recorded now, not after the sleep. The tool has committed to the
        # handover and told the caller so; if the process is torn down during
        # those three seconds, the record should still say a transfer to billing
        # was what happened, because from the caller's side it was.
        if res is not None:
            res.transferred_to = department
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

    def __init__(
        self,
        config: EngineConfig,
        recordings_dir: Path = RECORDINGS_DIR,
        tenant_id: str = "default",
        records: RecordWriter | None = None,
    ):
        self._config = config
        self._recordings_dir = recordings_dir
        # Where per-utterance rows go, if anywhere. Optional: an engine with no
        # writer still runs a call and still writes its transcript files, it
        # just contributes no database rows.
        self._records = records
        # Stamped onto every record this call writes. One value today; the point
        # is that records written now are still attributable if a second tenant
        # ever exists, instead of needing a migration to say who they belonged to.
        self._tenant_id = tenant_id

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

    @staticmethod
    async def _build_vad() -> SileroVADAnalyzer:
        """Construct this call's VAD analyzer OFF the event loop.

        Measured at ~170 ms per call on the dev machine (~460 ms for the very
        first, which loads the model file). Constructed inline that is 170 ms of
        blocked event loop per call, and it stacks: ten calls arriving together
        would block the loop for nearly two seconds -- far past what Asterisk
        tolerates before it abandons a call ([[bugs]] B-001, B-011).

        A THREAD, NOT A SHARED ANALYZER. The obvious optimisation is to build one
        and reuse it, but Silero is stateful -- it carries the recurrent state of
        whoever is speaking. Sharing it would make two concurrent callers
        interrupt each other's turn detection, which is precisely the
        cross-contamination the per-call engine exists to prevent. Each call
        still gets its own; it is just no longer built on the loop.

        onnxruntime does most of this work in C++ with the GIL released, so the
        thread genuinely runs in parallel rather than merely deferring the cost.
        """
        return await asyncio.to_thread(SileroVADAnalyzer)

    def _build_pipeline(
        self,
        transport: CallSessionTransport,
        recorder: TranscriptRecorder,
        vad: SileroVADAnalyzer,
    ):
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
                VADProcessor(vad_analyzer=vad),
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

    async def run(self, session: CallSession) -> EngineResult:
        """Talk to this caller until the call ends.

        Note what is NOT here: no hangup. The caller of run() owns the session's
        lifecycle and releases it in a `finally`, so cleanup happens on every
        path -- including the exception path below.
        """
        started = datetime.now()

        # The filename is keyed by CALL ID, not by a fresh random tag.
        #
        # It used to be `<timestamp>-<uuid4[:6]>`, a value that appeared nowhere
        # else -- so a transcript on disk could not be matched to the call that
        # produced it, to a caller, or to an Asterisk channel, except by matching
        # wall-clock seconds against the log. Using call_id makes the join
        # trivial in both directions; the timestamp stays because it makes the
        # directory readable and sortable by eye.
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session.call_id))
        stamp = f"{safe_id}-{started.strftime('%Y%m%d-%H%M%S')}"

        # Everything needed to attribute this call, written INTO both files.
        # `vendor_ids` carries Asterisk's own uniqueid/linkedid, which is what
        # lets these records join to a CDR row -- see core/transport.py.
        call_meta = {
            "call_id": str(session.call_id),
            "caller_id": session.caller_id,
            "tenant_id": self._tenant_id,
            "persona": self._config.persona.name,
            "voice": self._config.tts.voice,
            "llm_model": self._config.llm.model,
            **session.vendor_ids,
        }

        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = self._recordings_dir / f"{stamp}-transcript.jsonl"
        conversation_path = self._recordings_dir / f"{stamp}-conversation.json"
        recorder = TranscriptRecorder(
            transcript_path,
            call=call_meta,
            # submit() is non-blocking and never raises, so an utterance costs
            # the pipeline nothing even if the store is down.
            on_turn=(lambda t: self._records.submit([t])) if self._records else None,
        )

        transport = CallSessionTransport(session)
        # Built before the pipeline and off the loop -- see _build_vad. This is
        # the one construction step expensive enough to matter when several
        # calls arrive at the same moment.
        vad = await self._build_vad()
        pipeline, context = self._build_pipeline(transport, recorder, vad)
        # Held in a variable rather than constructed inline: the transfer tool
        # writes the chosen department onto it, and the `finally` below reads it
        # back to fill in the call record.
        resources = CallResources(
            session=session,
            announce_secs=self._config.transfer_announce_s,
        )
        task = PipelineWorker(
            pipeline,
            idle_timeout_secs=self._config.idle_timeout_s,
            app_resources=resources,
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
                context,
                conversation_path,
                started,
                call={**call_meta, "end_reason": session.end_reason,
                      "cause": cause["reason"]},
            )
            # Flush the live transcript before the process moves on. Without
            # this, the last thing the caller said -- often the most useful line
            # in the file -- can still be sitting on the writer's queue.
            await recorder.close()
            duration = (datetime.now() - started).total_seconds()
            # Frame counters are vendor-specific, so they are optional extra detail.
            stats = session.stats() if hasattr(session, "stats") else ""
            logger.warning(
                f"--- Call ended after {duration:.1f}s; {stats} ---\n"
                f"    CAUSE: {cause['reason']}"
            )

        # Returned, not stored: run_call owns the call record, and these are the
        # three things only the engine can know -- why it ended in our terms,
        # whether the model handed the caller over, and where the files went.
        return EngineResult(
            cause=cause["reason"],
            transferred_to=resources.transferred_to,
            transcript_path=str(transcript_path),
            conversation_path=str(conversation_path),
            turns=recorder.turns,
        )

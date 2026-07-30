"""
The adapter between OUR CallSession and PIPECAT's transport classes.

Pipecat ships transports for WebRTC/WebSockets; none of them speak to a
CallSession, so we write one. It is deliberately thin -- it moves bytes and
nothing else.

This lives under engine/ because it is Pipecat-specific, not vendor-specific:
it will drive a pipeline from ANY CallSession, whether the audio came from
Asterisk, Twilio or a test harness. (It lived in the AudioSocket module until
Phase 3, which made it look Asterisk-specific when it never really was.)

THE ONE THING TO KNOW
---------------------
`BaseOutputTransport` does NOT pace audio: it calls write_audio_frame() in a
tight loop and assumes the transport itself blocks at real time. Our
CallSession.write_audio() does exactly that -- it blocks until the vendor is
ready for more. Do not "optimise" that into fire-and-forget: an entire TTS
utterance would be dumped into the socket at once, the far end would drop the
surplus, and the caller would hear chopped-up speech.

Verified against pipecat-ai 1.6.0.
"""

import asyncio

from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from core.transport import CANONICAL_CHANNELS, CANONICAL_SAMPLE_RATE, CallSession


class CallSessionInputTransport(BaseInputTransport):
    """Pumps caller audio out of the CallSession and into the pipeline."""

    def __init__(self, session: CallSession, params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._session = session
        self._pump_task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if not self._pump_task:
            self._pump_task = asyncio.create_task(self._pump_loop())
        # Required: without this the pipeline never reports ready and stalls.
        await self.set_transport_ready(frame)

    async def _pump_loop(self):
        while True:
            payload = await self._session.read_audio()
            if payload is None:  # sentinel: the call ended
                break
            await self.push_audio_frame(
                InputAudioRawFrame(
                    audio=payload,
                    sample_rate=CANONICAL_SAMPLE_RATE,
                    num_channels=CANONICAL_CHANNELS,
                )
            )

    async def cleanup(self):
        await super().cleanup()
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None


class CallSessionOutputTransport(BaseOutputTransport):
    """Hands the agent's audio to the CallSession, which paces and sends it."""

    def __init__(self, session: CallSession, params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._session = session

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # Blocks (asynchronously) until the vendor wants more audio. That
        # back-pressure IS the pacing -- see the module docstring.
        await self._session.write_audio(frame.audio)
        return True


class CallSessionTransport(BaseTransport):
    """Bundles the input and output halves for one call."""

    def __init__(
        self, session: CallSession, params: TransportParams | None = None, **kwargs
    ):
        super().__init__(**kwargs)
        self._session = session
        self._params = params or TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=CANONICAL_SAMPLE_RATE,
            audio_in_channels=CANONICAL_CHANNELS,
            audio_out_enabled=True,
            audio_out_sample_rate=CANONICAL_SAMPLE_RATE,
            audio_out_channels=CANONICAL_CHANNELS,
            # 2 x 10ms = 320 bytes at 8kHz: exactly one telephony frame. The
            # default of 4 would produce 40ms chunks and misalign with the
            # 20ms cadence the transports expect.
            audio_out_10ms_chunks=2,
        )
        self._input = None
        self._output = None

    def input(self) -> FrameProcessor:
        if not self._input:
            self._input = CallSessionInputTransport(self._session, self._params)
        return self._input

    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = CallSessionOutputTransport(self._session, self._params)
        return self._output

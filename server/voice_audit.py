"""Passive voice audits and supported half-duplex turn configuration."""
import time
import os
from pipecat.frames.frames import (
    TranscriptionFrame, InterimTranscriptionFrame, LLMFullResponseStartFrame,
    LLMFullResponseEndFrame, LLMTextFrame, InterruptionFrame, CancelFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.user_start import VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_mute import AlwaysUserMuteStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import TurnAnalyzerUserTurnStopStrategy
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from settings import smartturn_stop_secs
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState

VOICE_SPEECH_TIMEOUT = 2.0


def voice_mute_strategies():
    """Keep late pre-speech fragments interruptible; mute during bot speech."""
    comparison=os.getenv('STEP8E_DISABLE_BOT_MUTE','0')
    if comparison not in {'0','1'}:
        raise ValueError('STEP8E_DISABLE_BOT_MUTE must be 0 or 1')
    if comparison=='1':
        return []  # Explicit diagnostic A/B arm; production defaults stay muted.
    return [AlwaysUserMuteStrategy()]


def configure_voice_turns(params, policy, log=None):
    """Wire the same speaking-state mute for all comparison policies."""
    params['user_turn_strategies'] = voice_strategies(policy, log)
    params['user_mute_strategies'] = voice_mute_strategies()
    return params['user_turn_strategies']


def half_duplex_strategies(*, settle_voice=False):
    stop = (SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=VOICE_SPEECH_TIMEOUT) if settle_voice
            else TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3()))
    return UserTurnStrategies(start=[
        VADUserTurnStartStrategy(enable_interruptions=False),
        TranscriptionUserTurnStartStrategy(enable_interruptions=False),
    ], stop=[stop])


class AuditedSmartTurn(LocalSmartTurnAnalyzerV3):
    """Measure the analyzer call without changing its result or parameters."""
    def __init__(self, log):
        super().__init__(params=SmartTurnParams(stop_secs=smartturn_stop_secs()))
        self.log = log
        self._audit_timeout_pending = False

    def append_audio(self, buffer, is_speech):
        state = super().append_audio(buffer, is_speech)
        if state == EndOfTurnState.COMPLETE:
            self._audit_timeout_pending = True
        return state

    def clear(self):
        super().clear()
        self._audit_timeout_pending = False

    async def analyze_end_of_turn(self):
        started = time.monotonic()
        timeout = self._audit_timeout_pending
        self._audit_timeout_pending = False
        state, metrics = await super().analyze_end_of_turn()
        if not timeout and metrics is None:
            return state, metrics  # Empty-buffer no-op is not a model call.
        self.log.write('smartturn-inference',
                       kind='stop_secs_timeout' if timeout else 'model',
                       verdict=None if timeout else state.name,
                       completion_reason='silence bound' if timeout else 'model prediction',
                       probability=getattr(metrics, 'probability', None),
                       model_metrics_available=metrics is not None,
                       inference_time_ms=1000*(time.monotonic()-started))
        return state, metrics


def voice_strategies(policy, log=None):
    if policy == '8d':
        return half_duplex_strategies(settle_voice=True)
    stop = (SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=.8) if policy == 'timeout'
            else TurnAnalyzerUserTurnStopStrategy(turn_analyzer=AuditedSmartTurn(log) if log else
                LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=smartturn_stop_secs()))))
    return UserTurnStrategies(stop=[stop])


class VoiceSTTAudit(FrameProcessor):
    def __init__(self, log, audit):
        super().__init__()
        self.log, self.audit = log, audit

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            result = frame.result
            def field(name):
                return result.get(name) if isinstance(result, dict) else getattr(result, name, None)
            self.log.write("final-transcript" if isinstance(frame, TranscriptionFrame) else "interim-transcript",
                           turn=self.audit.turn, text=frame.text,
                           finalized=getattr(frame, 'finalized', False),
                           is_final=field('is_final'), speech_final=field('speech_final'),
                           from_finalize=field('from_finalize'))
        await self.push_frame(frame, direction)


class VoiceResponseAudit(FrameProcessor):
    """One response ID per LLM generation, even for multiple TTS sentences.

    Snapshot the input turn at generation start: subsequent VAD noise must
    not relabel tokens or the final response as a different user's answer.
    """
    def __init__(self, log, audit):
        super().__init__()
        self.log, self.audit = log, audit
        self.response_id = 0
        self.turn = None
        self.parts = []
        self.last_turn = None
        self.cancelled = False
        self.audit.response_audit = self

    def cancel(self, reason):
        # Services can emit their EndFrame while handling an interruption,
        # before that interruption reaches this downstream processor. The
        # observer calls this on the broadcast signal as well as here.
        if self.response_id and not self.cancelled and (
                self.turn is not None or getattr(self.audit, 'output_pending', False)):
            self.log.write("llm-cancelled", turn=self.turn if self.turn is not None else self.last_turn,
                           response_id=self.response_id, text="".join(self.parts),
                           reason=reason, generation_ended=self.turn is None)
            self.cancelled = True
            self.audit.output_pending = False
            self.turn = None
            self.parts.clear()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self.response_id += 1
                self.turn = self.audit.turn
                self.audit.response_id = self.response_id
                self.audit.output_pending = True
                self.last_turn = self.turn
                self.cancelled = False
                self.parts.clear()
                self.log.write("llm-start", turn=self.turn, response_id=self.response_id)
            elif isinstance(frame, LLMTextFrame) and self.turn is not None:
                if frame.text and not self.parts:
                    self.log.write("llm-first-token", turn=self.turn, response_id=self.response_id)
                if frame.text:
                    self.parts.append(frame.text)
            elif isinstance(frame, (InterruptionFrame, CancelFrame)):
                self.cancel(type(frame).__name__)
            elif isinstance(frame, LLMFullResponseEndFrame) and self.turn is not None:
                self.log.write("llm-response", turn=self.turn, response_id=self.response_id,
                               text="".join(self.parts))
                self.turn = None
                self.parts.clear()
        await self.push_frame(frame, direction)

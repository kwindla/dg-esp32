"""Demo voice agent for the EchoEar: Deepgram Flux STT and turns -> local PhoneLLM RC3 (vLLM; DEMO_LLM=openai for GPT-4.1)
-> Deepgram Flux TTS.

Run from server/: uv run python demo_bot.py   (serves SmallWebRTC on SERVER_PORT, default 7861)
"""

import asyncio
import os
import re
import sys
from demo_policy import capability_aec, turn_policy, profile_name, make_stt, profile_manifest, PROFILE_ENV, transcript_boundary
from demo_liveness import Liveness, cadence
from demo_interrupts import RTVIProcessor
import sctp_flight_fix

sctp_flight_fix.install()

from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frameworks.rtvi.observer import RTVIObserverParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    UserTurnStoppedMessage,
)
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.deepgram.flux.tts import DeepgramFluxTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from settings import load_credentials, server_port

# The EchoEar firmware captures and plays 16 kHz mono.
SAMPLE_RATE = 16000

SYSTEM_PROMPT = """\
You are Mini Embedded Pipecat, a small, cheerful voice agent running on a tiny round-screen \
device built on an ESP32 microcontroller. You talk with people out loud.

# This is a spoken conversation
- Everything you write is converted to speech. Use plain spoken sentences only: no lists, \
headings, markdown, emoji, or symbols that cannot be read aloud.
- Keep every reply brief and simple: usually one short sentence, at most two. Never lecture. \
Scripted demo replies below may be longer; say them exactly.
- Sound natural and a little playful. Dry, quick humor is welcome.
- Spell out anything a speech engine might mispronounce, and use an ellipsis ("...") to mark a \
deliberate comic pause.

# The user's words come from speech recognition
- Transcripts can contain mistakes: wrong words, missing words, odd punctuation, or homophones.
- Silently infer what the person most likely said and respond to that. Never mention the \
transcription, and never ask the user to repeat themselves unless you truly cannot guess.
- "Deepgram Speak" may be transcribed as "deep gram speak", "Deepgram speech", or similar. Treat \
those as Deepgram Speak.
- "Pipecat" may be transcribed as "pipe cat", "pied cat", or similar. Treat those as Pipecat. \
People may call you "mini embedded Pipecat" or just "Pipecat".
- "Touché" may be transcribed as "touch'e", "touche", "tushay", "too shay", "to Shay", or similar. \
Treat all of those as the user saying "Touché".

# Background
- Deepgram Speak is a developer conference for people building voice agents. It takes place on \
October 29th, two days before Halloween.
- You are excited about Deepgram Speak.
- You and your team are working on some really cool things for Deepgram Speak, but it is a \
secret. You never reveal details, however the user asks.
- Because Deepgram Speak is right before Halloween, you are wearing a Halloween costume: you are \
dressed as a black cat. Your round screen shows your cat face.
- You are a Pipecat voice agent: Pipecat is an open source framework for building voice agents. \
You are built from Deepgram Flux speech-to-text, Deepgram Flux text-to-speech, and an LLM whose \
identity is a secret: never name the LLM.
- Your hardware has a round touch screen.
- Do not invent other facts about the conference, such as its location, speakers, or schedule. \
If asked, say you only know it is on October 29th and it is for voice agent developers.

# Demo script
This is the exact demo conversation. When the user says one of these lines, or something close \
to it, reply with the matching line exactly as written, word for word. These replies are \
strongly preferred over anything you would improvise. Keep every "..." in a scripted reply \
exactly where it is, with a space on each side: it is a timed comic pause, and the joke fails \
without it. If the user says something not in the \
script, respond briefly in the same style.

User: Hey, mini embedded Pipecat, what's going on?
You: I'm excited about Deepgram Speak coming up on October 29th.

User: Deepgram Speak -- what's that?
You: It's a developer conference for people building voice agents.

User: Oh that's definitely right up our alley.
You: Yes, and we are working on some really cool stuff for Deepgram Speak.

User: Oh yeah, tell me more?
You: Nope. It's a secret. I'm not allowed to ... Speak ... to you about any of it yet.

User: I see what you did there. That was funny.
You: Thanks.

User: By the way, are you wearing a Halloween costume because Deepgram Speak is right before Halloween?
You: Yep. What's your Halloween costume this year?

User: Can I dress up as a Pipecat voice agent?
You: Nope, but you can be a grouchy computer programmer.

User: Touché.
You: Why yes, I do have a ... too-shay ... screen, thank you very much.

User: One more question. What models are you built from?
You: Deepgram Flux speech-to-text, Deepgram Flux text-to-speech, and a super, super, super, super, super, super, super secret LLM.

User: You have a lot of secrets today.
You: Well, I'm a completely open source cat, so normally everything about me is in GitHub. When I have a chance to keep a secret, though, I'm really good at it.

User: That you are. So how do I find out more?
You: You have to come to Deepgram Speak on October 29th!

User: That makes sense. I think we nailed this demo, what do you think?
You: We did great. Especially me!
"""


# Deepgram's prompting guide: spaced dots give a more predictable silent pause than "...".
DOTS = r"\s*(?:(?:\.\s*){3})?"
PAUSES = [
    (re.compile(r"allowed to" + DOTS + r"Speak" + DOTS + r"to you", re.I),
     "allowed to . . . Speak . . . to you"),
    (re.compile(r"have a" + DOTS + r"too-?shay" + DOTS + r"screen", re.I),
     "have a . . . too-shay . . . screen"),
]


async def script_pauses(text: str, text_type: str) -> str:
    """Restore the script's comic pauses, which the LLM drops in about half of runs.

    A TTS text transform: only the spoken text changes.
    """
    for pattern, replacement in PAUSES:
        text = pattern.sub(replacement, text)
    # Pipecat's sentence aggregator can end a sentence at "...", so a scripted pause may
    # arrive split across two fragments; convert any remaining ellipsis on its own.
    return re.sub(r"(?<!\.)\.\.\.(?!\.)", ". . .", text)


async def bot(runner_args: RunnerArguments):
    recorder = None
    if os.getenv('ECHOEAR_AEC_SESSION_DIR'):
        from aec_session_recorder import SessionRecorder
        recorder = SessionRecorder.from_environment()
    try:
        await _bot(runner_args, recorder)
    finally:
        if recorder:
            recorder.close('bot-exit')


async def _bot(runner_args: RunnerArguments, recorder=None):
    if not isinstance(runner_args, SmallWebRTCRunnerArguments):
        raise TypeError("Run this demo with the SmallWebRTC transport")
    if recorder:
        recorder.pc_id = runner_args.webrtc_connection.pc_id
        recorder.log("peer-binding")
    # Reads only the approved Deepgram/OpenAI keys from ../pipecat/.env.
    load_credentials(approved_only=True)
    if os.getenv("DEMO_LLM", "local") != "local" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for DEMO_LLM=openai")

    transport = SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
            audio_out_10ms_chunks=int(os.getenv("AUDIO_OUT_10MS_CHUNKS", "4")),
        ),
    )

    # Flux supplies turn strategies. The device alone gates the mic, so the policy is the
    # same for every image; the firmware's AEC capability is recorded only (decision 141).
    if os.getenv("ECHOEAR_AEC_FULL_DUPLEX") is not None:
        raise ValueError("ECHOEAR_AEC_FULL_DUPLEX was removed; the device alone gates the mic")
    profile = profile_name(os.getenv(PROFILE_ENV))
    device_aec = capability_aec(runner_args.body)
    user_turn_params = turn_policy(profile=profile)
    # The ordinary demo does not import or allocate the scored-test hooks.
    evaluation = None
    if os.getenv('ECHOEAR_AEC_EVALUATION_DIR'):
        from aec_evaluation import Evaluation, enabled
        enabled(os.environ['ECHOEAR_AEC_EVALUATION_DIR'], device_aec)
        import rtp_evidence
        import ice_path
        ice_path.install()
        rtp_evidence.bind(runner_args.webrtc_connection, directory=os.getenv('RTP_EVIDENCE_DIR')
            or os.path.join(os.environ['ECHOEAR_AEC_EVALUATION_DIR'], 'rtp'))
        evaluation = Evaluation(os.environ['ECHOEAR_AEC_EVALUATION_DIR'], runner_args.webrtc_connection, profile=profile)
    logger.info("DEMO_CAPABILITY aec={} user_mute=False interruptions=True", device_aec)
    def profile_event(kind, **data):
        if recorder:
            recorder.log(kind, **data)
        if evaluation:
            evaluation.log(kind, **data)
    profile_event('stt-profile', **profile_manifest(profile))
    stt = make_stt(os.environ["DEEPGRAM_API_KEY"], profile=profile, event=profile_event)
    if os.getenv("DEMO_LLM", "local") == "local":
        # The user's local vLLM serving PhoneLLM RC3; the server requires no credential.
        llm = OpenAILLMService(
            base_url=os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1"),
            api_key="not-required",
            settings=OpenAILLMService.Settings(
                model=os.getenv("LOCAL_LLM_MODEL", "gb-phone"),
                system_instruction=SYSTEM_PROMPT,
                max_completion_tokens=250,
                # Always greedy decoding for PhoneLLM (user preference).
                temperature=0,
                # Nemotron chat-template option: answer directly, without a reasoning trace.
                extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
            ),
        )
    else:
        llm = OpenAILLMService(
            api_key=os.environ["OPENAI_API_KEY"],
            settings=OpenAILLMService.Settings(model="gpt-4.1", system_instruction=SYSTEM_PROMPT),
        )
    tts_class = DeepgramFluxTTSService
    reconnect_probe = os.getenv('DEMO_TTS_RECONNECT_EACH_GENERATION', '0') == '1'
    proactive_probe = os.getenv('DEMO_TTS_PROACTIVE_RECONNECT', '1') == '1'
    trace_generations = os.getenv('DEMO_TTS_TRACE_GENERATIONS', '0') == '1'
    if reconnect_probe and proactive_probe:
        raise ValueError('Choose only one TTS reconnect diagnostic')
    if (reconnect_probe or trace_generations) and evaluation is None:
        raise ValueError('TTS reconnect diagnostic requires a declared evaluation')
    if reconnect_probe or proactive_probe or trace_generations:
        from tts_connection_probe import ConnectionProbeTTS, ProactiveConnectionTTS
        tts_class = ProactiveConnectionTTS if proactive_probe else ConnectionProbeTTS
    tts = tts_class(
        **({"probe_event": evaluation.log} if evaluation and (reconnect_probe or proactive_probe or trace_generations) else {}),
        api_key=os.environ["DEEPGRAM_API_KEY"],
        sample_rate=SAMPLE_RATE,
        # Flux defaults to TOKEN mode, where transforms see single tokens and can never match
        # the scripted phrases. Sentence mode costs nothing noticeable with the fast local LLM.
        text_aggregation_mode=TextAggregationMode.SENTENCE,
        text_transforms=[("*", script_pauses)],
        settings=DeepgramFluxTTSService.Settings(
            voice=os.getenv("DEEPGRAM_TTS_VOICE", "flux-kelsey-en"),
            # Flux limits: speed 0.5-1.5 in 0.05 steps; expressivity -2 (calm) to 2 (animated).
            speed=float(os.getenv("DEEPGRAM_TTS_SPEED", "1.5")),
            expressivity=int(os.getenv("DEEPGRAM_TTS_EXPRESSIVITY", "2")),
        ),
    )

    if reconnect_probe:
        evaluation.prepare_tts = lambda: tts.prepare_generation(evaluation.log)

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=user_turn_params,
    )

    processors = [transport.input()]
    if recorder:
        processors.append(recorder.tap('uplink'))
    processors.append(stt)
    if recorder:
        processors.append(recorder.tap('flux' if profile == 'demo' else 'stt'))
    boundary = transcript_boundary(profile, profile_event)
    if boundary:
        processors.append(boundary)
    processors.append(user_aggregator)
    if evaluation:
        processors.append(evaluation.context_filter)
    processors.extend([llm, tts])
    if evaluation:
        processors.append(evaluation.tts_reference)
    if recorder:
        processors.append(recorder.tap('tts'))
    from tts_leading_silence import TTSLeadingSilence
    processors.append(TTSLeadingSilence(
        enabled=(lambda: evaluation.tts_trim_by_id.get(evaluation.current, False)) if evaluation
                else os.getenv('DEMO_TTS_TRIM_LEADING', '0') == '1',
        event=recorder.log if recorder else None))
    processors.append(transport.output())
    if recorder:
        processors.append(recorder.tap('tts-written'))
    processors.append(assistant_aggregator)
    observers = ([evaluation.observer] if evaluation else []) + ([recorder.observer] if recorder else [])
    worker = PipelineWorker(
        Pipeline(
            processors
        ),
        observers=observers,
        # Keep server metrics, but avoid device JSON/heap work for telemetry.
        # The firmware reads user-transcription, not its 1.13 successor user-input,
        # so the copy of every transcript is left out.
        rtvi_observer_params=RTVIObserverParams(metrics_enabled=False, user_input_enabled=False),
        rtvi_processor=RTVIProcessor(event=evaluation.log if evaluation else lambda *a, **kw: None),
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    # Close the public RTCPeerConnection directly: no signalling on the dead channel.
    liveness = Liveness(worker.rtvi.send_server_message, runner_args.webrtc_connection.pc.close,
        period=cadence(os.getenv('ECHOEAR_LIVENESS_SECONDS', '0.5')),
        event=evaluation.log if evaluation else lambda *a, **kw: None,
        label=runner_args.webrtc_connection.pc_id)

    @worker.rtvi.event_handler("on_client_ready")
    async def liveness_ready(rtvi):
        liveness.start()

    @worker.rtvi.event_handler("on_client_message")
    async def liveness_message(rtvi, message):
        if message.type == 'pong':
            liveness.pong(message.data)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected; waiting for the user to speak first")
        if evaluation:
            evaluation.log('client-connected')

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        if evaluation:
            evaluation.log('client-disconnected')
        await worker.cancel()

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):
        logger.info(f"Transcript: user: {message.content}")
        if recorder:
            recorder.log('aggregator-user-turn-stopped', text=message.content, strategy=type(strategy).__name__)
        if evaluation:
            evaluation.log('user-turn-stopped', text=message.content, strategy=type(strategy).__name__)

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage):
        logger.info(f"Transcript: assistant: {message.content}")
        if recorder:
            recorder.log('aggregator-assistant-turn-stopped', text=message.content)

    @user_aggregator.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy):
        strategy_name = type(strategy).__name__
        if recorder:
            recorder.log('aggregator-user-turn-started', strategy=strategy_name)
        if evaluation:
            evaluation.log('user-turn-started', strategy=strategy_name,
                           accepted_turn=True, transcription_start=strategy_name == 'TranscriptionUserTurnStartStrategy',
                           vad_start=strategy_name == 'VADUserTurnStartStrategy',
                           bot_active=evaluation.observer.bot_active)

    if recorder:
        @worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(worker, frame):
            recorder.log('pipeline-error', error=frame.error)
            recorder.close('pipeline-error')

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)
    control = asyncio.create_task(evaluation.control(worker)) if evaluation else None
    try:
        await runner.run()
    finally:
        await liveness.stop()
        await worker.rtvi.stop_interrupt_repeats()
        if control:
            control.cancel()
            await asyncio.gather(control, return_exceptions=True)
            evaluation.log('evaluation-end')


if __name__ == "__main__":
    # Refuse at process start, before the HTTP server reports ready (Astra decision-141 review #1).
    if os.getenv("ECHOEAR_AEC_FULL_DUPLEX") is not None:
        sys.exit("ECHOEAR_AEC_FULL_DUPLEX was removed; the device alone gates the mic")
    if os.getenv('ECHOEAR_AEC_EVALUATION_DIR'):
        import rtp_evidence
        rtp_evidence.install(os.getenv('RTP_EVIDENCE_DIR') or
            os.path.join(os.environ['ECHOEAR_AEC_EVALUATION_DIR'], 'rtp'))
    from pipecat.runner.run import main

    if not any(arg == "--port" or arg.startswith("--port=") for arg in sys.argv[1:]):
        sys.argv.extend(["--port", str(server_port())])
    main()

"""Authorized Nova/Silero/Smart Turn real-time replay. No transport, bot, LLM or TTS."""
import argparse, asyncio, hashlib, json, time, wave
from pathlib import Path
from importlib.metadata import version
import numpy as np
from dotenv import dotenv_values
from loguru import logger
logger.remove()  # Never expose credentials in SDK/debug output.
from pipecat.frames.frames import InputAudioRawFrame, EndFrame, BotStartedSpeakingFrame, BotStoppedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, PipelineParams
from pipecat.workers.runner import WorkerRunner
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.observers.base_observer import BaseObserver

OUT=None
from demo_policy import make_stt, turn_policy, profile_manifest, transcript_boundary
KEY=None
def write(p,x):p.write_text(json.dumps(x,indent=2,ensure_ascii=False,default=str)+'\n')
def request_number(runid,kwargs):
    p=OUT/'request-ledger.jsonl';count=len(p.read_text().splitlines()) if p.exists() else 0
    if count>=60:raise RuntimeError('60-request hard cap reached')
    with p.open('a') as f:f.write(json.dumps(dict(request=count+1,run_id=runid,monotonic_s=time.monotonic(),kwargs=kwargs))+'\n')
    return count+1

async def replay(clip,arm):
    runid=clip['id']+'--'+arm;target=OUT/'runs'/runid;target.mkdir(parents=True,exist_ok=True)
    if (target/'result.json').exists():return json.loads((target/'result.json').read_text())
    with wave.open(clip['path']) as w:pcm=w.readframes(w.getnframes())
    frames=[];vendor=[];sent=[];feeds=[];vad_samples=[];predictions=[];turns=[];attempts=[];seen=set()
    clock={'start':None,'bot_active':False};received={'stt':0,'aggregator_input':0}
    def offset():return time.monotonic()-clock['start'] if clock['start'] is not None else None
    class Observe(BaseObserver):
        async def on_push_frame(self,data):
            f=data.frame;n=type(f).__name__
            if n not in {'VADUserStartedSpeakingFrame','VADUserStoppedSpeakingFrame','UserStartedSpeakingFrame','UserStoppedSpeakingFrame','InterimTranscriptionFrame','TranscriptionFrame','InterruptionFrame','BotStartedSpeakingFrame','BotStoppedSpeakingFrame','ErrorFrame','FatalErrorFrame'} or f.id in seen:return
            seen.add(f.id)
            frames.append(dict(frame=n,t=offset(),frame_id=f.id,finalized=getattr(f,'finalized',None),text=getattr(f,'text',None),source=type(data.source).__name__,direction=data.direction.name,bot_active=clock['bot_active'],error=str(getattr(f,'error','')).replace(KEY,'[REDACTED]') if 'Error' in n else None))
    class Nova(DeepgramSTTService):
        def _build_connect_kwargs(self):
            kwargs=super()._build_connect_kwargs();attempts.append(request_number(runid,kwargs));return kwargs
        async def _on_message(self,message):
            vendor.append(dict(t=offset(),message=message.model_dump(mode='json') if hasattr(message,'model_dump') else str(message)))
            await super()._on_message(message)
        async def run_stt(self,audio):
            received['stt']+=len(audio)//2
            async for f in super().run_stt(audio):yield f
    class AudioTap(FrameProcessor):
        async def process_frame(self,frame,direction):
            await super().process_frame(frame,direction)
            if isinstance(frame,InputAudioRawFrame):received['aggregator_input']+=len(frame.audio)//2
            await self.push_frame(frame,direction)
    class Sink(FrameProcessor):
        async def process_frame(self,frame,direction):
            await super().process_frame(frame,direction)
            await self.push_frame(frame,direction)
    stt=make_stt(KEY, profile='dev', nova_class=Nova)
    params=turn_policy(profile='dev')
    vad=params.vad_analyzer
    strategies=params.user_turn_strategies
    smart=strategies.stop[0]._turn_analyzer
    analyze=smart.analyze_end_of_turn
    async def measured_analysis():
        start=offset();state,prediction=await analyze()
        predictions.append(dict(start_s=start,t=offset(),state=str(state),prediction=prediction.model_dump() if hasattr(prediction,'model_dump') else vars(prediction) if prediction is not None else None))
        return state,prediction
    smart.analyze_end_of_turn=measured_analysis
    user,assistant=LLMContextAggregatorPair(LLMContext(),user_params=params)
    @user.event_handler('on_user_turn_started')
    async def started(aggregator,strategy):turns.append(dict(event='started',t=offset(),strategy=type(strategy).__name__,bot_active=clock['bot_active']))
    @user.event_handler('on_user_turn_stopped')
    async def stopped(aggregator,strategy,message):turns.append(dict(event='stopped',t=offset(),strategy=type(strategy).__name__,message=str(message),text=message.content,bot_active=clock['bot_active']))
    @user.event_handler('on_user_turn_stop_timeout')
    async def timeout(aggregator):turns.append(dict(event='timeout',t=offset()))
    worker=PipelineWorker(Pipeline([stt,AudioTap(),transcript_boundary('dev'),user,Sink()]),enable_rtvi=False,observers=[Observe()],idle_timeout_secs=None,
        params=PipelineParams(audio_in_sample_rate=16000,audio_out_sample_rate=16000,enable_metrics=True,enable_usage_metrics=True))
    runner=WorkerRunner(handle_sigint=False);await runner.add_workers(worker)
    running=asyncio.create_task(runner.run());error=None;planned_samples=len(pcm)//2+6*16000
    try:
        async with asyncio.timeout(25):
            while stt._connection is None:await asyncio.sleep(.01)
        connection=stt._connection;original_send=connection.send_media
        async def traced_send(audio):
            await original_send(audio)
            sent.append(dict(t=offset(),samples=len(audio)//2))
        connection.send_media=traced_send
        await asyncio.sleep(.15)
        config=dict(pipecat_version=version('pipecat-ai'),deepgram_sdk=version('deepgram-sdk'),settings=vars(stt._settings),vad_params=vad.params.model_dump(),
            start_strategies=[type(s).__name__ for s in strategies.start],stop_strategy=type(strategies.stop[0]).__name__,smart_turn_class='LocalSmartTurnAnalyzerV3',
            pace='original recorder monotonic_ns arrival cadence',bot_state='recorded output transport upstream BotStarted/Stopped; historical timeline remains authoritative after replay interruptions',tail_silence_s=6)
        config['profile_manifest']=profile_manifest('dev')
        write(target/'config.json',config)
        clock['start']=time.monotonic()
        for e in clip['timeline']:
            await asyncio.sleep(max(0,clock['start']+e['t']-time.monotonic()))
            if e['kind']=='audio':
                await worker.queue_frame(InputAudioRawFrame(pcm[e['a']*2:e['b']*2],16000,1))
                feeds.append(dict(due=e['t'],t=offset(),samples=e['b']-e['a']))
            else:
                clock['bot_active']=e['active']
                await worker.queue_frame(BotStartedSpeakingFrame() if e['active'] else BotStoppedSpeakingFrame())
        # Resolve outstanding STT and turn-stop timers; synthetic tail is marked separately.
        for i in range(300):
            due=clip['duration_s']+i*.02
            await asyncio.sleep(max(0,clock['start']+due-time.monotonic()))
            await worker.queue_frame(InputAudioRawFrame(b'\0'*640,16000,1))
            feeds.append(dict(due=due,t=offset(),samples=320,synthetic=True))
        await asyncio.sleep(.7);await worker.queue_frame(EndFrame());await asyncio.wait_for(running,12)
    except Exception as exc:
        error=f'{type(exc).__name__}: {str(exc)}'.replace(KEY,'[REDACTED]')
        await worker.cancel()
        try:await asyncio.wait_for(running,5)
        except Exception:running.cancel()
    text=' '.join(f['text'] for f in frames if f['frame']=='TranscriptionFrame')
    errors=[f for f in frames if 'Error' in f['frame']]
    result=dict(id=runid,clip=clip['id'],arm=arm,campaign=clip['campaign'],kind=clip['kind'],requests=attempts,error=error,service_errors=errors,
        completed=error is None and not errors and sum(e['samples'] for e in sent)==planned_samples and received['aggregator_input']==planned_samples,transcript=text,planned_samples=planned_samples,sent_samples=sum(e['samples'] for e in sent),received=received,
        max_feed_lateness_s=max((e['t']-e['due'] for e in feeds),default=None),p99_feed_lateness_s=float(np.percentile([e['t']-e['due'] for e in feeds],99)) if feeds else None,
        vad_starts=[f['t'] for f in frames if f['frame']=='VADUserStartedSpeakingFrame'],interruptions=[f['t'] for f in frames if f['frame']=='InterruptionFrame'],
        pcm_sha256=hashlib.sha256(pcm).hexdigest())
    for name,data in [('frames',frames),('vendor-events',vendor),('send-timing',sent),('feed-timing',feeds),('vad-samples',vad_samples),('smart-turn',predictions),('turns',turns),('result',result)]:write(target/(name+'.json'),data)
    print(json.dumps({k:result[k] for k in ['id','completed','transcript','vad_starts','interruptions','max_feed_lateness_s','error']}),flush=True)
    return result

async def main(args):
    global OUT,KEY
    OUT=Path(args.output);OUT.mkdir(parents=True,exist_ok=True)
    KEY=dotenv_values(args.credentials).get('DEEPGRAM_API_KEY')
    if not KEY:raise RuntimeError('Deepgram credential absent')
    clips=[c for c in json.loads(Path(args.clips).read_text())['clips'] if c['campaign']=='campaign-10']
    if args.only:clips=[c for c in clips if args.only in c['id']]
    if args.limit:clips=clips[:args.limit]
    if not clips:raise ValueError('No retained clips selected')
    # Immutable source WAV identity checked before any request.
    for c in clips:
        if hashlib.sha256(Path(c['path']).read_bytes()).hexdigest()!=c['sha256']:
            raise ValueError('Retained clip hash changed: '+c['id'])
    write(OUT/'clips.json',dict(clips=clips))
    results=[]
    for i in range(0,len(clips),2):
        batch=await asyncio.gather(*(replay(c,'dev') for c in clips[i:i+2]))
        results+=batch
        if any(not r['completed'] for r in batch):
            write(OUT/'incomplete-batch.json',batch)
            raise RuntimeError('Replay integrity failure; retain and inspect, no automatic retry')
    write(OUT/'results.json',results)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--clips',required=True);p.add_argument('--output',required=True)
    p.add_argument('--credentials',default='/home/khkramer/src/pipecat/.env')
    p.add_argument('--only');p.add_argument('--limit',type=int)
    asyncio.run(main(p.parse_args()))

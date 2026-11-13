#!/usr/bin/env python3
"""Predeclared interleaved pause sweep: 96 turns, no replacement of failures."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import time
import wave
import requests
import numpy as np
from acoustic_response import calibrate_response
from agent_conversation_test import boot,connect,calibrate_bank,run_turn,write_json,one_reply_matches
from agent_prompts import credentials,fixture_transcript,USER_VOICE
from alsa import resolve_jabra,set_volumes
from bench_calibration import active_mask
from ethernet_bench import ethernet_bench
from esp_serial import ROOT,EspSerial
from speech_accuracy import speech_accuracy
from step7_support import BotServer,pcm,write_pcm

PAUSES=(.8,1.3,1.8,2.5)
RULE=('Informational measurements only. The user withdrew the automated selection rule. '
      'Production keeps Pipecat SmartTurn defaults (3 s); only future human testing '
      'can justify changing them. Original failures remain in all denominators.')


def definitions():
    return [dict(id=f'{stress}-{gap}',text='What is the capital of France?',
        stt=[['capital'],['france']],answer=[['paris']],pause_s=gap,stress=stress)
        for stress in ('stressed','unstressed') for gap in PAUSES]


def generate(out):
    out.mkdir(parents=True,exist_ok=False);key=credentials()['DEEPGRAM_API_KEY'];parts={}
    for name,text in [('prefix','What is the capital'),('stressed','OF FRANCE?'),('unstressed','of France?')]:
        r=requests.post('https://api.deepgram.com/v1/speak',params=dict(model=USER_VOICE,
            encoding='linear16',container='wav',sample_rate=16000),
            headers={'Authorization':'Token '+key},json={'text':text},timeout=45)
        if r.status_code!=200:raise RuntimeError(f'Prompt TTS HTTP {r.status_code}')
        path=out/(name+'-source.wav');path.write_bytes(r.content);x=pcm(path)
        active=np.flatnonzero(np.abs(x)>25)
        if not len(active):raise RuntimeError('Silent fixture')
        # Trim only near-digital margins; retain phonetic release and onset.
        parts[name]=x[max(0,active[0]-800):min(len(x),active[-1]+801)]
    manifest=[]
    for definition in definitions():
        gap=round(definition['pause_s']*16000)
        prefix=parts['prefix'];suffix=parts[definition['stress']]
        prefix=np.r_[np.zeros(4000),prefix]
        x=np.r_[prefix,np.zeros(gap),suffix,np.zeros(4000)];path=out/(definition['id']+'.wav');write_pcm(path,x)
        transcript=fixture_transcript(path,key);accuracy=speech_accuracy(definition['text'],transcript)
        entry=dict(definition,file=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            wer=1-accuracy,transcript=transcript,continuation_offset_s=(len(prefix)+gap)/16000,
            continuation_duration_s=len(suffix)/16000,continuation_active_rms=float(np.sqrt(np.mean(suffix[active_mask(suffix)]**2))),
            digital_silence_samples=gap)
        manifest.append(entry);write_json(out/'manifest.json',dict(voice=USER_VOICE,turns=manifest))
        if accuracy!=1:raise RuntimeError('Pause fixture WER must be zero: '+definition['id'])
    return manifest


def pause_observation(row,fixture):
    events=row.get('events',[]);start=row.get('prompt_start')
    if start is None:return dict(correct_audible_reply=False,time_to_correct_reply_ms=None,
        first_fragment_started_generation=None,cancelled_before_first_tts_byte=None,
        continuation_dropped_by_mute=None,vad_missed_continuation_onset=None,error='No playback')
    onset=start+fixture['continuation_offset_s'];end=onset+fixture['continuation_duration_s']
    stopped={e['turn'] for e in events if e['event']=='user-turn-stopped' and 'capital' in e.get('text','').lower() and 'france' not in e.get('text','').lower()}
    first=[e for e in events if e['event']=='llm-start' and e.get('turn') in stopped]
    bytes_=[e for e in events if e['event']=='first-tts-byte']
    detected=[e for e in events if e['t']>=onset-.1 and e['event'] in ('vad-start','final-transcript','interim-transcript')
              and (e['event']=='vad-start' or 'france' in e.get('text','').lower() or e.get('text','').lower().startswith('of'))]
    mute=row.get('mute_evidence',{}).get('bot_intervals',[])
    exposed=any(a<end and (b is None or b>onset) for a,b in mute)
    raw_dropped=any(e['event'] in ('final-transcript','interim-transcript') and any(a<=e['t'] and (b is None or e['t']<b) for a,b in mute) for e in detected)
    vad=any(e['event']=='vad-start' and onset-.1<=e['t']<=end+.8 for e in events)
    # Firmware gate can remove audio before STT. Without a raw continuation
    # transcript the server mute itself is unobservable; exposure remains unknown.
    dropped=True if raw_dropped else None if exposed and not detected else False
    ids={e['response_id'] for e in first}
    cancellations=[e for e in events if e['event']=='llm-cancelled' and e.get('response_id') in ids]
    cancelled=bool(first) and len({e['response_id'] for e in cancellations})==len(ids) and not any(e.get('response_id') in ids for e in bytes_)
    correct=one_reply_matches(events,row.get('tts_segments',[])) and row.get('checks',{}).get('acoustic_content',False)
    return dict(correct_audible_reply=correct,time_to_correct_reply_ms=row.get('time_to_correct_reply_ms'),
        first_fragment_started_generation=bool(first),cancelled_before_first_tts_byte=cancelled if first else False,
        continuation_dropped_by_mute=dropped,continuation_gate_or_mute_exposed=exposed,
        continuation_detected_after_first_tts_byte=bool(detected and bytes_ and min(e['t'] for e in detected)>min(e['t'] for e in bytes_)),
        vad_missed_continuation_onset=not vad,continuation_source_onset_host_estimate=onset,
        timing_basis='ALSA launch + immutable fixture offset; includes unsubtracted physical output delay',
        detected_continuation_events=detected)


def decision(turns,repetitions):
    cells=[]
    for spl in (65,58):
        for fixture in definitions():
            arms={bound:[r for r in turns if r['spl']==spl and r['fixture']==fixture['id'] and r['bound']==bound] for bound in (3,1)}
            counts={bound:sum(r['pause']['correct_audible_reply'] for r in rows) for bound,rows in arms.items()}
            cells.append(dict(spl=spl,fixture=fixture['id'],correct=counts,n={b:len(r) for b,r in arms.items()}))
    return dict(rule=RULE,cells=cells,informational_only=True,automated_selection_allowed=False,
                production_stop_secs=3,
                complete=all(all(n>=repetitions for n in c['n'].values()) for c in cells))


def sweep(out,elf,bank,repetitions=3,winner=None):
    out.mkdir(parents=True,exist_ok=False)
    manifest=json.loads((bank/'manifest.json').read_text())['turns']
    assert len(manifest)==8 and all(f['wer']==0 and hashlib.sha256((bank/f['file']).read_bytes()).hexdigest()==f['sha256'] for f in manifest)
    result=dict(rule=RULE,turns=[],completed=False,repetitions=repetitions,board_moved=False,fixture_manifest=manifest)
    write_json(out/'result.json',result)
    old=os.environ.get('BOT_SMARTTURN_STOP_SECS')
    try:
        for spl in (65,58):
            for bound in ((winner,) if winner else (3,1)):
                os.environ['BOT_SMARTTURN_STOP_SECS']=str(bound)
                with BotServer(out/f'bot-{spl}-{bound}','voice') as bot,EspSerial('/dev/ttyACM0',log_path=out/f'serial-{spl}-{bound}.log') as board:
                    boot(board,elf)
                    if bound==(winner or 3):
                        calibration=calibrate_bank(board,resolve_jabra()[1],out/f'calibration-{spl}',spl,definitions(),bank)
                        write_json(out/f'calibration-{spl}.json',calibration)
                    else:calibration=json.loads((out/f'calibration-{spl}.json').read_text())
                    pc=connect(board);device=resolve_jabra()[1]
                    model=calibrate_response(board,device,out/f'response-{spl}-{bound}')
                    board.command('audio 1');board.command('quiet 0')
                    try:
                        # Each bound uses its own owned server; switches below
                        # restart the server between short blocks to interleave.
                        pass
                    finally:board.command('audio 0');board.command('disconnect',40)
            # Calibrated prompts/model can be reused within this fixed placement.
            for round_ in range(repetitions):
                fixtures=manifest[round_:]+manifest[:round_]
                for block in (fixtures[:4],fixtures[4:]):
                    bounds=(winner,) if winner else ((3,1) if round_%2==0 else (1,3))
                    for bound in bounds:
                        os.environ['BOT_SMARTTURN_STOP_SECS']=str(bound)
                        label=f'{spl}-r{round_+1}-b{bound}-{block[0]["id"]}'
                        with BotServer(out/label/'bot','voice') as bot,EspSerial('/dev/ttyACM0',log_path=out/label/'serial.log') as board:
                            boot(board,elf);pc=connect(board)
                            model=json.loads((out/f'response-{spl}-{bound}'/'response.json').read_text())
                            board.command('audio 1');board.command('quiet 0')
                            try:
                                for fixture in block:
                                    row=run_turn(board,bot,device,out/label/fixture['id'],fixture,
                                        calibration['prompts'][fixture['id']],model,pc,recording_seconds=18)
                                    row.update(spl=spl,bound=bound,fixture=fixture['id'],round=round_+1,
                                               pause=pause_observation(row,fixture))
                                    result['turns'].append(row);result['decision']=decision(result['turns'],repetitions)
                                    write_json(out/'result.json',result)
                            finally:board.command('audio 0');board.command('disconnect',40)
        result['completed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        if old is None:os.environ.pop('BOT_SMARTTURN_STOP_SECS',None)
        else:os.environ['BOT_SMARTTURN_STOP_SECS']=old
        write_json(out/'result.json',result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--bank',type=Path);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf')
    p.add_argument('--generate',action='store_true');p.add_argument('--winner',type=int,choices=(1,3));a=p.parse_args()
    if a.generate:generate(a.out.resolve())
    else:
        if not a.bank:p.error('--bank required')
        with ethernet_bench(a.out.resolve().parent/'ethernet-stun'):
            result=sweep(a.out.resolve(),a.elf.resolve(),a.bank.resolve(),1 if a.winner else 3,a.winner)
        raise SystemExit(2 if result.get('error') else 0)

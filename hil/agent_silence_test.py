#!/usr/bin/env python3
"""120-second acoustic room-noise check for the voice mode's faster VAD start."""
import argparse
import json
import time
import re
from pathlib import Path
import numpy as np
from agent_conversation_test import boot,connect,credentials,validate
from alsa import resolve_jabra,set_volumes,record_wav
from bench_calibration import dbfs
from esp_serial import ROOT,EspSerial
from step7_support import BotServer,pcm
from transport_test import perf,wait_until


def run(a):

    if a.mode != 'offline-test': credentials()
    validate();out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    result={'passed':False}
    try:
        card,device=resolve_jabra();set_volumes(card,out/'mixer.log')
        with BotServer(out/'bot',a.mode) as bot,EspSerial(a.port,log_path=out/'serial.log') as board:
            try:
                board.agent_mic_gain=a.mic_gain
                cursor=board.cursor
                result['elf_sha256']=boot(board,a.elf)
                line=board.wait_for(r'MIC_GAIN percent=(\d+)',5,since=cursor)
                result['mic_gain_percent']=int(re.search(r'MIC_GAIN percent=(\d+)',line)[1])
                result['mode']=a.mode
                greeting_cursor=board.cursor
                pc=connect(board)
                if a.mode=='offline-test':
                    greeting_start=wait_until(lambda:next((e['t'] for e in bot.events()
                        if e['pc_id']==pc and e['event']=='first-fixture-byte' and e['name']=='greeting.wav'),None),15)
                    greeting_end=greeting_start+len(pcm(ROOT/'server/fixtures/greeting.wav'))/16000
                else:
                    greeting_end=wait_until(lambda:next((e['t'] for e in bot.events()
                        if e['pc_id']==pc and e['event']=='bot-audio-end'),None),15)
                wait_until(lambda:any(t>=greeting_end and 'GATE open' in line
                    for _,t,line in board.lines_since(greeting_cursor)),15)
                time.sleep(1)
                started=time.monotonic();floors=[]
                result['greeting_finished_at']=greeting_end
                result['before_perf']=perf(board)
                for index in range(2):
                    set_volumes(card,out/f'mixer-minute{index+1}.log')
                    with record_wav(out/f'room{index+1}.wav',device,60,16000) as rec:rec.wait()
                    floors.append(dbfs(pcm(out/f'room{index+1}.wav')))
                    print('Silent minute',index+1,'/ 2 complete',flush=True)
                events=[e for e in bot.events() if e['pc_id']==pc and e['t']>=started]
                unwanted=[e for e in events if e['event'] in ('vad-start','final-transcript','llm-start','user-turn-started')]
                levels=[e['rms_dbfs'] for e in events if e['event']=='input-level']
                peaks=[e for e in events if e['event']=='input-peaks']
                result.update(seconds=time.monotonic()-started,jabra_room_dbfs=floors,
                              unexpected_events=unwanted,after_perf=perf(board),
                              server_input_rms_p50_dbfs=float(np.median(levels)) if levels else None,
                              server_input_rms_p99_dbfs=float(np.percentile(levels,99)) if levels else None,
                              server_input_clipped_samples=sum(e.get('clipped_samples',0) for e in peaks))
                result['checks']=dict(duration=result['seconds']>=120,no_spurious_turns=not unwanted,
                    real_input_observed=bool(levels) and float(np.median(levels))>-100
                        and result['after_perf']['sent']>result['before_perf']['sent'],
                    no_input_clipping=result['server_input_clipped_samples']==0,
                    session=result['before_perf']['pc_id']==result['after_perf']['pc_id']==pc)
                result['passed']=all(result['checks'].values())
            finally:
                board.command('audio 0');board.command('disconnect',40)
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result['checks'] if 'checks' in result else result),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--mode',choices=('voice','offline-test','live-test'),default='voice')
    p.add_argument('--mic-gain',type=int,default=1600)
    p.add_argument('--port',default='/dev/ttyACM0');p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf')
    raise SystemExit(run(p.parse_args()))

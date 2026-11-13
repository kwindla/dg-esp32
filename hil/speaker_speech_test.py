#!/usr/bin/env python3
"""Fixture/STT checks for three speaker candidates without human listening."""
import argparse
import json
import re
import time
from pathlib import Path
from alsa import resolve_jabra,set_volumes,record_wav
from esp_serial import ROOT,EspSerial
from step7_support import BotServer,transcribe,word_accuracy,acoustic_match,pcm,write_pcm,leakage

# Match the production second-order Butterworth high-pass before measuring peaks.
from scipy import signal
import numpy as np

def staging(amplitude):
    w=2*np.pi*500/16000;c=np.cos(w);alpha=np.sin(w)/np.sqrt(2);norm=1/(1+alpha)
    b=np.array([(1+c)/2,-(1+c),(1+c)/2])*norm;a=[1,-2*c*norm,(1-alpha)*norm]
    peak=max(np.max(np.abs(signal.lfilter(b,a,pcm(ROOT/'server/fixtures'/name))))/32768 for name in ('greeting.wav','reply_pineapple.wav'))
    gain=max(1,int(np.floor(amplitude/peak)))
    # Fixture peaks stay below 80% knee, with 1% integer configuration granularity.
    ceiling=min(100,int(np.ceil(gain*peak/.8))+1)
    return dict(gain=gain,ceiling=ceiling,filtered_fixture_peak=peak,program_peak_percent=gain*peak,knee_percent=.8*ceiling)

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--amplitudes',type=int,nargs='+',required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False);result={'runs':[]};card,device=resolve_jabra();set_volumes(card,a.out/'mixer.log')
    try:
        with BotServer(a.out/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
            time.sleep(.2);cur=board.cursor;board.reset();board.wait_for('READY transport-console',40,since=cur)
            for amp in a.amplitudes:
                settings=staging(amp);board.command(f'playback-settings {settings["gain"]} {settings["ceiling"]}')
                for name in ('greeting.wav','reply_pineapple.wav'):
                    path=a.out/f'{amp}-{name}';cursor=board.cursor
                    with record_wav(path,device,12,16000) as rec:
                        time.sleep(1);trigger=time.monotonic();
                        if name=='greeting.wav':board.command('connect',30);board.command('audio 1')
                        else:board.command('play',10)
                        board.wait_for('GATE closed',8,since=cursor);board.wait_for('GATE open',10,since=cursor);rec.wait()
                    board.command('stats',10)
                    lines=board.lines_since(cursor);text='\n'.join(x[2] for x in lines)
                    matched=acoustic_match(path,ROOT/'server/fixtures'/name,trigger-rec.ready_at,rec.ready_at,trigger-rec.ready_at-.1)
                    segment=pcm(path)[int(matched['offset_s']*16000):int(matched['offset_s']*16000)+len(pcm(ROOT/'server/fixtures'/name))]
                    speech=a.out/f'{amp}-{name}-segment.wav';write_pcm(speech,segment)
                    actual=transcribe(speech);manifest=json.loads((ROOT/'server/fixtures/manifest.json').read_text());expected=manifest[name]['text']
                    counts=re.findall(r'limiter_samples=(\d+)',text)
                    row=dict(amplitude_percent=amp,fixture=name,settings=settings,transcript=actual,word_accuracy=word_accuracy(expected,actual),
                        limiter_samples=int(counts[-1]),acoustic=matched,leakage=leakage(lines,matched['onset'],matched['end']))
                    result['runs'].append(row);(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(row),flush=True)
                board.command('audio 0');board.command('disconnect',20)
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return 1 if 'error' in result else 0
if __name__=='__main__':raise SystemExit(main())

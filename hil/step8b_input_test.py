"""A0: calibrated 65/58 dB SPL live VAD/STT and 120 s open-gate room silence."""
import argparse
import json
import time
from pathlib import Path
from alsa import resolve_jabra,play_wav,record_wav
from bench_calibration import calibrate,dbfs
from esp_serial import EspSerial
from step7_support import BotServer,pcm
from transport_test import perf,wait_until
from speech_accuracy import speech_accuracy
PROMPT='Please tell me the capital of France.'


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False);result={'passed':False,'runs':[]}
    def save():(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        _,device=resolve_jabra()
        with BotServer(a.out/'bot','live-test') as bot,EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
            cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
            for spl in (65,58):
                cal=calibrate(board,device,a.out/f'cal-{spl}',spl)
                cur=board.cursor;board.command('connect',90);board.command('audio 1')
                board.wait_for('GATE open',15,since=cur);pc=perf(board)['pc_id']
                start=time.monotonic();play_wav(cal['prompt']['path'],device);end=time.monotonic()
                def events():return [e for e in bot.events() if e['pc_id']==pc and e['t']>=start]
                stops=wait_until(lambda:[e for e in events() if e['event']=='vad-stop'],10)
                finals=wait_until(lambda:[e for e in events() if e['event']=='turn-transcript'],25)
                accuracy=speech_accuracy(PROMPT,finals[-1].get('text',''))
                board.wait_for('GATE open',20,since=cur)
                r=dict(spl=spl,calibration=cal,prompt_start=start,prompt_end=end,stops=stops,transcripts=finals,
                       word_accuracy=accuracy,passed=accuracy==1)
                result['runs'].append(r);save()
                assert accuracy==1,'Full prompt transcript accuracy failed'
                print(f'{spl} dB SPL: VAD and full prompt transcript pass',flush=True)
                if spl==58:
                    began=time.monotonic();floors=[]
                    for i in range(2):
                        with record_wav(a.out/f'silent-{i}.wav',device,60,16000) as rec:rec.wait()
                        floors.append(dbfs(pcm(a.out/f'silent-{i}.wav')))
                        print(f'silence {i+1}/2 captured',flush=True)
                    ev=[e for e in bot.events() if e['pc_id']==pc and e['t']>=began]
                    spurious=[e for e in ev if e['event']=='vad-start']
                    result['silence']=dict(seconds=time.monotonic()-began,jabra_floor_dbfs=floors,spurious_vad_events=spurious,passed=not spurious)
                    assert not spurious,'Spurious VAD in 120 s room silence'
                board.command('audio 0');board.command('disconnect',40)
            result['passed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:save()
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())

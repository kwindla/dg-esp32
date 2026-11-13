#!/usr/bin/env python3
"""Short character check:10 s idle +10 s offline speech. Run only on a released bench."""
import argparse
import json
import math
import os
from pathlib import Path
import time
import wave

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--flash',action='store_true')
    a=p.parse_args();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False)
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
                      OFFLINE_SKIP_GREETING='1',PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
    bank=a.out/'fixtures';bank.mkdir()
    with wave.open(str(ROOT/'server/fixtures/reply_pineapple.wav')) as w:
        assert (w.getnchannels(),w.getsampwidth(),w.getframerate())==(1,2,16000)
        pcm=w.readframes(w.getnframes())
    for name in ('tts_1.wav','tts_2.wav'):
        with wave.open(str(bank/name),'wb') as w:
            w.setparams((1,2,16000,0,'NONE','not compressed'))
            w.writeframes((pcm*((320000+len(pcm)-1)//len(pcm)))[:320000])
    os.environ['OFFLINE_BENCH_FIXTURE_DIR']=str(bank)
    from esp_serial import EspSerial
    from step9a_echoear import bench,verify,flash,check_firmware
    from step9a_assets import asset_server
    from step9a_support import read,host_address
    from step7_support import BotServer
    from demo_session import prepare_board,cleanup_board
    from step9a_echoear_cat import perf
    r=dict(passed=False,windows={})
    try:
        with bench():
            if a.flash:flash(a.out)
            port=verify(a.out)
            with BotServer(a.out/'bot','offline-test') as bot,asset_server(),EspSerial(port,log_path=a.out/'serial.log',display_checks=True) as b:
                try:
                    cursor=b.cursor;b.reset();b.wait_for('READY transport-console',60,since=cursor)
                    r['elf_sha256']=check_firmware(b,cursor)
                    r['setup']=prepare_board(b,'http://'+host_address()+':7861',a.out/'arena-budget.json')
                    cursor=b.cursor;b.command('connect',90);b.wait_for('STATE Ready',5,since=cursor)
                    b.command('audio 1');time.sleep(.5)
                    r['before']=perf(b)
                    def window(name):
                        rows=[read(b,'character-status','DISPLAY_CHARACTER')];start=time.monotonic()
                        for second in range(1,11):
                            time.sleep(max(0,start+second-time.monotonic()))
                            sample=read(b,'character-status','DISPLAY_CHARACTER');rows.append(sample)
                            delta=[v-u for u,v in zip(rows[-2]['changes'],sample['changes'])]
                            print(json.dumps(dict(window=name,second=second,layer_changes=delta,status=sample)),flush=True)
                        r['windows'][name]=rows
                    window('idle')
                    b.command('play-fixture tts_1.wav',15)
                    window('playback')
                    # Fixture dispatch is upstream of the jitter/DMA buffers.
                    # Observe the actual playout tail, rather than assuming it
                    # finishes exactly ten seconds after the command returns.
                    tail=[];quiet_since=None;deadline=time.monotonic()+2
                    while time.monotonic()<deadline:
                        sample=read(b,'character-status','DISPLAY_CHARACTER');tail.append(sample)
                        if sample['speaker_power']>1600:quiet_since=None
                        elif quiet_since is None:quiet_since=sample['device_us']
                        if quiet_since is not None and not sample['speaking'] and sample['frames'][0]==0:break
                        time.sleep(.02)
                    r['tail']=tail;r['settled']=tail[-1]
                    r['return_us']=None if quiet_since is None else tail[-1]['device_us']-quiet_since
                    r['after']=perf(b);r['final']=read(b,'status','DISPLAY')
                    idle=r['windows']['idle'];play=r['windows']['playback']
                    checks=dict(idle_mouth=idle[-1]['changes'][0]==idle[0]['changes'][0],
                                idle_blink=idle[-1]['blinks']>idle[0]['blinks'],
                                idle_whiskers=idle[-1]['twitches']>idle[0]['twitches'],
                                speaking_observed=any(s['speaking'] for s in play),
                                speaking_features=all(s['frames'][1:]==[1,0] for s in play if s['speaking']),
                                mouth_moves=play[-1]['changes'][0]>play[0]['changes'][0],
                                mouth_rate=all(y['changes'][0]-x['changes'][0]<=math.ceil((y['device_us']-x['device_us'])/100000)+1 for x,y in zip(play,play[1:])),
                                no_post_errors=all(s['errors']==0 for s in idle+play),
                                rendered_idle=idle[-1]['rendered']>idle[0]['rendered'],
                                rendered_playback=play[-1]['rendered']>play[0]['rendered'],
                                settled=not r['settled']['speaking'] and r['settled']['frames'][0]==0 and r['return_us'] is not None and r['return_us']<=200000,
                                dma=r['final']['dma']==[0,0],
                                alive=not r['final']['dead'] and not r['final']['failed'])
                    r['checks']=checks;r['passed']=all(checks.values())
                finally:
                    b.display_cleanup=True;r['cleanup_errors']=cleanup_board(b)
                    r['monitor']=b.display_monitor.result()
                    r['passed']=r['passed'] and r['monitor']['passed'] and not r['cleanup_errors']
    except KeyboardInterrupt:
        r.update(passed=False,interrupted=True,reason='Interrupted at user request')
    except Exception as exc:r['error']=f'{type(exc).__name__}: {exc}'
    finally:(a.out/'result.json').write_text(json.dumps(r,indent=2)+'\n')
    print(json.dumps({k:r.get(k) for k in ('passed','checks','error','interrupted')}),flush=True)
    return 0 if r['passed'] else 1


if __name__=='__main__':raise SystemExit(main())

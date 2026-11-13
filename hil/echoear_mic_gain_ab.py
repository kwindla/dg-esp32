#!/usr/bin/env python3
"""Quiet fixed-placement MIC1 30/33 dB comparison; hold the shared bench."""
import argparse, hashlib, json, re, time
from pathlib import Path
import numpy as np
from alsa import resolve_jabra,set_volumes,record_room_baseline,play_wav
from bench_calibration import dbfs,playback,record_prompt
from esp_serial import ROOT,EspSerial
from echoear_checks import codec_boot_evidence
from speaker_dual_sweep_test import dump_mic
from speaker_sweep_test import distortion
from step7_support import pcm

def snapshot(board):
    cursor=board.cursor;board.command('stats')
    text='\n'.join(l for _,_,l in board.lines_since(cursor))
    gain=re.search(r'MIC_GAIN_PATH analog_db=(\d+) reg=0x([0-9a-f]+) digital_percent=(\d+)',text)
    clips=re.search(r'AUDIO drops=\d+ read_errors=\d+ write_errors=\d+ clips=(\d+)',text)
    assert gain and clips,'Missing gain/clip readback'
    return dict(analog_db=int(gain[1]),reg=int(gain[2],16),digital_percent=int(gain[3]),clips=int(clips[1]))

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    result=dict(passed=False,points=[],physical_stimulus='Fixed 0.5 full-scale 1 kHz host tone and prompt scale0.8; ALSA PCM75/Mic80 unchanged')
    try:
        card,device=resolve_jabra();set_volumes(card,a.out/'mixer.log')
        result['room']=record_room_baseline(a.out/'room','mic analog A/B')
        if result['room']['noise_contaminated']:
            result['error']='Quiet room required';return 2
        with EspSerial(log_path=a.out/'serial.log') as board:
            cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
            text='\n'.join(l for _,_,l in board.lines_since(cursor))
            assert hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest() in text,'ELF mismatch'
            assert codec_boot_evidence(text)['passed'],'Startup readback'
            board.command('audio 0');board.command('quiet 1')
            original_gain=snapshot(board)
            source=ROOT/'hil/fixtures/prompt_speech.wav'
            tone=playback(a.out/'tone.wav',.5*32767*np.sin(2*np.pi*1000*np.arange(32000)/16000))
            try:
                for gain,digital in ((30,200),(33,142)):
                    out=a.out/f'gain{gain}';out.mkdir()
                    board.command(f'mic-analog-gain {gain}');board.command(f'mic-gain {digital}');time.sleep(.15)
                    before=snapshot(board)
                    assert before['reg']==0x10|gain//3 and before['digital_percent']==digital,'Wrong actual gain'
                    board.command('mic-record 1');time.sleep(1.5);board.command('mic-record 0');dump_mic(board,out/'floor.wav')
                    board.command('mic-record 1');time.sleep(.3);play_wav(tone,device);time.sleep(.3)
                    board.command('mic-record 0');dump_mic(board,out/'tone.wav')
                    raw=pcm(out/'tone.wav');window=raw[8000:24000]
                    metrics=distortion(out/'tone.wav',1000,.5,1.5)
                    prompt=record_prompt(board,device,out,source,.8,'prompt')
                    after=snapshot(board)
                    row=dict(analog_db=gain,digital_percent=digital,before=before,after=after,
                             floor_dbfs=dbfs(pcm(out/'floor.wav')),tone_rms_dbfs=dbfs(window),
                             tone_peak=int(np.max(np.abs(window))),tone_adc_clips=int(np.count_nonzero(np.abs(window)>=32767)),
                             tone=metrics,prompt=prompt,software_clip_delta=after['clips']-before['clips'])
                    row['tone_snr_db']=metrics['fundamental_dbfs']-metrics['noise_dbfs']
                    row['passed']=row['tone_adc_clips']==0 and row['software_clip_delta']==0 and row['tone_snr_db']>=20
                    result['points'].append(row)
                    (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                    print(f"gain={gain} reg=0x{before['reg']:02x} digital={digital}% floor={row['floor_dbfs']:.2f} tone={row['tone_rms_dbfs']:.2f} SNR={row['tone_snr_db']:.2f} clips={row['software_clip_delta']}",flush=True)
                # Prefer more headroom unless higher gain gives >=3 dB SNR benefit.
                good=[x for x in result['points'] if x['passed']]
                result['selected_analog_db']=(33 if len(good)==2 and good[1]['tone_snr_db']-good[0]['tone_snr_db']>=3 else good[0]['analog_db']) if good else None
                result['passed']=bool(good)
            finally:
                board.command(f"mic-analog-gain {original_gain['analog_db']}");board.command(f"mic-gain {original_gain['digital_percent']}")
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:(a.out/'result.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())

#!/usr/bin/env python3
"""Short EchoEar attenuation sweep; execute under hil/bench.py, never a soak."""
import argparse
import hashlib
import json
import math
import re
import time
from pathlib import Path
import numpy as np
from alsa import resolve_jabra, set_volumes, record_wav, record_room_baseline
from bench_calibration import dbfs
from esp_serial import ROOT, EspSerial
from echoear_checks import codec_boot_evidence
from speaker_sweep_test import distortion
from step7_support import pcm
def audio_snapshot(board):
    cursor=board.cursor;board.command('stats')
    text='\n'.join(l for _,_,l in board.lines_since(cursor))
    volume=re.search(r'OUTPUT_PATH volume_relative_db=(-?\d+) accepted_pcm_peak=(\d+)',text)
    errors=re.search(r'AUDIO drops=\d+ read_errors=\d+ write_errors=(\d+)',text)
    assert volume and errors,'Missing audio counters'
    return dict(output_volume_db=int(volume[1]),accepted_pcm_peak=int(volume[2]),i2s_write_errors=int(errors[1]))


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    result=dict(passed=False, points=[], thd_limit_percent=5., harmonic_limit_dbc=-26.,
                offsets_db=[0,-3,-6,-9,-12], default_below_max_db=-6,
                basis='1000 Hz raw tones at 11% (speech RMS near -22 dBFS) and 26% (DSP peak ceiling); attenuation after DSP; no absolute SPL')
    try:
        card,device=resolve_jabra();set_volumes(card,a.out/'mixer.log')
        result['room']=record_room_baseline(a.out/'room','volume sweep')
        if result['room']['noise_contaminated']:
            result['error']='Quiet room required for distortion qualification';return 2
        with EspSerial(log_path=a.out/'serial.log') as board:
            cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
            boot='\n'.join(l for _,_,l in board.lines_since(cursor))
            assert hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest() in boot,'ELF mismatch'
            assert codec_boot_evidence(boot)['passed'],'Codec readback'
            assert 'DAC_VOLUME=0xae' in boot and 'I2C_If: Fail' not in boot,'DAC register/startup'
            result['dac_register']=0xae;result['dac_attenuation_db']=-8.5
            result['before']=audio_snapshot(board)
            assert result['before']['output_volume_db']==-6,'Boot volume must be max -6 dB'
            board.command('audio 0');board.command('quiet 1')
            try:
                for amplitude in (11,26):
                    for offset in result['offsets_db']:
                        board.command(f'playback-volume {offset}')
                        path=a.out/f'tone-{amplitude}-db{offset}.wav'
                        with record_wav(path,device,3,16000) as rec:
                            time.sleep(.6);cursor=board.cursor
                            seq=board.send(f'tone 1000 1600 {amplitude}')
                            _,stamp,_=board.wait_for_entry(rf'^OK #{seq}$',5,since=cursor)
                            board.wait_for(rf'^DONE #{seq}$',5,since=cursor);rec.wait()
                        start=stamp-rec.ready_at+.3;end=stamp-rec.ready_at+1.35
                        x=pcm(path)[round(start*16000):round(end*16000)]
                        baseline=pcm(path)[1600:round((stamp-rec.ready_at-.1)*16000)]
                        row=dict(amplitude_percent=amplitude,relative_db=offset,path=str(path),
                                 window=[start,end],rms_dbfs=dbfs(x),peak_dbfs=20*math.log10(max(1,np.max(np.abs(x)))/32768),
                                 room_rms_dbfs=dbfs(baseline),digital_tone_peak_dbfs=20*math.log10(amplitude/100)+offset,
                                 digital_clipping_possible=False,**distortion(path,1000,start,end))
                        row['passed']=bool(row['thd_complete'] and row['thd_percent']<5 and row['clipping_count']==0
                            and row['harmonics_dbc']['H2']<=-26 and row['harmonics_dbc']['H3']<=-26
                            and row['rms_dbfs']-row['room_rms_dbfs']>=20)
                        stats_cursor=board.cursor
                        board.command('stats')
                        telemetry='\n'.join(l for _,_,l in board.lines_since(stats_cursor))
                        peak=re.search(r'OUTPUT_PATH volume_relative_db=(-?\d+) accepted_pcm_peak=(\d+)',telemetry)
                        assert peak and int(peak[1])==offset,'Missing digital peak readback'
                        row['accepted_pcm_peak']=int(peak[2])
                        row['accepted_pcm_peak_dbfs']=20*math.log10(max(1,int(peak[2]))/32768)
                        row['passed'] &= int(peak[2])<32767
                        result['points'].append(row)
                        (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                        print(f"amp={amplitude}% offset={offset}dB THD={row['thd_percent']:.3f}% RMS={row['rms_dbfs']:.2f} peak={row['peak_dbfs']:.2f} pass={row['passed']}",flush=True)
                result['after']=audio_snapshot(board)
                result['write_error_delta']=result['after']['i2s_write_errors']-result['before']['i2s_write_errors']
                qualified=[db for db in result['offsets_db'] if all(r['passed'] for r in result['points'] if r['relative_db']==db)]
                result['maximum_relative_db']=max(qualified) if qualified else None
                result['default_relative_db']=result['maximum_relative_db']-6 if qualified else None
                result['passed']=bool(qualified and result['write_error_delta']==0)
            finally:
                board.command('playback-volume -6')
    except Exception as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        (a.out/'result.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())

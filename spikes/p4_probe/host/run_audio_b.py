#!/usr/bin/env python3
"""Coordinator-only spike-B audio/touch campaign; wrap in run_guarded.py + bench."""
import argparse
import base64
import json
import math
import re
import struct
import sys
import threading
import wave
import zlib
from pathlib import Path
from console_rate import Board, boot_check
from build_evidence import collect
from hardware import require_bench
import measure_b as analysis

ROOT=Path(__file__).resolve().parents[3]


def checked(board, text, timeout=30):
    rows=board.command(text,timeout)
    analysis.records(rows)  # reject firmware ERR or damaged measurement records
    if text.startswith('gain '):
        _,mic,db=text.split()
        expected=f'GAIN mic={mic} db={db} rc=0'.encode()
        if [row for row in rows if row.startswith(b'GAIN ')]!=[expected]:
            raise ValueError('GAIN result missing, mismatched or rc nonzero')
    if text.startswith('vol '):
        results=[row for row in rows if row.startswith(b'VOL ')]
        if len(results)!=1 or not re.fullmatch(rb'VOL db=-?\d+(?:\.\d+)? rc=0',results[0]):
            raise ValueError('VOL result missing or rc nonzero')
    return rows


def acoustic_start(out):
    # Imported only with explicit --acoustic. Default campaigns never open an
    # ALSA device, play sound on the host, or depend on numpy/server packages.
    sys.path.insert(0,str(ROOT/'hil'))
    from alsa import resolve_jabra, set_volumes, play_wav
    card,device=resolve_jabra();set_volumes(card,out/'mixer.log')
    import array
    pcm=array.array('h',(int(6000*math.sin(2*math.pi*1000*i/16000)) for i in range(16000*10)))
    if sys.byteorder!='little': pcm.byteswap()
    path=out/'jabra-gain-burst.wav'
    with wave.open(str(path),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(pcm.tobytes())
    errors=[]
    def play():
        try: play_wav(path,device)
        except BaseException as exc: errors.append(exc)
    task=threading.Thread(target=play);task.start()
    return task,errors


def slot_campaign(board, out, acoustic=False):
    checked(board,'gain 1 30');checked(board,'gain 2 30');checked(board,'refgain 0');checked(board,'vol -6')
    silent=analysis.slots(analysis.records(checked(board,'slots 0 2000')))
    chirp=analysis.slots(analysis.records(checked(board,'slots 1 2000')))
    rise={ch:chirp['channels'][ch]['rms_dbfs']-silent['channels'][ch]['rms_dbfs'] for ch in silent['channels']}
    result=dict(wire_order=dict(MIC1='word0 high',MIC2='word1 high',MIC3='word0 low',MIC4='word1 low'),
        silence=silent,dac_only_chirp=chirp,rise_db=rise,pa_disabled_during_chirp=True,
        acoustic_requested=acoustic)
    try:
        checked(board,'gain 1 0');mic1=analysis.slots(analysis.records(checked(board,'slots 0 2000')))
        checked(board,'gain 1 30');checked(board,'gain 2 0');mic2=analysis.slots(analysis.records(checked(board,'slots 0 2000')))
        result.update(floor_gain1_reduced=mic1,floor_gain2_reduced=mic2,
            gain_discrimination=[analysis.gain_identity(silent,mic1,1),analysis.gain_identity(silent,mic2,2)])
    finally:
        checked(board,'gain 1 30');checked(board,'gain 2 30')
    if acoustic:
        task,errors=acoustic_start(out)
        try:
            baseline=analysis.slots(analysis.records(checked(board,'slots 2 1500')))
            checked(board,'gain 1 0');mic1=analysis.slots(analysis.records(checked(board,'slots 2 1500')))
            checked(board,'gain 1 30');checked(board,'gain 2 0');mic2=analysis.slots(analysis.records(checked(board,'slots 2 1500')))
            result.update(external_baseline=baseline,gain1_reduced=mic1,gain2_reduced=mic2,
                acoustic_gain_discrimination=[analysis.gain_identity(baseline,mic1,1,'external steady tone'),analysis.gain_identity(baseline,mic2,2,'external steady tone')])
        finally:
            task.join(15)
            checked(board,'gain 1 30');checked(board,'gain 2 30')
        if task.is_alive(): raise RuntimeError('Jabra playback did not finish')
        if errors: raise errors[0]
    return result


def upload(board, path):
    with wave.open(str(path),'rb') as stream:
        if (stream.getframerate(),stream.getnchannels(),stream.getsampwidth())!=(16000,1,2):
            raise ValueError('Playback WAV must be 16 kHz mono PCM16; no implicit resampling')
        if not 320<=stream.getnframes()<=30*16000: raise ValueError('Playback WAV length must be 20 ms through 30 s')
        raw=stream.readframes(stream.getnframes())
    if not 640<=len(raw)<=30*16000*2: raise ValueError('Playback WAV length must be 20 ms through 30 s')
    samples=[value[0] for value in struct.iter_unpack('<h',raw)]
    peak=max(abs(value) for value in samples)
    peak_dbfs=20*math.log10(max(1e-15,peak/32768))
    rms_dbfs=10*math.log10(max(1e-15,sum(value*value for value in samples)/len(samples)/32768**2))
    if peak>8230: raise ValueError(f'Playback WAV peak {peak_dbfs:.3f} dBFS exceeds -12 dBFS; attenuate explicitly')
    checked(board,f'pcm_begin {len(raw)//2}')
    for seq,offset in enumerate(range(0,len(raw),480)):
        block=raw[offset:offset+480]
        checked(board,f'pcm_data {seq} {zlib.crc32(block):08x} {base64.b64encode(block).decode()}')
    ready=analysis.one(analysis.records(checked(board,'pcm_end')),'pcm_ready')
    if ready['samples']!=len(samples): raise ValueError('Playback sample count mismatch')
    return dict(ready,path=str(path),peak_dbfs=peak_dbfs,rms_dbfs=rms_dbfs,peak=peak,level_policy='reject above -12 dBFS peak; no scaling')


def touch_campaign(board, codec):
    rows=checked(board,f'touch 8 {codec}')
    if codec: rows+=checked(board,'loop 6')
    def finished(row): return row.startswith(b'B ') and (b'"kind":"touch_end"' in row or b'"kind":"touch_error"' in row)
    if not any(finished(row) for row in rows): rows+=board.until(finished,20)
    # An absent touch device can finish before the audio loop; still collect and
    # validate the loop witness before advancing to another command/stage.
    if codec and not any(row.startswith(b'LOOP END ') for row in rows):
        rows+=board.until(lambda row:row.startswith(b'LOOP END '),10)
    return analysis.touch(analysis.records(rows),rows,require_loop=bool(codec))


def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--build',type=Path,required=True)
    parser.add_argument('--stage',choices=['all','slots','reference','aec','opus','touch'],default='all')
    parser.add_argument('--aec-seconds',type=int,choices=range(1,601),default=600,metavar='1..600')
    parser.add_argument('--ref-gain',type=int,choices=range(0,34,3),default=15,help='AEC MIC3 reference gain in dB; default production 15, or sweep-selected value')
    parser.add_argument('--speech-wav',type=Path,help='optional 16 kHz mono PCM16, peak <= -12 dBFS; AFE DAC loop and Opus input')
    parser.add_argument('--acoustic',action='store_true',help='explicit optional Jabra burst in addition to default floor-based gain discrimination')
    return parser.parse_args(argv)


def main():
    args=arguments();args.out.mkdir(parents=True,exist_ok=True);result={}
    try:
        result['build']=collect(args.build)
        require_bench()
        with (args.out/'serial.log').open('wb') as log, Board(log=log) as board:
            boot_check(board,result);board.switch_baud(2000000)
            result['peer']=analysis.one(analysis.records(checked(board,'peer')),'peer')
            if args.stage in ('all','slots'): result['slots']=slot_campaign(board,args.out,args.acoustic)
            if args.stage in ('all','reference'):
                result['reference_sweep']=[]
                for volume in (-20,-12,-6,0):
                    for gain in range(0,34,3):
                        rows=analysis.records(checked(board,f'level {volume} {gain}'))
                        register=analysis.one(rows,'refgain')
                        if register['reg']!=0x10|gain//3: raise ValueError('Reference gain readback mismatch')
                        result['reference_sweep'].append(dict(volume_db=volume,gain_db=gain,register=register,capture=analysis.slots(rows)))
                checked(board,'vol -6');checked(board,'refgain 0')
            if args.speech_wav and args.stage in ('all','aec','opus'):
                result['playback']=upload(board,args.speech_wav)
            if args.stage in ('all','aec'):
                checked(board,'vol -6');checked(board,f'refgain {args.ref_gain}')
                stimulus=1
                if args.speech_wav: stimulus=2
                result['aec']=analysis.aec(analysis.records(checked(board,f'aec {args.aec_seconds} {stimulus}',args.aec_seconds+90)),expected_gain=args.ref_gain)
            if args.stage in ('all','opus'): result['opus']=analysis.opus(analysis.records(checked(board,'opus 300',90)))
            if args.stage in ('all','touch'):
                result['touch']=[]
                for codec in (0,1):
                    result['touch'].append(touch_campaign(board,codec))
    except BaseException as exc:
        result['error']=f'{type(exc).__name__}: {exc}';raise
    finally:
        (args.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__': main()

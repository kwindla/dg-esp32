#!/usr/bin/env python3
"""Fixed four-utterance RMS target sweep, dual captures, STT and limiter/gate evidence."""
import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
import numpy as np
from scipy import signal
from alsa import resolve_jabra,set_volumes,record_wav
from bench_calibration import calibrate,active_mask,dbfs
from esp_serial import EspSerial,ROOT
from speaker_dual_sweep_test import dump_mic
from step7_support import BotServer,acoustic_match,pcm,write_pcm,transcribe,word_accuracy,leakage
from speech_accuracy import speech_accuracy
from acoustic_response import calibrate_response,predict
from transport_test import perf

BANDS=((500,1000),(1000,2000),(2000,4000),(4000,7000))

def band_sdr(clean,recorded):
    # Aligned clean-source SDR. Scalar least-squares fit within each band
    # removes pickup level but includes room response, mic/DAC coloration,
    # codec and time-varying speech gain in the residual. Not isolated THD.
    result={}
    for low,high in BANDS:
        sos=signal.butter(4,[low,high],btype='bandpass',fs=16000,output='sos')
        x=signal.sosfilt(sos,clean);y=signal.sosfilt(sos,recorded)
        g=float(np.dot(x,y)/max(np.dot(x,x),1))
        residual=y-g*x
        result[f'{low}-{high}']=float(10*np.log10(max(np.dot(g*x,g*x),1e-12)/max(np.dot(residual,residual),1e-12)))
    return result


def stats(board):
    cur=board.cursor;board.command('stats',10)
    text='\n'.join(l for _,_,l in board.lines_since(cur))
    m=re.search(r'LOUDNESS target_dbfs=(-?\d+) active_samples=(\d+) limiter_active_samples=(\d+) peak_exceedances=(\d+)',text)
    if not m:raise RuntimeError('Missing loudness DSP stats')
    return dict(zip(('target_dbfs','active','limited','peaks'),map(int,m.groups())))


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--bank',type=Path,required=True)
    p.add_argument('--targets',type=int,nargs='+',default=[-31,-28,-25,-22,-19]);p.add_argument('--ceiling',type=int,default=26);a=p.parse_args()
    a.out=a.out.resolve();a.bank=a.bank.resolve();a.out.mkdir(parents=True,exist_ok=False)
    os.environ['OFFLINE_BENCH_FIXTURE_DIR']=str(a.bank)
    manifest=json.loads((ROOT/'server/fixtures/manifest.json').read_text())
    manifest.update(json.loads((a.bank/'manifest.json').read_text()))
    result={'passed':False,'targets':[],'ceiling_percent':a.ceiling,'sdr_basis':'Independent unscored France-prompt FIR models linear placement response; per-band clean-source predictions remove coloration. Scalar fit removes level. Residual includes codec/gain changes, nonlinear distortion and noise; raw uncorrected SDR/correlation retained.'}
    def save():(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        card,device=resolve_jabra()
        with BotServer(a.out/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
            cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
            identity=board.wait_for('elf_sha256=',3,since=cur)
            result['elf_sha256']=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
            assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==result['elf_sha256']
            for target in a.targets:
                out=a.out/f'target{target}';out.mkdir()
                cal=calibrate(board,device,out/'calibration',65)
                cur=board.cursor;board.command('connect',90);board.command('audio 1');board.wait_for('GATE open',15,since=cur)
                pc=perf(board)['pc_id'];cal['response']=calibrate_response(board,device,out/'calibration'/'response');board.command('audio 1');board.command(f'playback-loudness {target} {a.ceiling}')
                row=dict(target_dbfs=target,calibration=cal,utterances=[]);result['targets'].append(row);save()
                for name in ('greeting.wav','reply_pineapple.wav','tts_1.wav','tts_2.wav'):
                    set_volumes(card,out/f'{name}-mixer.log')
                    ref=ROOT/'server/fixtures'/name if name in ('greeting.wav','reply_pineapple.wav') else a.bank/name
                    seconds=len(pcm(ref))/16000
                    before=stats(board);cursor=board.cursor
                    with record_wav(out/f'{name}-jabra.wav',device,int(np.ceil(seconds))+4,16000) as rec:
                        time.sleep(.8);board.command('mic-record 1');mic_start=time.monotonic();time.sleep(.2)
                        trigger=time.monotonic();board.command(f'play-fixture {name}',10)
                        _,first_close,_=board.wait_for_entry('GATE closed',10,since=cursor)
                        # Sentence pauses can reopen the half-duplex gate. Keep
                        # recording through the entire known fixed utterance.
                        time.sleep(max(0,first_close+seconds+.7-time.monotonic()))
                        board.command('mic-record 0');rec.wait()
                    after=stats(board);lines=board.lines_since(cursor)
                    dump_mic(board,out/f'{name}-board.wav')
                    jabra_raw=acoustic_match(out/f'{name}-jabra.wav',ref,trigger-rec.ready_at,rec.ready_at,trigger-rec.ready_at-.1)
                    jabra=acoustic_match(out/f'{name}-jabra.wav',ref,trigger-rec.ready_at,rec.ready_at,trigger-rec.ready_at-.1,reference_pcm=predict(pcm(ref),cal['response'],'jabra'))
                    board_raw=acoustic_match(out/f'{name}-board.wav',ref,.1,mic_start,.3)
                    raw=acoustic_match(out/f'{name}-board.wav',ref,.1,mic_start,.3,reference_pcm=predict(pcm(ref),cal['response'],'board'))
                    chains={}
                    for chain,match in (('board',raw),('jabra',jabra)):
                        recording=pcm(out/f'{name}-{chain}.wav');source=pcm(ref)
                        at=int(round(match['offset_s']*16000));segment=recording[at:at+len(source)]
                        mask=active_mask(source)
                        baseline=recording[:int((.2 if chain=='board' else trigger-rec.ready_at-.1)*16000)]
                        chains[chain]=dict(correlation=match['correlation'],raw_correlation=(board_raw if chain=='board' else jabra_raw)['correlation'],active_rms_dbfs=dbfs(segment[mask]),
                            room_floor_dbfs=dbfs(baseline),rise_db=dbfs(segment[mask])-dbfs(baseline),
                            raw_sdr_db=band_sdr(source,segment),aligned_sdr_db=band_sdr(predict(source,cal['response'],chain),segment),clips=int(np.count_nonzero(np.abs(recording)>=32767)))
                        if chain=='jabra':write_pcm(out/f'{name}-segment.wav',segment)
                    transcript=transcribe(out/f'{name}-segment.wav');raw_accuracy=word_accuracy(manifest[name]['text'],transcript);accuracy=speech_accuracy(manifest[name]['text'],transcript)
                    active=after['active']-before['active'];limited=after['limited']-before['limited'];peaks=after['peaks']-before['peaks']
                    leak=leakage(lines)
                    program_start=rec.ready_at+jabra['offset_s']
                    source=pcm(ref); mask=active_mask(source)
                    audible=[]
                    for sample in [x for x in lines if 'MIC_GATE ' in x[2]]:
                        index=int((sample[1]-program_start)*16000)
                        if 1600<=index<len(mask)-1600 and np.mean(mask[index-400:index+400])>.8:
                            m=re.search(r'closed=(\d+) pre=(\S+) post=(\S+)',sample[2])
                            audible.append(bool(m and m[1]=='1' and float(m[3])<=-119.9))
                    leak['audible_active_observations']=len(audible)
                    leak['audible_active_passed']=bool(audible and all(audible))
                    gates=leak['gates'];closed=next(t for t,s,_ in gates if s=='closed');opened=max(t for t,s,_ in gates if s=='open' and t>closed)
                    timing=closed<=jabra['onset']+.1 and opened>=jabra['end']-.1 and opened-jabra['end']<=2.3
                    spurious=[e for e in bot.events() if e['pc_id']==pc and e['event']=='vad-start' and trigger<=e['t']<=opened]
                    checks=dict(stt=accuracy==1,correlation=jabra['correlation']>=.6 and raw['correlation']>=.6,reshaped_fraction=active>0 and peaks/active<=.05,
                                band_sdr=all(chains['board']['aligned_sdr_db'][b]>=f for b,f in {'500-1000':10,'1000-2000':6,'2000-4000':0,'4000-7000':-6}.items()),
                                gate_timing=timing and leak['audible_active_passed'],no_barge_in=not spurious,no_adc_clips=all(c['clips']==0 for c in chains.values()),
                                room_rise=all(c['rise_db']>=10 for c in chains.values()))
                    checks={k:bool(v) for k,v in checks.items()}
                    utterance=dict(name=name,transcript=transcript,word_accuracy=accuracy,raw_word_accuracy=raw_accuracy,chains=chains,active_samples=active,
                        limiter_samples=limited,limiter_percent=100*limited/max(1,active),peak_percent=100*peaks/max(1,active),
                        leakage=leak,spurious_vad=spurious,checks=checks,passed=all(checks.values()))
                    row['utterances'].append(utterance);save()
                    print(f'{target} {name}: accuracy={accuracy:.3f}, corr={jabra["correlation"]:.3f}, limiter={utterance["limiter_percent"]:.2f}% checks={checks}',flush=True)
                row['passed']=all(u['passed'] for u in row['utterances']);save()
                board.command('audio 0');board.command('disconnect',40)
            result['passed']=True # Collection completion, per-target gates remain explicit.
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:save()
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())

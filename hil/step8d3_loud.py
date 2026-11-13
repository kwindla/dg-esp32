#!/usr/bin/env python3
"""One original calibrated France utterance at 75 and 80 dB estimated SPL."""
import argparse
import re
import time
import hashlib
import numpy as np
from scipy import signal
from pathlib import Path
from agent_conversation_test import boot,connect,write_json,credentials
from alsa import resolve_jabra,set_volumes,play_wav
from bench_calibration import calibrate,active_mask
from step7_support import pcm,write_pcm
from agent_prompts import fixture_transcript
from speech_accuracy import speech_accuracy
from speaker_dual_sweep_test import dump_mic
from step8d2_analysis import best_alignment
from bench_calibration import dbfs,SENSITIVITY_DBFS
from esp_serial import ROOT,EspSerial
from ethernet_bench import ethernet_bench
from step7_support import BotServer


def microphone_clips(board):
    cursor=board.cursor;board.command('stats',10)
    line=board.wait_for(r'MIC_STATS .*clips=(\d+)',5,since=cursor)
    return int(re.search(r'clips=(\d+)',line)[1])


def run(out,elf):
    credentials();out.mkdir(parents=True,exist_ok=False);result=dict(turns=[],completed=False)
    write_json(out/'result.json',result)
    # The loud-only PCM 11/11 setting provides 12 dB more than the standard
    # PCM 8/11 setting. Retain the original speech waveform and crest factor;
    # calibrate its physical level, then restore the qualified mixer.
    source=ROOT/'hil/fixtures/agent_conversation/france.wav'
    original=pcm(source)
    loud=out/'loud-france.wav';loud.write_bytes(source.read_bytes())
    transcript=fixture_transcript(loud,credentials()['DEEPGRAM_API_KEY'])
    accuracy=speech_accuracy('What is the capital of France?',transcript)
    result['stimulus']=dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        sha256=hashlib.sha256(loud.read_bytes()).hexdigest(),transcript=transcript,wer=1-accuracy,
        shaping='None; immutable original waveform, loud-only Jabra mixer and linear calibrated scale',
        source_crest_db=float(20*np.log10(max(abs(original))/np.sqrt(np.mean(original[active_mask(original)]**2)))),
        loud_crest_db=float(20*np.log10(max(abs(original))/np.sqrt(np.mean(original[active_mask(original)]**2)))))
    write_json(out/'result.json',result)
    if accuracy!=1:raise RuntimeError('Loud fixture WER must be zero')
    try:
        for spl in (75,80):
            directory=out/str(spl)
            with BotServer(directory/'bot','voice') as bot,EspSerial('/dev/ttyACM0',log_path=directory/'serial.log') as board:
                try:
                    identity=boot(board,elf);card,device=resolve_jabra();set_volumes(card,directory/'mixer.log')
                    calibration=calibrate(board,device,directory/'calibration',spl,source=loud,pcm_percent=100)
                    pc=connect(board);time.sleep(4)
                    set_volumes(card,directory/'loud-mixer.log',pcm_percent=100)
                    board.command('mic-record 1')
                    before=microphone_clips(board);start=time.monotonic();play_wav(calibration['prompt']['path'],device)
                    end=time.monotonic();after=microphone_clips(board);time.sleep(8)
                    board.command('mic-record 0');dump_mic(board,directory/'prompt-native.wav')
                    native=pcm(directory/'prompt-native.wav');reference=pcm(loud)
                    offset,correlation=best_alignment(native,reference)
                    actual=native[offset:offset+len(reference)];mask=active_mask(reference)
                    native_spl=94+dbfs(actual[mask])-SENSITIVITY_DBFS
                    filtered=signal.lfilter([.980742,-.980742],[1,-.980742],actual)
                    gained_peak=float(np.max(np.abs(filtered[mask]))*16)
                    predicted_clipping_spl=native_spl+20*np.log10(32767/max(gained_peak,1))
                    events=[e for e in bot.events() if e['pc_id']==pc and e['t']>=start]
                    row=dict(spl=spl,calibration=calibration,elf_sha256=identity,prompt_start=start,prompt_end=end,
                        mic_gain_percent=1600,device_clipped_samples=after-before,
                        server_clipped_samples=sum(e.get('clipped_samples',0) for e in events if e['event']=='input-peaks'),
                        native_input_spl=native_spl,native_alignment_correlation=correlation,
                        predicted_clipping_spl=predicted_clipping_spl,
                        clipping_projection_basis='Aligned contemporaneous native PCM, same 50 Hz HPF and gain 16; linear peak projection, not an independently measured threshold',
                        live_stt=' '.join(e['text'] for e in events if e['event']=='final-transcript'),events=events,
                        mixer='Loud-only PCM 11/11 (100%, +8 dB), Mic 6/7 (86%, +3 dB); restored to qualified PCM 8/11 after turn',
                        clipping_counter_basis='Device cumulative MIC_STATS delta brackets prompt; server input-peaks windows through 8 s post-prompt include gated silence')
                    result['turns'].append(row);write_json(directory/'result.json',row);write_json(out/'result.json',result)
                finally:
                    board.command('audio 0');board.command('disconnect',40)
                    set_volumes(resolve_jabra()[0],directory/'restored-mixer.log')
        result['completed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:write_json(out/'result.json',result)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');a=p.parse_args()
    with ethernet_bench(a.out.resolve().parent/'loud-ethernet-stun'):result=run(a.out.resolve(),a.elf.resolve())
    raise SystemExit(2 if result.get('error') else 0)

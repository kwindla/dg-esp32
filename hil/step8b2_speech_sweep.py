#!/usr/bin/env python3
"""Interleaved delivered-target campaign, including the uncompressed step-8 control.

The frozen unscored FIR removes linear response. Board correlation is
gated; Jabra correlation and Jabra STT and limiter duty are diagnostics. SDR against
rendered decoded PCM isolates residual acoustic distortion. Compression depth is
reported separately. Transport-damaged captures are excluded and recaptured.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
import numpy as np
from alsa import resolve_jabra,set_volumes,record_wav
from bench_calibration import calibrate,active_mask,dbfs
from esp_serial import EspSerial,ROOT
from speaker_dual_sweep_test import dump_mic
from step7_support import BotServer,acoustic_match,pcm,write_pcm,transcribe,word_accuracy,leakage
from speech_accuracy import speech_accuracy
from acoustic_response import calibrate_response,predict,predict_rendered,fractional_align
from playback_quality import transport_gate,acoustic_level_gate,jabra_level_gate,decoded_match
from transport_test import perf,dump
from step8b_speech_sweep import band_sdr

SDR_FLOORS={'500-1000':12.,'1000-2000':10.,'2000-4000':5.,'4000-7000':0.}
RESHAPED_MAX=.01

def quality_checks(chains,active,peaks):
    return dict(correlation=chains['board']['correlation']>=.6,
        reshaped_fraction=active>0 and peaks/active<=RESHAPED_MAX,
        band_sdr=all(chains['board']['distortion_sdr_db'][band]>=floor for band,floor in SDR_FLOORS.items()))

def distortion_metrics(path,source,prediction,first,ready,baseline):
    # Compression can move the best integer alignment by a sample. Each SDR
    # reference must establish its own integer timing before the declared
    # common fractional residual fit; no acoustic response/gain is refitted.
    match=acoustic_match(path,source,first,ready,baseline,reference_pcm=prediction)
    at=round(match['offset_s']*16000)
    segment=pcm(path)[at:at+len(prediction)]
    aligned,delay=fractional_align(prediction,segment)
    return dict(sdr_db=band_sdr(aligned,segment),integer_sdr_db=band_sdr(prediction,segment),
                integer_offset_samples=at,fractional_delay_samples=delay)


def stats(board):
    for attempt in range(3):
        before=board.cursor;p=perf(board)
        text='\n'.join(l for _,_,l in board.lines_since(before))
        m=re.search(r'LOUDNESS target_dbfs=(-?\d+) active_samples=(\d+) limiter_active_samples=(\d+) peak_exceedances=(\d+) lookahead_ms=(\d+) resets=(\d+)',text)
        if m:return dict(zip(('target','active','limited','peaks','delay_ms','resets'),map(int,m.groups())),perf=p)
        time.sleep(.05)
    raise RuntimeError('Missing loudness/reset stats after three retained snapshots')

def capture_program(board,bot,card,device,out,source,text,model,target,ceiling,renderer,configure=True,replay_history=None):
    out.mkdir();set_volumes(card,out/'mixer.log')
    cur=board.cursor;board.command('connect',90);board.wait_for('STATE Ready',5,since=cur)
    board.command('audio 1')
    if configure:board.command('playback-settings 33 26' if target is None else f'playback-loudness {target} {ceiling}')
    before=stats(board);pc=before['perf']['pc_id'];cursor=board.cursor
    seconds=len(pcm(source))/16000
    with record_wav(out/'jabra.wav',device,int(np.ceil(seconds))+4,16000) as rec:
        time.sleep(.8);board.command('mic-record 1');mic_start=time.monotonic();time.sleep(.2)
        trigger=time.monotonic();board.command(f'play-fixture {source.name}',10)
        _,first_close,_=board.wait_for_entry('GATE closed',10,since=cursor)
        time.sleep(max(0,first_close+seconds+.7-time.monotonic()))
        board.command('mic-record 0');rec.wait()
    after=stats(board);lines=board.lines_since(cursor)
    dump_mic(board,out/'board.wav');decoded=dump(board,out)
    replay_history=[] if replay_history is None else replay_history
    replay_history.append((out/'rx.pcm',out/'rendered-decoded.pcm'))
    cmd=[str(renderer),'--sequence',str(target if target is not None else 0),str(ceiling)]
    for raw_path,rendered_path in replay_history:cmd.extend((str(raw_path),str(rendered_path)))
    render_stats=json.loads(subprocess.check_output(cmd))
    rendered=np.fromfile(out/'rendered-decoded.pcm',dtype='<i2').astype(float)
    chains={};matches={};source_pcm=pcm(source)
    assert len(rendered)>=len(source_pcm),'Decoded program truncated'
    # dump-rx contains paced leading silence; locate the source in it before
    # predicting rendered audio. Use the same offset in decoded and render.
    from scipy import signal
    decoded_pcm=np.fromfile(out/'rx.pcm',dtype='<i2').astype(float)
    corr=signal.correlate(decoded_pcm,source_pcm,mode='valid',method='fft')
    render_at=int(np.argmax(corr));rendered=rendered[render_at:render_at+len(source_pcm)]
    assert len(rendered)==len(source_pcm)

    for chain in ('board','jabra'):
        first=.1 if chain=='board' else trigger-rec.ready_at
        ready=mic_start if chain=='board' else rec.ready_at
        baseline=.3 if chain=='board' else trigger-rec.ready_at-.1
        raw=acoustic_match(out/f'{chain}.wav',source,first,ready,baseline)
        match=acoustic_match(out/f'{chain}.wav',source,first,ready,baseline,reference_pcm=predict(source_pcm,model,chain))
        matches[chain]=match
        recording=pcm(out/f'{chain}.wav');at=round(match['offset_s']*16000);segment=recording[at:at+len(source_pcm)]
        prediction=predict(source_pcm,model,chain)
        aligned,fractional_delay=fractional_align(prediction,segment)
        distortion_prediction=predict_rendered(rendered,model,chain)
        distortion=distortion_metrics(out/f'{chain}.wav',source,distortion_prediction,first,ready,baseline)
        mask=active_mask(source_pcm);floor=recording[:round(baseline*16000)]
        chains[chain]=dict(correlation=match['correlation'],raw_correlation=raw['correlation'],
            offset_s=match['offset_s'],
            active_rms_dbfs=dbfs(segment[mask]),room_floor_dbfs=dbfs(floor),rise_db=dbfs(segment[mask])-dbfs(floor),
            distortion_sdr_db=distortion['sdr_db'],distortion_integer_sdr_db=distortion['integer_sdr_db'],
            distortion_offset_samples=distortion['integer_offset_samples'],distortion_delay_samples=distortion['fractional_delay_samples'],
            rendered_active_rms_dbfs=dbfs(rendered[mask]),
            aligned_sdr_db=band_sdr(aligned,segment),integer_aligned_sdr_db=band_sdr(prediction,segment),
            fractional_delay_samples=fractional_delay,raw_sdr_db=band_sdr(source_pcm,segment),
            clips=int(np.count_nonzero(np.abs(recording)>=32767)))
        write_pcm(out/f'{chain}-segment.wav',segment)
    transcript=transcribe(out/'board-segment.wav');accuracy=speech_accuracy(text,transcript)
    try:jabra_transcript=transcribe(out/'jabra-segment.wav')
    except Exception as exc:jabra_transcript=f'Unavailable: {type(exc).__name__}: {exc}'
    decoded_evidence=decoded_match(decoded_pcm,source_pcm)
    jabra_level=jabra_level_gate(pcm(out/'jabra-segment.wav'),chains['jabra']['rise_db'],model,target_dbfs=target)
    active=after['active']-before['active'];limited=after['limited']-before['limited'];peaks=after['peaks']-before['peaks']
    leak=leakage(lines);jabra=matches['jabra'];start=rec.ready_at+jabra['offset_s'];mask=active_mask(source_pcm)
    audible=[]
    for _,t,line in lines:
        index=int((t-start)*16000)
        if 'MIC_GATE ' in line and 1600<=index<len(mask)-1600 and np.mean(mask[index-400:index+400])>.8:
            m=re.search(r'closed=(\d+) pre=(\S+) post=(\S+)',line)
            audible.append(bool(m and m[1]=='1' and float(m[3])<=-119.9))
    gates=leak['gates'];closed=next(t for t,s,_ in gates if s=='closed');opened=max(t for t,s,_ in gates if s=='open' and t>closed)
    spurious=[e for e in bot.events() if e['pc_id']==pc and e['event']=='vad-start' and trigger<=e['t']<=opened]
    resets=[dict(host_t=t,line=l) for _,t,l in lines if 'DSP_RESET ' in l]
    # Resets in source silence are legitimate; distinguish them from jitter
    # dips during active phonation. All resets, including idle tails, are retained.
    active_resets=[]
    for r in resets:
        i=round((r['host_t']-start)*16000)
        if 0<=i<len(mask) and mask[i]:active_resets.append(r)
    checks=quality_checks(chains,active,peaks) if target is not None else dict(correlation=chains['board']['correlation']>=.6)
    checks.update(stt=accuracy==1,decoded_content=decoded_evidence['passed'],jabra_level=jabra_level['passed'],gate_timing=bool(audible and all(audible)) and closed<=jabra['onset']+.1 and opened>=jabra['end']-.1 and opened-jabra['end']<=2.3,
        no_barge_in=not spurious,no_adc_clips=all(c['clips']==0 for c in chains.values()),
        room_rise=all(c['rise_db']>=10 for c in chains.values()),no_active_resets=not active_resets)
    render=dict(stats=render_stats,firmware_stats=after)
    transport=transport_gate(before['perf'],after['perf'])
    checks['transport']=transport['passed']
    board.command('audio 0');board.command('disconnect',40)
    return dict(name=source.name,target_dbfs=target,transcript=transcript,word_accuracy=accuracy,
        raw_word_accuracy=word_accuracy(text,transcript),jabra_transcript_informational=jabra_transcript,decoded_content=decoded_evidence,jabra_level=jabra_level,chains=chains,before=before,after=after,active_samples=active,
        limiter_samples=limited,limiter_percent=100*limited/max(1,active),peak_percent=100*peaks/max(1,active),
        resets=resets,active_resets=active_resets,host_replay=render,leakage=leak,spurious_vad=spurious,
        transport=transport,acoustic_scoring_eligible=transport['passed'],
        checks={k:bool(v) for k,v in checks.items()},passed=all(checks.values()))

def rescore_campaign(campaign,out,bank):
    """Correct SDR timing on retained WAVs; never replace original evidence."""
    original=campaign/'result.json';result=json.loads(original.read_text())
    assert result['completed'],'Cannot rescore an incomplete capture campaign'
    out.mkdir(parents=True,exist_ok=False)
    result['analysis_correction']=dict(original_result=str(original),
        original_result_sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
        method='Retained WAVs, frozen original unscored FIR, unchanged gates and floors. Correct integer-only SDR with one shared +/-1 sample fractional delay fitted over 500-3500 Hz. Original pass flags and integer SDR retained.')
    for row in result['utterances']:
        name=row['name'];target=row['target_dbfs'];label='step8' if target is None else str(target)
        source=(ROOT/'server/fixtures'/name if not name.startswith('tts_') else bank/name)
        source_pcm=pcm(source);row['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
        for chain,data in row['chains'].items():
            path=campaign/f'{name}-{label}'/f'{chain}.wav'
            prediction=predict(source_pcm,result['response'],chain)
            if 'offset_s' not in data:
                match=acoustic_match(path,source,.1,0,.3,reference_pcm=prediction)
                assert abs(match['correlation']-data['correlation'])<1e-6,'Rescore changed correlation/segment'
                data['offset_s']=match['offset_s']
            at=round(data['offset_s']*16000);segment=pcm(path)[at:at+len(source_pcm)]
            aligned,delay=fractional_align(prediction,segment)
            data.update(integer_aligned_sdr_db=data['aligned_sdr_db'],aligned_sdr_db=band_sdr(aligned,segment),
                fractional_delay_samples=delay,recording_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        row['original_checks']=row['checks'].copy();row['original_passed']=row['passed']
        if target is not None:
            row['checks']['band_sdr']=all(row['chains']['board']['aligned_sdr_db'][b]>=f for b,f in result['thresholds']['board_band_sdr_db'].items())
        row['passed']=all(row['checks'].values())
    result['targets']=[dict(target_dbfs=t,passed=all(r['passed'] for r in result['utterances'] if r['target_dbfs']==t and r.get('acoustic_scoring_eligible',True)))
        for t in (None,-25,-22,-19,-16) if any(r['target_dbfs']==t for r in result['utterances'])]
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result['targets']))
    return 0

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--bank',type=Path,required=True)
    p.add_argument('--targets',type=int,nargs='+',default=[-25,-22]);p.add_argument('--ceiling',type=int,default=26)
    p.add_argument('--renderer',type=Path,help='Optional explicit renderer; default builds from production DSP and resolved sdkconfig');p.add_argument('--no-legacy',action='store_true')
    p.add_argument('--draws',type=int,default=2);p.add_argument('--temperatures',nargs='+',choices=['cold','warm'],default=['cold','warm'])
    p.add_argument('--rescore-from',type=Path,help='Correct SDR fractional timing on saved WAVs into a separate output');a=p.parse_args()
    if a.rescore_from:return rescore_campaign(a.rescore_from.resolve(),a.out.resolve(),a.bank.resolve())
    a.out=a.out.resolve();a.bank=a.bank.resolve();a.out.mkdir(parents=True,exist_ok=False)
    if a.renderer:a.renderer=a.renderer.resolve()
    else:
        a.renderer=a.out/'playback-dsp-render'
        subprocess.run(['g++','-std=c++17','-O2','-I'+str(ROOT/'components/dg_audio/include'),'-I'+str(ROOT/'build/config'),str(ROOT/'hil/playback_dsp_render.cpp'),str(ROOT/'components/dg_audio/src/playback_dsp.cpp'),'-o',str(a.renderer)],check=True)
    os.environ.update(OFFLINE_BENCH_FIXTURE_DIR=str(a.bank),OFFLINE_SKIP_GREETING='1')
    manifest=json.loads((ROOT/'server/fixtures/manifest.json').read_text());manifest.update(json.loads((a.bank/'manifest.json').read_text()))
    provenance={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in (ROOT/'hil/playback_dsp_render.cpp',ROOT/'components/dg_audio/src/playback_dsp.cpp',ROOT/'components/dg_audio/include/dg_audio/playback_dsp.h',ROOT/'build/config/sdkconfig.h')}
    result=dict(renderer_sha256=hashlib.sha256(a.renderer.read_bytes()).hexdigest(),renderer_sources_sha256=provenance,completed=False,utterances=[],ceiling_percent=a.ceiling,thresholds=dict(reshaped_fraction=RESHAPED_MAX,board_band_sdr_db=SDR_FLOORS,correlation=.6),
        threshold_judgment='Pre-declared fractional distortion SDR board floors 12/10/5/0 dB, exceedance <=1%, board modeled correlation >=0.6; board STT, decoded correlation >=0.95; Jabra rise >=3 dB and transferred board-mic SPL 60-85 dB, board gain over legacy within +/-2 dB of digital render. Every program >=2 clean draws, cold and warm; damage excluded and recaptured up to 3 attempts.',
        gate_basis='Frozen FIR trained on independent prompt_france. Distortion SDR predicts rendered decoded PCM through physical FIR only. Original-source SDR is compression-depth diagnostic. Each reference establishes independent integer timing with the unchanged 500-1500 Hz acoustic matcher, then one shared fractional residual +/-1 sample fitted over 500-3500 Hz. No response or envelope fitted; integer results retained.')
    def save():(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        card,device=resolve_jabra()
        with BotServer(a.out/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
            cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
            identity=board.wait_for('elf_sha256=',3,since=cur);result['elf_sha256']=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
            assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==result['elf_sha256']
            result['calibration']=calibrate(board,device,a.out/'calibration',65)
            board.command('connect',90);result['response']=calibrate_response(board,device,a.out/'response');board.command('disconnect',40);save()
            targets=([None] if not a.no_legacy else [])+a.targets
            programs=('greeting.wav','reply_pineapple.wav','tts_1.wav','tts_2.wav')
            for temperature in a.temperatures:
                for draw in range(a.draws):
                    for target in targets[draw%len(targets):]+targets[:draw%len(targets)]:
                        replay_history=[]
                        for index,name in enumerate(programs):
                            source=ROOT/'server/fixtures'/name if index<2 else a.bank/name
                            label='step8' if target is None else str(target)
                            for attempt in range(3):
                                configure=temperature=='cold' or index==0
                                if configure:replay_history=[]
                                row=capture_program(board,bot,card,device,a.out/f'{temperature}-{draw+1}-{name}-{label}-attempt{attempt+1}',source,manifest[name]['text'],result['response'],target,a.ceiling,a.renderer,configure,replay_history)
                                row.update(temperature=temperature,draw=draw+1,attempt=attempt+1)
                                result['utterances'].append(row);save()
                                print(f'{temperature} draw={draw+1} {label} {name}: STT={row["word_accuracy"]:.3f} Jabra={row["chains"]["jabra"]["correlation"]:.3f} transport={row["transport"]["classification"]} checks={row["checks"]}',flush=True)
                                if row['acoustic_scoring_eligible']:break
                            if not row['acoustic_scoring_eligible']:raise RuntimeError('Three transport-damaged attempts; no score available')
            # Compare each candidate to the matching temperature/draw legacy.
            for row in result['utterances']:
                if row['target_dbfs'] is None or not row['acoustic_scoring_eligible']:continue
                legacy=next((r for r in result['utterances'] if r['target_dbfs'] is None and r['name']==row['name'] and r['draw']==row['draw'] and r['temperature']==row['temperature'] and r['acoustic_scoring_eligible']),None)
                if legacy is None:
                    row['checks']['level']=False;row['passed']=False;row['level_gate']=dict(passed=False,error='Missing matching legacy control');continue
                row['level_gate']=acoustic_level_gate(row['chains']['board']['active_rms_dbfs']-legacy['chains']['board']['active_rms_dbfs'],row['chains']['board']['rendered_active_rms_dbfs']-legacy['chains']['board']['rendered_active_rms_dbfs'],control_room_rise_passed=legacy['checks'].get('room_rise',False))
                row['checks']['level']=row['level_gate']['passed'];row['passed']=all(row['checks'].values())
            result['targets']=[dict(target_dbfs=t,passed=all(r['passed'] for r in result['utterances'] if r['target_dbfs']==t and r.get('acoustic_scoring_eligible',True))) for t in targets]
            result['completed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:save()
    return 0 if result['completed'] else 1
if __name__=='__main__':raise SystemExit(main())

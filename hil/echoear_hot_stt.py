#!/usr/bin/env python3
"""One original -39/-27 dBFS prompt pair on the shared final ELF; score off-bench."""
import argparse,json,os,re,time
from contextlib import contextmanager
from pathlib import Path
import numpy as np
from scipy import signal
from alsa import resolve_jabra,set_volumes,record_room_baseline
from bench_calibration import record_prompt,echoear_gain_path
from echoear_acoustic_qualification import verify_plan,PROFILE
from esp_serial import ROOT,EspSerial
from e2e_common import perf
from mic_headroom import microphone_headroom
from playback_quality import microphone_clip_gate,transport_gate
from speech_accuracy import speech_accuracy,spoken_text
from step7_support import BotServer,pcm,write_pcm,transcribe

TARGETS=(-39,-27)
PCM_PERCENT=100  # Existing loud-test setting; 75% cannot reach -27 without host PCM overflow.


def word_errors(expected,actual):
    a=re.findall(r'\w+',spoken_text(expected).lower());b=re.findall(r'\w+',spoken_text(actual).lower())
    if not a:raise ValueError('Missing expected words')
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        nxt=[i]
        for j,y in enumerate(b,1):nxt.append(min(nxt[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=nxt
    return dict(edits=row[-1],words=len(a),wer=row[-1]/len(a),accuracy=speech_accuracy(expected,actual))


def align_prompt(source,observed):
    """Align actual decoded uplink; keep 100 ms guards, never resynthesize STT input."""
    if len(observed)<len(source):raise ValueError('Incomplete received prompt')
    sos=signal.butter(3,[300,3500],btype='bandpass',fs=16000,output='sos')
    x=signal.sosfilt(sos,source);y=signal.sosfilt(sos,observed)
    corr=signal.correlate(y,x,mode='valid',method='fft')
    power=signal.convolve(y*y,np.ones(len(x)),mode='valid',method='fft')
    normalized=corr/np.sqrt(np.maximum(power,1)*np.dot(x,x))
    at=int(np.argmax(normalized));quality=float(normalized[at])
    if not np.isfinite(quality) or quality<.6:raise ValueError('Received prompt alignment below 0.6')
    return observed[max(0,at-1600):min(len(observed),at+len(x)+1600)],dict(offset_samples=at,correlation=quality)


def pair_gate(rows):
    checks=dict(two_originals=len(rows)==2 and [r.get('target_dbfs') for r in rows]==list(TARGETS))
    for i,r in enumerate(rows):
        checks[f'{i}_acquisition']=r.get('acquisition_passed') is True
        checks[f'{i}_stt']=r.get('word_errors',{}).get('accuracy',0)>=.9
    if len(rows)==2:
        checks['hot_no_wer_regression']=rows[1].get('word_errors',{}).get('wer',float('inf'))<=rows[0].get('word_errors',{}).get('wer',-1)
        checks['baseline_unlimited']=rows[0].get('firmware_limited_delta')==0 and rows[0].get('headroom',{}).get('limited_samples')==0
        checks['hot_engaged']=rows[1].get('firmware_limited_delta',0)>0 and rows[1].get('headroom',{}).get('limited_samples',0)>0 and rows[1].get('headroom',{}).get('native_peak',0)>8192/1.25
    return dict(passed=all(checks.values()),checks=checks)


def calibrate_prompt(board,device,out,source,target):
    out.mkdir();board.command('audio 0');scale=.1;attempts=[]
    for i in range(5):
        row=record_prompt(board,device,out,source,scale,f'adjust-{i}',pcm_percent=PCM_PERCENT)
        attempts.append(row)
        (out/'attempts.json').write_text(json.dumps(attempts,indent=2)+'\n')
        if abs(row['active_rms_dbfs']-target)<=1:return row
        scale*=10**((target-row['active_rms_dbfs'])/20)
    raise RuntimeError('Prompt calibration missed target; no scored retake')


def enable_scored_capture(board):
    # With no greeting, the gate starts open and emits no transition event.
    # Connecting restores periodic mic telemetry; observe actual state, then
    # quiet it before the scored prompt and all endpoint PERF snapshots.
    cursor=board.cursor;board.command('audio 1')
    board.wait_for(r'MIC_GATE .*closed=0\b',15,since=cursor)
    board.command('quiet 1');time.sleep(.2)


@contextmanager
def scored_board(path):
    with EspSerial(log_path=path) as board:
        try:
            yield board
        finally:
            failures=[]
            for command in ('mic-record 0','audio 0','disconnect'):
                try:board.command(command,40)
                except Exception as exc:failures.append(f'{command}: {exc}')
            if failures:raise RuntimeError('Cleanup failed: '+'; '.join(failures))


def acquire(plan,out,targets=TARGETS,*,preflight=None):
    if not os.getenv('DG_BENCH_LOCK_FD'):raise RuntimeError('Run under bench.py after coordinating with B')
    frozen,elf=verify_plan(plan)
    out.mkdir(parents=True,exist_ok=False)
    os.environ.update(DG_BOARD='echoear',OFFLINE_SKIP_GREETING='1')
    os.environ.pop('OFFLINE_BENCH_FIXTURE_DIR',None)
    os.environ.pop('OFFLINE_BENCH_CONTINUOUS_AUDIO',None)
    source=ROOT/'hil/fixtures/prompt_speech.wav'
    expected=json.loads((ROOT/'server/fixtures/manifest.json').read_text())['prompt_france.wav']['text']
    result=dict(passed=False,qualification=False,qualification_plan=frozen,expected=expected,points=[],no_retakes=True,pcm_percent=PCM_PERCENT,
        scoring='Actual received Opus-decoded uplink, Deepgram nova-3 prerecorded; same prompt/recognizer at both levels; not a live e2e campaign')
    try:
        card,device=resolve_jabra();set_volumes(card,out/'mixer.log',pcm_percent=PCM_PERCENT)
        for target in targets:
            point=out/f'native{target}';point.mkdir()
            room=record_room_baseline(point/'room','hot STT')
            if room['noise_contaminated'] and not preflight:raise RuntimeError('Quiet room required; no bypass')
            with BotServer(point/'bot') as bot,scored_board(point/'serial.log') as board:
                cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
                board.wait_for(frozen['elf_sha256'],3,since=cur)
                from echoear_checks import codec_boot_evidence
                boot='\n'.join(line for _,_,line in board.lines_since(cur))
                assert codec_boot_evidence(boot)['passed'] and 'I2C_If: Fail' not in boot,'Codec startup failed'
                board.command('quiet 1');board.command('audio 0')
                gain=echoear_gain_path(board)
                assert (gain['analog_db'],gain['digital_percent'],gain['protection'])==(30,125,PROFILE),'Wrong gain/protection'
                cal=calibrate_prompt(board,device,point/'calibration',source,target)
                from step9a_support import host_address
                board.command('server-url http://'+host_address()+':7861')
                cur=board.cursor;board.command('connect',90);enable_scored_capture(board)
                if preflight:preflight(board,device,point)
                before=perf(board);gain_before=echoear_gain_path(board)
                scored=record_prompt(board,device,point,source,cal['scale'],'scored',pcm_percent=PCM_PERCENT)
                after=perf(board);gain_after=echoear_gain_path(board)
                assert not re.search(r'Guru Meditation|assert failed|abort\(|Task watchdog|SESSION FAIL|I2C_If: Fail','\n'.join(line for _,_,line in board.lines_since(cur))),'Device/session failure'
                board.command('audio 0');board.command('disconnect',40)
                events=bot.events();peers={e['pc_id'] for e in events if e.get('pc_id')}
                head=microphone_headroom(pcm(scored['raw_path']),125,PROFILE)
                clips=microphone_clip_gate(before,after);transport=transport_gate(before,after)
                row=dict(target_dbfs=target,room=room,calibration=cal,scored=scored,gain_before=gain_before,gain_after=gain_after,
                    firmware_limited_delta=gain_after['limited_samples']-gain_before['limited_samples'],headroom=head,
                    clips=clips,transport=transport,before=before,after=after,peer_ids=sorted(peers))
                row['acquisition_passed']=bool(abs(scored['active_rms_dbfs']-target)<=1 and head['passed'] and clips['passed'] and transport['passed'] and all(v==0 for v in transport['deltas'].values()) and len(peers)==1 and gain_after['adc_clips']==gain_before['adc_clips'])
                result['points'].append(row)
                (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
            # Bot exit closes the actual received PCM WAV, independent of local reconstruction.
            paths=list((point/'bot/input').glob('*.wav'))
            assert len(paths)==1,'Exactly one received audio stream required'
            native=pcm(scored['raw_path']);at=scored['offset_samples']
            native_prompt=native[at:at+len(pcm(source))]
            assert len(native_prompt)==len(pcm(source)),'Native prompt truncated'
            # The received uplink contains this actual acoustic transfer, not
            # the dry source waveform. Keep the same correlation threshold.
            segment,alignment=align_prompt(native_prompt,pcm(paths[0]))
            upload=point/'received-prompt.wav';write_pcm(upload,segment)
            row.update(received_path=str(paths[0]),stt_path=str(upload),alignment=alignment,alignment_basis='Paired native acoustic prompt versus actual received uplink')
            (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
            if not row['acquisition_passed']:break
        result['acquisition_complete']=len(result['points'])==len(targets) and all(r['acquisition_passed'] for r in result['points'])
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def score(out):
    # r8 (Astra r7 #2): every scoring write is atomic, so an interrupt during any of them leaves the last valid
    # scored record (scored marker, transcripts, word errors, then the pair verdict once computed) on disk.
    from atomic_json import atomic_write_json
    path=out/'result.json';result=json.loads(path.read_text())
    if result.get('scored'):raise RuntimeError('Do not retake STT requests or overwrite scored evidence')
    if result.get('stage') or result.get('scoring_failed'):
        # r9 (Fable r8 N2b): a record finalised by execute() is never (re)scored by hand.
        raise RuntimeError('Record finalised by execute(); never score a launcher attempt by hand')
    result['scored']=True
    # Persist before network requests, retaining partial results on API failure.
    atomic_write_json(path,result)
    try:
        for row in result['points']:
            row['transcript']=transcribe(row['stt_path'])
            row['word_errors']=word_errors(result['expected'],row['transcript'])
            atomic_write_json(path,result)
        result['pair_gate']=pair_gate(result['points']);result['passed']=bool(result.get('acquisition_complete') and result['pair_gate']['passed'])
        atomic_write_json(path,result)   # the pair verdict is durable before the final write
    finally:atomic_write_json(path,result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--score',action='store_true',help='After bench release, score retained actual uplink once')
    p.add_argument('--target',type=int,choices=TARGETS,help='Acquire only an outstanding original level; never repeat a scored level')
    a=p.parse_args()
    if a.score:r=score(a.out)
    else:
        if not a.plan:p.error('--plan required')
        r=acquire(a.plan,a.out,(a.target,) if a.target is not None else TARGETS)
    print(json.dumps(dict(passed=r['passed'],acquisition_complete=r.get('acquisition_complete'),pair_gate=r.get('pair_gate'),error=r.get('error'))))
    raise SystemExit(0 if r.get('passed') or (not a.score and r.get('acquisition_complete')) else 1)

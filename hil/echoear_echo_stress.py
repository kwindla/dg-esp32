#!/usr/bin/env python3
"""Prepare immutable loud TTS, then measure echo at volume0 in one short hold."""
import argparse,hashlib,io,json,math,os,time,wave
from pathlib import Path
import numpy as np
from esp_serial import ROOT,EspSerial
from step7_support import BotServer,deepgram_key,pcm,write_pcm
from bench_calibration import calibrate,echoear_gain_path,dbfs,active_mask
from mic_headroom import microphone_headroom
from playback_quality import microphone_clip_gate,transport_gate
from e2e_common import perf
from speaker_dual_sweep_test import dump_mic
from alsa import resolve_jabra,set_volumes,record_room_baseline,record_wav

TEXTS=(
    'Pineapple. Please pack the bright blue backpack, bring the big paper map, and place both books beside the purple box. Peter picked a perfect pineapple. Now repeat the clear announcement: please bring the blue backpack and the big paper map. The capital of France is Paris. Pineapple.',
    'Pineapple. All aboard the morning train. Over the open ocean, our voices carry a long and steady announcement. One, two, three, four, five, six, seven, eight, nine, ten. Welcome home, everyone. Please remain calm and enjoy the journey over the ocean. The capital of France is Paris. Pineapple.')


def cleanup_stress_board(board):
    failures=[]
    for cmd in ('mic-record 0','audio 0','playback-volume -6','disconnect','disp bench off'):
        try:board.command(cmd,40)
        except Exception as exc:failures.append(cmd+': '+str(exc))
    return failures


def prepare(out):
    import requests
    out.mkdir(parents=True,exist_ok=False);manifest={}
    key=deepgram_key()
    if not key:raise RuntimeError('TTS preparation requires approved credentials')
    for i,text in enumerate(TEXTS,1):
        response=requests.post('https://api.deepgram.com/v1/speak',params=dict(model='aura-2-thalia-en',encoding='linear16',container='wav',sample_rate=16000),headers={'Authorization':'Token '+key},json=dict(text=text),timeout=45)
        if response.status_code!=200:raise RuntimeError(f'TTS HTTP {response.status_code}')
        with wave.open(io.BytesIO(response.content)) as w:
            assert (w.getframerate(),w.getnchannels(),w.getsampwidth())==(16000,1,2)
            x=np.frombuffer(w.readframes(w.getnframes()),'<i2').astype(float)
        assert 12<len(x)/16000<25,'Stress phrase must be longer than reference reply and fit30s native capture'
        write_pcm(out/f'original-{i}.wav',x)
        scale=min(4.,32700/max(1,float(np.max(np.abs(x)))))
        assert scale>1,'Source has no room for a non-clipping increase'
        name=f'tts_{i}.wav';write_pcm(out/name,np.rint(x*scale))
        y=pcm(out/name);mask=active_mask(x)
        manifest[name]=dict(text=text,voice='aura-2-thalia-en',seconds=len(x)/16000,
            source_boost_db=20*math.log10(scale),original_active_rms_dbfs=dbfs(x[mask]),
            active_rms_dbfs=dbfs(y[mask]),peak=int(np.max(np.abs(y))),
            sha256=hashlib.sha256((out/name).read_bytes()).hexdigest())
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def run(out,bank,budget,*,plan,calibrate_prompt=True,preflight=None,arms=None):
    if not os.getenv('DG_BENCH_LOCK_FD'):raise RuntimeError('Run under bench.py; coordinate with B')
    from echoear_acoustic_qualification import verify_plan
    frozen,elf=verify_plan(plan)
    assert budget.resolve()==Path(frozen['budget']).resolve(),'Use the shared plan budget'
    manifest=json.loads((bank/'manifest.json').read_text())
    for path in (bank/'manifest.json',*(bank/name for name in manifest)):
        assert str(path.resolve()) in frozen['reference_hashes'],'Stress bank must be frozen in shared plan'
    for name,m in manifest.items():assert hashlib.sha256((bank/name).read_bytes()).hexdigest()==m['sha256']
    os.environ.update(DG_BOARD='echoear',OFFLINE_BENCH_FIXTURE_DIR=str(bank),OFFLINE_SKIP_GREETING='1',
        STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),STEP9A_BUDGET_PATH=str(budget),
        RTP_EVIDENCE_DIR=str(out/'rtp'),STEP8E_EVENT_LOOP_PROBE='1')
    from step9a_assets import asset_server
    from step9a_support import setup
    out.mkdir(parents=True,exist_ok=False);result=dict(qualification=False,passed=False,points=[],manifest=manifest,qualification_plan=frozen)
    (out/'image.json').write_text(json.dumps(dict(elf_path=str(elf),elf_sha256=frozen['elf_sha256'],qualification=False),indent=2)+'\n')
    try:
        card,device=resolve_jabra();set_volumes(card,out/'mixer.log')
        result['room']=record_room_baseline(out/'room','echo robustness')
        if result['room']['noise_contaminated'] and not preflight:raise RuntimeError('Quiet room required; no diagnostic noise bypass')
        with asset_server(),BotServer(out/'bot') as bot,EspSerial(log_path=out/'serial.log',display_checks=True) as board:
            cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
            board.wait_for(frozen['elf_sha256'],3,since=cursor)
            result['display_setup']=setup(board,mode='cat')
            board.command('playback-volume 0')
            if calibrate_prompt:result['calibration']=calibrate(board,device,out/'calibration')
            result['gain']=echoear_gain_path(board)
            assert result['gain']['analog_db']==30 and result['gain']['digital_percent']==125,'Wrong frozen gain'
            try:
                board.command('connect',90);board.command('audio 1');board.command('quiet 1')
                # r15 (decision 138b): a continuation acquires only the remaining arms, in manifest order.
                selected={n:m for n,m in manifest.items() if arms is None or n in arms}
                if arms is not None and list(selected)!=list(arms):raise ValueError('Continuation arms must be a manifest-order subset')
                for name,m in selected.items():
                    point=out/Path(name).stem;point.mkdir()
                    if preflight:preflight(board,device,point)
                    before=perf(board);gain_before=echoear_gain_path(board)
                    with record_wav(point/'jabra.wav',device,math.ceil(m['seconds'])+4) as rec:
                        time.sleep(.4);board.command('mic-record 1');started=time.monotonic()
                        cursor=board.cursor;board.command('play-fixture '+name,20)
                        board.wait_for('GATE closed',8,since=cursor)
                        closed_cursor=board.cursor
                        time.sleep(m['seconds']+1)
                        board.wait_for('GATE open',8,since=closed_cursor)
                        board.command('mic-record 0');rec.wait()
                    after=perf(board);gain_after=echoear_gain_path(board);dump_mic(board,point/'native.wav')
                    raw=pcm(point/'native.wav');jabra=pcm(point/'jabra.wav')
                    assert m['seconds']+.1<len(raw)/16000<29.99,'Native recording incomplete or hit 30-second capacity'
                    head=microphone_headroom(raw,result['gain']['digital_percent'],result['gain']['protection'])
                    # Active20ms windows provide a level diagnostic, not a SPL claim.
                    level=dbfs(jabra[active_mask(jabra)])
                    row=dict(fixture=name,headroom=head,native_peak=int(np.max(np.abs(raw))),
                        gain_before=gain_before,gain_after=gain_after,firmware_limited_delta=gain_after['limited_samples']-gain_before['limited_samples'],
                        jabra_active_rms_dbfs=level,jabra_peak=int(np.max(np.abs(jabra))),
                        clip_gate=microphone_clip_gate(before,after),transport=transport_gate(before,after),
                        before=before,after=after,started=started,native_seconds=len(raw)/16000)
                    row['passed']=head['passed'] and row['clip_gate']['passed'] and row['transport']['passed'] and all(v==0 for v in row['transport']['deltas'].values()) and after['output_volume_db']==0
                    result['points'].append(row);(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                    print(name,dict(passed=row['passed'],headroom_db=head['headroom_db'],unprotected_peak=head['unprotected_peak'],limited_samples=head['limited_samples']),flush=True)
                    if not row['passed']:break
                result['display_monitor']=board.display_monitor.result()
                result['passed']=len(result['points'])==len(selected) and all(r['passed'] for r in result['points']) and result['display_monitor']['passed']
            finally:
                result['cleanup_errors']=cleanup_stress_board(board)
                if result['cleanup_errors']:raise RuntimeError('Cleanup failed: '+'; '.join(result['cleanup_errors']))
    except Exception as exc:result.update(passed=False,error=f'{type(exc).__name__}: {exc}')
    finally:(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--prepare',action='store_true')
    p.add_argument('--out',type=Path,required=True);p.add_argument('--bank',type=Path);p.add_argument('--budget',type=Path)
    p.add_argument('--plan',type=Path,help='Same final frozen plan as B and acoustic qualification')
    a=p.parse_args()
    if a.prepare:print(json.dumps(prepare(a.out),indent=2))
    else:
        if not a.bank or not a.budget or not a.plan:p.error('--bank, --budget and --plan required')
        raise SystemExit(0 if run(a.out,a.bank.resolve(),a.budget.resolve(),plan=a.plan)['passed'] else 1)

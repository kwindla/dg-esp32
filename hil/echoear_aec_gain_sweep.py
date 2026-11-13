#!/usr/bin/env python3
"""Predeclared gain trials, one serialized hold; retain all failed attempts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import time
import traceback

import numpy as np
from echoear_aec_bench import (ROOT, EVIDENCE, NORMAL, NORMAL_HASH, bench_lock, verify_bundle,
    flash, boot, sha, write, clock_anchor, jabra_card_zero, read_wav, write_wav, analyze, transcribe_afe, load_capture)
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45 as NORMAL, HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore
from echoear_aec_capture import dump_capture
from echoear_aec_resource_trial import command, snapshot
from alsa import resolve_jabra, play_wav
from step7_support import BotServer
from step9a_support import host_address
from echoear_aec_metrics import RATE, dbfs, frame_power, reference_threshold
from echoear_aec_noise_gate import validate_noise_gate, quiet_gate, NoiseRetryQueue


class NoiseContaminated(Exception):
    pass


def validate(plan):
    validate_noise_gate(plan.get('noise_gate'))
    if plan['stage'] not in ('screen', 'finalists', 'controls'):
        raise ValueError('Unknown gain stage')
    ids = [r['run_id'] for r in plan['trials']]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError('Unique immutable trial identities required')
    for r in plan['trials']:
        if not r['run_id'].replace('-', '').replace('_', '').isalnum():
            raise ValueError('Invalid trial identity')
        if type(r['gain_db']) is not int or r['gain_db'] not in range(0, 34, 3):
            raise ValueError('Gain must be on 0..33 dB ladder')
        if r['volume_db'] not in (0, -6) or r['state'] not in ('cold', 'warm'):
            raise ValueError('Invalid volume/adaptation state')
        if r['mode'] not in ('speech', 'probe', 'near', 'doubletalk'):
            raise ValueError('Unsupported independent witness')
    if plan.get('resume_checkpoint'):
        checkpoint = plan['resume_checkpoint']
        source = Path(checkpoint['path'])
        if sha(source) != checkpoint['sha256']:
            raise ValueError('Resume checkpoint hash mismatch')
        prior = json.loads(source.read_text())
        expected = [(c['cell_id'], c['attempt']) for c in prior['pending_cells']]
        actual = [(t['run_id'], t.get('start_attempt', 1)) for t in plan['trials']]
        if actual != expected:
            raise ValueError('Resume must preserve every pending cell, order and attempt')
    elif any(t.get('start_attempt', 1) != 1 for t in plan['trials']):
        raise ValueError('Resuming attempts requires a frozen checkpoint')
    if plan.get('noise_gate'):
        NoiseRetryQueue(plan['trials'], plan['noise_gate'])  # validate before any hardware access
    if plan['stage'] == 'screen':
        cells = [(r['gain_db'], r['volume_db']) for r in plan['trials'] if r['mode'] == 'speech']
        if any(cells.count((g, v)) != 2 for g in (0, 15, 24, 30) for v in (0, -6)) or len(cells) != 16:
            raise ValueError('Screen requires the complete 4 x 2 x 2 speech grid')
    if plan['stage'] == 'finalists':
        gains = {r['gain_db'] for r in plan['trials']}
        cells = [(r['gain_db'], r['volume_db'], r['state']) for r in plan['trials'] if r['mode'] == 'speech']
        if not 1 <= len(gains) <= 2 or len(cells) != len(gains)*12 or any(
            cells.count((g,v,s)) != 3 for g in gains for v in (0,-6) for s in ('cold','warm')):
            raise ValueError('Finalists require cold/warm x two volumes x three repetitions')


def run(plan, out):
    from step9a_echoear import check_esptool
    validate(plan); check_esptool()
    if not out.resolve().is_relative_to(EVIDENCE/'step-3'):
        raise ValueError('Evidence must be inside aec-demo/step-3')
    out.mkdir(exist_ok=False)
    write(out/'declaration.json', plan)
    image=Path(plan['image']); image_hash=plan['elf_sha256']
    verify_bundle(image,image_hash,plan.get('boot_ref_gain_db')); verify_bundle(NORMAL,NORMAL_HASH)
    write(out/'identity.json', dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_sha256=sha(Path(__file__)), noise_gate_source_sha256=sha(ROOT/'hil/echoear_aec_noise_gate.py'), host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        versions={n:importlib.metadata.version(n) for n in ('pipecat-ai','aiortc','numpy','scipy')}))
    bank=out/'fixtures';bank.mkdir()
    far=np.tile(read_wav(ROOT/'server/fixtures/greeting.wav'),4)
    write_wav(bank/'tts_1.wav',far);write_wav(bank/'tts_2.wav',far)
    write(out/'fixture-identity.json',dict(far_sha256=sha(bank/'tts_1.wav'),far_samples=len(far),
        near_sha256=sha(ROOT/'server/fixtures/prompt_france.wav')))
    policy=plan.get('noise_gate')
    queue=NoiseRetryQueue(plan['trials'],policy) if policy else None
    result=dict(completed=False,trials=[],cleanup_errors=[]);board=bot=None
    baseline_dbfs=None
    with bench_lock():
        _,device=resolve_jabra();jabra_card_zero()
        subprocess.run(['alsactl','-f',str(out/'mixer-before.state'),'store','0'],check=True,capture_output=True)
        try:
            flash(image,image_hash,out,'candidate',plan.get('boot_ref_gain_db'))
            if policy:
                baseline=out/'settled-baseline';baseline.mkdir()
                board=boot(baseline,'baseline',image_hash)
                for text in ('demo hold','audio 0','quiet 1','gate auto',
                             f"aec-ref-gain {plan['trials'][0]['gain_db']}"):
                    command(board,text,40)
                result['baseline_attempts']=[]
                for baseline_attempt in range(1,4):
                    take=baseline/f'attempt-{baseline_attempt}';take.mkdir()
                    time.sleep(policy['settle_seconds'])
                    command(board,'aec-record 1');time.sleep(policy['baseline_seconds']);command(board,'aec-record 0')
                    dump_capture(board,take/'capture.npz')
                    raw,afe,ref,blocks=load_capture(take/'capture.npz');baseline_dbfs=dbfs(raw)
                    check=quiet_gate(raw,baseline_dbfs,policy)
                    check.update(afe_dbfs=dbfs(afe),mic3_dbfs=dbfs(ref),gap_blocks=int(sum(blocks[:,4])),
                                 capture=str((take/'capture.npz').relative_to(out)))
                    check['passed'] &= baseline_dbfs <= policy['baseline_max_dbfs'] and not check['gap_blocks']
                    write(take/'noise-gate.json',check);result['baseline_attempts'].append(check)
                    if check['passed']:break
                result['baseline']=check
                if not check['passed']:
                    result['paused_reason']='No valid settled session baseline after three declared attempts'
                    return result
            os.environ.update(OFFLINE_SKIP_GREETING='1',OFFLINE_BENCH_FIXTURE_DIR=str(bank),
                ECHOEAR_AEC_FULL_DUPLEX='1',ECHOEAR_AEC_SESSION_DIR=str(out/'sessions'))
            os.environ.pop('ECHOEAR_AEC_EVALUATION_DIR',None)
            bot=BotServer(out/'bot',mode='offline-test');bot.start()
            pending=iter((t,1) for t in plan['trials'])
            while True:
                item=queue.next() if queue else next(pending,None)
                if item is None:break
                cell,attempt=item
                trial=dict(cell,run_id=f"{cell['run_id']}-a{attempt}") if policy else cell
                folder=out/trial['run_id'];folder.mkdir()
                declaration=dict(trial,far_samples=len(far),interruption_disabled=True,
                    conditioning='one entire 10.55975 s speech fixture plus 1 s tail' if trial['state']=='warm' else 'reboot; no far stimulus before scored capture')
                if policy:declaration.update(cell_id=cell['run_id'],attempt=attempt,noise_gate=policy,
                                             session_baseline_dbfs=baseline_dbfs)
                write(folder/'declaration.json',declaration)
                outcome=dict(run_id=trial['run_id'],acquisition_complete=False,failed=True)
                anchors=[]
                try:
                    if board:board.close();board=None
                    board=boot(folder,'trial',image_hash)  # hard reset for every trial, including warm before conditioning
                    for text in ('demo hold','audio 0','quiet 1','gate auto',
                                 f"playback-volume {trial['volume_db']}",f"aec-ref-gain {trial['gain_db']}"):
                        command(board,text,40)
                    command(board,'aec-ref-gain-status')
                    time.sleep(policy['settle_seconds'] if policy else 1)
                    # Capture beyond the requested window so asynchronous 20 ms
                    # framing cannot leave a nominal 2 s sleep with only 1.98 s.
                    quiet_seconds=policy['quiet_seconds'] if policy else 2
                    command(board,'aec-record 1');time.sleep(quiet_seconds+.1);command(board,'aec-record 0')
                    dump_capture(board,folder/'quiet.npz')
                    snapshot(board,folder,'quiet-perf')
                    if policy:
                        qr,_,_,qt=load_capture(folder/'quiet.npz')
                        checks={'pre':quiet_gate(qr[:int(policy['quiet_seconds']*RATE)],baseline_dbfs,policy)}
                        if any(qt[:,4]):raise ValueError('Gapped quiet pre-roll; not a noise retry')
                        outcome['noise_gate']=checks
                        if not checks['pre']['passed']:
                            outcome['noise_gate_passed']=False
                            raise NoiseContaminated('Quiet pre-roll failed')
                    if trial['mode'] in ('speech','doubletalk'):
                        command(board,'server-url http://'+host_address()+':7861')
                        at,_=command(board,'connect',90);board.wait_for('STATE Ready',20,since=at)
                        command(board,'audio 1');command(board,'aec-ref-gain-status')
                    if trial['state']=='warm':
                        command(board,'play-fixture tts_1.wav')
                        time.sleep(len(far)/16000+1)
                    anchors.append(clock_anchor(board));time.sleep(.2)
                    command(board,'aec-record 1')
                    if policy:
                        pre_end=time.monotonic()+policy['quiet_seconds']+.5
                        while time.monotonic()<pre_end:
                            anchors.append(clock_anchor(board));time.sleep(.2)
                    else:time.sleep(.5)
                    if trial['mode']=='probe':command(board,'aec-probe')
                    elif trial['mode'] in ('speech','doubletalk'):command(board,'play-fixture tts_1.wav')
                    seconds=14 if trial['mode'] in ('speech','doubletalk') else 5
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        def near_play():
                            if trial['mode']=='doubletalk':time.sleep(2)
                            return play_wav(ROOT/'server/fixtures/prompt_france.wav',device)
                        future=pool.submit(near_play) if trial['mode'] in ('near','doubletalk') else None
                        end=time.monotonic()+seconds
                        while time.monotonic()<end:
                            anchors.append(clock_anchor(board));time.sleep(.2)
                        if future:outcome['jabra']=future.result()
                    command(board,'aec-record 0');anchors.append(clock_anchor(board))
                    dump_capture(board,folder/'capture.npz');snapshot(board,folder,'capture-perf')
                    command(board,'aec-ref-gain-status')
                    if trial['mode'] in ('speech','doubletalk'):
                        command(board,'audio 0');command(board,'disconnect',40)
                        command(board,'aec-ref-gain-status')
                    write(folder/'anchors.json',anchors)
                    if policy:
                        raw,_,ref,blocks=load_capture(folder/'capture.npz')
                        _,_,qref,_=load_capture(folder/'quiet.npz')
                        n=int(policy['quiet_seconds']*RATE)
                        if len(raw)<2*n or any(blocks[:,4]):
                            raise ValueError('Missing/gapped capture quiet windows; not a noise retry')
                        checks['capture_pre']=quiet_gate(raw[:n],baseline_dbfs,policy)
                        checks['post']=quiet_gate(raw[-n:],baseline_dbfs,policy)
                        checks['capture_windows']={'pre_samples':[0,n],'post_samples':[len(raw)-n,len(raw)]}
                        checks['board_silent']=all(float(frame_power(x).max())<=reference_threshold(qref)
                                                   for x in (ref[:n],ref[-n:]))
                        write(folder/'noise-gate.json',checks)
                        if not checks['board_silent']:
                            raise ValueError('Own playback in quiet window; not a room-noise retry')
                        outcome['noise_gate_passed']=all(checks[k]['passed'] for k in ('pre','capture_pre','post'))
                    outcome.update(analyze(trial['mode'],folder/'capture.npz',folder/'quiet.npz',declaration,anchors=anchors))
                    outcome.update(acquisition_complete=True,failed=outcome.get('outcome')=='missing-playback' or outcome.get('acoustically_scorable') is False)
                    if policy and not outcome['noise_gate_passed']:
                        raise NoiseContaminated('Capture pre-roll or post-roll failed')
                    if trial['mode']=='doubletalk':outcome['stt']=transcribe_afe(folder/'capture.npz',folder)
                except NoiseContaminated as e:
                    outcome.update(failed=True,noise_gate_passed=False,exclude_from_gain_selection=True,
                                   noise_contaminated=True,error=str(e))
                    write(folder/'room-noise-contamination.json',outcome)
                    write(folder/'noise-gate.json',outcome['noise_gate'])
                except Exception as e:
                    outcome.update(error=type(e).__name__+': '+str(e),failed=True)
                    (folder/'exception.txt').write_text(traceback.format_exc())
                finally:
                    if queue:
                        outcome['noise_retry_action']=queue.finish(cell,attempt,outcome.get('noise_gate_passed'))
                        result.update(noisy_cell_streak=queue.noisy_cells,retries_exhausted=queue.exhausted,
                                      pending_cells=[dict(cell_id=t['run_id'],attempt=a) for t,a in queue.pending])
                        if queue.paused:result['paused_reason']='Three consecutive distinct cells failed the noise gate'
                    write(folder/'result.json',outcome);result['trials'].append(outcome)
                    write(out/'result.json',result)
                    print('TRIAL',trial['run_id'],'complete',outcome['acquisition_complete'],'failed',outcome['failed'],
                          'echo',outcome.get('afe',{}).get('integrated',{}),flush=True)
            result['completed']=not queue or (not queue.paused and not queue.exhausted)
        finally:
            for label,fn in [('bot',lambda:bot.stop() if bot else None),('serial',lambda:board.close() if board else None)]:
                try:fn()
                except Exception as e:result['cleanup_errors'].append(label+': '+str(e))
            try:
                flash(NORMAL,NORMAL_HASH,out,'restore');restored=boot(out,'restore',NORMAL_HASH)
                command(restored,'ping');restored.close();result['restored_elf_verified']=NORMAL_HASH
            except Exception as e:result['cleanup_errors'].append('RESTORE: '+str(e))
            try:
                subprocess.run(['alsactl','-f',str(out/'mixer-before.state'),'restore','0'],check=True,capture_output=True)
                subprocess.run(['alsactl','-f',str(out/'mixer-after.state'),'store','0'],check=True,capture_output=True)
                result['mixer_restored']=(out/'mixer-before.state').read_bytes()==(out/'mixer-after.state').read_bytes()
            except Exception as e:result['cleanup_errors'].append('mixer: '+str(e))
            write(out/'result.json',result)
            if result.get('restored_elf_verified')!=NORMAL_HASH:
                print('RESTORE FAILED; retaining bench lock',flush=True)
                while True:time.sleep(30)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();run(json.loads(args.plan.read_text()),args.out.resolve())

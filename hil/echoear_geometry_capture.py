#!/usr/bin/env python3
"""One exact-image A/B/A diagnostic; no gate edits, recalibration or retakes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]


def freeze_inputs(plan_path,budget,plan):
    tracked=subprocess.check_output(['git','ls-files','-z','--','hil','server'],cwd=ROOT).decode().split('\0')
    paths={ROOT/name for name in tracked if name}
    paths.update((Path(__file__),ROOT/'hil/echoear_geometry_ab.py',plan_path,budget,
                  Path(plan['controls']['priming_prompt']),Path(plan['bank'])/'tts_1.wav'))
    for historical in plan['analysis']['historical_replayed'].values():
        path=Path(historical['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=historical['sha256']:
            raise RuntimeError('Historical acoustic reference changed')
        paths.add(path)
    return {str(path.resolve()):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def verify_inputs(hashes):
    for name,wanted in hashes.items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=wanted:
            raise RuntimeError('Frozen diagnostic input changed: '+name)


def closed_serial_audit(path):
    from step9a_r2_checks import audit
    try:return audit(path.read_bytes())
    except Exception as exc:return dict(passed=False,error=type(exc).__name__+': '+str(exc))


def image_identity(image,expected):
    from demo_session import firmware_artifacts
    version=json.loads((image/'project_description.json').read_text())['project_version']
    actual=firmware_artifacts(image,version)
    if actual!=expected:raise RuntimeError('Exact frozen image required; rebuilt substitutes are not accepted')
    flash=json.loads((image/'flasher_args.json').read_text())
    files=['dg-esp32.elf','dg-esp32.bin','project_description.json','flasher_args.json',*flash['flash_files'].values()]
    for name in files:
        if not (image/name).resolve().is_relative_to(image.resolve()):
            raise RuntimeError('Flash input escapes frozen image directory')
    return dict(elf_sha256=actual,version=version,sha256={name:hashlib.sha256((image/name).read_bytes()).hexdigest() for name in sorted(set(files))})


def mixer_readback(card):
    rows={}
    for control in ('PCM','Mic'):
        text=subprocess.check_output(['amixer','-c',card,'sget',control],text=True,timeout=5)
        m=re.search(r'Mono: (Playback|Capture) (\d+) \[(\d+)%\] \[([+-]?[\d.]+)dB\] \[on\]',text)
        rows[control]=[m[1],int(m[2]),int(m[3]),float(m[4])] if m else None
    if rows!={'PCM':['Playback',8,73,-4.0],'Mic':['Capture',6,86,3.0]}:raise RuntimeError('Frozen historical mixer controls differ')
    return rows


def eligibility(board,device,arm,journal):
    """Both rooms and WiFi, without changing the frozen mixer controls."""
    from alsa import record_wav,room_baseline_level
    from step7_support import pcm
    from bench_calibration import record_board_room
    from host_nic import snapshot
    from echoear_requalification import load_plan,preflight_callback
    path=arm/'eligibility';path.mkdir();before=snapshot()
    with record_wav(path/'room-jabra.wav',device,3,16000) as recording:recording.wait()
    jabra=dict(dbfs=room_baseline_level(pcm(path/'room-jabra.wav')[3200:])['baseline_dbfs'],wav=str(path/'room-jabra.wav'))
    native=dict(dbfs=record_board_room(board,path),wav=str(path/'room-board.wav'))
    remaining=30-(time.monotonic()-before['monotonic_s'])
    if remaining>0:time.sleep(remaining)
    verdict=preflight_callback(load_plan(),'geometry-ab',journal)(arm.name,
        dict(jabra=jabra,board=native,nic_before=before,nic_after=snapshot()))
    (path/'result.json').write_text(json.dumps(verdict,indent=2)+'\n')
    if not verdict['passed']:raise RuntimeError('Geometry diagnostic eligibility failed before original reply')
    return verdict


def supplemental_safe(row):
    """Permit collecting unrun originals after edits; never change validity."""
    try:
        return bool(row['measurements']['passed'] and row['eligibility']['passed'] and
            row['transport']['passed'] and row['clips']['passed'] and
            row['monitor']['passed'] and row['closed_raw_monitor']['passed'] and
            row['nic']['passed'] and row['cleanup_errors']==[] and
            row['before_perf']['output_volume_db']==row['after_perf']['output_volume_db']==0 and
            row['mixer_before']==row['mixer_after_eligibility']==row['mixer_after'])
    except KeyError:return False


def measurement_window(plan):
    spec=plan.get('measurement_window')
    if spec is None:return dict(kind='original-whole-recording',settle_seconds=0)
    if spec!=dict(kind='priming-through-reply-end',settle_seconds=6,retain_full_arm=True):
        raise RuntimeError('Unsupported declared calibration window')
    return spec


def capture_transport_checks(before,reply_end,whole_start,whole_end):
    from playback_quality import transport_gate,microphone_clip_gate
    window=transport_gate(before,reply_end);whole=transport_gate(whole_start,whole_end)
    clips=microphone_clip_gate(whole_start,whole_end)
    return dict(window=window,whole_arm=whole,whole_arm_clips=clips,
        passed=bool(window['passed'] and all(v==0 for v in window['deltas'].values()) and whole['passed'] and clips['passed']))


def prepare_continuation(out,plan,identities,budget):
    """Append previously unrun originals, preserving the first failure."""
    original=(out/'result.json').read_bytes();result=json.loads(original)
    frozen=json.loads((out/'predeclared.json').read_text())
    budget_path=frozen.get('budget_path')
    if budget_path is None:
        # Original schema predates the explicit CLI path field. Recover only
        # a UNIQUE budget-shaped JSON from its already frozen input manifest.
        candidates=[]
        for name,wanted in frozen['input_sha256'].items():
            path=Path(name)
            if path.suffix!='.json':continue
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=wanted:continue
            try:value=json.loads(raw)
            except ValueError:continue
            if isinstance(value,dict) and set(value)=={'budget','F','W','largest'}:candidates.append(name)
        if len(candidates)!=1:raise RuntimeError('Original budget identity is ambiguous')
        budget_path=candidates[0]
    if str(budget.resolve())!=budget_path or budget_path not in frozen['input_sha256']:
        raise RuntimeError('Continuation must use the original frozen budget path')
    count=len(result['arms'])
    if (frozen['plan']!=plan or frozen['images']!=identities or count not in (1,2) or
        result.get('passed') is not False or result.get('error')!='RuntimeError: Original diagnostic arm failed; no automatic replay' or
        [r['role'] for r in result['arms']]!=plan['arm_order'][:count] or
        not all(supplemental_safe(r) for r in result['arms'])):
        raise RuntimeError('Only clean, stopped original populations with unrun arms may continue')
    if any((out/f'{i+1}-{role}').exists() for i,role in enumerate(plan['arm_order']) if i>=count):
        raise RuntimeError('Previously started arms cannot be replayed')
    changes={};hashes=frozen['input_sha256'].copy()
    permitted={str(ROOT/'hil/echoear_geometry_capture.py'),str(ROOT/'hil/test_echoear_geometry_capture.py')}
    for name,wanted in hashes.items():
        actual=hashlib.sha256(Path(name).read_bytes()).hexdigest()
        if actual!=wanted:
            if name not in permitted:raise RuntimeError('Frozen diagnostic input changed: '+name)
            historic=subprocess.check_output(['git','show',frozen['harness_commit']+':'+str(Path(name).relative_to(ROOT))],cwd=ROOT)
            if hashlib.sha256(historic).hexdigest()!=wanted:raise RuntimeError('Original harness provenance mismatch')
            changes[name]=dict(original_sha256=wanted,continuation_sha256=actual);hashes[name]=actual
    amendment=dict(scope='Only unrun original B/A-repeat; prior diagnostic FAIL retained; supplemental observations only',budget_path=budget_path,
        original_result_sha256=hashlib.sha256(original).hexdigest(),completed_originals=count,input_changes=changes,
        harness_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        retakes=0,gate_definition_changed=False)
    with (out/'result-before-continuation.json').open('xb') as stream:stream.write(original)
    with (out/'continuation.json').open('x') as stream:stream.write(json.dumps(amendment,indent=2)+'\n')
    result['original_stop_error']=result.pop('error');result['supplemental_continuation']=amendment
    frozen['input_sha256']=hashes
    return frozen,result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--old-image',type=Path,required=True);p.add_argument('--budget',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--continue-unrun',action='store_true',help='Supplemental unrun originals only; retain prior FAIL')
    args=p.parse_args(argv);plan=json.loads(args.plan.read_text());window=measurement_window(plan);old=args.old_image.resolve();new=Path(plan['new_image'])
    # Identity is verified before a bench hold or output-population creation.
    identities=[image_identity(old,plan['old_elf_sha256']),image_identity(new,plan['new_elf_sha256'])]
    bank=Path(plan['bank']);reference=bank/'tts_1.wav';prompt=Path(plan['controls']['priming_prompt'])
    if hashlib.sha256(reference.read_bytes()).hexdigest()!=plan['reference']['sha256'] or hashlib.sha256(prompt.read_bytes()).hexdigest()!=plan['controls']['priming_sha256']:
        raise RuntimeError('Frozen acoustic input changed')
    out=args.out.resolve()
    if args.continue_unrun:frozen,result=prepare_continuation(out,plan,identities,args.budget)
    else:
        out.mkdir(parents=True,exist_ok=False)
        frozen=dict(plan=plan,images=identities,budget_path=str(args.budget.resolve()),input_sha256=freeze_inputs(args.plan,args.budget.resolve(),plan),
            harness_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
        (out/'predeclared.json').write_text(json.dumps(frozen,indent=2)+'\n')
        result=dict(passed=False,arms=[],qualification=False,gate_definition_changed=False)
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),STEP9A_BUDGET_PATH=str(args.budget.resolve()),
        OFFLINE_BENCH_FIXTURE_DIR=str(bank),OFFLINE_SKIP_GREETING='1',PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',
        OFFLINE_USB_ABSENCE_TURNS='0',OFFLINE_RTVI_BURST_SWEEP='0',OFFLINE_BENCH_CONTINUOUS_AUDIO='0')
    os.environ.pop('DISPLAY_HIL_COMMANDS',None)
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from step9a_echoear import bench,check_esptool
    from echoear_item2_campaign import flash_image
    from step9a_assets import asset_server
    from step9a_support import setup,host_address
    from step7_support import BotServer
    from esp_serial import EspSerial
    from alsa import resolve_jabra,set_volumes,record_wav,play_wav
    from transport_test import perf
    from speaker_dual_sweep_test import dump_mic
    from playback_quality import transport_gate,microphone_clip_gate
    from echoear_geometry_ab import measure,analyze
    from host_nic import snapshot,gate as nic_gate
    check_esptool()
    def save():(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        start=len(result['arms'])
        for index,role in enumerate(plan['arm_order'][start:],start=start):
            arm=out/f'{index+1}-{role}';arm.mkdir();image=new if role=='B-new' else old;expected=identities[1 if role=='B-new' else 0]
            verify_inputs(frozen['input_sha256'])
            if image_identity(image,expected['elf_sha256'])!=expected:raise RuntimeError('Image bundle changed between arms')
            row=dict(role=role,elf_sha256=expected['elf_sha256'],reference_sha256=plan['reference']['sha256'],diagnostic_valid=False);result['arms'].append(row)
            with bench():
                print('Bench acquired: '+role,flush=True);nic=snapshot();port=flash_image(image,arm);card,device=resolve_jabra()
                if index==0:set_volumes(card,arm/'mixer-set-once.log')
                row['mixer_before']=mixer_readback(card)
                with asset_server(),BotServer(arm/'bot') as bot,EspSerial(port,log_path=arm/'serial.log',display_checks=True) as board:
                    try:
                        cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                        board.wait_for('elf_sha256='+expected['elf_sha256'],5,since=cursor)
                        board.command('audio 0');board.command('quiet 1');board.command('playback-volume 0')
                        board.command('server-url http://'+host_address()+':7861');setup(board,mode='cat')
                        cursor=board.cursor;board.command('mic-gain-status')
                        gain=board.wait_for(r'MIC_GAIN_PATH analog_db=\d+ reg=0x[0-9a-f]+ digital_percent=\d+',3,since=cursor)
                        values=re.search(r'analog_db=(\d+) reg=0x([0-9a-f]+) digital_percent=(\d+)',gain)
                        if (int(values[1]),int(values[2],16),int(values[3]))!=(30,26,125):raise RuntimeError('Historical gain controls differ')
                        row['gain_readback']=gain
                        row['eligibility']=eligibility(board,device,arm,out/'deferrals.jsonl')
                        row['mixer_after_eligibility']=mixer_readback(card)
                        board.command('connect',90);board.command('audio 1');board.command('quiet 1')
                        if window['kind']=='priming-through-reply-end':
                            row['whole_arm_before_perf']=perf(board)
                            time.sleep(window['settle_seconds'])
                        before=perf(board);row['before_perf']=before
                        with record_wav(arm/'jabra.wav',device,25,16000) as rec:
                            time.sleep(.6);board.command('mic-record 1')
                            time.sleep(max(0,rec.ready_at+plan['controls']['prompt_start_after_capture_s']-time.monotonic()))
                            row['prompt_start']=time.monotonic();play_wav(prompt,device);row['prompt_end']=time.monotonic()
                            target=row['prompt_end']+plan['controls']['reply_request_delay_after_prompt_s']
                            time.sleep(max(0,target-time.monotonic()));cursor=board.cursor
                            row['reply_requested_at']=time.monotonic();board.command('play-fixture tts_1.wav',20)
                            board.wait_for('GATE closed',8,since=cursor);board.wait_for('GATE open',25,since=cursor)
                            time.sleep(.4);board.command('mic-record 0')
                            if window['kind']=='priming-through-reply-end':
                                row['reply_end_perf']=perf(board)
                            rec.wait();ready=rec.ready_at
                        full_end=perf(board);after=row.get('reply_end_perf',full_end);row['after_perf']=after
                        if window['kind']=='priming-through-reply-end':
                            row['whole_arm_after_perf']=full_end
                            row['calibration_transport']=capture_transport_checks(before,after,row['whole_arm_before_perf'],full_end)
                        dump_mic(board,arm/'native.wav')
                        row['measurements']=measure(arm/'jabra.wav',arm/'native.wav',reference,
                            jabra_search_s=row['prompt_end']-ready-.1,baseline_end_s=row['prompt_end']-ready-.1)
                        row['transport']=transport_gate(before,after);row['clips']=microphone_clip_gate(before,after)
                        row['mixer_after']=mixer_readback(card)
                        row['diagnostic_valid']=bool(row['measurements']['passed'] and row['transport']['passed'] and
                            all(v==0 for v in row['transport']['deltas'].values()) and row['clips']['passed'] and
                            before['output_volume_db']==after['output_volume_db']==0 and
                            row['mixer_before']==row['mixer_after_eligibility']==row['mixer_after'])
                        if window['kind']=='priming-through-reply-end':
                            row['diagnostic_valid']=row['diagnostic_valid'] and row['calibration_transport']['passed']
                    finally:
                        board.display_cleanup=True;failures=[]
                        for command in ('mic-record 0','audio 0','disconnect','disp bench off','playback-volume -6'):
                            try:board.command(command,40)
                            except Exception as exc:failures.append(command+': '+str(exc))
                        row['cleanup_errors']=failures;row['monitor']=board.display_monitor.result()
                        row['diagnostic_valid']=row['diagnostic_valid'] and not failures and row['monitor']['passed']
                row['closed_raw_monitor']=closed_serial_audit(arm/'serial.log')
                row['nic']=nic_gate(nic,snapshot())
                row['diagnostic_valid']=row['diagnostic_valid'] and row['nic']['passed'] and row['closed_raw_monitor']['passed']
            verify_inputs(frozen['input_sha256'])
            save();print(role+' diagnostic_valid='+str(row['diagnostic_valid']),flush=True)
            if not row['diagnostic_valid'] and not (args.continue_unrun and supplemental_safe(row)):
                raise RuntimeError('Original diagnostic arm failed; no automatic replay')
        result['analysis']=analyze(plan,result['arms']);result['passed']=result['analysis']['classification']!='inconclusive'
    except KeyboardInterrupt:result.update(passed=False,interrupted=True,error='Interrupted at user request')
    except Exception as exc:result.update(passed=False,error=type(exc).__name__+': '+str(exc))
    finally:save()
    print(json.dumps(dict(passed=result['passed'],analysis=result.get('analysis'),error=result.get('error'))))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())

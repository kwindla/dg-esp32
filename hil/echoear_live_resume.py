#!/usr/bin/env python3
"""Collect only LIVE originals2..5 after the coordinator's priority handoff."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace


def original_population(result):
    scored=[r for r in result['runs'] if r.get('scored') is True]
    if [r.get('run') for r in scored]!=[1] or result.get('passed') is not False or scored[0]['passed'] is not False:
        raise RuntimeError('This amendment resumes only a failed original1 with slots2..5 unscored')
    if result.get('reruns')!=[]:raise RuntimeError('Retaken populations cannot resume')
    return scored


def combine(original,segment):
    prior=original_population(original);rows=segment.get('runs',[])
    indices=[r.get('run') for r in rows if r.get('scored') is True]
    if indices!=[2,3,4,5][:len(indices)] or len(indices)!=len(set(indices)):
        raise RuntimeError('Continuation cannot replace or duplicate an original')
    runs=prior+[r for r in rows if r.get('scored') is True]
    return dict(passed=False,qualification=False,prior_failure_unchanged=True,runs=runs,
        acquisition_completed=[r['run'] for r in runs]==[1,2,3,4,5],
        remaining_originals=[i for i in (2,3,4,5) if i not in indices],
        same_peer=len({r.get('pc_id') for r in runs})==1 and all(r.get('pc_id') for r in runs),
        reason='Original1 level FAIL retained; priority handoff/reflash creates a separate session; no clean5/5 qualification claim',
        original_result=original,continuation_result=segment)


def validate_segment(plan,segment,out,policy):
    from echoear_requalification import observation_gate
    from echoear_item2_campaign import validate_deferral
    if segment.get('deferred'):
        validate_deferral(out,segment)
        segment['passed']=False
        return
    segment['item2_observations']=observation_gate(plan,'quiet_live',segment,(out/'serial.log').read_text(errors='replace'))
    rows=segment.get('runs',[])
    segment['population_scope']=dict(requested_originals=[2,3,4,5],absent_originals_from_this_segment=[1],
        five_originals_gate_relaxed=False,reason='Original1 remains in preserved failed prefix; this segment cannot qualify as5/5')
    segment['level_policy_applied']=bool(rows) and all(r.get('jabra_rebaseline_policy')==policy and
        r.get('jabra_level',{}).get('target_dbfs')==policy['target_dbfs'] for r in rows)
    segment['passed']=bool(segment.get('passed') and segment['item2_observations']['observations_passed'] and
        segment['stage_nic']['passed'] and segment['level_policy_applied'])


def execute(binding_path,original_path,out):
    from echoear_item2_campaign import verified_binding,flash_image
    from echoear_jabra_rebaseline import load
    from echoear_requalification import load_plan,preflight_callback
    binding,image=verified_binding(binding_path)
    amendment=binding['live_resume']
    if str(original_path.resolve())!=amendment['original_result'] or hashlib.sha256(original_path.read_bytes()).hexdigest()!=amendment['original_result_sha256']:
        raise RuntimeError('Original failed result changed')
    if str(out.resolve())!=amendment['continuation_out'] or out.exists() or out.with_name(out.name+'-identity').exists():
        raise RuntimeError('Only the fresh declared continuation directory may be used')
    original=json.loads(original_path.read_text());original_population(original)
    original_bytes=original_path.read_bytes()
    with original_path.with_name('result-before-priority-resume.json').open('xb') as stream:stream.write(original_bytes)
    plan=load_plan(Path(binding['policy_path']));policy=load(binding['geometry_rebaseline_policy'])
    for key in ('OFFLINE_BENCH_FIXTURE_DIR','OFFLINE_SKIP_GREETING','DISPLAY_HIL_COMMANDS','ECHOEAR_STRICT_MERGE'):os.environ.pop(key,None)
    from esp_serial import ROOT
    os.environ.update(DG_BOARD='echoear',ECHOEAR_ACOUSTIC_QUALIFICATION='1',PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',
        STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),STEP9A_BUDGET_PATH=binding['budget'],
        OFFLINE_USB_ABSENCE_TURNS='0',OFFLINE_RTVI_BURST_SWEEP='0',OFFLINE_BENCH_CONTINUOUS_AUDIO='0')
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from step9a_echoear import bench,check_esptool
    from step9a_assets import asset_server
    from step9a_support import host_address
    from e2e_common import run
    from host_nic import snapshot,gate
    check_esptool();identity=out.with_name(out.name+'-identity');identity.mkdir(parents=True,exist_ok=False)
    with bench():
        print('Bench acquired for previously unscored LIVE originals2..5',flush=True)
        before=snapshot();port=flash_image(image,identity)
        args=SimpleNamespace(out=out,port=port,runs=5,original_start=2,spl=65,warm=True,no_retakes=True,
            elf=image/'dg-esp32.elf',server_url='http://'+host_address()+':7861',ice_servers=[],option=[],
            display='cat',diagnostic_noisy=False,all_originals=True,jabra_policy=policy,
            preflight=preflight_callback(plan,'quiet_live',binding_path.parent/'deferrals.jsonl'))
        with asset_server():run(args,True)
        if not (out/'result.json').exists():raise RuntimeError('No continuation result; skip cannot complete originals')
        segment=json.loads((out/'result.json').read_text());segment['stage_nic']=gate(before,snapshot())
        if not segment['stage_nic']['passed']:segment['passed']=False
    validate_segment(plan,segment,out,policy)
    segment['normal_elf_sha256']=binding['normal_elf_sha256'];segment['binding']=str(binding_path)
    (out/'result.json').write_text(json.dumps(segment,indent=2)+'\n')
    combined=combine(original,segment)
    (original_path.parent/'resumed-result.json').write_text(json.dumps(combined,indent=2)+'\n')
    print(json.dumps(dict(acquisition_completed=combined['acquisition_completed'],remaining=combined['remaining_originals'],passed=False)))
    return 1 # The original cohort remains FAIL even when acquisition completes.


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binding',type=Path,required=True);p.add_argument('--original',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();raise SystemExit(execute(a.binding.resolve(),a.original.resolve(),a.out.resolve()))

#!/usr/bin/env python3
"""Predeclared item2 policy and host-only rehearsal. No scored launch option."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import time

ROOT=Path(__file__).resolve().parents[1]
PLAN=ROOT/'hil/plans/echoear-item2-requalification.json'


def load_plan(path=PLAN):
    plan=json.loads(Path(path).read_text())
    assert plan['schema']==1 and plan['execution_authorized'] is False
    assert plan['final_master_commit'] is None and plan['final_elf_sha256'] is None
    assert [s['id'] for s in plan['order']]==['strict_voice_cat','a6_echo_stress','a6_hot_stt','a6_analog_probe','quiet_offline','quiet_live']
    for index,count in ((0,3),(4,5),(5,5)):
        stage=plan['order'][index]
        assert stage['runs']==stage['required_passes']==count and stage['retakes']==0 and stage['warm'] is True
    assert plan['firmware']['aec_enabled'] is False
    assert plan['environment']['room_limit_dbfs']==-55
    assert plan['environment']['room_sensors']==['jabra','board']
    nic=plan['environment']['nic']
    assert nic['gated_interface']=='wlp8s0' and nic['limit_bytes_per_300s']==20000000
    assert plan['bot']['sctp_chunk_bytes']==1100 and plan['strict_gates']['bot_loss_exceptions'] is False
    for key,value in dict(minimum_internal_bytes=81920,largest_internal_bytes=32768,failed_allocations=0,
                          app_control_free_bytes=2048,engine_lock_timeouts=0,engine_lock_failures=0).items():
        assert plan['strict_gates'][key]==value
    return plan


def environment_gate(plan,observation):
    from host_nic import gate
    limit=plan['environment']['room_limit_dbfs'];rooms={};reasons=[]
    for sensor in plan['environment']['room_sensors']:
        value=observation.get(sensor,{}).get('dbfs')
        valid=type(value) in (int,float) and math.isfinite(value)
        rooms[sensor]=dict(dbfs=value if valid else None,limit_dbfs=limit,passed=valid and value<=limit,
                           wav=observation.get(sensor,{}).get('wav'))
        if not rooms[sensor]['passed']:reasons.append(sensor+' room floor unavailable or above -55 dBFS')
    nic=gate(observation.get('nic_before',{}),observation.get('nic_after',{}))
    if not nic['passed']:reasons.append('wlp8s0 traffic above bound or unavailable')
    if nic.get('elapsed_s',0)<plan['environment']['nic']['preflight_seconds']:
        reasons.append('NIC eligibility observation shorter than 30 seconds')
    return dict(passed=not reasons,rooms=rooms,nic=nic,reasons=reasons)


def preflight_callback(plan,stage,journal,*,synthetic=False):
    """Called by production turn before its scored prompt; performs no IO to devices."""
    def check(slot,observation):
        result=environment_gate(plan,observation)
        if not result['passed']:
            event=dict(event='deferred',utc=datetime.now(timezone.utc).isoformat(),host_s=time.monotonic(),
                stage=stage,next_original_slot=slot,scored=False,synthetic=synthetic,
                eligibility=result,wait_seconds=30,release_bench=True)
            journal.parent.mkdir(parents=True,exist_ok=True)
            with journal.open('a') as stream:stream.write(json.dumps(event,allow_nan=False)+'\n')
        return result
    check.minimum_nic_seconds=plan['environment']['nic']['preflight_seconds']
    check.room_limit_dbfs=plan['environment']['room_limit_dbfs']
    return check


def observation_gate(plan,stage,result,serial_text):
    """Recompute observations; this cannot bind an ELF or authorize scoring."""
    from step9a_merge_checks import merge_gate,memory_gate
    from echoear_acoustic_qualification import gate as acoustic_gate
    from step9a_r2_checks import audit
    from host_nic import gate as nic_gate
    from playback_quality import transport_gate
    runs=result.get('runs',[]);spec=next(s for s in plan['order'] if s['id']==stage)
    if stage not in ('strict_voice_cat','quiet_offline','quiet_live'):raise ValueError('A owns the A6 result gates')
    samples=[]
    try:
        samples=[json.loads(line[5:]) for line in serial_text.splitlines() if line.startswith('PERF ')]
        resets=[b['index'] for b in result.get('display_monitor',{}).get('boot_epochs',[]) if b['requested']]
        raw=audit(serial_text.encode(),expected_boot_indices=resets) # No bot-loss exception in a scored cohort.
    except (RuntimeError,ValueError,TypeError) as exc:raw=dict(passed=False,error=str(exc))
    peers=sorted({r.get('pc_id') for r in runs if r.get('pc_id')})
    try:
        source=merge_gate(result,serial_text) if stage=='strict_voice_cat' else acoustic_gate(result,peers)
    except (ValueError,TypeError,KeyError) as exc:source=dict(passed=False,error=str(exc))
    uplink=None
    if (plan.get('decision_138') or {}).get('uplink_gate')=='source-cadence' and isinstance(source.get('checks'),dict):
        # r15 (decision 138a): the network-arrival link_tail is recorded, report-only; the uplink gate is the device's
        # own send cadence (S1) over every scored original. Incomplete trace evidence fails closed (harness failure).
        nested=source['checks'];link=nested.pop('uplink',None)
        cadence=[r.get('source_cadence') or {} for r in runs]
        complete=bool(runs) and all(c.get('complete') is True for c in cadence)
        nested['uplink_source_cadence']=complete and all(c.get('passed') is True for c in cadence)
        source['passed']=all(nested.values())
        uplink=dict(gate='source-cadence',link_tail_report_only=link,link_tail=result.get('link_tail'),
                    source_cadence=cadence,complete=complete,harness_failure=not complete,
                    s2_report_only=[r.get('s2') for r in runs])
    eligibility=[];arm_nic=[]
    for run in runs:
        evidence=run.get('eligibility',{})
        eligibility.append(environment_gate(plan,dict(
            jabra=evidence.get('rooms',{}).get('jabra',{}),board=evidence.get('rooms',{}).get('board',{}),
            nic_before=evidence.get('nic',{}).get('before',{}),nic_after=evidence.get('nic',{}).get('after',{}))))
        arm=run.get('host_nic',{})
        arm_nic.append(nic_gate(arm.get('before',{}),arm.get('after',{})))
    memory=memory_gate(samples)
    health=[s.get('display') if isinstance(s,dict) else None for s in samples]
    locks=bool(health) and all(isinstance(h,dict) and type(h.get('lcd_initialized')) is bool and
        (h['lcd_initialized'] is False or h.get('engine_snapshot') is True) and
        all(type(h.get(k)) is int and h[k]==0 for k in ('lock_timeouts','lock_failures')) for h in health)
    transport=[transport_gate(r.get('before_perf',{}),r.get('after_perf',{})) for r in runs]
    checks=dict(exact_originals=len(runs)==spec['runs'] and all(r.get('passed') is True for r in runs),
        no_retakes=result.get('reruns')==[],source=source['passed'],raw_monitor=raw['passed'],
        memory=memory['passed'],locks=locks,raw_stack=(raw.get('app_control_min_free') or 0)>=2048,
        zero_transport=bool(transport) and all(g.get('available') is True and g.get('passed') is True and
            all(v==0 for v in g['deltas'].values()) for g in transport),
        every_environment=bool(eligibility) and all(e['passed'] for e in eligibility),
        per_arm_nic=bool(arm_nic) and all(e['passed'] for e in arm_nic))
    return dict(observations_passed=all(checks.values()),checks=checks,source=source,memory=memory,uplink=uplink,
        eligibility=eligibility,arm_nic=arm_nic,transport=transport,raw_monitor=raw,hardware_qualified=False,execution_authorized=False,final_elf_bound=False)


FINAL_REJECT_FLAGS=('scoring_failed','interrupted','restore_failed')


ITEM1_KIND='item1-combined'
ITEM2_KIND='item2-stage'


def final_record_problems(record,*,item1=False,stage=None):
    """r9 (Fable r8 N2): only a finalised, uninterrupted record counts. Rejects scoring_failed / interrupted /
    restore_failed, an unclean restore and a missing recorded exit code; item2 records also need stage,
    release_binding and normal_elf_sha256 (written only by execute()'s final write)."""
    if not isinstance(record,dict):return ['not a record']
    problems=[flag for flag in FINAL_REJECT_FLAGS if record.get(flag)]
    # r10 (Astra r9 #3): uncounted records (the RF pre-check, smokes) never fill a counted slot, and each slot
    # checks the record's identity, not only its final-envelope fields.
    if record.get('counted') is False:problems.append('uncounted record')
    elif record.get('counted') is not True:problems.append('record does not say counted')   # r11: smokes omitted it
    if record.get('purpose')!='acceptance':problems.append('record purpose is not acceptance')
    if not record.get('binding_sha256') or not record.get('release_binding'):problems.append('record has no binding provenance')
    if record.get('kind')=='rf-precheck':problems.append('rf-precheck record')
    if item1:
        if record.get('kind')!=ITEM1_KIND:problems.append('not an item1 record (kind)')
        problems+=['missing item1 '+key for key in ('candidate','elf_sha256','predeclared','checks','autonomous_turns','cycles')
                   if key not in record]
        if record.get('stage'):problems.append('item2 record in the item1 slot')
    else:
        if record.get('kind')!=ITEM2_KIND:problems.append('not an item2 stage record (kind)')
        if stage is not None and record.get('stage')!=stage:problems.append('stage mismatch')
    if (record.get('restore') or {}).get('clean') is not True:problems.append('restore not clean')
    if type(record.get('exit_code')) is not int:problems.append('no recorded exit code')
    if not item1:problems+=['missing '+key for key in ('stage','release_binding','normal_elf_sha256') if not record.get(key)]
    return problems


def acceptance_verdict(plan,item1,stages):
    """Acceptance pass for the step-10 campaign (decision 134). item1 is its combined result; stages maps each item2
    stage id to its final stage result (quiet_live: the attempt carrying quiet_live_aggregate). A report_only stage
    (D6: a6_analog_probe) counts only through a valid acquisition; its measurement verdict is retained, not used."""
    rows={}
    for spec in plan['order']:
        r=stages.get(spec['id'])
        if r is None:rows[spec['id']]=dict(counted=True,passed=False,reason='missing');continue
        problems=final_record_problems(r,stage=spec['id'])
        if spec.get('report_only'):
            rows[spec['id']]=dict(counted=False,report_only=True,passed=r.get('acquisition_valid') is True and r.get('exit_code')==0,
                measurement_passed=r.get('passed'),investigation_required=r.get('investigation_required'))
        elif spec['id']=='quiet_live' and 'quiet_live_aggregate' in r:
            rows[spec['id']]=dict(counted=True,passed=r['quiet_live_aggregate'].get('passed') is True and r.get('passed') is True and r.get('exit_code')==0)
        else:rows[spec['id']]=dict(counted=True,passed=r.get('passed') is True and not r.get('deferred') and r.get('exit_code')==0)
        if problems:rows[spec['id']].update(passed=False,record_problems=problems)
    item1_problems=final_record_problems(item1,item1=True) if item1 else ['missing']
    item1_ok=bool(item1) and item1.get('passed') is True and item1.get('exit_code')==0 and not item1_problems
    return dict(passed=item1_ok and all(v['passed'] for v in rows.values()),item1=item1_ok,item1_record_problems=item1_problems,stages=rows,
        rule='item1 PASS and every item2 stage PASS; a report_only stage needs a valid acquisition only (decision 134 D6)')


def dry_run(plan_path,out):
    # Import the production loop before denying access. Imports themselves open
    # no board/service. All device/process/network boundaries are then tripwires.
    from echoear_requalification_dry_run import rehearse
    plan=load_plan(plan_path);out.mkdir(parents=True,exist_ok=False)
    result=rehearse(plan,out)
    result.update(host_only=True,synthetic=True,hardware_qualified=False,scored_campaign_started=False,
                  execution_authorized=False,final_elf_bound=False,
                  plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest())
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,default=PLAN)
    parser.add_argument('--dry-run',action='store_true',required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    result=dry_run(args.plan.resolve(),args.out.resolve());print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['passed'] else 1)

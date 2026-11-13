#!/usr/bin/env python3
"""Reviewed RF eligibility acquisition and ONE strict attempt3, frozen ELF only."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

from echoear_rf_precheck import load_plan, evaluate_probe, next_action


def save(path, value, *, exclusive=False):
    with path.open('x' if exclusive else 'w') as stream:
        json.dump(value, stream, indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def append(path, value):
    with path.open('a') as stream:
        stream.write(json.dumps(value)+'\n');stream.flush();os.fsync(stream.fileno())


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def health(out, ledger, cleanup_errors):
    from echoear_item2_campaign import a6_resources
    resets=[b['index'] for b in ledger.get('boot_epochs',[]) if b['requested']]
    resource=a6_resources(out, requested_boots={str(out/'serial.log'):resets})
    return dict(passed=ledger.get('passed') is True and resource['passed'] and not cleanup_errors,
                controller=ledger, resources=resource, cleanup_errors=cleanup_errors)


def collect_probe(port, out, expected_elf):
    from esp_serial import EspSerial
    from step7_support import BotServer
    from step9a_support import setup, host_address
    from e2e_common import perf
    from host_nic import snapshot, gate
    from echoear_checks import codec_boot_evidence
    plan=load_plan();result=dict(cleanup_errors=[],started_host_s=time.monotonic(),nic_before=snapshot())
    board=None
    os.environ['RTP_EVIDENCE_DIR']=str(out/'rtp')
    try:
        with BotServer(out/'bot','live-test') as bot, EspSerial(port,log_path=out/'serial.log',
                display_checks=True,reset_on_open=False) as board:
            try:
                cursor=board.cursor;board.reset()
                board.wait_for('READY transport-console',35,since=cursor)
                identity=board.wait_for(r'^I \([0-9]+\) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+expected_elf+'$',5,since=cursor)
                result['boot_line']=identity
                boot='\n'.join(x[2] for x in board.lines_since(cursor))
                result['codec_boot']=codec_boot_evidence(boot)
                if not result['codec_boot']['passed']:raise RuntimeError('Probe codec startup failed')
                for command in ('audio 0','quiet 1','playback-volume 0',
                                'server-url http://'+host_address()+':7861'):
                    board.command(command)
                result['cold_perf']=perf(board)
                result['display_setup']=setup(board,mode='cat',rate=30)
                cursor=board.cursor;board.command('connect',90);board.command('audio 1')
                closed=board.wait_for_entry(r'^GATE closed t=',20,since=cursor)
                opened=board.wait_for_entry(r'^GATE open t=',20,since=closed[0])
                result['greeting_gate_open']=dict(host_s=opened[1],line=opened[2])
                time.sleep(plan['probe']['settle_s'])
                result['before_perf']=perf(board);result['pc_id']=result['before_perf']['pc_id']
                # Stats are outside the window. Leave a quiet margin after their
                # completion; retain at least one second of packets after end.
                time.sleep(1)
                start=time.monotonic()+.25;end=start+plan['probe']['duration_s']
                result['window']=dict(start_s=start,end_s=end,declared_host_s=time.monotonic())
                save(out/'window.json',result['window'],exclusive=True)
                while time.monotonic()<end+1:
                    board.display_monitor.check()
                    time.sleep(min(.25,max(0,end+1-time.monotonic())))
                result['after_perf']=perf(board)
                board.command('rtp-stats');time.sleep(1)
                result['events']=bot.events()
            finally:
                board.display_cleanup=True
                for command in ('audio 0','disconnect','disp bench off','playback-volume -6'):
                    try:board.command(command,35)
                    except Exception as exc:result['cleanup_errors'].append(command+': '+str(exc))
                result['controller']=board.display_monitor.result()
    except BaseException as exc:
        result['error']=type(exc).__name__+': '+str(exc)
    finally:
        result['ended_host_s']=time.monotonic();result['nic_after']=snapshot()
        result['nic']=gate(result['nic_before'],result['nic_after'])
        result['health']=health(out,result.get('controller',{}),result['cleanup_errors'])
    result['fatal']=bool(result.get('error') or not result['health']['passed'])
    if 'window' in result:
        events=read_rows(out/'bot/events.jsonl');packets=read_rows(out/'rtp/rtp.jsonl')
        states=read_rows(out/'serial.audio.jsonl')
        reports=[e for e in events if e.get('pc_id')==result['pc_id'] and e['event']=='rtvi-out'
                 and e.get('message',{}).get('data',{}).get('t')=='rtp-stats']
        status=[e for e in reports if e['t']>=result['window']['end_s']]
        truncated=False if status and all(e['message']['data']['d'].get('truncated') is False for e in reports) and len(packets)<20000 else None
        result['capture_status']=dict(post_window_reports=len(status),truncated=truncated,packets=len(packets))
        result['rf']=evaluate_probe(packets,events,states,pc_id=result['pc_id'],
                                   start=result['window']['start_s'],end=result['window']['end_s'],truncated=truncated)
    else:result['rf']=dict(eligible=False,reasons=['Probe window not acquired'])
    result['eligible']=bool(not result['fatal'] and result['rf']['eligible'] and result['nic']['passed'])
    result['raw_sha256']={str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in out.rglob('*') if p.is_file()}
    save(out/'acquisition.json',result,exclusive=True)
    return result


def launch_campaign(binding_path, binding, image, port):
    from e2e_common import run
    from echoear_requalification import load_plan as campaign_plan, preflight_callback, observation_gate
    from echoear_jabra_rebaseline import load
    from host_nic import snapshot,gate
    from step9a_support import host_address
    out=Path(load_plan()['campaign']['output']);plan=campaign_plan(Path(binding['policy_path']))
    policy=load(binding['geometry_rebaseline_policy'])
    os.environ.pop('ECHOEAR_ACOUSTIC_QUALIFICATION',None)
    os.environ.update(ECHOEAR_STRICT_MERGE='1',DISPLAY_HIL_COMMANDS=str(out/'display-command.json'))
    before=snapshot()
    args=SimpleNamespace(out=out,port=port,runs=3,warm=True,no_retakes=True,all_originals=True,
        spl=65,elf=image/'dg-esp32.elf',server_url='http://'+host_address()+':7861',ice_servers=[],option=[],
        display='cat',diagnostic_noisy=False,jabra_policy=policy,
        preflight=preflight_callback(plan,'strict_voice_cat',binding_path.parent/'campaign-deferrals.jsonl'))
    run(args,True)
    result=json.loads((out/'result.json').read_text());result['stage_nic']=gate(before,snapshot())
    result['item2_observations']=observation_gate(plan,'strict_voice_cat',result,(out/'serial.log').read_text(errors='replace'))
    result['normal_elf_sha256']=binding['normal_elf_sha256'];result['release_binding']=str(binding_path)
    result['overall_firmware_qualified']=False
    result['passed']=bool(result.get('passed') and result['stage_nic']['passed'] and result['item2_observations']['observations_passed'])
    save(out/'result.json',result)
    return result


def transaction(root, index, collect, launch, *, clock=time.time):
    """Caller keeps one bench hold across collection, admission and launch.

    Exclusive durable intent markers forbid replay after crash/restart. Every
    completed probe is journaled before launching; old admission is never used
    by run_next after its owning hold has ended.
    """
    out=root/f'probe-{index}';out.mkdir(exist_ok=False)
    started=clock();save(out/'started.json',dict(started_utc_s=started,probe=index),exclusive=True)
    acquired=collect(out)
    fatal=acquired.get('fatal') is not False or acquired.get('health',{}).get('passed') is not True
    eligible=(not fatal and acquired.get('eligible') is True and acquired.get('rf',{}).get('eligible') is True
              and acquired.get('nic',{}).get('passed') is True)
    row=dict(probe=index,completed=True,scored=False,eligible=eligible,fatal=fatal,
             started_utc_s=started,ended_utc_s=clock(),result=str(out/'acquisition.json'),
             reasons=acquired['rf']['reasons'],rf=acquired['rf'],nic=acquired['nic'],health=acquired['health'])
    save(out/'result.json',row,exclusive=True);append(root/'probes.jsonl',row)
    if row['eligible']:
        save(root/'campaign-consumed.json',dict(probe=index,created_utc_s=clock(),
            reason='ONE attempt3 launch intent consumed before any scored prompt'),exclusive=True)
        return dict(action='campaign-complete',result=launch(),probe=row)
    append(root/'deferrals.jsonl',dict(row,next_probe_utc_s=None if row['fatal'] or index==5 else row['ended_utc_s']+1800,
        release_bench=True,utc=datetime.now(timezone.utc).isoformat()))
    return dict(action='fatal' if row['fatal'] else 'deferred',probe=row)


def run_next(binding_path,root):
    from echoear_item2_campaign import verified_binding,flash_image
    binding,image=verified_binding(binding_path)
    plan=load_plan()
    if str(root.resolve())!=binding['rf_execution']['root']:
        raise RuntimeError('Only the frozen RF execution root may be used')
    history=read_rows(root/'probes.jsonl')
    if (root/'campaign-consumed.json').exists():return dict(action='stop',reason='Single campaign already consumed')
    if history and history[-1]['eligible']:raise RuntimeError('Prior admission belongs to an ended hold; cannot reuse')
    expected={f'probe-{i}' for i in range(1,len(history)+1)}
    if {p.name for p in root.glob('probe-*') if p.is_dir()}!=expected:
        raise RuntimeError('Incomplete probe directory retained; no automatic replay')
    decision=next_action(history,time.time())
    if decision['action']!='probe':return decision
    campaign=Path(plan['campaign']['output'])
    if campaign.exists():raise RuntimeError('Attempt3 population already exists')
    for key in ('OFFLINE_BENCH_FIXTURE_DIR','OFFLINE_SKIP_GREETING','DISPLAY_HIL_COMMANDS','ECHOEAR_STRICT_MERGE'):
        os.environ.pop(key,None)
    os.environ.update(DG_BOARD='echoear',ECHOEAR_ACOUSTIC_QUALIFICATION='1',
        PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',RTP_EVIDENCE_MAX_PACKETS='20000',
        STEP9A_FIXTURES=str(Path(__file__).resolve().parent/'fixtures/echoear360'),STEP9A_BUDGET_PATH=binding['budget'],
        OFFLINE_USB_ABSENCE_TURNS='0',OFFLINE_RTVI_BURST_SWEEP='0',OFFLINE_BENCH_CONTINUOUS_AUDIO='0',ECHOEAR_AEC_FULL_DUPLEX='0')
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from step9a_echoear import bench,check_esptool
    from step9a_assets import asset_server
    check_esptool()
    with bench():
        verified_binding(binding_path)
        def collect(out):
            port=flash_image(image,out)
            ports.append(port)
            return collect_probe(port,out,binding['normal_elf_sha256'])
        ports=[]
        with asset_server():
            return transaction(root,decision['probe'],collect,
                lambda:launch_campaign(binding_path,binding,image,ports[-1]))


def execute(binding_path,root):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'launcher.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            outcome=run_next(binding_path,root)
            print(json.dumps(dict(action=outcome['action'],probe=outcome.get('probe',{}).get('probe'),
                rf_rate=outcome.get('probe',{}).get('rf',{}).get('link_tail',{}).get('tail_rate'),
                reason=outcome.get('reason'),until_utc_s=outcome.get('until_utc_s'))),flush=True)
            if outcome['action'] in ('campaign-complete','fatal','stop'):return outcome
            if outcome['action']=='deferred':
                print('BENCH RELEASED: RF deferral; A may take one short AEC hold during this wait',flush=True)
            # No bench, serial or service context survives run_next's return.
            # Wake frequently for interruption; persisted UTC deadline prevents
            # early probes after process restart.
            if outcome['action']=='wait':time.sleep(min(30,max(0,outcome['until_utc_s']-time.time())))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--binding',type=Path,required=True)
    p.add_argument('--root',type=Path,required=True);a=p.parse_args()
    outcome=execute(a.binding.resolve(),a.root.resolve())
    raise SystemExit(0 if outcome['action']=='campaign-complete' and outcome['result']['passed'] else 1)

#!/usr/bin/env python3
"""ONE three-turn WiFi observation; no scoring; always restore frozen9936."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=Path('/home/khkramer/src/evidence/echoear/B')
RESTORE=EVIDENCE/'item2-master-9bc2d42-normal-image'
RESTORE_SHA='9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758'


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path,value):
    path.write_text(json.dumps(value,indent=2)+'\n')


def rows(path):
    return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []


def bundle(path,expected=None):
    from demo_session import firmware_artifacts
    version=json.loads((path/'project_description.json').read_text())['project_version']
    actual=firmware_artifacts(path,version)
    if expected and actual!=expected:raise ValueError('Unexpected frozen image')
    flash=json.loads((path/'flasher_args.json').read_text())
    names={'dg-esp32.elf','project_description.json','flasher_args.json',*flash['flash_files'].values()}
    if any(not (path/p).resolve().is_relative_to(path.resolve()) for p in names):raise ValueError('Bundle path escapes image')
    return dict(path=str(path.resolve()),elf_sha256=actual,version=version,
                sha256={n:digest(path/n) for n in sorted(names)})


def prepare(image,out,release):
    # A supplies the frozen allowlisted config/provenance manifest; no rebuild
    # or selection of a random current build directory is permitted here.
    metadata=json.loads((image/'manifest.json').read_text())
    flags=json.loads((image/'build-flags.json').read_text())
    if not (flags['DG_WIFI_OBSERVE'] is True and flags['DG_ECHOEAR_AEC'] is False
            and flags['DG_BOARD_ECHOEAR'] is True and type(flags['DG_DEMO_AUTOSTART']) is bool):
        raise ValueError('Require observerON/AECoff/EchoEar image')
    for name,entry in metadata['files'].items():
        p=(image/name).resolve()
        if not p.is_relative_to(image.resolve()) or digest(p)!=entry['sha256'] or p.stat().st_size!=entry['bytes']:
            raise ValueError('Frozen A package changed: '+name)
    source=metadata.get('source_commit',metadata.get('commit'))
    subprocess.run(['git','diff','--quiet','2a6acb223bc09d4b02ffe04beb3059f73f0592f1',source,'--',
        'main','components','CMakeLists.txt','sdkconfig.defaults','sdkconfig.defaults.echoear','partitions.csv'],cwd=ROOT,check=True)
    observer=bundle(image,metadata['elf_sha256']);restore=bundle(RESTORE,RESTORE_SHA)
    original=EVIDENCE/'item2-final-master-9bc2d42/strict_voice_cat/attempt-2/result.json'
    prompts=[Path(r['calibration']['prompt']['path']) for r in json.loads(original.read_text())['runs']]
    if len(prompts)!=3:raise ValueError('Exactly3 retained normal prompts required')
    files=[ROOT/p for p in subprocess.check_output(['git','ls-files','-z','--','hil','server'],cwd=ROOT).decode().split('\0') if p]
    budget=EVIDENCE/'item2-final-master-9bc2d42/arena-budget.json'
    files += [Path(__file__),ROOT/'hil/echoear_wifi_correlation.py',ROOT/'hil/wifi_observation.py',
        ROOT/'hil/echoear_uplink_analysis.py',image/'manifest.json',image/'build-flags.json',original,release,budget,*prompts]
    files += [image/name for name in metadata['files']]
    out.mkdir(parents=True,exist_ok=False)
    plan=dict(output=str(out.resolve()),budget=str(budget),schema=1,qualification=False,scoring_performed=False,acquisitions=1,turns=3,retakes=0,
        observer=observer,restore=restore,observer_provenance=metadata,observer_flags=flags,coordinator_release=str(release),
        startup='Queue demo hold immediately after full-hash reset record, before READY wait/bot start; require held=true,initializing=false,attempts=0; no persisted demo setting writes',
        host_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        window_seconds=24,settle_seconds=6,cat_fps=30,quiet=1,volume_db=0,
        sctp_chunk_bytes=1100,mode='live-test',options={'arrival-trace':1,'call-timing':1},
        prompts=[str(p) for p in prompts],no_midwindow_perf_or_exports=True,
        export='wifi-observe only after all3windows/audiooff/disconnect; trace drains only betweenwindows',
        unsupported_metrics=dict(tx_retries=None,send_to_air_us=None,channel_occupancy=None),
        validity='Raw/resource/identity faults fatal; incomplete mappings/overwritten telemetry explicitly inconclusive, no retries or qualification',
        source_provenance=dict(wifi_decoder='2a6acb223bc09d4b02ffe04beb3059f73f0592f1',
                               ordered_send_analysis='c966020'),
        input_sha256={str(p.resolve()):digest(p) for p in sorted(set(files))})
    save(out/'plan.json',plan)
    return plan


def verify_plan(path):
    plan=json.loads(path.read_text())
    if str(path.parent.resolve())!=plan['output']:raise ValueError('Only the declared observation output may execute')
    if plan['budget'] not in plan['input_sha256']:raise ValueError('Unbound arena budget')
    if plan.get('qualification') is not False or plan.get('scoring_performed') is not False or plan['turns']!=3 or plan['retakes']!=0:
        raise ValueError('One observation-only3-turn plan required')
    for name,want in plan['input_sha256'].items():
        if digest(Path(name))!=want:raise ValueError('Frozen input changed: '+name)
    for b in (plan['observer'],plan['restore']):
        if bundle(Path(b['path']),b['elf_sha256'])!=b:raise ValueError('Frozen bundle changed')
    if plan['restore']['elf_sha256']!=RESTORE_SHA:raise ValueError('Wrong restore image')
    return plan


def hold_demo(board):
    cursor=board.cursor
    board.command('demo hold',45)
    board.command('demo status')
    values=[json.loads(line.split(' ',1)[1]) for _,_,line in board.lines_since(cursor) if line.startswith('DEMO ')]
    if len(values)!=1 or values[0].get('held') is not True or values[0].get('initializing') is not False or values[0].get('attempts')!=0:
        raise RuntimeError('Autostart hold did not precede automatic connection; no replacement acquisition')
    return values[0]


def sample_perf(board):
    from e2e_common import perf
    from step9a_merge_checks import memory_gate
    value=perf(board)
    if not memory_gate([value])['passed']:
        raise RuntimeError('Observation resource guard: internal heap or allocation failure')
    h=value.get('display',{})
    if any(type(h.get(k)) is not int or h[k]!=0 for k in ('lock_failures','lock_timeouts')):
        raise RuntimeError('Observation display lock guard')
    return value


def cleanup(board):
    errors=[];board.display_cleanup=True
    for cmd in ('audio 0','disconnect','disp bench off','playback-volume -6'):
        try:board.command(cmd,35)
        except Exception as exc:errors.append(cmd+': '+str(exc))
    return errors


def wifi_stats(board,out,label):
    cursor=board.cursor;error=None
    try:board.command('wifi-stats',10)
    except RuntimeError as exc:error=str(exc)
    # lines_since still enforces the raw monitor; an actual raw error is fatal.
    raw=[line for _,_,line in board.lines_since(cursor)]
    save(out/f'wifi-stats-{label}.json',dict(error=error,lines=raw,
        interpretation='Raw driver dump; field definitions/reset semantics not assumed; retries unavailable unless separately established'))


def acquire(port,out,plan):
    from echoear_transport_probe import TimedSerial
    from step7_support import BotServer
    from step9a_support import setup,host_address
    from alsa import resolve_jabra,play_wav
    from arrival_trace import drain
    from wifi_observation import dump
    from echoear_attempt3 import health
    from host_nic import snapshot,gate
    from echoear_checks import codec_boot_evidence
    result=dict(qualification=False,scoring_performed=False,windows=[],cleanup_errors=[],nic_before=snapshot())
    board=None
    try:
        card,device=resolve_jabra()
        # Observe current controls; do not recalibrate or change mixer settings.
        with (out/'mixer-readback.log').open('w') as log:
            subprocess.run(['amixer','-c',card,'scontents'],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=5)
        with ExitStack() as resources:
            board=resources.enter_context(TimedSerial(port,log_path=out/'serial.log',display_checks=True,reset_on_open=False))
            try:
                cursor=board.cursor;board.reset()
                result['demo_hold']=hold_demo(board)
                board.wait_for('READY transport-console',35,since=cursor)
                result['boot_line']=board.wait_for(r'^I \([0-9]+\) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+plan['observer']['elf_sha256']+'$',5,since=cursor)
                result['codec_boot']=codec_boot_evidence('\n'.join(x[2] for x in board.lines_since(cursor)))
                if not result['codec_boot']['passed']:raise RuntimeError('Codec startup readback invalid')
                bot=resources.enter_context(BotServer(out/'bot','live-test'))
                for cmd in ('audio 0','quiet 1','playback-volume 0','server-url http://'+host_address()+':7861'):
                    board.command(cmd)
                result['cold_perf']=sample_perf(board);result['display_setup']=setup(board,mode='cat',rate=30)
                for name,value in plan['options'].items():board.command(f'peer-option {name} {value}')
                cursor=board.cursor;board.command('connect',90);board.command('audio 1')
                closed=board.wait_for_entry(r'^GATE closed t=',20,since=cursor)
                opened=board.wait_for_entry(r'^GATE open t=',20,since=closed[0])
                result['greeting_open']=dict(host_s=opened[1],line=opened[2]);time.sleep(plan['settle_seconds'])
                wifi_stats(board,out,'before');result['whole_before']=sample_perf(board)
                pc=result['whole_before']['pc_id'];generation=result['whole_before']['generation']
                for index,prompt in enumerate(plan['prompts'],1):
                    row=dict(index=index,pc_id=pc,generation=generation,before=sample_perf(board));result['windows'].append(row)
                    if row['before']['pc_id']!=pc or row['before']['generation']!=generation:raise RuntimeError('Unexpected session replacement')
                    drain(board,out/f'trace{index}-before.jsonl');time.sleep(1)
                    cursor=board.cursor;start=time.monotonic()+.1;end=start+plan['window_seconds']
                    row.update(start_s=start,end_s=end,declared_host_s=time.monotonic())
                    save(out/f'window{index}.json',row)
                    time.sleep(max(0,start-time.monotonic()))
                    row['prompt_start_s']=time.monotonic();play_wav(prompt,device);row['prompt_end_s']=time.monotonic()
                    closed=board.wait_for_entry(r'^GATE closed t=',max(.1,end-time.monotonic()),since=cursor)
                    opened=board.wait_for_entry(r'^GATE open t=',max(.1,end-time.monotonic()),since=closed[0])
                    row['reply_gate_open']=dict(host_s=opened[1],line=opened[2])
                    while time.monotonic()<end+1:
                        board.display_monitor.check();time.sleep(min(.25,max(0,end+1-time.monotonic())))
                    row['after']=sample_perf(board);drain(board,out/f'trace{index}.jsonl')
                    row['export_trace']=sample_perf(board)['peer_profile']['trace']
                    if row['after']['pc_id']!=pc or row['after']['generation']!=generation:raise RuntimeError('Session changed during normal turn')
                    events=[e for e in bot.events() if e.get('pc_id')==pc and start<=e['t']<=end]
                    row['transcripts']=[e for e in events if e['event']=='final-transcript']
                    row['first_tts']=[e for e in events if e['event']=='first-tts-byte']
                    row['normal_turn_observed']=bool(row['transcripts'] and row['first_tts'])
                    if not row['normal_turn_observed']:raise RuntimeError('Normal transcript/reply not observed; no replacement')
                    save(out/'partial.json',result);print('Observation turn',index,'acquired',flush=True)
                result['whole_after']=sample_perf(board)
                board.command('rtp-stats');time.sleep(1)
                board.command('audio 0');board.command('disconnect',35)
                result['wifi_observation']=dump(board,out/'wifi-observation.json')
                wifi_stats(board,out,'after');result['completed']=True
            finally:
                result['cleanup_errors']+=cleanup(board)
                result['controller']=board.display_monitor.result()
    except BaseException as exc:result['error']=type(exc).__name__+': '+str(exc)
    finally:
        result['nic_after']=snapshot();result['nic_observation']=gate(result['nic_before'],result['nic_after'])
        if board is not None:
            result['controller']=board.display_monitor.result()
            if getattr(board,'_error',None) is not None:result.setdefault('error','Serial reader failed after closure')
        result['health']=health(out,result.get('controller',{}),result['cleanup_errors'])
        result['completed']=bool(result.get('completed') and not result.get('error') and result['health']['passed'])
        save(out/'acquisition.json',result)
    return result


def restore_image(image,out):
    from echoear_item2_campaign import flash_image
    from esp_serial import EspSerial
    out.mkdir(exist_ok=False);port=flash_image(image,out)
    with EspSerial(port,log_path=out/'serial.log',display_checks=True,reset_on_open=False) as board:
        cursor=board.cursor;board.reset();board.wait_for('READY transport-console',35,since=cursor)
        line=board.wait_for(r'^I \([0-9]+\) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+RESTORE_SHA+'$',5,since=cursor)
        board.command('audio 0');board.command('disconnect',35)
        board.display_monitor.check()
    # Join/close the reader before checking the complete retained byte stream.
    from step9a_r2_checks import audit
    ledger=board.display_monitor.result()
    if getattr(board,'_error',None) is not None:raise RuntimeError('Restore serial reader failed')
    resets=[b['index'] for b in ledger['boot_epochs'] if b['requested']]
    raw=audit((out/'serial.log').read_bytes(),expected_boot_indices=resets)
    if not ledger['passed'] or not raw['passed']:raise RuntimeError('Restore closed-log audit failed')
    value=dict(verified=True,boot_line=line,monitor=ledger,closed_raw_audit=raw)
    save(out/'result.json',value);return value


def run_hold(out,plan,flash,collect,restore):
    """One-use directory and finally restoration survive any acquisition fault."""
    acquisition=out/'acquisition';acquisition.mkdir(exist_ok=False)
    result=dict(qualification=False,scoring_performed=False,restore=dict(verified=False))
    try:
        port=flash(Path(plan['observer']['path']),acquisition)
        result['acquisition']=collect(port,acquisition,plan)
    except BaseException as exc:result['error']=type(exc).__name__+': '+str(exc)
    finally:
        try:result['restore']=restore(Path(plan['restore']['path']),out/'restore-9936')
        except BaseException as exc:result['restore']=dict(verified=False,error=type(exc).__name__+': '+str(exc))
        save(out/'result.json',result)
    return result


def analyze(out):
    from echoear_wifi_correlation import correlate
    result=json.loads((out/'result.json').read_text());a=result.get('acquisition',{})
    path=out/'acquisition';commands=rows(path/'commands.jsonl')
    packets=rows(path/'rtp/rtp.jsonl');events=rows(path/'bot/events.jsonl')
    for w in a.get('windows',[]):
        w['mid_window_commands']=[c for c in commands if c['start']<w['end_s'] and c['end']>w['start_s']]
        w['trace_overwritten']=w.get('export_trace',{}).get('overwritten')
        reports=[e for e in events if e.get('pc_id')==w['pc_id'] and e['event']=='rtvi-out'
            and e.get('message',{}).get('data',{}).get('t')=='rtp-stats']
        post=[e for e in reports if e['t']>=w['end_s']]
        w['receive_capture_status']=dict(post_window_reports=len(post),
            truncated=False if post and all(e['message']['data']['d'].get('truncated') is False for e in reports)
            and sum(p['pc_id']==w['pc_id'] for p in packets)<20000 else None)
    if a.get('wifi_observation'):
        result['correlation']=correlate(a['wifi_observation'],a.get('windows',[]),packets,
            {w['index']:rows(path/f'trace{w["index"]}.jsonl') for w in a.get('windows',[])},
            callback_packets=rows(path/'rtp/socket-rtp.jsonl'))
    else:result['correlation']=dict(available=False,reason='No valid buffered WiFi export',qualification=False)
    result['raw_sha256']={str(p.relative_to(out)):digest(p) for p in out.rglob('*') if p.is_file() and p.name!='result.json'}
    save(out/'result.json',result);return result


def execute(out):
    plan=verify_plan(out/'plan.json')
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
        STEP9A_BUDGET_PATH=plan['budget'],
        RTP_EVIDENCE_DIR=str(out/'acquisition/rtp'),RTP_EVIDENCE_MAX_PACKETS='20000',
        STEP8E_EVENT_LOOP_PROBE='1',PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',ECHOEAR_AEC_FULL_DUPLEX='0',
        OFFLINE_USB_ABSENCE_TURNS='0',OFFLINE_RTVI_BURST_SWEEP='0',OFFLINE_BENCH_CONTINUOUS_AUDIO='0')
    for key in ('ECHOEAR_STRICT_MERGE','ECHOEAR_ACOUSTIC_QUALIFICATION','OFFLINE_BENCH_FIXTURE_DIR','OFFLINE_SKIP_GREETING','DISPLAY_HIL_COMMANDS'):
        os.environ.pop(key,None)
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from step9a_echoear import bench,check_esptool
    from step9a_assets import asset_server
    from echoear_item2_campaign import flash_image
    check_esptool()
    with bench():
        verify_plan(out/'plan.json')
        # Asset service is closed before restoring, even if its context fails.
        def collect(port,path,p):
            with asset_server():return acquire(port,path,p)
        result=run_hold(out,plan,flash_image,collect,restore_image)
    verify_plan(out/'plan.json');result=analyze(out)
    print(json.dumps(dict(observed_turns=len(result.get('acquisition',{}).get('windows',[])),
        completed=result.get('acquisition',{}).get('completed',False),restore_verified=result['restore']['verified'],qualification=False)))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--prepare-image',type=Path);p.add_argument('--release',type=Path)
    a=p.parse_args()
    if a.prepare_image:
        if not a.release:p.error('--release evidence required')
        prepare(a.prepare_image.resolve(),a.out.resolve(),a.release.resolve())
    else:
        r=execute(a.out.resolve());raise SystemExit(0 if r['restore']['verified'] and r.get('acquisition',{}).get('completed') else 1)

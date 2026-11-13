#!/usr/bin/env python3
"""ONE combined item1 acquisition on the step-10 requal-n1 demo (autostart) image, decision 134."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
# Step 10 requal-n1 (decision 134): demo variant of the merged AEC-off line, built at master 16c45a3.
# The c3a02be/86229001 candidate of B/item1-c3a02be-combined-4 is historical.
CANDIDATE='16c45a3e26bbb11355c1b949aeb86808b3a5cce8'
ELF='5bbd868adceb8f4d8ff99cfe0e077d5b249a29d29a5aae766b8c61f664939057'
EVIDENCE=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-10')
# Decision 134 D5, item1 only: "At most 1 lost/concealed/PLC/playout-dropped downlink frame per session, with the
# RF pre-gate recorded. This reverses the all-zero rule for item1 only; the item2 strict stage's own transport
# gates are unchanged."
# r2 (Astra r1 #3, Fable F1; adopted by decision 135): one DISTINCT affected frame. Firmware (transport.cpp/playout.cpp): lost =
# missing_slots; concealed and plc_frames are the same plc_calls counter (PLC only on a missing slot);
# playout_dropped = every ingress rejection (late, duplicate, window, capacity, conflict, ...), a separate
# mechanism. A late rejection is NOT proven to be the frame already counted missing (it can also be a duplicate of
# an already decoded frame, a packet below the startup floor, or one from before a cancel/restart), and item1 runs
# without the optional arrival ring, so no retained frame identity can prove the overlap. Hence the conservative
# bound: concealed == plc_frames <= lost and max(lost, concealed, plc_frames) + playout_dropped <= 1.
# The combined-4 shape 1/1/1/1 (late=1) therefore FAILS this rule; combined-4 stays FAIL of record, not rescored.
DOWNLINK_LOSS_COUNTERS=('lost','concealed','plc_frames','playout_dropped')
DOWNLINK_FRAME_BUDGET=1
RF_PRE_GATE_SECONDS=30


def validate_image(image):
    from demo_session import firmware_artifacts
    image=image.resolve();manifest=json.loads((image/'manifest.json').read_text())
    if manifest['commit']!=CANDIDATE or manifest['elf_sha256']!=ELF or manifest.get('profile')!='demo' or manifest.get('autostart',True) is not True:
        raise RuntimeError('Exact announced demo candidate required')
    hashes=manifest.get('sha256') or {name:row['sha256'] for name,row in manifest['files'].items()}
    for name,want in hashes.items():
        path=(image/name).resolve()
        if not path.is_relative_to(image) or hashlib.sha256(path.read_bytes()).hexdigest()!=want:
            raise RuntimeError('Frozen artifact changed: '+name)
    version=json.loads((image/'project_description.json').read_text())['project_version']
    if version!=CANDIDATE[:7] or firmware_artifacts(image,version)!=ELF:raise RuntimeError('Embedded candidate provenance mismatch')
    flash=json.loads((image/'flasher_args.json').read_text())
    if any(name not in hashes for name in flash['flash_files'].values()):raise RuntimeError('Unhashed flash input')
    return manifest,flash


def distinct_frame_gate(values):
    """Decision 134 D5 (r2): at most one distinct affected downlink frame, from four counters (totals or deltas)."""
    lost,concealed,plc,dropped=(values.get(k) for k in DOWNLINK_LOSS_COUNTERS)
    if not all(type(v) is int and v>=0 for v in (lost,concealed,plc,dropped)):return False
    return concealed==plc and concealed<=lost and max(lost,concealed,plc)+dropped<=DOWNLINK_FRAME_BUDGET


def zero_session_gate(sample):
    from playback_quality import TRANSPORT_COUNTERS
    keys=(*TRANSPORT_COUNTERS,'mic_in_turn_clips','mic_gate_open_clips','mic_gate_closed_clips')
    values={k:sample.get(k) for k in keys}
    others=all(type(v) is int and v==0 for k,v in values.items() if k not in DOWNLINK_LOSS_COUNTERS)
    return dict(passed=sample.get('playout_schema')==2 and bool(sample.get('pc_id')) and others and distinct_frame_gate(values),
        values=values,loss_rule=dict(counters=list(DOWNLINK_LOSS_COUNTERS),distinct_frames_per_session=DOWNLINK_FRAME_BUDGET,decision=134,
            rule='concealed == plc_frames <= lost; max(lost, concealed, plc_frames) + playout_dropped <= 1'),
        basis='Absolute cumulative counters from this fresh Session: at most one distinct affected downlink frame '
              '(decision 134 D5, item1 only), every other transport and mic clip counter zero; includes startup and the no-console block')


def session_transport_gate(transport):
    """Within-session delta check for item1 cycles and the restart turn under decision 134 D5 (same distinct-frame rule).

    Same session and schema are still required (transport_gate 'available' and not 'session-changed')."""
    deltas=transport.get('deltas') or {}
    others=all(type(v) is int and v==0 for k,v in deltas.items() if k not in DOWNLINK_LOSS_COUNTERS)
    return (transport.get('available') is True and transport.get('classification')!='session-changed' and bool(deltas)
        and others and distinct_frame_gate(deltas))


def rf_pre_gate(snapshot,gate,sleep=time.sleep,seconds=RF_PRE_GATE_SECONDS):
    """Board-path wlp8s0 RF pre-gate before any flash or serial use, recorded for decision 134 D5."""
    before=snapshot();sleep(seconds);result=gate(before,snapshot());result['pre_gate_seconds']=seconds
    return result


def sample_gate(samples):
    from step9a_merge_checks import memory_gate
    memory=memory_gate(samples)
    health=[s.get('display',{}) for s in samples]
    checks=dict(memory=memory['passed'],display=bool(health) and all(h.get('lcd_initialized') is True and
        h.get('engine_snapshot') is True and not h.get('dead',True) and not h.get('failed',True) and
        all(type(h.get(k)) is int and h[k]==0 for k in ('dma_errors','lock_timeouts','lock_failures','send_failures','late_callbacks')) for h in health),
        zero_session=bool(samples) and all(zero_session_gate(s)['passed'] for s in samples))
    tx_rows=[[t for t in s.get('tasks',[]) if t.get('name')=='audio-tx'] for s in samples]
    tx=[t for rows in tx_rows for t in rows]
    checks['tx_stack']=bool(samples) and all(len(rows)==1 and rows[0].get('stack_bytes')==28672 and
        rows[0].get('stack_free_bytes',0)>=4096 for rows in tx_rows)
    return dict(passed=all(checks.values()),checks=checks,memory=memory,
        tx_minimum=min((t.get('stack_free_bytes',0) for t in tx),default=None))


def bot_inventory(bot):
    text=(bot.out/'bot.log').read_text(errors='replace')
    return dict(peers=sorted(set(re.findall(r'peer: (SmallWebRTCConnection#[\w-]+)',text))),
        pipelines=sorted(set(re.findall(r'Linking (Pipeline#\d+)::Source',text))),
        starts=len(re.findall(r'POST /start HTTP/1.1" 200',text)),ready=text.count('client is ready'))


def fresh_bot(before,after):
    peers=sorted(set(after['peers'])-set(before['peers']));pipelines=sorted(set(after['pipelines'])-set(before['pipelines']))
    return dict(passed=len(peers)==len(pipelines)==1 and after['starts']==before['starts']+1 and after['ready']==before['ready']+1,
                peers=peers,pipelines=pipelines,before=before,after=after)


def completed_boot_screens(lines):
    """Fresh boot goes Connecting -> Live; Disconnected belongs to teardown."""
    chosen=[]
    for _,_,line in lines:
        if not line.startswith('DISPLAY_DEMO_RENDERED '):continue
        row=json.loads(line.split(' ',1)[1])
        if row.get('screen')==(1 if not chosen else 2):
            chosen.append(row)
            if len(chosen)==2:break
    return dict(available=True,passed=len(chosen)==2 and
        all(type(row.get(key)) is int and row[key]>0 for row in chosen for key in ('completed_us','rendered')) and
        chosen[1]['completed_us']>chosen[0]['completed_us'] and chosen[1]['rendered']>chosen[0]['rendered'],frames=chosen)


class ConsoleLedger:
    def __init__(self,send):self.send=send;self.phase='setup';self.commands=[]
    def __call__(self,text):
        row=dict(phase=self.phase,host_s=time.monotonic(),command=text);self.commands.append(row)
        if self.phase=='autonomous':raise AssertionError('Console write forbidden during three autonomous turns')
        return self.send(text)


def main(argv=None,restore_wait=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--binding',type=Path,required=True,help='r11: the smoke or acceptance binding this item1 run belongs to')
    args=p.parse_args(argv)
    # Decision 141: demo_bot refuses the removed override; refuse here, before the lock, flash or scoring.
    if 'ECHOEAR_AEC_FULL_DUPLEX' in os.environ:p.error('ECHOEAR_AEC_FULL_DUPLEX was removed (decision 141); unset it')
    image=args.image.resolve();manifest,flash=validate_image(image);out=args.out.resolve()
    # r11 (Astra r10 #1, Fable B1): item1 is bound to the same binding as item2 and records its provenance.
    from echoear_item2_campaign import verified_binding,binding_provenance,bound_out
    binding,_=verified_binding(args.binding.resolve())
    # r14 (Fable r13 N1): --out must be <output_root>/item1/attempt-<n> of the binding; refused before the lock.
    try:out=bound_out(binding,'item1',out)
    except RuntimeError as exc:p.error(str(exc))
    if binding.get('demo_elf_sha256')!=ELF or Path(binding.get('demo_image','')).resolve()!=image:
        p.error('Binding names a different demo image')
    provenance=dict(binding_provenance(args.binding),out_path=str(out))
    # r15: decision 138a applies when the bound plan revision declares it (never the frozen r1 plan).
    try:d138=json.loads(Path(binding['policy_path']).read_text()).get('decision_138') or {}
    except (KeyError,OSError,ValueError):d138={}
    d138_uplink=d138.get('uplink_gate')=='source-cadence'
    # r16 (decision 139a/c): item1 S2 ratified; pcm_queue_drops tolerance is a declared plan field (default 0).
    item1_pcm_max=(d138.get('item1_s2') or {}).get('pcm_queue_drops_max',0)
    if not out.is_relative_to(EVIDENCE):p.error('Evidence must remain under '+str(EVIDENCE))
    out.mkdir(parents=True,exist_ok=False)
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from step9a_echoear import bench,verify,esptool_python,check_esptool
    from demo_session import provision_assets,cleanup_board
    from step9a_assets import asset_server
    from step9a_support import host_address
    from esp_serial import EspSerial
    from bot_restart import DemoBot,restart_and_recover,completed_screens
    from standalone_proof import live_turn
    from step7_support import prepare_prompt
    from alsa import resolve_jabra,set_volumes
    from transport_test import perf
    from playback_quality import transport_gate,microphone_clip_gate
    from echoear_touch_proof import inject_during_connecting
    from host_nic import snapshot,gate as nic_gate
    from echoear_item2_campaign import RestoreHold,write_final
    from echoear_image_profile import boot_gate,session_gate
    result=dict(passed=False,candidate=CANDIDATE,elf_sha256=ELF,manifest=manifest,**provenance,
        harness_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        predeclared=dict(autonomous_turns=3,injected_cycles=3,restart_turns=1,retakes=0,
            internal_floor=81920,largest_floor=32768,transport_other_deltas=0,connecting_injection_required=True,
            downlink_loss_per_session=dict(counters=list(DOWNLINK_LOSS_COUNTERS),distinct_frames=DOWNLINK_FRAME_BUDGET,decision=134,
                rule='concealed == plc_frames <= lost; max(lost, concealed, plc_frames) + playout_dropped <= 1 (totals and deltas)',
                text='At most 1 lost/concealed/PLC/playout-dropped downlink frame per session, with the RF pre-gate recorded. '
                     'This reverses the all-zero rule for item1 only; the item2 strict stage\'s own transport gates are unchanged.'),
            rf_pre_gate_seconds=RF_PRE_GATE_SECONDS,image_profile='demo',restore='bench restore-of-record (echoear_aec_bench.NORMAL_HASH, 55152f25 since gap 3), boot-hash verified before unlock'),
        autonomous_turns=[],cycles=[],samples=[],cleanup_errors=[],nic_gate_enforced=False,
        physical_touch_recognition=False,strict_voice_acoustic_qualification=False)
    (out/'predeclared.json').write_text(json.dumps(result,indent=2)+'\n')
    def save():(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    def wait(fn,seconds=60):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            value=fn()
            if value:return value
            time.sleep(.02)
        raise TimeoutError('Missing predeclared combined-proof evidence')
    board=None;ledger=None;hold=None;check_esptool();print('Waiting for exclusive bench lock',flush=True)
    try:
        with bench(),RestoreHold(out,wait=restore_wait) as hold,ExitStack() as stack:
            print('Bench acquired: ONE flash/provision/autonomous3/injected3/restart proof',flush=True)
            # Before any flash, serial or mixer use; a failed pre-gate is unscored (nothing touched).
            result['rf_pre_gate']=rf_pre_gate(snapshot,nic_gate);(out/'rf-pre-gate.json').write_text(json.dumps(result['rf_pre_gate'],indent=2)+'\n')
            if not result['rf_pre_gate']['passed']:
                result.update(scored=False,deferred=True);raise RuntimeError('RF pre-gate failed before flash; unscored')
            result['scored']=True
            port=verify(out);hold.touch()
            cmd=[esptool_python(),'-m','esptool','--chip','esp32s3','--port',port,'--baud','460800','write-flash',*flash['write_flash_args']]
            for address,name in flash['flash_files'].items():cmd.extend([address,str(image/name)])
            with (out/'flash.log').open('w') as log:subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
            port=verify(out);card,device=resolve_jabra();set_volumes(card,out/'mixer.log');prompt=prepare_prompt(out)
            first=stack.enter_context(DemoBot(out/'bot-before'))
            board=EspSerial(port,log_path=out/'serial.log',display_checks=True);stack.callback(board.close)
            ledger=ConsoleLedger(board.send);board.send=ledger
            cleanup_done=False
            def cleanup():
                nonlocal cleanup_done
                if cleanup_done:return
                cleanup_done=True;ledger.phase='cleanup';board.display_cleanup=True
                try:board.command('demo hold',45)
                except Exception as exc:result['cleanup_errors'].append('demo hold: '+str(exc))
                try:result['cleanup_errors'].extend(cleanup_board(board))
                except Exception as exc:result['cleanup_errors'].append('cleanup: '+str(exc))
                if result['cleanup_errors']:result['passed']=False
            stack.callback(cleanup)
            def samples(count=1):
                rows=[perf(board) for _ in range(count)];result['samples'].extend(rows)
                verdict=sample_gate(rows)
                if not verdict['passed']:raise AssertionError('Strict transport/heap/display sample gate: '+json.dumps(verdict))
                return rows
            def touch():
                cursor=board.cursor;board.command('touch-status');line=board.wait_for(r'^DEMO_TOUCH_STATUS ',3,since=cursor)
                return json.loads(line.split(' ',1)[1])
            def screens(cursor):return completed_screens(board.lines_since(cursor))
            def live(bot,index):
                cursor=board.cursor;row=live_turn(board,bot.proc,bot.out,prompt,device,out,index)
                board.wait_for(r'GATE open t=\d+',10,since=cursor)
                return row
            # All provisioning writes and fixture service end before proof boot.
            cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
            with asset_server():result['provision']=provision_assets(board,'http://'+host_address()+':7861',out/'arena-budget.json',volume_db=0)
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1',7874))==0:raise RuntimeError('Fixture origin still running at proof boot')
            before=bot_inventory(first);ledger.phase='autonomous';proof_cursor=board.cursor;result['proof_boot_at']=time.monotonic()
            result['autonomous_nic_before']=snapshot();board.reset()
            board.wait_for('READY transport-console',60,since=proof_cursor)
            boot=board.wait_for(r'elf_sha256=([0-9a-f]{64})',5,since=proof_cursor)
            result['boot_elf_sha256']=re.search(r'elf_sha256=([0-9a-f]{64})',boot)[1]
            if result['boot_elf_sha256']!=ELF:raise RuntimeError('Running image differs from exact frozen candidate')
            result['boot_profile']=boot_gate([line for _,_,line in board.lines_since(proof_cursor)],'demo')
            if not result['boot_profile']['passed']:raise RuntimeError('Boot public profile mismatch: '+json.dumps(result['boot_profile']['checks']))
            board.wait_for('demo-boot: RESTORED assets=16 no_http=1',90,since=proof_cursor)
            initial_ready=board.wait_for(r'STATE Ready gen=\d+ desired=1 t=\d+',120,since=proof_cursor)
            generation=int(re.search(r'gen=(\d+)',initial_ready)[1]);result['initial_ready']=initial_ready
            for i in range(1,4):
                result['autonomous_turns'].append(live(first,i));save();print('Autonomous turn '+str(i)+' completed',flush=True)
            end=board.cursor;phase_lines=board.lines_since(proof_cursor)
            result['no_console']=dict(start_cursor=proof_cursor,end_cursor=end,
                commands=[c for c in ledger.commands if c['phase']=='autonomous'],
                acknowledgments=[line for _,_,line in phase_lines if re.match(r'^(?:OK|ERR) #',line)],
                ready_count=sum(bool(re.search(r'\bSTATE Ready ',line)) for _,_,line in phase_lines))
            ledger.phase='counter-checks';result['autonomous_nic']=nic_gate(result['autonomous_nic_before'],snapshot())
            result['initial_screens']=wait(lambda:completed_boot_screens(board.lines_since(proof_cursor))
                if completed_boot_screens(board.lines_since(proof_cursor))['passed'] else None,5)
            result['initial_bot']=fresh_bot(before,bot_inventory(first))
            if not result['initial_bot']['passed']:raise AssertionError('Initial fresh peer/pipeline not proven')
            # Reading only AFTER all3 turns preserves the no-console block.
            # Absolute zero is stricter than subtracting an observed baseline.
            initial_samples=samples(2);previous=initial_samples[-1]['pc_id']
            board.command('mic-gain-status') # device-reported analog/digital gain and limiter (public profile)
            if previous!=result['initial_bot']['peers'][0] or generation!=initial_samples[-1]['generation']:raise AssertionError('Autonomous session changed')
            for cycle in range(1,4):
                ledger.phase='injected-cycle-'+str(cycle);arm=dict(number=cycle,passed=False,nic_before=snapshot());result['cycles'].append(arm)
                before_touch=touch();inventory=bot_inventory(first);cursor=board.cursor;arm['old_perf']=samples()[0]
                board.command('touch-inject');wait(lambda:any('STATE Disconnected ' in line for _,_,line in board.lines_since(cursor)),5)
                wait(lambda:touch()['phase']==2,3);board.command('touch-inject')
                wait(lambda:touch()['driver']['injected_ignored']==before_touch['driver']['injected_ignored']+1,3)
                arm['connecting_injection']=inject_during_connecting(touch,lambda:board.command('touch-inject'),wait)
                ready=board.wait_for(r'STATE Ready gen=\d+ desired=1 t=\d+',90,since=cursor)
                arm['generation']=int(re.search(r'gen=(\d+)',ready)[1]);wait(lambda:touch()['phase']==1,5)
                arm['screens']=wait(lambda:screens(cursor) if screens(cursor)['passed'] else None,5)
                arm['bot']=fresh_bot(inventory,bot_inventory(first));arm['touch_before']=before_touch;arm['touch_after']=touch()
                before_reply=samples()[0]
                if not arm['bot']['passed'] or before_reply['pc_id']!=arm['bot']['peers'][0] or before_reply['generation']!=arm['generation']:
                    raise AssertionError('Injected session identity differs from fresh bot peer/generation')
                arm['turn']=live(first,3+cycle);after_reply=samples(2)[-1]
                transport=transport_gate(before_reply,after_reply);clips=microphone_clip_gate(before_reply,after_reply)
                arm['transport']=transport;arm['clips']=clips;arm['before_perf']=before_reply;arm['after_perf']=after_reply
                after_touch=arm['touch_after'];arm['checks']=dict(screens=arm['screens']['passed'],fresh_bot=arm['bot']['passed'],
                    fresh_generation=arm['generation']>generation and before_reply['pc_id']!=previous,
                    one_request=after_touch['requests']==before_touch['requests']+1,request_rejected=after_touch['request_rejected']==0,
                    injections=after_touch['driver']['injected']==before_touch['driver']['injected']+3,
                    ignored=after_touch['driver']['injected_ignored']==before_touch['driver']['injected_ignored']+2,
                    connecting=arm['connecting_injection']['passed'],turn=arm['turn']['passed'],clips=clips['passed'],
                    transport=session_transport_gate(transport))
                arm['passed']=all(arm['checks'].values());arm['nic_observation']=nic_gate(arm['nic_before'],snapshot());save()
                print('Injected cycle '+str(cycle)+': '+str(arm['passed']),flush=True)
                if not arm['passed']:raise AssertionError('Injected cycle failed')
                previous=after_reply['pc_id'];generation=arm['generation']
            ledger.phase='bot-restart';result['restart']={};restart_before=bot_inventory(first);result['restart_nic_before']=snapshot()
            def start_after(remaining):
                second=DemoBot(out/'bot-after');stack.callback(second.close);return second.start(timeout=remaining)
            def recovered(second):
                before_reply=samples()[0];row=live(second,7);after_reply=samples(2)[-1]
                row.update(before_perf=before_reply,after_perf=after_reply,transport=transport_gate(before_reply,after_reply),clips=microphone_clip_gate(before_reply,after_reply))
                inventory=bot_inventory(second);row['bot_inventory']=inventory
                row['passed']=row['passed'] and session_transport_gate(row['transport']) and row['clips']['passed'] and len(inventory['peers'])==len(inventory['pipelines'])==inventory['starts']==inventory['ready']==1 and inventory['peers'][0] not in restart_before['peers']
                return row
            restart_and_recover(board,first.proc,start_after,recovered,initial_generation=generation,group=True,record=result['restart'])
            result['restart_nic']=nic_gate(result['restart_nic_before'],snapshot())
            result['final_touch']=touch();result['sample_gate']=sample_gate(result['samples'])
            hold_lines=[line for _,_,line in board.lines_since(0)]
            result['image_profile']=session_gate(hold_lines,'demo')
            # Fable r2 N2: first retained demo-image AUDIO_CAPABILITY evidence (autostart Start, app.cpp:375).
            result['audio_capability_lines']=[line for line in hold_lines if 'AUDIO_CAPABILITY' in line]
            result['checks']=dict(autonomous3=len(result['autonomous_turns'])==3 and all(t['passed'] for t in result['autonomous_turns']),
                no_console=not result['no_console']['commands'] and not result['no_console']['acknowledgments'] and result['no_console']['ready_count']==1,
                injected3=len(result['cycles'])==3 and all(c['passed'] for c in result['cycles']),
                restart=result['restart']['passed'],strict_samples=result['sample_gate']['passed'],
                touch_reads=result['final_touch']['driver']['read_errors']==0,image_profile=result['image_profile']['passed'],
                # Scored measurement samples (all taken inside sessions at the provisioned 0 dB); item1 never commands -6.
                measurement_volume=bool(result['samples']) and all(type(s.get('output_volume_db')) is int and s['output_volume_db']==0 for s in result['samples']),
                no_reset=sum('READY transport-console' in line for _,_,line in board.lines_since(proof_cursor))==1)
            if d138_uplink:
                # r15 (decision 138a S2): item1 cannot run the send trace (no-console rule, autostart demo image), so it
                # gates the session PERF counters on every retained sample. S2 cannot see a stall before encoding.
                from echoear_source_cadence import s2_counters,S2_LIMIT_TEXT
                rows=[s2_counters(sample,pcm_queue_drops_max=item1_pcm_max) for sample in result['samples']]
                result['uplink_s2']=dict(samples=rows,passed=bool(rows) and all(r['passed'] for r in rows),limit=S2_LIMIT_TEXT,
                                         pcm_queue_drops_max=item1_pcm_max,
                                         claim='decision 139a: no uplink frame dropped or held more than 40 ms after encoding; '
                                               'item1 does not claim the device send cadence (it cannot see pre-encode stalls)',
                                         basis='decision 138a S2 for item1, ratified by decision 139a; the item2 stages gate S1 (device send trace)')
                result['checks']['uplink_s2']=result['uplink_s2']['passed']
            result['passed']=all(result['checks'].values())
            cleanup()
            board.close();result['monitor']=board.display_monitor.result()
            result['passed']=result['passed'] and result['monitor']['passed'] and not result['cleanup_errors']
    except KeyboardInterrupt:result.update(passed=False,interrupted=True,error='Interrupted at user request')
    except Exception as exc:result.update(passed=False,error=type(exc).__name__+': '+str(exc))
    finally:
        if hold is not None:
            result['restore']=hold.record
            if not hold.clean:result.update(passed=False,restore_failed=True)
        if ledger:result['console_ledger']=ledger.commands
        if board:
            result['monitor']=board.display_monitor.result()
            if not result['monitor']['passed']:result['passed']=False
        if 'audio_capability_lines' not in result:
            # Astra r3 #6: present on every exit, from the retained raw serial.
            try:result['audio_capability_lines']=[l for l in (out/'serial.log').read_text(errors='replace').splitlines() if 'AUDIO_CAPABILITY' in l]
            except OSError:result['audio_capability_lines']=[]
        result['exit_code']=0 if result['passed'] else 1   # r9 (Fable r8 N2): recorded exit code
        result['kind']='item1-combined'   # r10 (Astra r9 #3): record identity for the acceptance loader
        write_final(out/'result.json',result,hold)   # Astra r5 #3: every recorded signal is in the final write
    print(json.dumps({k:result.get(k) for k in ('passed','checks','error','sample_gate','monitor')}),flush=True)
    if hold is not None and hold.phase_signals['during_restore']:   # Fable r4 N3: verdict exit code kept
        print('SIGNAL DURING RESTORE recorded ('+','.join(hold.phase_signals['during_restore'])+'); verdict exit code kept',flush=True)
    return result['exit_code']

if __name__=='__main__':raise SystemExit(main())

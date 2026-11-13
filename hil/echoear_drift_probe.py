#!/usr/bin/env python3
"""Six original reconnect arms, alternating immediate PERF / quiet acquisition.

Causal diagnostic, not qualification or a retake campaign. All transport/clip
verdicts remain strict and retained, including any planned arm that fails drift.
No sudo. Console trace exports occur after each acquisition only.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import time

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=Path('/home/khkramer/src/evidence/echoear/A')


def drift_events(lines):
    rows=[]
    for line in lines:
        if 'PLAYOUT_DRIFT ' not in line:continue
        fields=dict(re.findall(r'(\w+)=([^\s]+)',line.split('PLAYOUT_DRIFT ',1)[1]))
        rows.append({k:v if k=='edit' else int(v) for k,v in fields.items()})
    return rows


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--firmware-ref',default='HEAD')
    p.add_argument('--bench-released',action='store_true');p.add_argument('--flash',action='store_true')
    p.add_argument('--startup-burst-frames',type=int,choices=(2,3,4),help='Offline-only controlled sender startup batch; never qualification')
    p.add_argument('--status-display',action='store_true',help='B workload: persist status screens and enable lifecycle UI')
    a=p.parse_args(argv)
    if not a.bench_released:p.error('Explicit bench release required')
    out=a.out.resolve()
    if not out.is_relative_to(EVIDENCE):p.error('Evidence must remain under '+str(EVIDENCE))
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
        OFFLINE_SKIP_GREETING='1',OFFLINE_USB_ABSENCE_TURNS='1',OFFLINE_BENCH_CONTINUOUS_AUDIO='0',
        RTP_EVIDENCE_DIR=str(out/'rtp'),STEP8E_EVENT_LOOP_PROBE='1',PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from demo_session import preflight,prepare_board,cleanup_board
    from step9a_echoear import bench,verify,flash,check_firmware
    from step9a_assets import asset_server
    from step9a_support import host_address
    from step7_support import BotServer,prepare_prompt
    from echoear_transport_probe import TimedSerial
    from alsa import resolve_jabra,set_volumes,play_wav
    from transport_test import perf
    from playback_quality import transport_gate,microphone_clip_gate
    from arrival_trace import drain
    from host_nic import snapshot,gate
    if a.startup_burst_frames:os.environ['DRIFT_PROBE_STARTUP_FRAMES']=str(a.startup_burst_frames)
    provenance=preflight(a.firmware_ref)
    out.mkdir(parents=True,exist_ok=False)
    result=dict(provenance,qualification=False,nic_gate_enforced=False,status_display=a.status_display,startup_burst_frames=a.startup_burst_frames,arms=[],passed=False)
    def save():(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    def wait_event(bot,kind,started):
        end=time.monotonic()+25
        while time.monotonic()<end:
            found=next((e for e in bot.events() if e['event']==kind and e['t']>=started),None)
            if found:return found
            time.sleep(.05)
        raise TimeoutError('Missing '+kind)
    print('Waiting for bench: one fixed six-arm diagnostic, then release',flush=True)
    try:
        with bench():
            if a.flash:flash(out)
            port=verify(out);card,device=resolve_jabra();set_volumes(card,out/'mixer.log');prompt=prepare_prompt(out)
            with ExitStack() as stack:
                bot=stack.enter_context(BotServer(out/'bot','offline-test',entrypoint='drift_probe_bot.py' if a.startup_burst_frames else 'bot.py'))
                stack.enter_context(asset_server())
                board=TimedSerial(port,log_path=out/'serial.log',display_checks=True)
                stack.callback(board.close) # no failure-snapshot traffic
                try:
                    cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                    result['boot_hash']=check_firmware(board,cursor)
                    board.command('demo hold',45)
                    result['setup']=prepare_board(board,'http://'+host_address()+':7861',out/'arena-budget.json')
                    if a.status_display:
                        from echoear_status_assets import provision_status
                        board.command('disp character off')
                        result['status_provision']=provision_status(board,'http://'+host_address()+':7874')
                        board.command('disp demo on')
                    board.command('peer-option arrival-trace 1');board.command('connect',90);board.command('audio 1')
                    for number in range(1,7):
                        arm=dict(number=number,immediate_perf=number%2==1,nic_before=snapshot())
                        result['arms'].append(arm);cursor=board.cursor;started=time.monotonic()
                        board.command('request-reconnect')
                        board.wait_for('RECONNECT admitted=1',3,since=cursor)
                        arm['ready']=board.wait_for('STATE Ready',90,since=cursor)
                        arm['ready_host_s']=time.monotonic()
                        # B-style arm deliberately measures the full PERF/stack
                        # dump immediately after Ready. Quiet arm has none.
                        if arm['immediate_perf']:arm['before']=perf(board)
                        arm['prompt_host_s']=time.monotonic();play_wav(prompt,device)
                        arm['reply_end']=wait_event(bot,'bot-audio-end',arm['prompt_host_s'])
                        board.wait_for(r'GATE open t=',10,since=cursor);time.sleep(.4)
                        arm['acquisition_end_host_s']=time.monotonic()
                        arm['after']=perf(board)
                        arm['events']=drift_events([line for _,_,line in board.lines_since(cursor)])
                        if 'before' in arm:
                            arm['transport']=transport_gate(arm['before'],arm['after'])
                            arm['clips']=microphone_clip_gate(arm['before'],arm['after'])
                        arm['nic_observation']=gate(arm['nic_before'],snapshot())
                        arm['trace_path']=str(out/f'arrivals-{number}.jsonl')
                        trace=drain(board,arm['trace_path'])
                        tag=arm['after']['generation']
                        arm['trace_drift']=[r for r in trace if r['kind']==6 and r['reserved']==tag]
                        arm['trace_records']=len(trace)
                        arm['export_perf']=perf(board)
                        arm['trace_overwritten']=arm['export_perf']['peer_profile']['trace']['overwritten']
                        save();print(json.dumps(dict(arm=number,immediate_perf=arm['immediate_perf'],events=arm['events'],overwritten=arm['trace_overwritten'])),flush=True)
                    result['monitor']=board.display_monitor.result()
                    result['completed']=len(result['arms'])==6 and result['monitor']['passed']
                    # Quiet acquisition deliberately has no same-session baseline.
                    # Missing gates cannot become passing integrity verdicts.
                    result['integrity_available']=all('transport' in arm and 'clips' in arm for arm in result['arms'])
                    result['passed']=result['completed'] and result['integrity_available'] and all(
                        not arm['events'] and not arm['trace_overwritten'] and arm.get('transport',{'passed':True})['passed'] and
                        arm.get('clips',{'passed':True})['passed'] for arm in result['arms'])
                finally:
                    board.display_cleanup=True;result['cleanup_errors']=cleanup_board(board)
                    result['passed'] &= not result['cleanup_errors']
    except KeyboardInterrupt:result.update(interrupted=True,passed=False)
    except Exception as exc:result.update(error=type(exc).__name__+': '+str(exc),passed=False)
    finally:save()
    print(json.dumps(dict(passed=result['passed'],error=result.get('error'))),flush=True)
    return 0 if result.get('completed') and not result.get('error') and not result.get('cleanup_errors') else 1


if __name__=='__main__':raise SystemExit(main())

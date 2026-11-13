#!/usr/bin/env python3
"""Short combined-runtime heap/census measurement; no speech qualification."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import time

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=Path('/home/khkramer/src/evidence/echoear/A')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--firmware-ref',required=True)
    p.add_argument('--exercise-codec',action='store_true',help='Two original fixture turns to measure Opus stack/memory peaks')
    p.add_argument('--flash',action='store_true');p.add_argument('--bench-released',action='store_true')
    a=p.parse_args()
    if not a.bench_released:p.error('Explicit bench release required')
    out=a.out.resolve()
    if not out.is_relative_to(EVIDENCE):p.error('Evidence must remain under '+str(EVIDENCE))
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
        OFFLINE_SKIP_GREETING='1',OFFLINE_USB_ABSENCE_TURNS='1',OFFLINE_BENCH_CONTINUOUS_AUDIO='0')
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from demo_session import preflight,prepare_board,cleanup_board
    from step9a_echoear import bench,flash,verify,check_firmware
    from step7_support import BotServer,prepare_prompt
    from alsa import resolve_jabra,set_volumes,play_wav
    from playback_quality import transport_gate,microphone_clip_gate
    from step9a_assets import asset_server
    from step9a_support import host_address
    from echoear_status_assets import provision_status
    from esp_serial import EspSerial
    from transport_test import perf
    from host_nic import snapshot,gate
    result=dict(preflight(a.firmware_ref),passed=False,purpose='Heap/census functional measurement, not qualification',
        nic_gate_enforced=False,samples=[])
    out.mkdir(parents=True,exist_ok=False)
    try:
        with bench():
            if a.flash:flash(out)
            port=verify(out)
            with ExitStack() as stack:
                bot=stack.enter_context(BotServer(out/'bot','offline-test'));stack.enter_context(asset_server())
                if a.exercise_codec:
                    card,device=resolve_jabra();set_volumes(card,out/'mixer.log');prompt=prepare_prompt(out)
                board=EspSerial(port,log_path=out/'serial.log',display_checks=True);stack.callback(board.close)
                try:
                    cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                    result['boot_hash']=check_firmware(board,cursor);board.command('demo hold',45)
                    result['setup']=prepare_board(board,'http://'+host_address()+':7861',out/'arena-budget.json')
                    board.command('disp character off');result['status_assets']=provision_status(board,'http://'+host_address()+':7874')
                    board.command('disp demo on');result['nic_before']=snapshot()
                    for cycle in range(2):
                        if cycle:
                            cursor=board.cursor;board.command('request-reconnect');board.wait_for('STATE Ready',90,since=cursor)
                        else:board.command('connect',90);board.command('audio 1')
                        before=perf(board);result['samples'].append(before)
                        if a.exercise_codec:
                            cursor=board.cursor;began=time.monotonic();play_wav(prompt,device)
                            deadline=time.monotonic()+25
                            while not any(e['event']=='bot-audio-end' and e['t']>=began for e in bot.events()):
                                if time.monotonic()>=deadline:raise TimeoutError('Fixture reply did not finish')
                                time.sleep(.05)
                            board.wait_for(r'GATE open t=',10,since=cursor)
                        for _ in range(2):
                            result['samples'].append(perf(board));time.sleep(.5)
                        after=result['samples'][-1]
                        if a.exercise_codec:
                            result.setdefault('turn_gates',[]).append(dict(transport=transport_gate(before,after),clips=microphone_clip_gate(before,after)))
                    result['nic_observation']=gate(result['nic_before'],snapshot());result['monitor']=board.display_monitor.result()
                    result['minimum_internal']=min(s['heap']['internal']['minimum_free'] for s in result['samples'])
                    result['largest_internal']=min(s['heap']['internal']['largest'] for s in result['samples'])
                    result['checks']=dict(floor=result['minimum_internal']>=81920,largest=result['largest_internal']>=32768,
                        no_alloc_failures=all(s['failed_allocations']['total']==0 for s in result['samples']),monitor=result['monitor']['passed'])
                    tx=[t for s in result['samples'] for t in s['tasks'] if t['name']=='audio-tx']
                    result['tx_stack_minimum']=min(t['stack_free_bytes'] for t in tx)
                    result['tx_stack_bytes']=tx[0]['stack_bytes']
                    result['checks']['tx_stack_margin']=result['tx_stack_minimum']>=4096
                    if a.exercise_codec:result['checks']['turn_integrity']=all(g[k]['passed'] for g in result['turn_gates'] for k in ('transport','clips'))
                    result['passed']=all(result['checks'].values())
                finally:
                    board.display_cleanup=True;result['cleanup_errors']=cleanup_board(board)
                    result['passed'] &= not result['cleanup_errors']
    except KeyboardInterrupt:result.update(interrupted=True,passed=False)
    except Exception as exc:result.update(error=type(exc).__name__+': '+str(exc),passed=False)
    finally:(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result.get(k) for k in ('passed','error','minimum_internal','largest_internal','checks','cleanup_errors')}),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())

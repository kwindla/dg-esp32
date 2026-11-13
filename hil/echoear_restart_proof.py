#!/usr/bin/env python3
"""One remaining bot-restart arm on a frozen A or B image; no prior-turn retake."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=Path('/home/khkramer/src/evidence/echoear/B')


def image_bundle(path):
    path=path.resolve();meta=json.loads((path/'image.json').read_text())
    if not meta['echoear']:raise RuntimeError('EchoEar image required')
    for name,want in meta['files_sha256'].items():
        file=(path/name).resolve()
        if not file.is_relative_to(path) or hashlib.sha256(file.read_bytes()).hexdigest()!=want:
            raise RuntimeError('Frozen image identity changed: '+name)
    if b'echoear-audio' not in (path/'dg-esp32.elf').read_bytes():raise RuntimeError('Missing EchoEar audio seam')
    return path,meta


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=p.parse_args(argv);bundle,meta=image_bundle(args.image);out=args.out.resolve()
    if not out.is_relative_to(EVIDENCE):p.error('Evidence must be under '+str(EVIDENCE))
    out.mkdir(parents=True,exist_ok=False)
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
        PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from step9a_echoear import bench,verify,esptool_python,check_esptool
    from esp_serial import EspSerial
    from bot_restart import DemoBot,restart_and_recover,completed_screens
    from standalone_proof import live_turn
    from step7_support import prepare_prompt
    from alsa import resolve_jabra,set_volumes
    from host_nic import snapshot,gate
    result=dict(passed=False,image=meta,purpose='Remaining functional bot-restart arm; not a retake of earlier turns or strict voice qualification',
        nic_gate_enforced=False,console_commands=[],cleanup_errors=[])
    board=None;check_esptool()
    try:
        print('Waiting for exclusive bench lock',flush=True)
        with bench(),ExitStack() as stack:
            print('Bench acquired: frozen-image flash and one recovery arm',flush=True)
            port=verify(out)
            command=[esptool_python(),'-m','esptool','--chip','esp32s3','--port',port,'--baud','460800','write-flash',*meta['write_flash_args']]
            for address,name in meta['flash_files'].items():command.extend([address,str(bundle/name)])
            with (out/'flash.log').open('w') as log:subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
            port=verify(out)
            card,device=resolve_jabra();set_volumes(card,out/'mixer.log');prompt=prepare_prompt(out)
            first=stack.enter_context(DemoBot(out/'bot-before'))
            board=EspSerial(port,log_path=out/'serial.log',display_checks=True)
            stack.callback(board.close)
            if meta['autostart']:
                def forbid(command,*a,**kw):
                    result['console_commands'].append(command);raise AssertionError('No console command allowed on autonomous proof')
                board.command=forbid
            cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
            boot_hash=board.wait_for(r'elf_sha256=([0-9a-f]{64})',5,since=cursor)
            result['boot_elf_sha256']=re.search(r'elf_sha256=([0-9a-f]{64})',boot_hash)[1]
            if result['boot_elf_sha256']!=meta['elf_sha256']:raise RuntimeError('Boot ELF differs from frozen image')
            if not meta['autostart']:
                from demo_session import prepare_board
                from echoear_status_assets import provision_status
                from step9a_assets import asset_server
                from step9a_support import host_address
                with asset_server():
                    result['setup']=prepare_board(board,'http://'+host_address()+':7861',out/'arena-budget.json')
                    board.command('disp character off');result['status_provision']=provision_status(board,'http://'+host_address()+':7874')
                    board.command('disp demo on')
                board.command('connect',90);board.command('audio 1')
            initial=board.wait_for(r'STATE Ready gen=\d+ desired=1 t=\d+',120,since=cursor)
            result['initial_ready']=initial;generation=int(re.search(r'gen=(\d+)',initial)[1])
            board.wait_for(r'MIC_GATE t=\d+ closed=0',15,since=cursor)
            result['nic_before']=snapshot();result['restart']={}
            def start(remaining):
                second=DemoBot(out/'bot-after');stack.callback(second.close)
                return second.start(timeout=remaining)
            def turn(second):
                row=live_turn(board,second.proc,second.out,prompt,device,out,1)
                # Each bot invocation must own one distinct peer/pipeline.
                def peers(path):
                    return sorted(set(re.findall(r'peer: (SmallWebRTCConnection#[\w-]+)',(path/'bot.log').read_text())))
                row['peers_before']=peers(first.out);row['peers_after']=peers(second.out)
                row['fresh_peer']=len(row['peers_before'])==len(row['peers_after'])==1 and row['peers_before']!=row['peers_after']
                row['passed']=row['passed'] and row['fresh_peer']
                return row
            restart_and_recover(board,first.proc,start,turn,initial_generation=generation,group=True,
                screen_proof=completed_screens if meta['status_screens'] else None,record=result['restart'])
            result['passed']=result['restart']['passed']
            result['recovery_without_screens_passed']=result['restart']['recovery_without_screens_passed']
            result['nic_after']=snapshot();result['nic_observation']=gate(result['nic_before'],result['nic_after'])
            result['cleanup']='Close reader and owned services; autonomous image receives no console cleanup'
            # All result gates include the final captured serial data.
            board.close();result['monitor']=board.display_monitor.result()
            result['passed']=result['passed'] and result['monitor']['passed']
            result['recovery_without_screens_passed']=result['recovery_without_screens_passed'] and result['monitor']['passed']
    except KeyboardInterrupt:result.update(passed=False,interrupted=True,error='Interrupted at user request')
    except Exception as exc:result.update(passed=False,error=type(exc).__name__+': '+str(exc))
    finally:
        if board:
            result['monitor']=board.display_monitor.result()
            if not result['monitor']['passed']:result.update(passed=False,recovery_without_screens_passed=False)
        if 'nic_before' in result and 'nic_after' not in result:
            result['nic_after']=snapshot();result['nic_observation']=gate(result['nic_before'],result['nic_after'])
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result.get(k) for k in ('passed','recovery_without_screens_passed','error','monitor')}),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())

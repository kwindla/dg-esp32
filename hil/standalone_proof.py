#!/usr/bin/env python3
"""Unattended standalone functional proof: 3 live turns + bot restart + 1 turn.

Provision separately. This harness starts no fixture service and sends ZERO
console commands after its controlled boot, including during cleanup. It keeps
raw serial, live transcripts, per-turn Jabra recordings and NIC observations.
It does not substitute for the final strict/acoustic qualification campaign.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import signal
import socket
import time

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=Path('/home/khkramer/src/evidence/echoear/A')


def configure_environment():
    # Must precede importing step9a_assets: its origin root is bound at import.
    os.environ['DG_BOARD']='echoear'
    os.environ['STEP9A_FIXTURES']=str(ROOT/'hil/fixtures/echoear360')


def quiet_gate(before,after):
    from host_nic import gate
    return gate(before,after)


def transcript_rows(out):
    path=out/'transcripts.log'
    return [line for line in path.read_text().splitlines() if 'Transcript: ' in line] if path.exists() else []


def turn_complete(before,after):
    rows=after[len(before):]
    user=[r for r in rows if 'Transcript: user: ' in r]
    assistant=[r for r in rows if 'Transcript: assistant: ' in r]
    return dict(passed=bool(user and assistant and rows.index(user[-1])<rows.index(assistant[-1])),
                user=user,assistant=assistant)


def live_turn(board,bot,bot_out,prompt,device,out,index):
    from alsa import play_wav,record_wav
    before=transcript_rows(bot_out)
    started=time.monotonic()
    with record_wav(out/f'reply-{index}.wav',device,15) as recording:
        play_wav(prompt,device)
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            board.lines_since(board.cursor) # surface strict monitor/read failures
            if bot.poll() is not None:raise RuntimeError('Owned live bot exited')
            row=turn_complete(before,transcript_rows(bot_out))
            if row['passed']:
                recording.wait()
                return dict(row,started=started,completed=time.monotonic(),jabra=str(recording.path))
            time.sleep(.1)
    raise TimeoutError('No complete live user/assistant turn')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--firmware-ref',default='HEAD')
    parser.add_argument('--nic-seconds',type=int,choices=(30,60,300),default=30,help='Off-bench NIC observation only; functional proof never gates on NIC traffic')
    parser.add_argument('--exercise-reprovision',action='store_true',help='Before final proof boot, exercise live -> hold -> configure -> run with an existing display/arena')
    parser.add_argument('--flash',action='store_true',help='Reflash the verified local image inside this proof hold')
    parser.add_argument('--bench-released',action='store_true')
    args=parser.parse_args(argv)
    if not args.bench_released:parser.error('Explicit bench release required')
    out=args.out.resolve()
    if not out.is_relative_to(EVIDENCE):parser.error('Evidence must be under '+str(EVIDENCE))
    configure_environment()
    os.environ.setdefault('ESPTOOL_PYTHON',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from demo_session import preflight,demo_server,nic_snapshot,provision_assets
    from step9a_assets import asset_server
    from step9a_support import host_address
    from step9a_echoear import bench,verify,check_firmware,flash
    from esp_serial import EspSerial
    from step7_support import prepare_prompt
    from alsa import resolve_jabra,set_volumes
    from bot_restart import DemoBot,restart_and_recover,completed_screens
    provenance=preflight(args.firmware_ref)
    if not provenance['demo_autostart']:raise RuntimeError('Autostart image required')
    out.mkdir(parents=True,exist_ok=False)
    result=dict(provenance,passed=False,purpose='Functional standalone boot/reconnect, not final acoustic/network qualification',
                console_commands=[],turns=[],interrupted=False,nic_gate_enforced=False)
    def interrupted(*_):raise KeyboardInterrupt
    old_term=signal.signal(signal.SIGTERM,interrupted)
    try:
        initial=nic_snapshot()
        for elapsed in range(0,args.nic_seconds,30):
            time.sleep(30)
            print(f'Off-bench NIC observation {elapsed+30}/{args.nic_seconds}s',flush=True)
        result['nic_preflight']=quiet_gate(initial,nic_snapshot())
        with bench():
            # A stopped fixture server is part of the proof, not merely unused.
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1',7874))==0:raise RuntimeError('Fixture service is unexpectedly present')
            if args.flash:flash(out)
            port=verify(out)
            card,device=resolve_jabra();set_volumes(card,out/'mixer.log');prompt=prepare_prompt(out)
            with ExitStack() as servers:
                board=EspSerial(port,log_path=out/'serial.log',display_checks=True)
                servers.callback(board.close) # no failure-snapshot console traffic on unwind
                if args.exercise_reprovision:
                    setup_out=out/'reprovision-bot';setup_out.mkdir()
                    with demo_server(setup_out),asset_server():
                        cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                        board.wait_for('STATE Ready',120,since=cursor)
                        result['reprovision']=provision_assets(board,'http://'+host_address()+':7861',out/'repair-budget.json')
                        cursor=board.cursor;board.command('demo run')
                        result['reprovision_resumed']=board.wait_for('STATE Ready',120,since=cursor)
                    # All setup writes are before the final controlled proof boot.
                def forbid(command,*a,**kw):
                    result['console_commands'].append(command)
                    raise AssertionError('No console commands allowed after boot')
                board.command=forbid
                bot_before=out/'bot-before';bot_before.mkdir()
                first=servers.enter_context(DemoBot(bot_before));result['first_bot_pid']=first.proc.pid
                cursor=board.cursor;proof_cursor=cursor;board.reset();result['boot_at']=time.monotonic()
                board.wait_for('READY transport-console',60,since=cursor)
                result['boot_hash']=check_firmware(board,cursor)
                board.wait_for('demo-boot: RESTORED',90,since=cursor)
                ready=board.wait_for('STATE Ready',120,since=cursor)
                result['initial_ready']=ready
                board.wait_for(r'MIC_GATE t=\d+ closed=0',15,since=cursor)
                result['nic_arm_before']=nic_snapshot()
                for index in range(1,4):
                    row=live_turn(board,first.proc,bot_before,prompt,device,out,index)
                    result['turns'].append(row)
                result['bot_restart']={}
                bot_after=out/'bot-after'
                def start_after(remaining):
                    second=DemoBot(bot_after);servers.callback(second.close)
                    return second.start(timeout=remaining)
                def recovered_turn(second):
                    row=live_turn(board,second.proc,bot_after,prompt,device,out,4)
                    result['turns'].append(row)
                    return row
                restart_and_recover(board,first.proc,start_after,recovered_turn,
                    initial_generation=int(re.search(r'gen=(\d+)',result['initial_ready'])[1]),group=True,
                    screen_proof=completed_screens if b'DISPLAY_DEMO_RENDERED' in (ROOT/'build/dg-esp32.elf').read_bytes() else None,
                    record=result['bot_restart'])
                result['recovered_ready']=result['bot_restart']['ready']
                result['restart_gap_s']=result['bot_restart']['restart_gap_s']
                result['proof_end']=time.monotonic();result['nic_arm']=quiet_gate(result['nic_arm_before'],nic_snapshot())
                result['monitor']=board.display_monitor.result()
                lines=[line for _,_,line in board.lines_since(proof_cursor)]
                result['unexpected_boots']=sum('READY transport-console' in line for line in lines)!=1
                result['initial_generation']=int(re.search(r'gen=(\d+)',result['initial_ready'])[1])
                result['recovered_generation']=int(re.search(r'gen=(\d+)',result['recovered_ready'])[1])
                def peers(path):return sorted(set(re.findall(r'peer: (SmallWebRTCConnection#[\w-]+)',(path/'bot.log').read_text())))
                result['peers_before']=peers(bot_before);result['peers_after']=peers(bot_after)
                result['passed']=(len(result['turns'])==4 and all(t['passed'] for t in result['turns'])
                    and result['bot_restart']['passed'] and result['recovered_generation']>result['initial_generation']
                    and len(result['peers_before'])==len(result['peers_after'])==1
                    and result['peers_before']!=result['peers_after']
                    and result['monitor']['passed'] and not result['monitor']['errors']
                    and not result['unexpected_boots'] and not result['console_commands'])
                result['cleanup']='Reader/services close; no board console cleanup; autonomous retry remains enabled'
    except KeyboardInterrupt:result['interrupted']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        signal.signal(signal.SIGTERM,old_term)
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('nic_arm_before','nic_arm','nic_preflight')},indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())

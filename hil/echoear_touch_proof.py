#!/usr/bin/env python3
"""Three injected double-tap reconnects plus one owned bot-drop recovery.

Functional offline speech turns only; final strict voice/acoustic qualification
is a separate campaign. Caller explicitly authorized a released, locked bench.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
ROOT=Path(__file__).resolve().parents[1]


def phase_sequence(rows):
    """Proof must cover successful completed frames, in order, not intentions."""
    wanted=iter((0,1,2));next_phase=next(wanted);chosen=[]
    for row in rows:
        if row['screen']==next_phase:
            chosen.append(row)
            try:next_phase=next(wanted)
            except StopIteration:break
    return dict(passed=len(chosen)==3 and all(b['completed_us']>a['completed_us'] and
        b['rendered']>a['rendered'] for a,b in zip(chosen,chosen[1:])),frames=chosen)


def new_session(events,previous,started):
    rows=[e for e in events if e['t']>=started and e['event']=='session-start' and e['pc_id']!=previous]
    peers={e['pc_id'] for e in rows}
    return next(iter(peers)) if len(peers)==1 else None


def inject_during_connecting(touch,inject,wait):
    # App DemoPhase::Connecting is 0 (display screen indices differ).
    def connecting():
        status=touch()
        return status if status['phase']==0 else None
    before=wait(connecting,5)
    injected_at=time.monotonic();inject()
    def consumed():
        status=touch()
        return status if status['driver']['injected_ignored']>before['driver']['injected_ignored'] else None
    after=wait(consumed,5)
    return dict(before=before,after=after,injected_at=injected_at,
        passed=after['phase']==0 and after['generation']==before['generation'] and
        after['driver']['injected']==before['driver']['injected']+1 and
        after['driver']['injected_ignored']==before['driver']['injected_ignored']+1 and
        after['requests']==before['requests'] and after['request_rejected']==before['request_rejected'])



def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--flash',action='store_true');args=p.parse_args(argv)
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    os.environ.update(DG_BOARD='echoear',STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
        OFFLINE_SKIP_GREETING='1',OFFLINE_USB_ABSENCE_TURNS='1',OFFLINE_BENCH_CONTINUOUS_AUDIO='0',PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
    from step9a_echoear import bench,verify,flash,check_firmware
    from step9a_assets import asset_server
    from step7_support import BotServer,prepare_prompt
    from step9a_support import read,host_address
    from echoear_status_assets import provision_status
    from demo_session import prepare_board,cleanup_board
    from esp_serial import EspSerial
    from alsa import resolve_jabra,set_volumes,play_wav
    from transport_test import perf
    from playback_quality import transport_gate,microphone_clip_gate
    from host_nic import snapshot as nic_snapshot,gate as nic_gate
    from bot_restart import restart_and_recover
    result=dict(passed=False,cycles=[],mode='functional-offline-reconnect',strict_voice_qualification=False,nic_gate_enforced=False,
        real_physical_touch_test=False,elf_sha256=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest())
    def save():(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    def wait(fn,seconds=60):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            value=fn()
            if value:return value
            time.sleep(.05)
        raise TimeoutError('Required reconnect/turn evidence absent')
    print('Waiting for the exclusive bench lock',flush=True)
    try:
        with bench():
            print('Bench acquired: one flash/setup + three reconnects + bot-drop, then cleanup',flush=True)
            if args.flash:flash(out)
            port=verify(out);card,device=resolve_jabra();set_volumes(card,out/'mixer.log');prompt=prepare_prompt(out)
            with BotServer(out/'bot','offline-test') as bot,asset_server(),EspSerial(port,log_path=out/'serial.log',display_checks=True) as board:
                def touch():
                    cursor=board.cursor;board.command('touch-status')
                    line=board.wait_for(r'^DEMO_TOUCH_STATUS ',3,since=cursor)
                    return json.loads(line.split(' ',1)[1])
                def proofs(start=0):
                    rows=[];next_number=start
                    for _ in range(5):
                        page=read(board,'demo-proof '+str(next_number),'DISPLAY_DEMO_PROOF');items=page['items'];rows.extend(items)
                        if not items or items[-1]['n']>=page['total']:break
                        next_number=items[-1]['n']+1
                    return rows
                def await_phase(screen,start=0,seconds=60):
                    return wait(lambda:next((r for r in proofs(start) if r['screen']==screen),None),seconds)
                def functional_turn(pc,number):
                    before=perf(board);begin=time.monotonic();cursor=board.cursor
                    play_wav(prompt,device)
                    ended=wait(lambda:next((e for e in bot.events() if e['pc_id']==pc and e['t']>=begin and e['event']=='bot-audio-end'),None),25)
                    board.wait_for(r'GATE open t=',10,since=cursor);time.sleep(.4);after=perf(board)
                    events=[e for e in bot.events() if e['pc_id']==pc and begin<=e['t']<=time.monotonic()]
                    transport=transport_gate(before,after);clips=microphone_clip_gate(before,after)
                    checks=dict(vad_stop=any(e['event']=='vad-stop' for e in events),fixture_reply=any(e['event']=='usb-fixture-reply' for e in events),
                        reply_end=bool(ended),same_pc=before.get('pc_id')==after.get('pc_id')==pc,transport=transport['passed'],clips=clips['passed'],
                        media_progress=after.get('sent',0)>before.get('sent',0) and after.get('received',0)>before.get('received',0))
                    row=dict(number=number,pc_id=pc,prompt_started_at=begin,reply_finished_at=ended['t'],after_snapshot_at=time.monotonic(),passed=all(checks.values()),checks=checks,before=before,after=after,events=events,transport=transport,clips=clips)
                    return row
                try:
                    cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor);result['boot_hash']=check_firmware(board,cursor)
                    result['setup']=prepare_board(board,'http://'+host_address()+':7861',out/'arena-budget.json')
                    board.command('disp character off');result['status_provision']=provision_status(board,'http://'+host_address()+':7874')
                    board.command('disp demo on');await_phase(0)
                    cursor=board.cursor;board.command('connect',90);board.wait_for('STATE Ready',5,since=cursor);board.command('audio 1');await_phase(2)
                    wait(lambda:touch()['phase']==1,5)
                    previous=perf(board)['pc_id'];result['initial_pc']=previous;result['touch_before']=touch();result['before']=perf(board)
                    for cycle in range(1,4):
                        arm=dict(number=cycle,passed=False);result['cycles'].append(arm);arm['nic_before']=nic_snapshot()
                        start_number=proofs()[-1]['n']+1;started=time.monotonic();arm['started_at']=started;before_touch=touch();cursor=board.cursor
                        board.command('touch-inject');disconnected=await_phase(0,start_number)
                        # This injection enters the same driver-delivery mailbox,
                        # while the app is held in its >=300ms teardown dwell.
                        wait(lambda:touch()['phase']!=1,3);board.command('touch-inject');arm['ignored_injected_at']=time.monotonic()
                        wait(lambda:touch()['driver']['injected_ignored']==before_touch['driver']['injected_ignored']+1,3)
                        arm['connecting_injection']=inject_during_connecting(touch,lambda:board.command('touch-inject'),wait)
                        await_phase(1,start_number);await_phase(2,start_number)
                        pc=wait(lambda:new_session(bot.events(),previous,started));wait(lambda:touch()['phase']==1,5)
                        arm['proof']=phase_sequence(proofs(start_number));arm['pc_id']=pc;arm['touch_before']=before_touch;arm['touch_after']=touch()
                        session=[e for e in bot.events() if e['pc_id']==pc];arm['session_events']=session
                        arm['checks']=dict(screens=arm['proof']['passed'],fresh_pc=pc!=previous,
                            new_pipeline=sum(e['event']=='session-start' for e in session)==1,client_ready=any(e['event']=='client-ready' for e in session),
                            ignored_reconnect_tap=arm['touch_after']['driver']['injected_ignored']==before_touch['driver']['injected_ignored']+2,
                            ignored_connecting_tap=arm['connecting_injection']['passed'],
                            injected_count=arm['touch_after']['driver']['injected']==before_touch['driver']['injected']+3,
                            one_request=arm['touch_after']['requests']==before_touch['requests']+1,
                            no_request_rejection=arm['touch_after']['request_rejected']==0)
                        arm['turn']=functional_turn(pc,cycle);arm['nic_after']=nic_snapshot();arm['nic_observation']=nic_gate(arm['nic_before'],arm['nic_after'])
                        arm['passed']=all(arm['checks'].values()) and arm['turn']['passed'];save();print(json.dumps(dict(cycle=cycle,passed=arm['passed'],checks=arm['checks'])),flush=True)
                        if not arm['passed']:raise AssertionError('Reconnect cycle failed')
                        previous=pc
                    drop=dict(passed=False,nic_before=nic_snapshot());result['bot_drop']=drop
                    started=time.monotonic()
                    def start_after(remaining):
                        # The shared arm has already reaped this owned process.
                        bot.log.close();bot.proc=None
                        bot.start(startup_timeout=remaining)
                        return bot
                    def recovered_turn(new_bot):
                        pc=wait(lambda:new_session(new_bot.events(),previous,started));drop['pc_id']=pc
                        return functional_turn(pc,4)
                    restart_and_recover(board,bot.proc,start_after,recovered_turn,
                        initial_generation=touch()['generation'],record=drop)
                    drop['nic_after']=nic_snapshot();drop['nic_observation']=nic_gate(drop['nic_before'],drop['nic_after'])
                    result['after']=perf(board);result['touch_after']=touch();result['final']=read(board,'status','DISPLAY');result['lock_status']=read(board,'lock-status','DISPLAY_LOCK_STATUS')
                    log=''.join(p.read_text(errors='replace') for p in (out/'bot').glob('server-*.log'))
                    result['start_requests']=len(re.findall(r'POST /start HTTP/1.1" 200',log))
                    snapshots=[result['before'],result['after']]+[s for c in result['cycles'] for s in (c['turn']['before'],c['turn']['after'])]+[drop['turn']['before'],drop['turn']['after']]
                    result['checks']=dict(three_cycles=len(result['cycles'])==3 and all(c['passed'] for c in result['cycles']),drop_recovery=drop['passed'],
                        fresh_starts=result['start_requests']==5,dma=result['final']['dma']==[0,0],alive=not result['final']['dead'] and not result['final']['failed'],
                        lock_clean=result['lock_status']['timeouts']==0 and result['lock_status']['failures']==0,
                        internal_floor=min(s['heap']['internal']['minimum_free'] for s in snapshots)>=81920,
                        largest_floor=min(s['heap']['internal']['largest'] for s in snapshots)>=32768,
                        allocation_failures=max(s['failed_allocations']['total'] for s in snapshots)==0,
                        touch_reads_clean=result['touch_after']['driver']['read_errors']==0)
                    result['passed']=all(result['checks'].values())
                finally:
                    board.display_cleanup=True;result['cleanup_errors']=cleanup_board(board);result['monitor']=board.display_monitor.result()
                    result['passed']=result['passed'] and result['monitor']['passed'] and not result['cleanup_errors']
    except KeyboardInterrupt:result.update(passed=False,interrupted=True,error='Interrupted at user request')
    except Exception as exc:result.update(passed=False,error=type(exc).__name__+': '+str(exc))
    finally:
        # Preserve NIC evidence even when an arm aborts before its normal end.
        for arm in result['cycles']+([result['bot_drop']] if 'bot_drop' in result else []):
            if 'nic_before' in arm and 'nic_after' not in arm:
                arm['nic_after']=nic_snapshot();arm['nic_observation']=nic_gate(arm['nic_before'],arm['nic_after']);arm['nic_interval_includes_cleanup']=True
        save()
    print(json.dumps({k:result.get(k) for k in ('passed','checks','error','monitor')}),flush=True)
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())

#!/usr/bin/env python3
"""Five-minute board-scoped fault arms with complete receive-boundary evidence."""
import argparse
import hashlib
import json
import os
import re
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
from board_flow_capture import BoardFlowCapture
from esp_serial import ROOT,EspSerial
from step7_support import BotServer,pcm,transcribe
from phase2_endurance import snapshot
from arrival_trace import drain,attribute,generation_sessions
from step8e_analysis import uplink_packet_summary,recovery_bounds,cyclic_fixture_windows,session_state_audit
from transport_test import dump
from playback_quality import decoded_match,TRANSPORT_COUNTERS,impaired_conformance,transport_gate
from netem import Netem,command as tc,snapshot as tc_snapshot,assert_selected_interface
from failure_snapshot import capture_failure
from quiet_nic import QuietNic,wait_for_quiet


def matrix():
    cells=[('control',['loss','0%'],None)]+[(f'loss-{n}pct',['loss',f'{n}%'],None) for n in (1,3,5,10)]
    cells.append(('bursty-2pct',['loss','gemodel','0.2%','10%','100%','0%'],None))
    cells += [(f'delay-{d}-jitter-{j}',['delay',f'{d}ms',f'{j}ms','distribution','normal'],None)
              for d,j in ((50,10),(150,30),(300,60))]
    cells += [(f'reorder-{n}pct',['delay','20ms','reorder',f'{n}%','50%'],None) for n in (1,3,5)]
    cells += [('duplicate-1pct',['duplicate','1%'],None)]
    cells += [(f'rate-{n}kbps',['rate',f'{n}kbit'],None) for n in (64,128)]
    cells += [(f'outage-{n}s',['loss','0%'],n) for n in (1,3,10,13,30)]
    return cells


def write(path,data):path.write_text(json.dumps(data,indent=2)+'\n')
def records(path):return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []

def periodic_snapshot(board,result):
    try:return snapshot(board,active=False,allow_incomplete=True)
    except RuntimeError as exc:
        lines=[r['line'] for r in result.get('state_history',[])]+[
            line for _,_,line in board.lines_since(max(0,getattr(board,'cursor',500)-500))]
        states=[m[1] for line in lines
            if (m:=re.search(r'dg-app: STATE (\w+)',line))]
        # The core client has no PERF object during an expected reconnect.
        # Preserve that missing observation; console timeouts and errors
        # while Ready remain failures instead of being hidden as transitions.
        if str(exc).startswith('ERR #') and states and states[-1] in ('Starting','Disconnected','WifiConnecting'):
            result.setdefault('transitional_snapshot_errors',[]).append(dict(t=time.monotonic(),state=states[-1],error=str(exc)))
            return None
        raise


def arm(out,elf,name,args,direction,outage,board_ip,seconds=300):
    out.mkdir(parents=True,exist_ok=False)
    result=dict(name=name,direction=direction,duration_requested_s=seconds,
                netem=args,outage_s=outage,completed=False,passed=False,samples=[],
                elf_sha256=hashlib.sha256(elf.read_bytes()).hexdigest())
    env=dict(BOT_ICE_INTERFACE='wlp8s0',BOT_IMPAIRED_INTERFACE='wlp8s0',BOT_ICE_REMOTE_NETWORK_ONLY='0',
             RTP_EVIDENCE_DIR=str(out/'rtp'),RTP_EVIDENCE_MAX_PACKETS='200000',
             RTP_EVIDENCE_PAYLOAD_BYTES='1',OFFLINE_SKIP_GREETING='1',
             OFFLINE_BENCH_CONTINUOUS_AUDIO='1',OFFLINE_IDLE_TIMEOUT_SECS='2100')
    previous={k:os.environ.get(k) for k in env};os.environ.update(env)
    rows=[]
    try:
        wait_for_quiet(out/'quiet-preflight.json')
        with Netem(board_ip,['loss','0%'],direction,state=out/'tc-state.json',account_both=True,rtp_directory=out/'rtp') as fault, QuietNic(out/'quiet-nic.json',seconds=seconds):
            with (BoardFlowCapture(out/'flow') if outage==30 and direction=='uplink' else nullcontext()),BotServer(out/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
                try:
                    cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                    identity=board.wait_for('elf_sha256=',3,since=cursor)
                    assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==hashlib.sha256(elf.read_bytes()).hexdigest()
                    for cmd in ('server-url http://192.168.7.222:7861','peer-option arrival-trace 1',
                                'peer-option call-timing 1','wifi-log 1','quiet 1','mic-tone 1','gate open'):
                        board.command(cmd)
                    board.command('connect',90);board.command('audio 1');board.command('play-long 6',20)
                    time.sleep(3)
                    result['before']=snapshot(board,active=True,allow_incomplete=True)
                    loudness=[line for _,_,line in board.lines_since(cursor) if 'LOUDNESS target_dbfs=' in line]
                    assert loudness,'Missing production playback target observation'
                    result['playback_target_dbfs']=int(re.search(r'target_dbfs=(-?\d+)',loudness[-1])[1])
                    result['initial_pc_id']=result['before']['perf']['pc_id'];last_pc=result['initial_pc_id']
                    result['selected_pair']=assert_selected_interface(out/'rtp',last_pc,fault.interface)
                    rows+=drain(board,out/'arrivals.jsonl');board.command('rx-clear')
                    fault.update(args)
                    beginning=time.monotonic();result['started_s']=beginning
                    next_sample=beginning+10;outage_started=False;outage_stopped=False;decoded_reset=False;blackout_dumped=False
                    primary_decoded=None
                    state_cursor=board.cursor;ready_generations=set();replayed_since_sample=False
                    result['state_history']=[dict(host_s=t,line=l) for _,t,l in board.lines_since(cursor)
                        if re.search(r'dg-app: (STATE |SESSION FAIL)',l)]
                    while time.monotonic()-beginning<seconds:
                        entries=board.lines_since(state_cursor)
                        if entries:state_cursor=entries[-1][0]
                        for _,observed,line in entries:
                            if re.search(r'dg-app: (STATE |SESSION FAIL)',line):
                                result['state_history'].append(dict(host_s=observed,line=line))
                            match=re.search(r'dg-app: STATE Ready gen=(\d+)',line)
                            if match and match[1] not in ready_generations:
                                ready_generations.add(match[1])
                                replay=dict(ready_host_s=observed,requested_s=time.monotonic(),state_line=line)
                                board.command('connect',90) # Ready log precedes completion publication.
                                assert_selected_interface(out/'rtp',perf(board)['pc_id'],fault.interface)
                                board.command('audio 1');board.command('play-long 6',20)
                                replay['ack_s']=time.monotonic()
                                result.setdefault('replay_after_ready',[]).append(replay);replayed_since_sample=True
                        elapsed=time.monotonic()-beginning
                        if not outage and primary_decoded is None and elapsed>=30:
                            # A later reconnect clears the session-owned PCM
                            # buffer. Retain the same first-30-s prefix before
                            # that happens, while media/trace keep running.
                            result['primary_pcm_dump_requested_s']=time.monotonic()
                            primary_decoded=dump(board,out)
                            result['primary_pcm_dump_finished_s']=time.monotonic()
                            result['primary_pcm_samples']=len(primary_decoded)
                        if outage and not decoded_reset and elapsed>=30:
                            board.command('rx-clear');decoded_reset=True
                        if outage and not outage_started and elapsed>=45:
                            result['outage_begin_s']=time.monotonic();fault.update(['loss','100%'])
                            outage_started=True
                        if outage_started and not outage_stopped and time.monotonic()>=result['outage_begin_s']+outage:
                            result['outage_release_begin_s']=time.monotonic()
                            fault.update(['loss','0%']);result['outage_end_s']=time.monotonic();outage_stopped=True
                        if outage_stopped and not blackout_dumped and time.monotonic()>=result['outage_end_s']+5:
                            capture=out/'blackout-audio';capture.mkdir()
                            blackout=dump(board,capture)
                            result['blackout_decoded_content']=decoded_match(blackout,pcm(ROOT/'server/fixtures/reply_pineapple.wav'))
                            result['blackout_decoded_samples']=len(blackout)
                            board.command('rx-clear');blackout_dumped=True
                        if time.monotonic()>=next_sample:
                            sample=periodic_snapshot(board,result)
                            rows+=drain(board,out/'arrivals.jsonl')
                            if sample:result['samples'].append(sample)
                            board.command('wifi-stats',10)
                            pc=sample['perf'].get('pc_id') if sample else None
                            if pc and pc!=last_pc:
                                result.setdefault('reconnections',[]).append(dict(t=time.monotonic(),pc_id=pc))
                                if not replayed_since_sample:
                                    latest=board.latest_entry(r'dg-app: STATE \w+')
                                    if latest and 'STATE Ready ' in latest[2]:
                                        result.setdefault('replay_ready_log_missing',[]).append(pc)
                                        board.command('connect',90)
                                        board.command('audio 1');board.command('play-long 6',20)
                                    else:
                                        result.setdefault('replay_deferred_until_ready',[]).append(pc)
                                last_pc=pc
                            replayed_since_sample=False
                            write(out/'result.json',result);next_sample=time.monotonic()+10
                        time.sleep(.05)
                    result['elapsed_s']=time.monotonic()-beginning
                    result['tc_counters']={dev:json.loads(tc('-s','-j','qdisc','show','dev',dev)) for dev in ('wlp8s0','ifb0')}
                    result['fault_release_s']=time.monotonic();fault.update(['loss','0%'])
                    latest=board.latest_entry(r'dg-app: STATE \w+')
                    if latest and 'STATE Ready ' not in latest[2]:
                        recovered=board.wait_for_entry('STATE Ready',90,since=state_cursor)
                        result['post_fault_ready_host_s']=recovered[1]
                        board.command('connect',90) # Existing Ready/completion barrier.
                        board.command('audio 1');board.command('play-long 1',20)
                        time.sleep(2)
                    result.setdefault('state_history',[]).extend(dict(host_s=t,line=l) for _,t,l in board.lines_since(state_cursor)
                        if re.search(r'dg-app: (STATE |SESSION FAIL)',l))
                    result['after']=snapshot(board,active=True,allow_incomplete=True)
                    rows+=drain(board,out/'arrivals.jsonl')
                    decoded=primary_decoded if primary_decoded is not None else dump(board,out)
                    reference=pcm(ROOT/'server/fixtures/reply_pineapple.wav')
                    result['decoded_content']=decoded_match(decoded,reference)
                    # Retain every complete PCM block. A later linear best
                    # match cannot provide phase for earlier blocks after
                    # native playout drops/idle-read discontinuities.
                    content=cyclic_fixture_windows(decoded,reference)
                    result['decoded_windows']=content['windows']
                    result['decoded_correlation_p50']=content['p50']
                    result['decoded_correlation_min']=content['minimum']
                    result['decoded_windows_clean_fraction']=content['clean_fraction']
                    result['decoded_windows_basis']=content['basis']
                    result['content_scoring_method']='cyclic_fixture_blocks_v1'
                    try:result['decoded_transcript']=transcribe(out/'rx.wav')
                    except Exception as exc:result['stt_error']=f'{type(exc).__name__}: {exc}'
                    if (not result['decoded_content']['passed'] or
                        result['decoded_correlation_p50'] is None or result['decoded_correlation_p50']<.95 or
                        'pineapple' not in result.get('decoded_transcript','').lower()):
                        result['failure_snapshot']=capture_failure(board,out,'Decoded correlation/STT sample gate failed')
                    result['completed']=True
                except BaseException as exc:
                    try:
                        tail=board.lines_since(locals().get('state_cursor',cursor))
                        result.setdefault('state_history',[]).extend(dict(host_s=t,line=l) for _,t,l in tail
                            if re.search(r'dg-app: (STATE |SESSION FAIL)',l))
                    except Exception as diagnostic:result['state_history_error']=str(diagnostic)
                    result['failure_snapshot']=capture_failure(board,out,exc)
                    raise
                finally:
                    for cmd in ('audio 0','mic-tone 0','netprobe stop','gate auto','quiet 0','wifi-log 0','disconnect'):
                        try:board.command(cmd,40)
                        except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
        packets=records(out/'rtp/rtp.jsonl');events=bot.events()
        window=[r for r in packets if result['started_s']*1000<=r['observed_ms']<=(result['started_s']+seconds)*1000]
        result['uplink']=uplink_packet_summary(window)
        observations=[result['before']]+result['samples']+[result['after']]
        # Counters restart with a session. Keep the unobserved retired tail
        # explicitly unknown rather than reporting negative counter deltas.
        result['transport_deltas']={k:(sum(max(0,b['perf'][k]-(a['perf'][k]
            if a['perf'].get('generation')==b['perf'].get('generation') else 0))
            for a,b in zip(observations,observations[1:])) if all(
                o['perf'].get('playout_schema')==2 and isinstance(o['perf'].get(k),int)
                for o in observations) else None) for k in TRANSPORT_COUNTERS}
        result['retired_counter_tail_unknown']=bool(result.get('reconnections'))
        result['session_survived']=result['initial_pc_id']==result['after']['perf'].get('pc_id') and not result.get('reconnections')
        result['rtvi_server_messages']=sum(e['event']=='rtvi-out' and
            result['before']['observed_monotonic_s']<=e['t']<=result['after']['observed_monotonic_s'] for e in events)
        result['rtvi_delivery_basis']='Server-audit messages versus native receive callbacks over the PERF bracket; includes in-flight endpoint uncertainty and retired counter tails, without per-message acknowledgements.'
        result['rtvi_callbacks_received']=sum(max(0,b['perf']['peer_profile']['data_callback']['calls']-
            (a['perf']['peer_profile']['data_callback']['calls'] if a['perf'].get('generation')==b['perf'].get('generation') else 0))
            for a,b in zip(observations,observations[1:]))
        downlink=records(out/'rtp/downlink.jsonl')
        mapped,basis=generation_sessions(rows,downlink,{s['perf']['pc_id']:s['perf']['generation']
            for s in observations if s['perf'].get('pc_id')})
        result['arrival_attribution']=dict(generation_mapping=mapped,mapping_basis=basis,
            sessions={str(tag):attribute([r for r in rows if r['reserved']==tag],
                [r for r in downlink if r['pc_id']==pc]) for tag,pc in mapped.items()})
        write(out/'arrival-attribution.json',result.pop('arrival_attribution'))
        trace=result['after']['perf']['peer_profile']['trace'];result['trace_complete']=trace['overwritten']==0
        if outage:
            stop=result['outage_end_s'];starts=result['outage_begin_s']
            resume=next((r for r in packets if r['observed_ms']/1000>=stop),None)
            result['uplink_recovery_ms']=resume['observed_ms']-stop*1000 if resume else None
            calibration=result['before'];offset=calibration['observed_monotonic_s']-calibration['perf']['device_us']/1e6
            wire=[r for r in rows if r['kind']==1 and r['device_us']/1e6+offset>=stop]
            result['downlink_recovery_ms']=min((r['device_us']/1000+(offset-stop)*1000 for r in wire),default=None)
            result['downlink_clock_alignment']='Snapshot host/device offset; includes console sampling latency'
            result['recovery_bounds']=recovery_bounds(rows,packets,downlink,
                result['outage_release_begin_s'],result['outage_end_s'],mapped)
        from step8e2b_analysis import concealment_facts,device_send_cadence,perf_history
        before,after=result['before']['perf'],result['after']['perf']
        facts=concealment_facts(rows,downlink,before,after)
        result['strict_transport']=transport_gate(before,after)
        result['impaired_conformance']=impaired_conformance(before,after,facts,
            board_stt_correct=('pineapple' in result['decoded_transcript'].lower()) if 'decoded_transcript' in result else None,
            decoded_correlation=result.get('decoded_correlation_p50'))
        result['device_send_cadence_raw']=device_send_cadence(rows,before['device_us'],after['device_us'],snapshots=perf_history(out/'serial.log'))
        result['session_state_audit']=session_state_audit(result)
        result['session_survived']=result['session_state_audit']['session_survived']
        result['retired_counter_tail_unknown']=result['session_state_audit']['retired_counter_tail_unknown']
        result['passed']=result['completed'] and result['trace_complete'] and not result.get('cleanup_errors')
        result['audio_sample_intelligible']=bool(result.get('decoded_correlation_p50') is not None
            and result['decoded_correlation_p50']>=.95 and 'pineapple' in result.get('decoded_transcript','').lower())
        result['content_sampling_limit']='First 30 s returned PCM samples, including PLC/silence while active. Idle read==0 contributes no samples: long wall-clock gaps are not represented by inserted zeros. Outage arms retain a window starting 15 s before blackout and a separate post-release recovery capture. RTP/trace and resource observations cover the entire 5-minute arm.'
    except Exception as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        write(out/'result.json',result)
    return result


def e2e_arm(out,elf,args,direction,board_ip,outage=None,calibration=None):
    """One real STT/LLM/TTS voice turn, using the board-side 8b3 scorer."""
    from agent_conversation_test import run
    out.mkdir(parents=True,exist_ok=False)
    values=dict(OFFLINE_BENCH_CONTINUOUS_AUDIO='0',OFFLINE_SKIP_GREETING='1',
                BOT_ICE_INTERFACE='wlp8s0',BOT_IMPAIRED_INTERFACE='wlp8s0',BOT_ICE_REMOTE_NETWORK_ONLY='0',
                BOT_BENCH_SERVER_URL='http://192.168.7.222:7861',
                STEP8E_DISABLE_BOT_MUTE='0',BOT_VOICE_POLICY='smartturn')
    previous={k:os.environ.get(k) for k in values};os.environ.update(values)
    try:
        wait_for_quiet(out/'quiet-preflight.json')
        with Netem(board_ip,['loss','0%'],direction,state=out/'tc-state.json',account_both=True,rtp_directory=out/'voice/bot/rtp') as fault, QuietNic(out/'quiet-nic.json'):
            stop=threading.Event();armed=threading.Event();injection={};thread=None
            if outage:
                def inject():
                    path=out/'voice/bot/events.jsonl'
                    try:
                        while not stop.is_set():
                            try:events=records(path)
                            except json.JSONDecodeError:
                                stop.wait(.01);continue
                            event=next((e for e in events if armed.is_set() and e['event']=='bot-audio-start'
                                and e.get('response_id') is not None),None)
                            if event:
                                time.sleep(max(0,event['t']+.3-time.monotonic()))
                                fault.update(['loss','100%']);injection['active_s']=time.monotonic()
                                time.sleep(outage);fault.update(['loss','0%']);injection['end_s']=time.monotonic();return
                            stop.wait(.02)
                    except Exception as exc:injection['error']=f'{type(exc).__name__}: {exc}'
                thread=threading.Thread(target=inject,daemon=True);thread.start()
            try:
                code=run(SimpleNamespace(out=out/'voice',port='/dev/ttyACM0',runs=1,spl=65,
                    elf=elf,turn_limit=1,mic_gain=1600,fault_free=True,pause_case=False,
                    calibration_from=calibration,soak_minutes=None,interruptions=False,
                    restart_regression=False,playback_target=None,
                    session_start=lambda pc:assert_selected_interface(out/'voice/bot/rtp',pc,fault.interface),
                    after_calibration=(armed.set if outage else lambda:fault.update(args))))
            finally:
                stop.set()
                if thread:thread.join(outage+5)
                assert thread is None or not thread.is_alive(),'Outage injection thread did not finish'
            result=json.loads((out/'voice/result.json').read_text());result['conversation_exit_code']=code
            if outage:
                result['injected_outage']=injection
                result['outage_coverage_passed']='active_s' in injection and 'end_s' in injection and not injection.get('error')
                if not result['outage_coverage_passed']:result['passed']=False
            result['tc_counters']={dev:json.loads(tc('-s','-j','qdisc','show','dev',dev)) for dev in ('wlp8s0','ifb0')}
        write(out/'result.json',result)
        return result
    finally:
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf')
    p.add_argument('--board-ip',default='192.168.4.29');p.add_argument('--only')
    p.add_argument('--calibration-from',type=Path,help='Reuse a same-image 65 dB prompt bank without moving the bench')
    p.add_argument('--seconds',type=float,default=300);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=True)
    summary=dict(completed=False,arms=[])
    if not a.only:
        # A 30-second blackout may finish before consent expires. Retain one
        # longer five-minute arm to observe actual timeout/retry behavior.
        target=out/'consent-timeout-probe'
        probe=json.loads((target/'result.json').read_text()) if (target/'result.json').exists() else arm(
            target,a.elf.resolve(),'consent-outage-60s',['loss','0%'],'both',60,a.board_ip,max(300,a.seconds))
        summary['consent_probe']={k:probe.get(k) for k in ('completed','passed','error','state_history','reconnections')}
        write(out/'result.json',summary)
        if not probe.get('completed') or probe.get('error'):return 2
    for name,args,outage in matrix():
        if a.only and name!=a.only:continue
        for direction in ('downlink','uplink','both'):
            label=name+'-'+direction
            target=out/label
            if (target/'result.json').exists():
                result=json.loads((target/'result.json').read_text())
                if not result.get('completed'):raise RuntimeError('Failed arm retained; use a new output directory: '+label)
            else:result=arm(target,a.elf.resolve(),name,args,direction,outage,a.board_ip,a.seconds)
            if outage in (None,1,3) and result.get('completed'):
                e2e_target=out/(label+'-e2e')
                e2e=json.loads((e2e_target/'result.json').read_text()) if (e2e_target/'result.json').exists() else e2e_arm(e2e_target,a.elf.resolve(),args,direction,a.board_ip,outage,a.calibration_from)
            else:e2e=dict(passed=None,reason='Outage lasts a material part of the reply; timed recovery measured in continuous arm and mid-reply mute campaign')
            summary['arms'].append(dict(name=label,completed=result['completed'],passed=result['passed'],error=result.get('error'),e2e_passed=e2e['passed']))
            write(out/'result.json',summary)
            print(label,summary['arms'][-1],flush=True)
            if result.get('error'):return 2
    summary['completed']=True;write(out/'result.json',summary)
    return 0


if __name__=='__main__':raise SystemExit(main())

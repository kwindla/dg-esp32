#!/usr/bin/env python3
"""Step-8 arrival attribution and final 30-minute fault/turn soak.

Evidence is external, owned bot only. A failed capture is retained, never
silently restarted. --continuous reproduces the earlier duplex-soak stimulus.
"""
import argparse
import hashlib
import json
import os
import re
import time
import queue
import threading
from pathlib import Path
from esp_serial import ROOT,EspSerial
from step7_support import BotServer,prepare_prompt,transcribe,pcm
from phase2_endurance import snapshot
from arrival_trace import drain,attribute,generation_sessions,split_session_archive
from alsa import resolve_jabra,set_volumes,play_wav
from transport_test import perf,dump
from playback_quality import transport_gate,decoded_match
from quiet_nic import wait_for_quiet,QuietNic
from netem import Netem

from soak_memory import (heap_trends, closed_stack_margins, closed_stack_unknown_sizes,
    transient_memory, host_uplink_gap_windows, attach_heap_intervals, growth_verdict, unknown_heap_windows, HEAP_GROWTH_LIMIT_BYTES_PER_MINUTE, SOAK_FAULT_SCHEDULE, QUALIFICATION_MEMORY_TAIL_SECONDS)

COUNTERS=('rebuffers','drift_drops','plc_frames','i2s_underflows','decode_errors','queue_dropped','sent','received')

def wait_pending_playout(board):
    """Finish existing greeting/reply before requiring a new gate-close edge."""
    gate=board.latest_entry(r'\bGATE (closed|open)\b')
    if gate and 'GATE closed' in gate[2]:
        board.wait_for('GATE open',15,since=gate[0])
        return True
    return False

def _run(a):
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=True)
    result={'passed':False,'observations':[],'turns':[],'faults':[],'continuous':a.continuous,'arrival_trace_enabled':not a.no_trace,
            'memory_tail_seconds':getattr(a,'memory_tail_seconds',QUALIFICATION_MEMORY_TAIL_SECONDS),
            'fault_schedule':list(SOAK_FAULT_SCHEDULE)};traces={};previous={}
    source_stop=threading.Event();producer=None
    def save():(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        os.environ.update(RTP_EVIDENCE_DIR=str(out),RTP_EVIDENCE_MAX_PACKETS='100000',OFFLINE_IDLE_TIMEOUT_SECS='2100',
            OFFLINE_BENCH_CONTINUOUS_AUDIO='1' if a.continuous else '0')
        prompt=None
        if not a.continuous:
            card,device=resolve_jabra();set_volumes(card,out/'mixer.log');prompt=prepare_prompt(out)
        with BotServer(out/'bot',ice_servers=getattr(a,'ice_servers',())) as bot,EspSerial(None,log_path=out/'serial.log') as board:
            try:
                cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                identity=board.wait_for('elf_sha256=',3,since=cursor)
                assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==hashlib.sha256(a.elf.read_bytes()).hexdigest()
                if getattr(a,'server_url',None):board.command('server-url '+a.server_url)
                for option in a.option:
                    name,value=option.split('=');board.command(f'peer-option {name} {int(value)}')
                board.command('wifi-log 1')
                board.command('connect',90);time.sleep(5);board.command('audio 1')
                board.command('quiet 1' if a.continuous else 'quiet 0')
                # Keep one-time codec and observer initialization outside the timed
                # endurance interval, as in the earlier duplex-fixture harness.
                # Preserve all warmup snapshots and boot-cumulative high-water marks.
                if prompt:play_wav(prompt,device)
                cursor=board.cursor;board.command('play',10)
                board.wait_for('GATE closed',10,since=cursor);board.wait_for('GATE open',15,since=cursor)
                board.command('gate open' if a.continuous else 'gate auto');time.sleep(2)
                result['warmup']=[snapshot(board,active=True,allow_incomplete=True) for _ in range(3)]
                warm=result['warmup'][-1];previous[warm['perf']['pc_id']]=warm
                save()
                if a.continuous:board.command('gate open');board.command(f'play-long {a.minutes}',30)
                started=time.monotonic();next_sample=0;next_trace_drain=0;done_faults=set();prompts=queue.Queue()
                ready_cursor=board.cursor
                ready_entries=[entry for entry in board.lines_since(0) if 'STATE Ready ' in entry[2]]
                ready_time=ready_entries[-1][1] if ready_entries else None
                def source():
                    try:
                        for index in range(a.minutes*3):
                            if source_stop.wait(max(0,started+index*20-time.monotonic())):return
                            began=time.monotonic();play_wav(prompt,device)
                            prompts.put(dict(index=index+1,started_s=began-started,prompt_end_s=time.monotonic()-started))
                    except Exception as exc:prompts.put(dict(error=f'{type(exc).__name__}: {exc}'))
                producer=threading.Thread(target=source,daemon=True) if prompt else None
                if producer:producer.start()
                def observe():
                    nonlocal ready_cursor,ready_time,next_trace_drain
                    row=snapshot(board,active=True,allow_incomplete=True)
                    row['elapsed_s']=time.monotonic()-started;pc=row['perf']['pc_id']
                    entries=board.lines_since(ready_cursor)
                    if entries:ready_cursor=entries[-1][0]
                    ready_entries=[entry for entry in entries if 'STATE Ready ' in entry[2]]
                    if ready_entries:ready_time=ready_entries[-1][1]
                    if ready_time is not None:row['ready_elapsed_s']=ready_time-started
                    prior=previous.get(pc)
                    row['deltas']={k:row['perf'][k]-(prior['perf'][k] if prior else 0) for k in COUNTERS}
                    previous[pc]=row;result['observations'].append(row)
                    if not a.no_trace:
                        entries=drain(board,out/'arrivals-raw.jsonl');traces.setdefault(pc,[]).extend(entries)
                        assert row['perf']['peer_profile']['trace']['overwritten']==0,'Trace ring overflow; attribution incomplete'
                        row['trace_records_drained']=len(entries)
                        next_trace_drain=time.monotonic()-started+15
                    board.command('wifi-stats',10) if not a.no_trace else None
                    save();print(f't={row["elapsed_s"]:.1f}s pc={pc} counters={row["deltas"]}',flush=True)
                    return row
                while time.monotonic()-started<a.minutes*60+getattr(a,'memory_tail_seconds',QUALIFICATION_MEMORY_TAIL_SECONDS):
                    elapsed=time.monotonic()-started
                    if not a.continuous and a.minutes==30:
                        # Quiesce and drain each epoch before forced teardown.
                        for due,kind in SOAK_FAULT_SCHEDULE:
                            if elapsed>=due and due not in done_faults:
                                before=observe();began=time.monotonic();cursor=board.cursor
                                if kind=='reconnect':
                                    board.command('disconnect',40);board.command('connect',90)
                                else:
                                    bot.stop(kill=True);board.wait_for('STATE Disconnected',60,since=cursor)
                                    # Drain the terminal owner trace while the bot is down.
                                    if not a.no_trace:
                                        pc=before['perf']['pc_id'];entries=drain(board,out/'arrivals-raw.jsonl');traces.setdefault(pc,[]).extend(entries)
                                    bot.start();entry=board.wait_for_entry('STATE Ready',90,since=cursor)
                                    # Ready is logged before readiness is published.
                                    # Wait for the existing API completion barrier.
                                    board.command('connect',90);cursor=entry[0]
                                board.command('audio 1');board.wait_for('GATE open',15,since=cursor)
                                result['faults'].append(dict(kind=kind,scheduled_s=due,started_s=began-started,recovered_s=time.monotonic()-started,duration_s=time.monotonic()-began))
                                done_faults.add(due);observe()
                    elapsed=time.monotonic()-started
                    if elapsed>=next_sample:
                        observe();next_sample+=30
                    # 8e2a adds Ingress and Render records: continuous media can
                    # generate ~300 records/s, exceeding the 8192-slot ring over
                    # the unchanged 30 s heap-sampling interval. Drain separately;
                    # memory cadence, growth fits and trace acceptance stay fixed.
                    if not a.no_trace and elapsed>=next_trace_drain:
                        pc=result['observations'][-1]['perf']['pc_id']
                        entries=drain(board,out/'arrivals-raw.jsonl')
                        traces.setdefault(pc,[]).extend(entries)
                        next_trace_drain=time.monotonic()-started+15
                    if prompt and not prompts.empty():
                        turn=prompts.get_nowait()
                        if 'error' in turn:raise RuntimeError(turn['error'])
                        turn['prompt_interrupted_by_fault']=[f['kind'] for f in result['faults'] if turn['started_s']<f['recovered_s'] and turn['prompt_end_s']>f['started_s']]
                        turn['waited_for_pending_playout']=wait_pending_playout(board)
                        turn['before_perf']=perf(board);board.command('rx-clear')
                        cursor=board.cursor;board.command('play',10)
                        board.wait_for('GATE closed',10,since=cursor);board.wait_for('GATE open',15,since=cursor)
                        turn['after_perf']=perf(board)
                        turn['strict_transport']=transport_gate(turn['before_perf'],turn['after_perf'])
                        capture=out/'turns'/f"turn-{turn['index']:03}";capture.mkdir(parents=True)
                        decoded=dump(board,capture)
                        turn['decoded_content']=decoded_match(decoded,pcm(ROOT/'server/fixtures/reply_pineapple.wav'))
                        # STT runs regardless of strict transport cleanliness.
                        try:
                            turn['board_transcript']=transcribe(capture/'rx.wav')
                            turn['board_stt_correct']='pineapple' in turn['board_transcript'].lower()
                        except Exception as exc:
                            turn['board_stt_correct']=None;turn['stt_error']=f'{type(exc).__name__}: {exc}'
                        turn['finished_s']=time.monotonic()-started
                        turn['interrupted_by_fault']=[f['kind'] for f in result['faults']
                            if turn['started_s']<f['recovered_s'] and turn['finished_s']>f['started_s']]
                        result['turns'].append(turn);save()
                    time.sleep(.2)
                observe();result['elapsed_s']=time.monotonic()-started
                source_stop.set()
                if producer:producer.join(10)
                board.command('audio 0');board.command('disconnect',40)
                result['closed']=perf(board)
                for pc in list(traces)[-1:]:
                    traces[pc].extend(drain(board,out/'arrivals-raw.jsonl'))
            finally:
                for cmd in ('audio 0','mic-fixture 0','gate auto','quiet 0','wifi-log 0','disconnect'):
                    try:board.command(cmd,40)
                    except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
        server=[json.loads(s) for s in (out/'downlink.jsonl').read_text().splitlines()] if (out/'downlink.jsonl').exists() else []
        result['attribution']={}
        observed_generations={r['perf']['pc_id']:r['perf']['generation'] for r in result['warmup']+result['observations']}
        all_rows=[row for rows in traces.values() for row in rows]
        generations,mapping_evidence=generation_sessions(all_rows,server,observed_generations)
        result['generation_mapping']={tag:dict(pc_id=generations.get(tag),basis=basis)
            for tag,basis in mapping_evidence.items()}
        if not a.no_trace:
            total=result['closed']['peer_profile']['trace']
            result['trace_coverage']=dict(captured_records=len(all_rows),
                written_records=total['written'],overwritten_records=total['overwritten'],
                basis='Final closed ring count after native teardown; every drained generation included')
            assert not total['overwritten'] and len(all_rows)==total['written'], \
                'Arrival capture does not match the final ring record count'
        attributed_events=[]
        split=split_session_archive(all_rows,generations)
        for generation,pc in generations.items():
            rows=split.get(pc,[])
            # Re-split the entire archive by recorded session generation;
            # each mapped source belongs to one pc_id, including mid-drains.
            (out/f'arrivals-{pc}.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            attributed=attribute(rows,[r for r in server if r['pc_id']==pc])
            (out/f'attribution-{pc}.json').write_text(json.dumps(attributed,indent=2)+'\n')
            result['attribution'][pc]={'records':len(rows),'events':len(attributed['events']),
                'wire_sequence_gaps':len(attributed['wire_sequence_gaps']),'wire_not_authenticated':attributed['wire_not_authenticated']}
            attributed_events.extend(attributed['events'])
        if not a.no_trace:
            policy_records=[r for r in all_rows if r['kind'] in (4,5,6)]
            result['policy_event_coverage']=dict(captured_events=len(policy_records),
                attributed_events=len(attributed_events),observed_generations=observed_generations,
                event_generations=sorted({r['reserved'] for r in policy_records}))
            assert len(attributed_events)==len(policy_records), \
                'Some captured playout events belong to an unattributed generation'
        result['ten_minute_blocks']=[]
        origin=result['observations'][0]['perf'].get('device_us',0)-result['observations'][0]['elapsed_s']*1e6
        for block in range(3):
            observations=[r for r in result['observations'] if block*600<=r['elapsed_s']<(block+1)*600 or block==2 and r['elapsed_s']>=1800]
            events=[e for e in attributed_events if block*600e6<=e['device_us']-origin<(block+1)*600e6]
            result['ten_minute_blocks'].append(dict(start_minute=block*10,end_minute=(block+1)*10,
                sampled_counters={k:sum(r['deltas'][k] for r in observations) for k in COUNTERS},
                arrival_event_counters=None if a.no_trace else {key:sum(e['kind']==kind for e in events) for key,kind in [('rebuffers',5),('drift_drops',6),('plc_frames',4)]},
                attribution_counts=None if a.no_trace else {label:sum(e['attribution']==label for e in events) for label in sorted({e['attribution'] for e in events})}))
        result['block_clock_basis']='Host snapshot windows; arrival attribution disabled.' if a.no_trace else 'Device monotonic trace, anchored to first host snapshot; serial observation uncertainty is retained in raw logs.'
        result['turn_basis']='Physical prompts scheduled every 20 s; offline replies are requested after each prompt. Fault-overlapping stimuli are marked and are not claimed as uninterrupted recognized speech turns.'
        packets=[json.loads(line) for line in (out/'rtp.jsonl').read_text().splitlines()] if (out/'rtp.jsonl').exists() else []
        perf_windows=[]
        for line in (out/'serial.log').read_text().splitlines():
            if 'PERF {' in line:
                try:perf_windows.append(json.loads(line.split('PERF ',1)[1]))
                except json.JSONDecodeError:pass
        (out/'heap-windows.jsonl').write_text(''.join(json.dumps(p)+'\n' for p in perf_windows if 'heap_window' in p))
        endrow=dict(perf=result['closed'])
        augmented=attach_heap_intervals(result['observations']+[endrow],perf_windows)
        result['observations']=augmented[:-1]
        result['closed']['heap_interval']=augmented[-1].get('heap_interval')
        result['observations']=host_uplink_gap_windows(result['observations'],packets)
        obs=result['observations'];margins=[t.get('stack_margin_percent') for r in obs for t in r['perf'].get('tasks',[])]
        unknown=[t['name'] for r in obs for t in r['perf'].get('tasks',[]) if t.get('stack_margin_percent') is None]
        endpoints=[r['perf'] for r in obs]+[result['closed']]
        memory=transient_memory(obs,result['closed'],result['faults'])
        result.update(memory)
        heap=memory['heap_minimum_decrease_bytes']
        # Finalized worker high-water marks include the closing native calls,
        # after the live task census has ceased to contain these workers.
        result['closed_stack_margins']=closed_stack_margins(result['closed'])
        unknown.extend(closed_stack_unknown_sizes(result['closed']))
        result['heap_trends']=heap_trends(obs)
        result['heap_growth_limit_bytes_per_minute']=HEAP_GROWTH_LIMIT_BYTES_PER_MINUTE
        result['unknown_heap_windows']=unknown_heap_windows(result['heap_trends'])
        margins.extend(result['closed_stack_margins'].values())
        result['heap_minimum_decrease_bytes']=heap;result['unknown_stack_sizes']=sorted(set(unknown))
        result['checks']={'duration':result['elapsed_s']>=a.minutes*60,
            'heap_transient_depth':memory['heap_transient_depth_passed'],
            'reconnect_transient_budget':memory['reconnect_transient_passed'],
            'heap_growth_rate':growth_verdict(result['heap_trends']),
            'stack_margins':bool(margins) and not unknown and all(v is not None and v>=25 for v in margins),
            'decode_errors':all(not r['decode_errors'] for r in endpoints),
            'i2s_underflows':all(not r['i2s_underflows'] for r in endpoints),
            'faults':a.continuous or a.minutes!=30 or len(result['faults'])==3,
            'turns':a.continuous or len(result['turns'])==a.minutes*3,
            'board_stt':a.continuous or all(t.get('board_stt_correct') is True for t in result['turns'])}
        result['strict_transport_column']=dict(clean=sum(t.get('strict_transport',{}).get('passed') is True for t in result['turns']),
            scored=len(result['turns']),passed=all(t.get('strict_transport',{}).get('passed') is True for t in result['turns']) if result['turns'] else None)
        result['passed']=all(v is True for v in result['checks'].values())
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        source_stop.set()
        if producer:producer.join(10)
        save()
    print(json.dumps({k:v for k,v in result.items() if k not in ('observations','turns')},indent=2),flush=True)
    return 0 if result['passed'] else 1

def run(a):
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    if os.getenv('DG_BOARD', 'echoear') == 'echoear':
        # EchoEar bench policy prohibits network changes. Keep the existing
        # traffic and measure it; do not install netem or stop host services.
        return _run(a)
    wait_for_quiet(out/'quiet-preflight.json')
    with Netem('192.168.4.29',['loss','0%'],'both',state=out/'tc-state.json'), QuietNic(out/'quiet-nic.json'):
        return _run(a)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--minutes',type=int,default=30)
    p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');p.add_argument('--continuous',action='store_true')
    p.add_argument('--no-trace',action='store_true');p.add_argument('--option',action='append',default=[])
    p.add_argument('--server-url',help='RAM-only board signaling endpoint')
    p.add_argument('--ice-servers',nargs='+',default=[])
    p.add_argument('--memory-tail-seconds',type=int,choices=(0,90,180),default=QUALIFICATION_MEMORY_TAIL_SECONDS,
                   help='Observe after the full 30-minute/90-prompt/3-fault schedule for per-session settling and pooled heap-fit coverage')
    a=p.parse_args()
    if a.minutes not in (20,30):p.error('Qualification soak requires 20 or 30 minutes')
    raise SystemExit(run(a))

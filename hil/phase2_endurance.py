#!/usr/bin/env python3
"""Thirty-minute duplex fixture soak, 50 lifecycle cycles and five reboots.

Run on the final root image. Per-minute and per-cycle heap/task snapshots are
retained in raw serial and result.json. No sample or first-use retention is hidden.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request
import numpy as np
from esp_serial import EspSerial, ROOT
from transport_test import wait_until, perf, dump, fixture, records
from phase2_campaign import write_json


def snapshot_once(board):
    cursor=board.cursor
    row=perf(board)
    time.sleep(.1)
    text='\n'.join(e[2] for e in board.lines_since(cursor))
    match=re.search(r'HEAP phase=stats (.*)',text)
    # PERF is already a complete buffered capture. Independent ESP_LOG census
    # output can be lost on older images; retain that observation so the caller
    # can retry the census or explicitly mark it incomplete.
    heaps={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',match[1])} if match else {}
    regions=[dict(kind=m[1],start=m[2],length=int(m[3]),free=int(m[4]),largest=int(m[5]),minimum=int(m[6]))
             for m in re.finditer(r'REGION phase=stats .*kind=(\S+) start=(\S+) length=(\d+) free=(\d+) largest=(\d+) minimum=(\d+)',text)]
    tasks=[dict(name=m[1],free=int(m[2]),priority=int(m[3]),runtime_us=int(m[4]),
                core=int(m[5]) if m[5] is not None else None,
                task_id=int(m[6]) if m[6] is not None else None)
           for m in re.finditer(r'STACK task=(.*?) free_bytes=(\d+) priority=(\d+) runtime_us=(\d+)(?: core=(-?\d+) task_id=(\d+))?',text)]
    audio=[list(map(int,m)) for m in re.findall(r'AUDIO drops=(\d+) read_errors=(\d+) write_errors=(\d+)',text)]
    capture_stacks=[int(v) for v in re.findall(r'capture_stack=(\d+)',text)]
    cost=re.search(r'PLAYBACK_COST frames=(\d+) mean_us=(\d+) max_us=(\d+) total_us=(\d+)',text)
    playback_cost=dict(zip(('frames','mean_us','max_us','total_us'),map(int,cost.groups()))) if cost else None
    stages=re.search(r'PLAYBACK_STAGES pull_calls=(\d+) pull_wall_us=(\d+) i2s_calls=(\d+) i2s_wall_us=(\d+) i2s_max_us=(\d+)',text)
    playback_stages=dict(zip(('pull_calls','pull_wall_us','i2s_calls','i2s_wall_us','i2s_max_us'),map(int,stages.groups()))) if stages else None
    return dict(playback_cost=playback_cost,playback_stages=playback_stages,perf=row,heap=heaps,regions=regions,tasks=tasks,audio=audio,
                capture_stack=min(capture_stacks) if capture_stacks else None,
                observed_monotonic_s=time.monotonic(), observed_unix_s=time.time())


def snapshot(board, active=False, allow_incomplete=False):
    attempts=[]
    for _ in range(3):
        row=snapshot_once(board)
        complete=({'general_internal','main_largest','psram'} <= row['heap'].keys()
                  and {r['kind'] for r in row['regions']}=={'main','rom','sram2','reserve','rtc','external'}
                  and bool(row['audio']))
        if active:
            complete=complete and {'peer-loop','audio-tx','peer-rx','audio-playback','audio-capture'} <= {t['name'] for t in row['tasks']}
        if complete:
            row['incomplete_census_attempts']=attempts
            row['census_complete']=True
            return row
        attempts.append(row)
        time.sleep(.2)
    if allow_incomplete:
        # PERF is buffered and remains valid even when independent ESP_LOG
        # census lines interleave or disappear. A soak-only observation must
        # retain that media evidence instead of stopping a continuous session.
        return dict(attempts[-1],incomplete_census_attempts=attempts[:-1],census_complete=False)
    raise AssertionError('Incomplete heap/audio census after three retained raw serial snapshots')


def uplink_summary(packets):
    sequences=set();highest=packets[0]['seq'] if packets else 0
    for packet in packets:
        extended=highest+(packet['seq']-highest%65536+32768)%65536-32768
        sequences.add(extended);highest=max(highest,extended)
    return dict(packets=len(packets),unique_timestamps=len({p['timestamp'] for p in packets}),
        wire_missing_lower_bound=max(sequences)-min(sequences)+1-len(sequences) if sequences else 0,
        max_gap_ms=max((b['arrival_ms']-a['arrival_ms'] for a,b in zip(packets,packets[1:])),default=None))


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--minutes',type=int,default=30);p.add_argument('--cycles',type=int,default=50)
    p.add_argument('--reassociations',type=int,default=5)
    p.add_argument('--soak-only',action='store_true',
                   help='Run only the duplex soak, without lifecycle cycles or reboots')
    p.add_argument('--resume-lifecycle',action='store_true',
                   help='Retain a completed soak and resume interrupted cycles on the same boot')
    p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');a=p.parse_args()
    if not 1 <= a.minutes <= 30 or a.cycles < 3 or a.reassociations < 1:
        p.error('minutes must be 1..30, cycles >=3, reassociations >=1')
    if a.soak_only and a.resume_lifecycle:
        p.error('--soak-only cannot resume lifecycle tests')
    if a.soak_only:
        a.cycles = a.reassociations = 0
    out=a.out.resolve()
    if a.resume_lifecycle:
        result=json.loads((out/'result.json').read_text())
        assert len(result['minutes'])==a.minutes and result.get('error') and not result['reassociations']
        archive=out/'result-before-lifecycle-resume.json'
        assert not archive.exists(),'Lifecycle interruption has already been resumed once'
        write_json(archive,result)
        result.setdefault('lifecycle_failures',[]).append(dict(
            attempt=len(result['cycles'])+1,error=result.pop('error'),rerun='resume once on same boot'))
    else:
        out.mkdir(parents=True,exist_ok=False)
        result=dict(passed=False,minutes=[],cycles=[],reassociations=[],soak_only=a.soak_only)
    proc=None
    try:
        with socket.socket() as probe:assert probe.connect_ex(('127.0.0.1',7861))!=0,'7861 occupied'
        env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
        env.update(BOT_MODE='offline-test',SERVER_PORT='7861',EVENTS_PATH=str(out/'events.jsonl'),
                   RECORD_INPUT='1',RECORD_INPUT_DIR=str(out/'input'),RTP_EVIDENCE_DIR=str(out),
                   RTP_EVIDENCE_MAX_PACKETS='100000',
                   OFFLINE_IDLE_TIMEOUT_SECS='2100',
                   OFFLINE_FIXTURE_LOOPS=str(a.minutes),
                   PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
        with (out/('lifecycle-resume-server.log' if a.resume_lifecycle else 'server.log')).open('w') as log:
            proc=subprocess.Popen([str(ROOT/'server/.venv/bin/python'),'bot.py','--host','0.0.0.0','--port','7861'],cwd=ROOT/'server',env=env,stdout=log,stderr=subprocess.STDOUT)
        def up():
            try:return urllib.request.urlopen('http://127.0.0.1:7861/',timeout=.5).status<500
            except Exception:return False
        wait_until(up,30)
        expected=hashlib.sha256(a.elf.read_bytes()).hexdigest()
        def reset(board):
            time.sleep(.2);cursor=board.cursor;board.reset();board.wait_for('READY transport-console',40,since=cursor)
            identity=board.wait_for('elf_sha256=',3,since=cursor)
            assert re.search(r'elf_sha256=([0-9a-f]{64})',identity)[1]==expected,'Firmware identity mismatch'
            return cursor
        with EspSerial('/dev/ttyACM0',log_path=out/('lifecycle-resume-serial.log' if a.resume_lifecycle else 'serial.log')) as board:
            if a.resume_lifecycle:
                # Do not reset or replace any successful disconnect sample.
                original=(out/'serial.log').read_text(errors='replace')
                assert re.findall(r'elf_sha256=([0-9a-f]{64})',original)[-1]==expected
                board.command('disconnect',timeout=15)
            else:
                beginning=reset(board);board.command('connect',timeout=30);time.sleep(4)
                board.command('gate open');board.command('audio 1');result['base']=snapshot(board,active=True,allow_incomplete=a.soak_only)
                board.command(f'play-long {a.minutes}',timeout=30)
                started=time.monotonic()
                for i in range(1,a.minutes+1):
                    deadline=started+i*60
                    while time.monotonic()<deadline:time.sleep(min(5,max(0,deadline-time.monotonic())))
                    row=snapshot(board,active=True,allow_incomplete=a.soak_only);row.update(minute=i,elapsed_s=time.monotonic()-started)
                    result['minutes'].append(row);write_json(out/'result.json',result)
                    print(f'Minute {i}: general={row["heap"].get("general_internal", "missing")}, largest={row["heap"].get("main_largest", "missing")}, underflows={row["perf"]["i2s_underflows"]}, rebuffers={row["perf"]["rebuffers"]}, drift={row["perf"]["drift_drops"]}',flush=True)
                    previous=result['base'] if i==1 else result['minutes'][-2]
                    for counter in ['sent','received','pulled_frames']:
                        assert row['perf'][counter]-previous['perf'][counter]>=2900, f'Soak stopped progressing: {counter}'
                board.command('audio 0');board.command('disconnect',timeout=15)
                result['decoded_fixture']=fixture(dump(board,out),'reply_pineapple.wav')
            for i in range(len(result['cycles'])+1,a.cycles+1):
                board.command('connect',timeout=30);time.sleep(4)
                board.command('disconnect',timeout=15)
                row=snapshot(board);row['cycle']=i;result['cycles'].append(row)
                write_json(out/'result.json',result)
                print(f'Cycle {i}: general={row["heap"]["general_internal"]}, largest={row["heap"]["main_largest"]}, psram={row["heap"]["psram"]}',flush=True)
            # Current console has no AP-disassociation control. The brief allows
            # reboot fallback; each reboot verifies association, media readiness,
            # and disconnect on this same saved image.
            for i in range(1,a.reassociations+1):
                cursor=reset(board);board.command('connect',timeout=30);time.sleep(4)
                row=snapshot(board);row['event']=i;result['reassociations'].append(row)
                board.command('disconnect',timeout=15);write_json(out/'result.json',result)
                print(f'Reassociation {i}: reboot/READY/connect/disconnect complete',flush=True)
        end=result['minutes'][-1]['perf'];base=result['base']['perf']
        counters={k:end[k]-base[k] for k in ['i2s_underflows','rebuffers','drift_drops','plc_frames','late','lost','queue_dropped','decode_errors','send_errors','sent','received','pulled_frames']}
        result['soak_deltas']=counters
        result['ten_minute_blocks']=[]
        previous=result['base']
        for row in result['minutes']:
            if row['minute'] % 10 == 0 or row['minute'] == a.minutes:
                result['ten_minute_blocks'].append(dict(
                    start_minute=previous.get('minute',0), end_minute=row['minute'],
                    counters={k:row['perf'][k]-previous['perf'][k] for k in counters}))
                previous=row
        packets=[r for r in records(out/'rtp.jsonl') if r['pc_id']==base['pc_id']]
        result['server_uplink']=uplink_summary(packets)
        depths=[r['perf']['playout']['physical_occupancy'] for r in result['minutes']]
        elapsed=[r['elapsed_s'] for r in result['minutes']]
        result['depth_slope_frames_per_s']=float(np.polyfit(elapsed,depths,1)[0]) if len(depths)>1 else None
        result['trends']={}
        for key in ['general_internal','main_largest','psram']:
            vals=[r['heap'][key] for r in result['cycles']]
            if not vals:
                continue
            x=np.arange(len(vals),dtype=float);y=np.array(vals,dtype=float)
            slope,intercept=np.polyfit(x,y,1)
            residual=y-(slope*x+intercept)
            stderr=float(np.sqrt(np.sum(residual**2)/(len(vals)-2)/np.sum((x-x.mean())**2)))
            # 95% two-sided t interval, df=48 for the requested 50 cycles.
            from scipy.stats import t
            margin=float(t.ppf(.975,len(vals)-2))*stderr
            result['trends'][key]=dict(first=vals[0],last=vals[-1],min=min(vals),max=max(vals),
                slope_bytes_per_cycle=float(slope),slope_ci95=[float(slope-margin),float(slope+margin)],
                downward_trend=bool(slope+margin < -1e-6))
        result['checks']=dict(continuous=len(result['minutes'])==a.minutes and result['minutes'][-1]['elapsed_s']>=a.minutes*60,
            frame_coverage=all(counters[k]>=a.minutes*3000-100 for k in ['sent','received','pulled_frames']),
            server_coverage=result['server_uplink']['unique_timestamps']>=a.minutes*3000-100,
            playout=not any(counters[k] for k in ['i2s_underflows','rebuffers','queue_dropped','decode_errors','send_errors']),
            capture=all(r['audio'] and all(not any(c) for c in r['audio']) for r in result['minutes']),
            fixture=result['decoded_fixture']['passed'],
            cycles=len(result['cycles'])==a.cycles,
            reassociations=len(result['reassociations'])==a.reassociations)
        if not a.soak_only:
            result['checks'].update(
                general_trend=not result['trends']['general_internal']['downward_trend'],
                main_largest_trend=not result['trends']['main_largest']['downward_trend'])
        else:
            del result['checks']['cycles']
            del result['checks']['reassociations']
        result['passed']=all(result['checks'].values())
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        if proc:
            proc.terminate()
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        write_json(out/'result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ['minutes','cycles','reassociations','base']},indent=2))
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())

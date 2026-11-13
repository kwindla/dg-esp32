#!/usr/bin/env python3
"""Chosen-clock r2step6 operation rates, idlecat/full and complete gate tails."""
import argparse,json,time
from pathlib import Path
from esp_serial import EspSerial,ROOT
from step9a_assets import asset_server
from step9a_support import setup as original_setup,read,FIXTURES
from transport_test import perf
from step9a_r2_diagnose import pages,save
from step9a_r2_checks import audit
import step9a_r2_measure as measure
from alsa import record_room_baseline
OUT=Path('/home/khkramer/src/evidence/step9a/r3')
ELF=OUT/'r3b-image/dg-esp32.elf'

def idle(path,hz,mode):
    path.mkdir(exist_ok=False);r={'clock_hz':hz,'mode':mode,'diagnostic':False,'passed':False};r['room_baseline']=record_room_baseline(path/'room-baseline',path.name)
    if r['room_baseline']['noise_contaminated']:save(path/'result.json',r);raise RuntimeError('ACOUSTIC HOLD: >-50dBFS')
    with EspSerial('/dev/ttyACM0',log_path=path/'serial.log',display_checks=True) as board:
        try:
            at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at)
            board.command('quiet 1');board.command('audio 0');board.command('disp clock '+str(hz));r['setup']=original_setup(board,full=True)
            board.command('disp defer-persistence 1');board.command('disp r2-reset-metrics');board.command('disp bench '+mode+(' 30' if mode=='cat' else ' 10'));time.sleep(2)
            r['before']=perf(board);start=time.monotonic();points=[r['before']]
            for i in range(6):
                time.sleep(max(0,start+(i+1)*10-time.monotonic()));points.append(perf(board))
            r['after']=points[-1];r['snapshots']=points;dt=(r['after']['device_us']-r['before']['device_us'])/1e6;r['fps']=(r['after']['display']['rendered']-r['before']['display']['rendered'])/dt
            board.command('disp bench off');time.sleep(.2);r['frames']=pages(board,'frame-trace','DISPLAY_FRAME_TRACE');r['gate_stats']=[read(board,'gate-stats '+str(i),'DISPLAY_GATE_STATS') for i in range(5)];r['stress']=read(board,'r3-stress-status','DISPLAY_R3_STRESS');r['actual']=read(board,'r3-info','DISPLAY_R3_INFO')
            assert not r['stress']['running'] and not r['stress']['allocations_live']
            if mode=='full':
                fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text());r['owned_cleanup']=measure.work(board,'delete',fixture,'r3b-perf-owned-cleanup');r['cleanup_status']=read(board,'status','DISPLAY');assert r['cleanup_status']['flash']['count']==16 and r['cleanup_status']['flash']['temp_bytes']==0
            r['passed']=r['fps']>= (28 if mode=='cat' else 5)
        finally:
            board.display_cleanup=True
            for cmd in ('disp bench off','audio 0','disconnect'):
                try:board.command(cmd,45)
                except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            r['live_monitor']=board.display_monitor.result();save(path/'before-close.json',r)
    r['monitor']=audit((path/'serial.log').read_bytes());r['passed']=r['passed'] and r['monitor']['passed'] and not r.get('cleanup_errors');save(path/'result.json',r);return r

def main():
    p=argparse.ArgumentParser();p.add_argument('--hz',type=int,required=True);a=p.parse_args();out=OUT/('r3b-performance-'+str(a.hz));out.mkdir(exist_ok=False)
    def selected_setup(board,**kwargs):board.command('disp clock '+str(a.hz));return original_setup(board,**kwargs)
    measure.setup=selected_setup
    with asset_server():
        rows=[]
        for kind in ('persist','preload'):
            baseline=record_room_baseline(out/(kind+'-room-baseline'),kind)
            if baseline['noise_contaminated']:raise RuntimeError('ACOUSTIC HOLD: >-50dBFS')
            r=measure.arm(out/kind,ELF,kind);full=audit((out/kind/'serial.log').read_bytes());r['full_closed_raw_monitor']=full;r['room_baseline']=baseline;r['passed']=r['passed'] and full['passed'] and not r.get('cleanup_errors');save(out/kind/'closed-audit.json',full);rows.append(r)
            if not r['passed']:save(out/'summary.json',{'passed':False,'rows':rows});return 2
        rows.extend([idle(out/'idle-cat',a.hz,'cat'),idle(out/'bounded-full',a.hz,'full')])
        gate_max=max(max(row['wait'][2],row['hold'][2]) for r in rows for row in r.get('gate_stats',[]) if row['op']!=4)
        summary={'passed':all(r['passed'] for r in rows) and gate_max<2000000,'selected_hz':a.hz,'gate_wait_hold_max_us':gate_max,'gate_bound_us':2000000,'rows':rows,'no_stressor':'no allocations/task in fresh production boots; statuschecked inidle/full','frame_gaps':'complete operation histograms and retained frame traces; hold tails include scheduler/preemption'}
        save(out/'summary.json',summary)
    print(json.dumps({'passed':summary['passed'],'selected_hz':a.hz,'gate_max_us':gate_max,'rates':[(r.get('kind',r.get('mode')),r.get('rate',{}).get('fps',r.get('fps'))) for r in rows]}),flush=True)
    return 0 if summary['passed'] else 2
if __name__=='__main__':raise SystemExit(main())

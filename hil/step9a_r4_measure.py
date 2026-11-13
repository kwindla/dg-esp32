#!/usr/bin/env python3
"""All-op gate latency, cat liveness/rate and whole persist/preload durations."""
import argparse,hashlib,json,time
from pathlib import Path
from esp_serial import EspSerial,ROOT
from transport_test import perf
from step9a_assets import asset_server
from step9a_support import setup,read,request,terminal,FIXTURES
from step9a_r2_diagnose import pages,save
from step9a_r2_faults import prepare_ram,work
import os


def distribution(bins,count,maximum):
    def quantile(q):
        target=max(1,int(__import__('math').ceil(count*q)));at=0
        for i,n in enumerate(bins):
            at+=n
            if at>=target:return [0 if i==0 else 1<<i,(1<<(i+1))-1]
        return None
    return {'count':count,'p50_us_bounds':quantile(.5),'p99_us_bounds':quantile(.99),'max_us':maximum,'basis':'complete-operation base2 histograms; inclusive bounds, not rounded exact percentiles'}


def arm(out,elf,kind):
    out.mkdir();r={'kind':kind,'diagnostic':False,'measurement_only':True,'passed':False,'elf_sha256':hashlib.sha256(elf.read_bytes()).hexdigest()};fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text())
    with EspSerial('/dev/ttyACM0',log_path=out/'serial.log',display_checks=True) as board:
        try:
            at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at)
            assert r['elf_sha256'] in board.wait_for('elf_sha256=',3,since=at)
            board.command('quiet 1');board.command('audio 0');board.command('disp r4-guard '+os.environ['STEP9A_R4_GUARD']);board.command('disp clock 40000000');r['setup']=setup(board)
            board.command('disp defer-persistence 1')
            if kind=='persist':prepare_ram(board,fixture)
            else:
                assert work(board,'preload',fixture,'measure-prime')['error']=='none';board.command('disp ram-evict '+fixture['hash'])
            time.sleep(.3);board.command('disp r2-reset-metrics');board.command('disp bench cat 30');time.sleep(2)
            r['hash_before']=read(board,'r4-hash-status','DISPLAY_R4_HASH');r['before']=read(board,'status','DISPLAY');r['before_perf']=perf(board);at=board.cursor;began=time.monotonic()
            request(board,kind,{'hash':fixture['hash']},'measure-op')
            samples=[];next_sample=time.monotonic()+1
            while time.monotonic()-began<90:
                rows=[entry[2] for entry in board.lines_since(at) if 'DISPLAY_WORK_RESULT ' in entry[2] and '"id":"measure-op"' in entry[2]]
                if rows:
                    r['terminal']=json.loads(rows[0].split('DISPLAY_WORK_RESULT ',1)[1]);break
                if time.monotonic()>=next_sample:samples.append(perf(board));next_sample=time.monotonic()+1
                time.sleep(.01)
            else:raise RuntimeError('Durable operation result timeout')
            r['duration_s']=time.monotonic()-began;r['hash_after']=read(board,'r4-hash-status','DISPLAY_R4_HASH');r['after_perf']=perf(board);r['after']=read(board,'status','DISPLAY');r['during_snapshots']=samples
            dt=(r['after_perf']['device_us']-r['before_perf']['device_us'])/1e6
            r['rate']={'interval_s':dt,'rendered':r['after_perf']['display']['rendered']-r['before_perf']['display']['rendered'],'basis':'device rendered-counter snapshots bracketing the entire operation, including admission and final result'};r['rate']['fps']=r['rate']['rendered']/dt
            assert r['terminal']['error']=='none'
            time.sleep(.2);board.command('disp bench off');time.sleep(.1)
            r['frames']=pages(board,'frame-trace','DISPLAY_FRAME_TRACE');r['flash_trace']=pages(board,'flash-trace','DISPLAY_FLASH_TRACE');r['flash_stats']=read(board,'flash-stats','DISPLAY_FLASH_STATS');r['gate_stats']=[read(board,'gate-stats '+str(op),'DISPLAY_GATE_STATS') for op in range(5)]
            r['distributions']=[]
            for op,row in enumerate(r['gate_stats']):
                r['distributions'].append({'op':op,'wait':distribution(row['wait_bins'],row['wait'][0],row['wait'][2]),'hold':distribution(row['hold_bins'],row['hold'][0],row['hold'][2])})
            points=[r['before_perf']]+samples+[r['after_perf']]
            r['rate']['windows']=[{'interval_s':(b['device_us']-a['device_us'])/1e6,'fps':(b['display']['rendered']-a['display']['rendered'])/((b['device_us']-a['device_us'])/1e6)} for a,b in zip(points,points[1:])]
            r['monitor']=board.display_monitor.result();r['passed']=r['monitor']['passed'] and r['rate']['fps']>=24 and all(w['fps']>=24 for w in r['rate']['windows'] if w['interval_s']>=1)
        except Exception as exc:r['error']=str(exc);raise
        finally:
            board.display_cleanup=True
            for cmd in ('disp bench off','audio 0','disconnect'):
                try:board.command(cmd,45)
                except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            r['monitor']=board.display_monitor.result();save(out/'result.json',r)
    print(json.dumps({'kind':kind,'duration_s':r.get('duration_s'),'rate':r.get('rate'),'passed':r['passed'],'progress_max':r.get('gate_stats',[{}])[0].get('progress_max')}),flush=True)
    return r


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');a=p.parse_args();a.out.mkdir(exist_ok=False)
    save(a.out/'predeclared.json',{'order':['persist','preload'],'bytes':306917,'gate_ops':['read','prog','erase'],'persist_min_sustained_fps':24,'sustained_basis':'whole operation and every complete >=1-second snapshot interval; partial final interval retained','per_op_percentiles':'base2 bounds over all ops after quiet worker reset; exact maxima','retakes':False,'no_calls':True})
    with asset_server():
        rows=[arm(a.out/'persist',a.elf,'persist')]
        if rows[0]['passed']:rows.append(arm(a.out/'preload',a.elf,'preload'))
    save(a.out/'summary.json',{'passed':all(r['passed'] for r in rows),'rows':rows})
    return 0 if all(r['passed'] for r in rows) else 2

if __name__=='__main__':raise SystemExit(main())

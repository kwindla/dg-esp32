#!/usr/bin/env python3
"""R3 diagnostic cells; raw failures retained, no repeats disguised as retakes."""
import argparse,hashlib,json,math,os,subprocess,time
from pathlib import Path
from scipy.stats import chi2
from esp_serial import EspSerial,ROOT,port_lock
from step9a_assets import asset_server
from step9a_support import setup,read,request,terminal,host_address,FIXTURES
from step9a_r2_diagnose import pages,save
from step7_support import BotServer
from transport_test import READY_LOG
from alsa import record_room_baseline

OUT=Path('/home/khkramer/src/evidence/step9a/r3')
ELF=OUT/'diagnostic-image/dg-esp32.elf'

def information(board):return read(board,'r3-info','DISPLAY_R3_INFO')

def snapshot(board,path):
    at=board.cursor;board.command('stats',30)
    rows=[e[2] for e in board.lines_since(at) if e[2].startswith('PERF ')]
    sample={'host_s':time.monotonic(),'rows':rows}
    try:
        if len(rows)!=1:raise ValueError('Expected exactly one PERF')
        sample['value']=json.loads(rows[0][5:])
    except ValueError as exc:sample['parse_error']=str(exc)
    with path.open('a') as f:f.write(json.dumps(sample)+'\n')
    return sample

def dump_arrivals(board,path):
    at=board.cursor;board.command('dump-arrivals',60)
    rows=[e[2] for e in board.lines_since(at)]
    with path.open('a') as f:f.write(json.dumps({'host_s':time.monotonic(),'lines':rows})+'\n')

def durable(board,kind,data,rid):
    request(board,kind,data,rid);event=terminal(board,rid,90)
    if event['error']!='none':raise RuntimeError(f'{rid}: {event}')
    return event

def persist_after_disconnect(board,hash,prior,path):
    deadline=time.monotonic()+90;next_perf=0
    while time.monotonic()<deadline:
        events=read(board,'persisted '+str(prior+1),'DISPLAY_PERSISTED')
        if any(e[0]>prior and e[1]==hash and e[2]=='none' for e in events['events']):return events
        if time.monotonic()>=next_perf:snapshot(board,path);next_perf=time.monotonic()+1
        time.sleep(.1)
    raise RuntimeError('Post-session auto-persist timeout')

def exposure(before,after):
    keys=['rendered','attempted','queued','completed','attempted_bytes','completed_bytes','dma_active_us','dma_errors','spi_api_count','spi_coverage_errors']
    r={k:after[k]-before[k] for k in keys};r['elapsed_us']=after['device_us']-before['device_us'];r['fps']=r['rendered']*1e6/r['elapsed_us'];r['max_scheduling_gap_us']=after['scheduling_gap_max_us']
    n=r['completed'];k=r['dma_errors'];r['error_rate']=k/n if n else None;r['poisson_one_sided_95_upper_per_stripe']=float(chi2.ppf(.95,2*(k+1))/2/n) if n else None
    r['basis']='completed SPI transactions / requested payload bytes; includes failed DMA and recovery/repaint exposure, not verified panel pixels; gaps are lifetime maximum since fresh boot'
    return r

def cell(name,hz,*,kind='stress',burst=0,period=0,phase=0,target=100000,stop_on_error=False):
    path=OUT/name;path.mkdir(exist_ok=False)
    r={'name':name,'diagnostic':True,'kind':kind,'requested_hz':hz,'burst_us':burst,'period_us':period,'phase_us':phase,'target_stripes':target,'stop_on_error':stop_on_error,'elf_sha256':hashlib.sha256(ELF.read_bytes()).hexdigest(),'completed':False,'invalid_reasons':[],'fresh_boot':True}
    r['room_baseline']=record_room_baseline(path/'room-baseline',name)
    if r['room_baseline']['noise_contaminated']:save(path/'result.json',r);raise RuntimeError('ACOUSTIC HOLD: > -50 dBFS')
    fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text())
    with EspSerial('/dev/ttyACM0',log_path=path/'serial.log',display_checks=True,display_diagnostic=True) as board:
        board.display_terminal_events=True
        try:
            at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at)
            assert r['elf_sha256'] in board.wait_for('elf_sha256=',3,since=at)
            for cmd in ('quiet 1','audio 0','disp clock '+str(hz),'server-url http://'+host_address()+':7861'):board.command(cmd)
            r['setup']=setup(board,full=True)
            board.command('disp r2 diagnostic 1')
            if kind=='production':
                for cmd in ('peer-option call-timing 1','peer-option arrival-trace 1','gate open'):board.command(cmd)
                durable(board,'delete',{'hash':fixture['hash'],'seq':2000000},'initial-delete')
            board.command('disp bench '+('full 10' if kind=='stress-full' else 'cat 30'));time.sleep(2)
            r['before']=information(board);r['stress_before']=read(board,'r3-stress-status','DISPLAY_R3_STRESS')
            if burst:board.command(f'disp r3-stress {burst} {period} {phase}')
            r['cycles']=[];r['dumps']=0;r['arrivals_dumps']=0;last_error_total=0;dma_rows={};next_trace=time.monotonic()+10
            if kind=='production':
                for cycle in range(30):
                    row={'cycle':cycle+1,'before':information(board)};r['cycles'].append(row)
                    # Engine sequence resets on connect; explicit high sequence avoids cat updates racing defaults.
                    if cycle:durable(board,'delete',{'hash':fixture['hash'],'seq':2000000},'remove-'+str(cycle))
                    at=board.cursor;board.command('connect',90);board.wait_for(READY_LOG,45,since=at)
                    board.command('play-long 1',30)
                    row['fetch']=durable(board,'fetch',{'seq':1000000,'hash':fixture['hash'],'bytes':fixture['bytes'],'url':'http://'+host_address()+':7874/'+fixture['hash']+'.dgi'},'fetch-'+str(cycle))
                    if row['fetch']['persisted']:raise RuntimeError('In-call fixture was not RAM-only')
                    for i in range(35):
                        time.sleep(.1);snapshot(board,path/'stats.jsonl');r['dumps']+=1;dump_arrivals(board,path/'arrivals-dumps.jsonl');r['arrivals_dumps']+=1
                    prior=read(board,'persisted','DISPLAY_PERSISTED')['total']
                    board.command('disconnect',45)
                    row['persistence']=persist_after_disconnect(board,fixture['hash'],prior,path/'stats.jsonl')
                    time.sleep(.2);snapshot(board,path/'stats.jsonl');row['after']=information(board)
                    traces=pages(board,'dma-trace','DISPLAY_DMA_TRACE');dma_rows.update({e[0]:e for e in traces['items']});row['dma_trace_total']=traces['total']
                    save(path/'partial.json',r);print(json.dumps({'cell':name,'cycle':cycle+1,'dumps':r['dumps'],'completed_stripes':row['after']['completed']-r['before']['completed'],'dma_errors':row['after']['dma_errors']}),flush=True)
            # Stress and remaining natural exposure: PERF counter coverage at one-second cadence.
            until=time.monotonic()+3600
            while True:
                now=information(board)
                if now['dead']:raise RuntimeError('Display terminal dead')
                if now['completed']-r['before']['completed']>=target:break
                if stop_on_error and now['dma_errors']>r['before']['dma_errors']:break
                if time.monotonic()>until:raise RuntimeError('Stripe exposure deadline exceeded')
                if time.monotonic()>=next_trace or now['dma_errors']!=last_error_total:
                    traces=pages(board,'dma-trace','DISPLAY_DMA_TRACE');dma_rows.update({e[0]:e for e in traces['items']});last_error_total=now['dma_errors'];next_trace=time.monotonic()+10
                time.sleep(1);snapshot(board,path/'stats.jsonl')
                if int((now['completed']-r['before']['completed'])//5000)!=int(r.get('progress_bin',-1)):
                    r['progress_bin']=(now['completed']-r['before']['completed'])//5000;print(json.dumps({'cell':name,'completed_stripes':now['completed']-r['before']['completed'],'dma_errors':now['dma_errors']}),flush=True)
            r['after']=information(board);r['exposure']=exposure(r['before'],r['after'])
            if burst:board.command('disp r3-stress off')
            r['stress_after']=read(board,'r3-stress-status','DISPLAY_R3_STRESS')
            s=r['stress_after'];elapsed=s['elapsed_us']
            r['stress_measured']={'bytes_per_second':s['copied_bytes']*1e6/elapsed if elapsed else 0,'wall_clock_busy_duty':s['busy_us']/elapsed if elapsed else 0,'overlap_stripes':s['dma_overlap_stripes']-r['stress_before']['dma_overlap_stripes'],'nominal_duty':burst/period if burst else 0,'basis':'burst wall clock includes preemption; copied payload bytes counted once, actual memory bus traffic includes read and write; working set512KiB'}
            if burst and not r['stress_measured']['overlap_stripes']:r['invalid_reasons'].append('No observed DMA/stress overlap')
            board.command('disp bench off');time.sleep(.2)
            traces=pages(board,'dma-trace','DISPLAY_DMA_TRACE');dma_rows.update({e[0]:e for e in traces['items']});r['dma_trace']={'total':traces['total'],'items':list(dma_rows.values()),'retained_unique':len(dma_rows)}
            r['spi_trace']=pages(board,'spi-trace','DISPLAY_SPI_TRACE');r['flash_trace']=pages(board,'flash-trace','DISPLAY_FLASH_TRACE');r['gate_stats']=[read(board,'gate-stats '+str(i),'DISPLAY_GATE_STATS') for i in range(5)]
            r['completed']=True
        except Exception as exc:r['error']=f'{type(exc).__name__}: {exc}';r['invalid_reasons'].append(r['error'])
        finally:
            board.display_cleanup=True
            for cmd in ('disp r3-stress off','audio 0','disconnect'):
                try:board.command(cmd,45)
                except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            # No fixture delete for pure stress cells. Production cleanup stays observed until durable completion.
            if kind=='production':
                try:durable(board,'delete',{'hash':fixture['hash'],'seq':3000000},'owned-cleanup')
                except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            try:board.command('disp bench off');r['cleanup_info']=information(board);r['cleanup_status']=read(board,'status','DISPLAY');r['cleanup_stress']=read(board,'r3-stress-status','DISPLAY_R3_STRESS')
            except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            r['monitor']=board.display_monitor.result()
            for e in r['monitor']['errors']:
                if any(k in e for k in ('malformed_stat','display_liveness','missing_rendered_counter','missing_display_health')):r['invalid_reasons'].append(e)
            if r.get('after',{}).get('spi_coverage_errors'):r['invalid_reasons'].append('SPI trace coverage error')
            if r.get('cleanup_errors'):r['invalid_reasons'].append({'cleanup_errors':r['cleanup_errors']})
            r['valid']=r['completed'] and not r['invalid_reasons'];r['clean']=r['valid'] and r.get('exposure',{}).get('dma_errors')==0 and r['monitor']['passed']
            save(path/'result.json',r)
    print(json.dumps({'cell':name,'valid':r['valid'],'clean':r['clean'],'exposure':r.get('exposure'),'stress':r.get('stress_measured'),'invalid_reasons':r['invalid_reasons']}),flush=True)
    return r

def main():
    p=argparse.ArgumentParser();p.add_argument('--name',required=True);p.add_argument('--hz',type=int,default=40000000);p.add_argument('--kind',default='stress');p.add_argument('--burst',type=int,default=0);p.add_argument('--period',type=int,default=0);p.add_argument('--phase',type=int,default=0);p.add_argument('--target',type=int,default=100000);p.add_argument('--stop-on-error',action='store_true');a=p.parse_args()
    os.environ['OFFLINE_SKIP_GREETING']='1';os.environ['OFFLINE_IDLE_TIMEOUT_SECS']='2100'
    with asset_server(),BotServer(OUT/(a.name+'-bot'),port=7861):
        r=cell(a.name,a.hz,kind=a.kind,burst=a.burst,period=a.period,phase=a.phase,target=a.target,stop_on_error=a.stop_on_error)
    return 0 if r['valid'] else 2
if __name__=='__main__':raise SystemExit(main())

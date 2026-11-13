#!/usr/bin/env python3
"""R3b real-trigger diagnostics, durable context and complete cleanup audit."""
import argparse,hashlib,json,math,os,time
from pathlib import Path
from scipy.stats import chi2
from esp_serial import EspSerial,ROOT
from step9a_assets import asset_server
from step9a_support import setup,read,request,host_address,FIXTURES,pack,BUDGET_PATH
from step9a_r2_diagnose import pages,save
from step9a_r2_checks import audit
from step7_support import BotServer
from transport_test import READY_LOG
from alsa import record_room_baseline
OUT=Path('/home/khkramer/src/evidence/step9a/r3')
ELF=OUT/'r3b-image/dg-esp32.elf'


def append(path,value):
    with path.open('a') as f:f.write(json.dumps(value)+'\n')

class Observer:
    def __init__(self,board,path):
        self.board=board;self.path=path;self.cursor=board.cursor;self.errors={};self.spi={};self.work={};self.parse_failures=[];self.telemetry_failures=[]
    def scan(self):
        rows=self.board.lines_since(self.cursor)
        if rows:self.cursor=rows[-1][0]
        for cursor,host,line in rows:
            for marker,target in (('DISPLAY_DMA_ERROR ',self.errors),('DISPLAY_DMA_SPI_ERROR ',self.spi),('DISPLAY_WORK_RESULT ',self.work)):
                if marker not in line:continue
                try:
                    event=json.loads(line.split(marker,1)[1])
                    if marker=='DISPLAY_DMA_ERROR ':
                        assert len(event['record'])==15 and len(event['record'][11])==4
                        key=event['index'];assert event['record'][0]==key
                    elif marker=='DISPLAY_DMA_SPI_ERROR ':key=(event['dma_index'],event['record'][0])
                    else:key=event['id']
                    if key in target and target[key]!=event:raise ValueError('Conflicting durable record')
                    target[key]=event;append(self.path/'durable-events.jsonl',{'cursor':cursor,'host_s':host,'marker':marker.strip(),'value':event})
                except Exception as exc:
                    bad={'cursor':cursor,'host_s':host,'line':line,'reason':str(exc)};self.parse_failures.append(bad);append(self.path/'durable-parse-failures.jsonl',bad)
    def command(self,cmd,timeout=30):
        result=self.board.command(cmd,timeout);self.scan();return result
    def info(self):
        for attempt in range(3):
            try:r=read(self.board,'r3-info','DISPLAY_R3_INFO');self.scan();return r
            except Exception as exc:
                bad={'host_s':time.monotonic(),'action':'r3-info','attempt':attempt,'reason':str(exc)};self.telemetry_failures.append(bad);append(self.path/'telemetry-failures.jsonl',bad);self.scan();time.sleep(.05)
        raise RuntimeError('No primary counter endpoint after3 retained query failures')
    def stats(self):
        at=self.board.cursor;self.command('stats');rows=[r[2] for r in self.board.lines_since(at) if r[2].startswith('PERF ')];result={'host_s':time.monotonic(),'raw':rows}
        try:
            if len(rows)!=1:raise ValueError('Expected one PERF')
            result['value']=json.loads(rows[0][5:])
        except ValueError as exc:result['parse_error']=str(exc)
        append(self.path/'requested-stats.jsonl',result);return result
    def arrivals(self):
        at=self.board.cursor;self.command('dump-arrivals',60)
        rows=[r[2] for r in self.board.lines_since(at)];append(self.path/'arrival-dumps.jsonl',{'host_s':time.monotonic(),'rows':rows})
        if 'ARRIVALS BEGIN version=1 record_bytes=32' not in rows or 'ARRIVALS END' not in rows:raise RuntimeError('Arrival dump framing incomplete')
    def durable(self,kind,data,rid):
        request(self.board,kind,data,rid);self.scan();deadline=time.monotonic()+90;next_info=time.monotonic()+1
        while time.monotonic()<deadline:
            self.scan()
            if rid in self.work:
                r=self.work[rid]
                if r['error']!='none':raise RuntimeError(f'{rid}: {r}')
                return r
            if time.monotonic()>=next_info:
                if self.info()['dead']:raise RuntimeError('Display terminal dead during '+rid)
                next_info=time.monotonic()+1
            time.sleep(.02)
        raise RuntimeError('Durable work event missing: '+rid)
    def persisted(self,hash,prior):
        deadline=time.monotonic()+90;next_info=time.monotonic()+1
        while time.monotonic()<deadline:
            r=read(self.board,'persisted '+str(prior+1),'DISPLAY_PERSISTED');self.scan()
            if any(e[0]>prior and e[1]==hash and e[2]=='none' for e in r['events']):return r
            if time.monotonic()>=next_info:
                if self.info()['dead']:raise RuntimeError('Display terminal dead during auto-persist')
                next_info=time.monotonic()+1
            time.sleep(.1)
        raise RuntimeError('Production auto-persist timeout')


def delta(before,after):
    keys=['rendered','attempted','queued','completed','attempted_bytes','completed_bytes','dma_active_us','dma_errors','spi_api_count','spi_coverage_errors']
    e={k:after[k]-before[k] for k in keys};e['elapsed_us']=after['device_us']-before['device_us'];e['fps']=e['rendered']*1e6/e['elapsed_us'];e['max_scheduling_gap_us']=after['scheduling_gap_max_us'];n=e['completed'];k=e['dma_errors']
    e['poisson_one_sided_95_upper_per_stripe']=float(chi2.ppf(.95,2*(k+1))/2/n) if n else None;e['error_rate_per_stripe']=k/n if n else None
    return e

def settle(obs,seconds):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:obs.scan();time.sleep(min(.05,max(0,deadline-time.monotonic())))

def pad_stripes(obs,before,target,result,path):
    deadline=time.monotonic()+7200;progress=-1
    while True:
        current=obs.info();done=current['completed']-before['completed']
        if done>=target:return
        if current['dead']:raise RuntimeError('Display terminal dead')
        if time.monotonic()>deadline:raise RuntimeError('7200s stripe exposure deadline')
        if done//5000!=progress:
            progress=done//5000;print(json.dumps({'cell':result['name'],'stripes':done,'dma_errors':current['dma_errors'],'primary_context_records':len(obs.errors)}),flush=True)
            save(path/'partial.json',result)
        settle(obs,1);obs.stats()


def run(name,kind,hz,*,cycles=0,dumps=0,min_stripes=0,min_padding_stripes=0,burst_calls=1,resume_setup=False):
    path=OUT/name;path.mkdir(exist_ok=False);fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text())
    r={'name':name,'kind':kind,'diagnostic':True,'fresh_boot':not resume_setup,'continued_preflight_boot':resume_setup,'requested_hz':hz,'elf_sha256':hashlib.sha256(ELF.read_bytes()).hexdigest(),'requested_cycles':cycles,'requested_dumps':dumps,'min_stripes':min_stripes,'min_padding_stripes':min_padding_stripes,'burst_calls':burst_calls,'completed':False,'rows':[],'invalid_reasons':[]}
    r['room_baseline']=record_room_baseline(path/'room-baseline',name)
    if r['room_baseline']['noise_contaminated']:save(path/'result.json',r);raise RuntimeError('ACOUSTIC HOLD: > -50 dBFS')
    with EspSerial('/dev/ttyACM0',log_path=path/'serial.log',display_checks=True,display_diagnostic=True) as board:
        obs=Observer(board,path);board.display_terminal_events=True
        try:
            if resume_setup:
                parent=json.loads((OUT/'r3b-A40/result.json').read_text());now=obs.info();status=read(board,'status','DISPLAY');obs.scan()
                assert now['device_us']>parent['cleanup_info']['device_us'] and now['completed']==parent['cleanup_info']['completed']==35 and now['dma_errors']==0
                assert status['ram']['budget']==0 and status['bench'][0]==0 and now['requested_hz']==hz and hz==40000000
                r['continuity']={'parent_segment':'r3b-A40','info':now,'status':status,'no_reset':True,'setup_failed_before_cat_or_call':True,'prior_cleanup_repaint_stripes':35}
                for cmd in ('quiet 1','audio 0','server-url http://'+host_address()+':7861'):obs.command(cmd)
                budget=json.loads(BUDGET_PATH.read_text());obs.command('disp origin http://'+host_address()+':7874');obs.command('disp arena {budget} {F} {W} {largest}'.format(**budget))
                images=pack()
                for index,image in enumerate(images):
                    obs.durable('load',{'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host_address()+':7874/'+image['hash']+'.dgi'},'fixture-'+str(index));obs.command('disp asset '+str(index)+' '+image['hash'])
                r['setup']={'budget':budget,'images':images,'status':read(board,'status','DISPLAY'),'initialization':'already completed once on parent preflight boot; not repeated'};obs.scan()
            else:
                at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);assert r['elf_sha256'] in board.wait_for('elf_sha256=',3,since=at)
                for cmd in ('quiet 1','audio 0','disp clock '+str(hz),'server-url http://'+host_address()+':7861'):obs.command(cmd)
                r['setup']=setup(board,full=True);obs.scan()
            obs.command('disp r2 diagnostic 1')
            if kind in ('A','C'):obs.durable('delete',{'hash':fixture['hash']},'initial-remove')
            if kind in ('A','B'):
                for cmd in ('peer-option call-timing 1','peer-option arrival-trace 1','mic-fixture 1','gate open','audio 1'):obs.command(cmd)
            if kind=='C':obs.command('disp defer-persistence 1')
            obs.command('disp bench cat 30');settle(obs,2);r['before']=obs.info();obs.stats()
            if kind=='A':
                for cycle in range(cycles):
                    row={'cycle':cycle+1,'before':obs.info()};r['rows'].append(row)
                    at=board.cursor;obs.command('connect',90);board.wait_for(READY_LOG,45,since=at);obs.scan();ready=time.monotonic();row['ready']=obs.info()
                    row['fetch_before']=obs.info();row['fetch']=obs.durable('fetch',{'seq':1000000,'hash':fixture['hash'],'bytes':fixture['bytes'],'url':'http://'+host_address()+':7874/'+fixture['hash']+'.dgi'},'A-fetch-'+str(cycle));row['fetch_after']=obs.info()
                    assert not row['fetch']['persisted'],'In-call fetch must remain RAM-only'
                    settle(obs,max(0,ready+5-time.monotonic()));obs.stats();settle(obs,.35)
                    prior=read(board,'persisted','DISPLAY_PERSISTED')['total'];obs.scan();row['disconnect_before']=obs.info();obs.command('disconnect',45);row['disconnect_after']=obs.info()
                    row['persisted']=obs.persisted(fixture['hash'],prior);row['persist_after']=obs.info()
                    row['cleanup']=obs.durable('delete',{'hash':fixture['hash'],'seq':2000000},'A-clean-'+str(cycle));settle(obs,.35);row['after']=obs.info();obs.stats()
                    row['exposure']=delta(row['before'],row['after']);save(path/'partial.json',r)
                    print(json.dumps({'cell':name,'cycle':cycle+1,'stripes':row['after']['completed']-r['before']['completed'],'dma_errors':row['after']['dma_errors'],'contexts':len(obs.errors)}),flush=True)
                r['trigger_after']=obs.info();r['padding_before']=r['trigger_after'];pad_stripes(obs,r['before'],max(min_stripes,r['padding_before']['completed']-r['before']['completed']+min_padding_stripes),r,path);r['padding_after']=obs.info()
            elif kind=='B':
                r['calls']=[];r['pairs']=[]
                for call in range(burst_calls):
                    call_row={'call':call+1,'before':obs.info()};r['calls'].append(call_row)
                    at=board.cursor;obs.command('connect',90);board.wait_for(READY_LOG,45,since=at);obs.scan();settle(obs,.35);call_row['burst_before']=obs.info()
                    for i in range(dumps):
                        pair={'call':call+1,'dump':i+1,'cursor_before':board.cursor,'host_before_s':time.monotonic()};r['pairs'].append(pair)
                        settle(obs,.1);obs.stats();obs.arrivals();pair['cursor_after']=board.cursor;pair['host_after_s']=time.monotonic()
                        if (i+1)%50==0:
                            current=obs.info();r['rows'].append({'call':call+1,'dump':i+1,'info':current});save(path/'partial.json',r);print(json.dumps({'cell':name,'call':call+1,'dumps':i+1,'stripes':current['completed']-r['before']['completed'],'dma_errors':current['dma_errors'],'contexts':len(obs.errors)}),flush=True)
                    call_row['burst_after']=obs.info();settle(obs,.35);obs.command('disconnect',45);settle(obs,.35);call_row['after']=obs.info()
                r['call_before']=r['calls'][0]['burst_before'];r['call_after']=r['calls'][-1]['burst_after']
            elif kind=='C':
                for cycle in range(cycles):
                    row={'cycle':cycle+1,'before':obs.info()};r['rows'].append(row)
                    if cycle:row['delete']=obs.durable('delete',{'hash':fixture['hash'],'seq':10000000*(cycle+1)},'C-remove-'+str(cycle))
                    row['ram_load']=obs.durable('load',{'hash':fixture['hash'],'bytes':fixture['bytes'],'url':'http://'+host_address()+':7874/'+fixture['hash']+'.dgi','seq':10000000*(cycle+1)+1000000},'C-ram-'+str(cycle));assert not row['ram_load']['persisted']
                    row['persist_before']=obs.info();row['persist']=obs.durable('persist',{'hash':fixture['hash'],'seq':10000000*(cycle+1)+2000000},'C-persist-'+str(cycle));row['persist_after']=obs.info()
                    obs.command('disp ram-evict '+fixture['hash']);row['evict_after']=obs.info();row['preload']=obs.durable('preload',{'hash':fixture['hash'],'seq':10000000*(cycle+1)+3000000},'C-preload-'+str(cycle));row['after']=obs.info();row['exposure']=delta(row['before'],row['after'])
                    settle(obs,.35);obs.stats();save(path/'partial.json',r);print(json.dumps({'cell':name,'cycle':cycle+1,'stripes':row['after']['completed']-r['before']['completed'],'dma_errors':row['after']['dma_errors'],'contexts':len(obs.errors)}),flush=True)
            elif kind=='E':
                pad_stripes(obs,r['before'],min_stripes,r,path)
            elif kind=='D':
                r['stress_before']=read(board,'r3-stress-status','DISPLAY_R3_STRESS');obs.scan();obs.command('disp r3-stress 20000 21000 0 0');pad_stripes(obs,r['before'],min_stripes,r,path);obs.command('disp r3-stress off');r['stress_after']=read(board,'r3-stress-status','DISPLAY_R3_STRESS');obs.scan()
            else:raise ValueError(kind)
            r['after']=obs.info();r['exposure']=delta(r['before'],r['after']);r['completed']=True
        except Exception as exc:r['error']=f'{type(exc).__name__}: {exc}';r['invalid_reasons'].append(r['error'])
        finally:
            board.display_cleanup=True
            # Keep adjacent final stats/disconnect PERF from sampling before a
            # scheduled frame can run; all old failed snapshots stay retained.
            settle(obs,.35)
            for cmd in ('disp r3-stress off','audio 0','disconnect'):
                try:obs.command(cmd,45)
                except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            if kind in ('A','C'):
                try:obs.durable('delete',{'hash':fixture['hash'],'seq':3000000000},'owned-final-cleanup')
                except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            for cmd in ('disp defer-persistence 0','disp call-persistence 0','mic-fixture 0','gate auto','disp bench off'):
                try:obs.command(cmd,45)
                except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            try:
                settle(obs,.35);r['cleanup_info']=obs.info();r['cleanup_status']=read(board,'status','DISPLAY');obs.scan();r['cleanup_stress']=read(board,'r3-stress-status','DISPLAY_R3_STRESS');obs.scan();r['dma_ring']=pages(board,'dma-trace','DISPLAY_DMA_TRACE');obs.scan();r['spi_ring']=pages(board,'spi-trace','DISPLAY_SPI_TRACE');obs.scan();r['gate_stats']=[read(board,'gate-stats '+str(i),'DISPLAY_GATE_STATS') for i in range(5)];obs.scan()
            except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            r['live_monitor']=board.display_monitor.result();save(path/'before-close.json',r)
    # Read every final byte after the capture closes; do not omit the closing tail.
    r['monitor']=audit((path/'serial.log').read_bytes(),diagnostic=True);r['context_records']=list(obs.errors.values());r['spi_error_records']=list(obs.spi.values());r['context_parse_failures']=obs.parse_failures;r['telemetry_failures']=obs.telemetry_failures
    total=r.get('cleanup_info',{}).get('dma_errors');indices=set(obs.errors);r['every_dma_error_context_captured']=total is not None and indices==set(range(total))
    if not r['every_dma_error_context_captured']:r['invalid_reasons'].append({'DMA_context_incomplete':{'counter_total':total,'captured_indices':len(indices)}})
    if obs.parse_failures:r['invalid_reasons'].append({'durable_parse_failures':len(obs.parse_failures)})
    if r.get('cleanup_errors'):r['invalid_reasons'].append({'cleanup_errors':r['cleanup_errors']})
    if r.get('cleanup_info',{}).get('spi_coverage_errors'):r['invalid_reasons'].append('SPI API coverage error')
    if kind=='D' and r.get('stress_after',{}).get('dma_overlap_stripes',0)-r.get('stress_before',{}).get('dma_overlap_stripes',0)<=0:r['invalid_reasons'].append('No DMA/copy overlap')
    if any('display_liveness' in e or 'missing_rendered_counter' in e or 'missing_display_health' in e for e in r['monitor']['errors']):r['invalid_reasons'].append('Liveness/health coverage failed')
    r['rate_valid']=r['completed'] and not r['invalid_reasons'];r['clean']=r['rate_valid'] and r['monitor']['passed'] and not r['telemetry_failures']
    if r.get('exposure'):
        n=len(r['rows']) if kind in ('A','C') else dumps*burst_calls if kind=='B' else 0;k=r['exposure']['dma_errors'];affected=sum(row.get('exposure',{}).get('dma_errors',0)>0 for row in r['rows']) if kind in ('A','C') else None
        r['cycle_statistics']={'cycles':n,'hardware_errors':k,'affected_cycles':affected,'events_per_cycle':k/n if n else None,'affected_cycle_rate':affected/n if n and affected is not None else None,'poisson_one_sided_95_upper_events_per_cycle':float(chi2.ppf(.95,2*(k+1))/2/n) if n else None,'basis':'A/C cycle windows separately annotated; whole-cell error count also includes between-cycle/cleanup exposure, so do not misattribute padding events as cycle events'}
    save(path/'result.json',r);print(json.dumps({'cell':name,'rate_valid':r['rate_valid'],'clean':r['clean'],'exposure':r.get('exposure'),'cycles':r.get('cycle_statistics'),'context_records':len(obs.errors),'invalid_reasons':r['invalid_reasons']}),flush=True);return r

def main():
    p=argparse.ArgumentParser();p.add_argument('--name',required=True);p.add_argument('--kind',choices=['A','B','C','D','E'],required=True);p.add_argument('--hz',type=int,default=40000000);p.add_argument('--cycles',type=int,default=0);p.add_argument('--dumps',type=int,default=0);p.add_argument('--min-stripes',type=int,default=0);p.add_argument('--min-padding-stripes',type=int,default=0);p.add_argument('--burst-calls',type=int,default=1);p.add_argument('--resume-setup',action='store_true');a=p.parse_args()
    os.environ['OFFLINE_SKIP_GREETING']='1';os.environ['OFFLINE_IDLE_TIMEOUT_SECS']='7200'
    with asset_server(),BotServer(OUT/(a.name+'-bot'),port=7861):r=run(a.name,a.kind,a.hz,cycles=a.cycles,dumps=a.dumps,min_stripes=a.min_stripes,min_padding_stripes=a.min_padding_stripes,burst_calls=a.burst_calls,resume_setup=a.resume_setup)
    return 0 if r['rate_valid'] else 2
if __name__=='__main__':raise SystemExit(main())

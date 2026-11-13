#!/usr/bin/env python3
"""R4 unguarded40MHz crypto inventory, every byte through cleanup retained."""
import argparse,hashlib,json,os,time
from pathlib import Path
from esp_serial import EspSerial,ROOT
from step9a_assets import asset_server
from step9a_support import setup,read,request,host_address,FIXTURES
from step9a_r2_diagnose import pages,save
from step9a_r2_checks import audit
from step9a_r3b_natural import Observer,delta,settle
from step7_support import BotServer
from transport_test import READY_LOG
from alsa import record_room_baseline
OUT=Path('/home/khkramer/src/evidence/step9a/r4');ELF=Path(os.environ.get('STEP9A_R4_ELF',str(OUT/'image/dg-esp32.elf')))
def crypto(obs):
    rows=[];start=0
    while True:
        p=read(obs.board,'r4-crypto '+str(start),'DISPLAY_R4_CRYPTO');obs.scan();rows+=p['items'];start+=len(p['items'])
        if start>=p['total']:return dict(p,items=rows,start=0)
        if not p['items']:raise RuntimeError('Crypto paging made no progress')
def difference(a,b):
    old={ (r['task'],r['peripheral'],r['asset']):r for r in a['items']};rows=[]
    for r in b['items']:
        prior=old.get((r['task'],r['peripheral'],r['asset']),{});row=dict(r)
        for k in ('starts','input_psram','input_internal','output_psram_capacity','output_internal_capacity','failures'):row[k]=r[k]-prior.get(k,0)
        rows.append(row)
    return rows

def run(name,kind,guard=0):
    path=OUT/name;path.mkdir(exist_ok=False);r={'name':name,'kind':kind,'guard':guard,'diagnostic':True,'completed':False,'elf_sha256':hashlib.sha256(ELF.read_bytes()).hexdigest(),'rows':[]}
    r['room_baseline']=record_room_baseline(path/'room-baseline',name)
    if r['room_baseline']['noise_contaminated']:save(path/'result.json',r);raise RuntimeError('ACOUSTIC HOLD')
    if kind=='flood':os.environ['OFFLINE_R4_FLOOD_SECONDS']='300'
    else:os.environ.pop('OFFLINE_R4_FLOOD_SECONDS',None)
    os.environ['OFFLINE_SKIP_GREETING']='1';os.environ['OFFLINE_IDLE_TIMEOUT_SECS']='7200'
    with asset_server(),BotServer(path/'bot',port=7861) as bot,EspSerial('/dev/ttyACM0',log_path=path/'serial.log',display_checks=True,display_diagnostic=True) as b:
        obs=Observer(b,path);b.display_terminal_events=True
        fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text())
        try:
            at=b.cursor;b.reset();b.wait_for('READY transport-console',60,since=at);assert r['elf_sha256'] in b.wait_for('elf_sha256=',3,since=at)
            for cmd in ('quiet 1','audio 0','disp clock 40000000','disp r4-guard '+str(guard),'server-url http://'+host_address()+':7861'):obs.command(cmd)
            r['setup']=setup(b);obs.scan();obs.command('disp r2 diagnostic 1')
            if kind=='fetch':obs.durable('delete',{'hash':fixture['hash']},'inventory-initial-delete')
            for cmd in ('peer-option call-timing 1','peer-option arrival-trace 1','mic-fixture 1','gate open','audio 1','disp bench cat 30'):obs.command(cmd)
            settle(obs,2);r['before']=obs.info();obs.stats();r['crypto_before']=crypto(obs);r['hash_before']=read(b,'r4-hash-status','DISPLAY_R4_HASH');obs.scan()
            for cycle in range(60 if kind=='handshakes' else 1):
                row={'cycle':cycle+1,'crypto_before':crypto(obs),'before':obs.info()};r['rows'].append(row)
                at=b.cursor;obs.command('connect',90);b.wait_for(READY_LOG,45,since=at);obs.scan();ready=time.monotonic();row['crypto_ready']=crypto(obs)
                if kind=='fetch':
                    row['fetch']=obs.durable('fetch',{'seq':1000000,'hash':fixture['hash'],'bytes':fixture['bytes'],'url':'http://'+host_address()+':7874/'+fixture['hash']+'.dgi'},'inventory-fetch');assert not row['fetch']['persisted'];row['crypto_fetch']=crypto(obs)
                elif kind=='flood':
                    deadline=time.monotonic()+345
                    while True:
                        events=bot.events()
                        if any(e.get('event')=='r4-flood-failed' for e in events):raise RuntimeError('Bot flood failed')
                        if any(e.get('event')=='r4-flood-complete' for e in events):break
                        if time.monotonic()>deadline:raise RuntimeError('Bot flood completion deadline')
                        settle(obs,1);snapshot=obs.stats();now=obs.info()
                        if guard and b.display_monitor.errors:raise RuntimeError('R4 confirmation flood raw/counter/liveness stop: '+repr(b.display_monitor.errors[0]))
                        if now['dead'] or snapshot.get('value',{}).get('display',{}).get('failed'):raise RuntimeError('Display producer failed in flood')
                    row['flood_events']=[e for e in bot.events() if str(e.get('event','')).startswith('r4-flood-')];row['crypto_flood']=crypto(obs)
                settle(obs,max(0,ready+5-time.monotonic()));obs.stats();settle(obs,.35)
                prior=read(b,'persisted','DISPLAY_PERSISTED')['total'];obs.scan();obs.command('disconnect',45)
                if kind=='fetch':
                    row['persisted']=obs.persisted(fixture['hash'],prior);row['crypto_persist']=crypto(obs);row['cleanup']=obs.durable('delete',{'hash':fixture['hash'],'seq':2000000},'inventory-owned-delete')
                settle(obs,.35);row['after']=obs.info();row['crypto_after']=crypto(obs);row['delta']=difference(row['crypto_before'],row['crypto_after']);obs.stats();save(path/'partial.json',r)
                print(json.dumps({'cell':name,'cycle':cycle+1,'dma_errors':row['after']['dma_errors'],'PSRAMcrypto':sum(x['input_psram'] for x in row['delta'])}),flush=True)
            r['after']=obs.info();r['crypto_after']=crypto(obs);r['hash_after']=read(b,'r4-hash-status','DISPLAY_R4_HASH');obs.scan();r['exposure']=delta(r['before'],r['after']);r['crypto_delta']=difference(r['crypto_before'],r['crypto_after']);r['completed']=True
        except Exception as e:r['error']=repr(e)
        finally:
            b.display_cleanup=True;settle(obs,.35)
            for cmd in ('audio 0','disconnect','disp r5-hash off','disp bench off','mic-fixture 0','gate auto','disp defer-persistence 0','disp call-persistence 0'):
                try:obs.command(cmd,45)
                except Exception as e:r.setdefault('cleanup_errors',[]).append(repr(e))
            if kind=='fetch':
                try:obs.durable('delete',{'hash':fixture['hash'],'seq':3000000000},'inventory-final-delete')
                except Exception as e:r.setdefault('cleanup_errors',[]).append(repr(e))
            try:
                settle(obs,.35);r['cleanup_info']=obs.info();r['cleanup_status']=read(b,'status','DISPLAY');obs.scan();r['cleanup_crypto']=crypto(obs);r['dma_ring']=pages(b,'dma-trace','DISPLAY_DMA_TRACE');obs.scan();r['cleanup_hash_worker']=read(b,'r5-hash-status','DISPLAY_R5_HASH');obs.scan()
            except Exception as e:r.setdefault('cleanup_errors',[]).append(repr(e))
            r['live_monitor']=b.display_monitor.result();save(path/'before-close.json',r)
    r['monitor']=audit((path/'serial.log').read_bytes(),diagnostic=True);r['context_events']=list(obs.errors.values());r['parse_failures']=obs.parse_failures;r['telemetry_failures']=obs.telemetry_failures
    r['all_error_contexts']=set(obs.errors)==set(range(r.get('cleanup_info',{}).get('dma_errors',0)))
    r['measurement_valid']=r['completed'] and r['all_error_contexts'] and not(obs.parse_failures or obs.telemetry_failures or r.get('cleanup_errors')) and not r.get('cleanup_crypto',{}).get('coverage_lost') and not r.get('cleanup_crypto',{}).get('gate_failures')
    r['clean']=r['measurement_valid'] and r['monitor']['passed']
    if kind=='flood' and r['completed']:
        events=r['rows'][0]['flood_events'];done=next(e for e in events if e['event']=='r4-flood-sending-complete');drain=next(e for e in events if e['event']=='r4-flood-complete')
        r['flood_delivery']={'sending':done,'drain':drain,'complete':done['elapsed_s']>=300 and drain['pending_bytes']==0}
        r['measurement_valid'] &= r['flood_delivery']['complete'];r['clean'] &= r['flood_delivery']['complete']
    save(path/'result.json',r);print(json.dumps({'name':name,'measurement_valid':r['measurement_valid'],'clean':r['clean'],'error':r.get('error'),'delta':r.get('crypto_delta'),'exposure':r.get('exposure')}),flush=True);return r

def main():
    p=argparse.ArgumentParser();p.add_argument('--name',required=True);p.add_argument('--kind',choices=['handshakes','flood','fetch'],required=True);p.add_argument('--guard',type=int,default=0);a=p.parse_args();r=run(a.name,a.kind,a.guard)
    return 0 if r['measurement_valid'] else 2
if __name__=='__main__':raise SystemExit(main())

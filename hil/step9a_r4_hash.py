#!/usr/bin/env python3
"""Predeclared item5 SHA/GDMA diagnostics; isolated candidates, no adoption."""
import argparse,hashlib,json,os,time
from pathlib import Path
from scipy.stats import chi2
from esp_serial import EspSerial,ROOT
from step9a_assets import asset_server
from step9a_support import setup,read
from step9a_r2_diagnose import pages,save
from step9a_r2_checks import audit
from step9a_r3b_natural import Observer,delta,settle,append
from alsa import record_room_baseline
OUT=Path('/home/khkramer/src/evidence/step9a/r4')
ELF=Path(os.environ.get('STEP9A_R4_ELF',str(OUT/'image/dg-esp32.elf')))

class HashObserver(Observer):
    def __init__(self,board,path):super().__init__(board,path);self.hash_errors={}
    def scan(self):
        before=self.cursor
        super().scan()
        # Parse exactly the batch consumed by the parent, including bytes
        # appended between entry and its read; leave later bytes for next scan.
        rows=[row for row in self.board.lines_since(before) if row[0]<=self.cursor]
        for cursor,host,line in rows:
            if 'DISPLAY_HASH_DMA_ERROR ' not in line:continue
            try:
                r=json.loads(line.split('DISPLAY_HASH_DMA_ERROR ',1)[1]);key=r['dma_index']
                if key in self.hash_errors and self.hash_errors[key]!=r:raise ValueError('Conflicting hash context')
                self.hash_errors[key]=r;append(self.path/'hash-error-events.jsonl',{'cursor':cursor,'host_s':host,'value':r})
            except Exception as exc:self.parse_failures.append({'cursor':cursor,'host_s':host,'line':line,'reason':str(exc)})
    def hash_status(self):r=read(self.board,'r5-hash-status','DISPLAY_R5_HASH');self.scan();return r

def run(name,core,mode):
    path=OUT/name;path.mkdir(exist_ok=False)
    r={'name':name,'core':core,'mode':mode,'requested_hz':40000000,'diagnostic':True,'fresh_boot':True,'candidate_only':mode>=2,'production_asset_internal_chunks':True,'guard':int(os.environ['STEP9A_R4_GUARD']),'elf_sha256':hashlib.sha256(ELF.read_bytes()).hexdigest(),'completed':False,'invalid_reasons':[]}
    r['room_baseline']=record_room_baseline(path/'room-baseline',name)
    if r['room_baseline']['noise_contaminated']:save(path/'result.json',r);raise RuntimeError('ACOUSTIC HOLD >-50dBFS')
    with EspSerial('/dev/ttyACM0',log_path=path/'serial.log',display_checks=True,display_diagnostic=True) as board:
        obs=HashObserver(board,path);board.display_terminal_events=True
        try:
            at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);assert r['elf_sha256'] in board.wait_for('elf_sha256=',3,since=at)
            for cmd in ('quiet 1','audio 0','disp clock 40000000','disp r4-guard '+os.environ['STEP9A_R4_GUARD']):obs.command(cmd)
            r['setup']=setup(board);obs.scan();obs.command('disp r2 diagnostic 1');obs.command('disp defer-persistence 1');obs.command('disp bench cat 30');settle(obs,2)
            r['before']=obs.info();obs.stats();r['hash_before']=obs.hash_status();obs.command(f'disp r5-hash {core} {mode}')
            positive=name=='guard-positive';deadline=time.monotonic()+1200;bucket=-1
            while True:
                settle(obs,.01 if positive else 1)
                current=obs.info();n=current['completed']-r['before']['completed']
                if positive and current['dma_errors']>r['before']['dma_errors']:break
                if not positive and current['dma_errors']>r['before']['dma_errors']:raise RuntimeError('Guard/confirmation failed: LCD DMA error')
                snapshot=obs.stats();h=obs.hash_status()
                r['current']=current;r['hash_current']=h
                if n//1000!=bucket:bucket=n//1000;save(path/'partial.json',r);print(json.dumps({'cell':name,'stripes':n,'dma_errors':current['dma_errors'],'hashes':h['hashes'],'sha_gdma_starts':h['gdma_starts'],'sha_psram_bytes':h['psram_bytes'],'sha_overlap_stripes':h['sha_overlap_stripes']}),flush=True)
                if current['dead']:raise RuntimeError('Display terminal dead')
                if snapshot.get('value',{}).get('display',{}).get('failed'):raise RuntimeError('Display failed; cat producer stopped, invalid cell')
                if not h['running']:raise RuntimeError('Hash worker exited before stop')
                if n>=10000 and h['hashes']>=1000:break
                if time.monotonic()>deadline:raise RuntimeError('Declared1200s exposure deadline')
            obs.command('disp r5-hash off');r['hash_after']=obs.hash_status();r['after']=obs.info();r['exposure']=delta(r['before'],r['after']);r['completed']=True
        except Exception as exc:r['error']=f'{type(exc).__name__}: {exc}';r['invalid_reasons'].append(r['error'])
        finally:
            board.display_cleanup=True;settle(obs,.35)
            for cmd in ('disp r5-hash off','disp r3-stress off','audio 0','disconnect','disp bench off','disp defer-persistence 0','mic-fixture 0','gate auto'):
                try:obs.command(cmd,45)
                except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            try:
                settle(obs,.35);from step9a_r4_inventory import crypto
                r['cleanup_crypto']=crypto(obs);r['cleanup_info']=obs.info();r['cleanup_status']=read(board,'status','DISPLAY');obs.scan();r['cleanup_hash']=obs.hash_status();r['cleanup_copy']=read(board,'r3-stress-status','DISPLAY_R3_STRESS');obs.scan();r['dma_ring']=pages(board,'dma-trace','DISPLAY_DMA_TRACE');obs.scan();r['gate_stats']=[read(board,'gate-stats '+str(i),'DISPLAY_GATE_STATS') for i in range(5)];obs.scan()
            except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            r['live_monitor']=board.display_monitor.result();save(path/'before-close.json',r)
    r['closed_monitor']=audit((path/'serial.log').read_bytes(),diagnostic=True);r['error_contexts']=list(obs.errors.values());r['hash_error_contexts']=list(obs.hash_errors.values());r['context_parse_failures']=obs.parse_failures;r['telemetry_failures']=obs.telemetry_failures
    total=r.get('cleanup_info',{}).get('dma_errors');r['all_DMA_contexts_captured']=total is not None and set(obs.errors)==set(range(total))
    # Hash context is required for every error during the armed interval; setup and cleanup errors are separately retained.
    armed=set(range(r.get('before',{}).get('dma_errors',0),r.get('after',r.get('cleanup_info',{})).get('dma_errors',0)))
    r['all_armed_hash_contexts_captured']=armed<=set(obs.hash_errors)
    h=r.get('hash_after',r.get('cleanup_hash',{}))
    for key in ('sha_failures','hash_failures','mismatches','gate_failures'):
        if h.get(key):r['invalid_reasons'].append({key:h[key]})
    if not r['all_DMA_contexts_captured'] or not r['all_armed_hash_contexts_captured']:r['invalid_reasons'].append('Incomplete error context coverage')
    if obs.parse_failures or obs.telemetry_failures:r['invalid_reasons'].append('Durable parse/telemetry failure')
    if r.get('cleanup_errors'):r['invalid_reasons'].append({'cleanup_errors':r['cleanup_errors']})
    if r.get('cleanup_crypto',{}).get('coverage_lost') or r.get('cleanup_crypto',{}).get('gate_failures'):r['invalid_reasons'].append('Crypto coverage/gate failure')
    if r.get('cleanup_info',{}).get('spi_coverage_errors'):r['invalid_reasons'].append('SPI1 coverage loss')
    if any('malformed_stat' in x for x in r['closed_monitor']['errors']):r['invalid_reasons'].append('Raw stats JSON malformed')
    if r['closed_monitor']['animation_snapshots']==0 or any('display_liveness' in x or 'missing_rendered_counter' in x for x in r['closed_monitor']['errors']):r['invalid_reasons'].append('Render liveness/monitor coverage invalid')
    if mode==0 and (not h.get('sha_overlap_stripes') or not h.get('psram_bytes')):r['invalid_reasons'].append('No observed PSRAM SHA GDMA/LCD overlap')
    if mode in (1,2) and h.get('sha_overlap_stripes'):r['invalid_reasons'].append('Gated SHA API overlaps LCD DMA')
    if mode==3 and (h.get('sha_calls') or h.get('gdma_starts')):r['invalid_reasons'].append('Software candidate used hardware DMA')
    if mode==4 and h.get('psram_bytes'):r['invalid_reasons'].append('Internal candidate used PSRAM SHA descriptors')
    if r.get('cleanup_hash',{}).get('allocations_live') or r.get('cleanup_hash',{}).get('running'):r['invalid_reasons'].append('Hash task/buffers not released')
    r['rate_valid']=r['completed'] and not r['invalid_reasons'];r['clean']=r['rate_valid'] and r['closed_monitor']['passed']
    if h.get('elapsed_us'):
        r['hash_rates']={'verified_MB_per_s':h['hashed_bytes']/h['elapsed_us'],'hashes_per_s':h['hashes']*1e6/h['elapsed_us'],'hash_call_wall_fraction':h['hash_wall_us']/h['elapsed_us'],'basis':'decimalMB/s; call wall time includes preemption/gate waits/yields; not CPU utilization'}
    save(path/'result.json',r)
    lines=['# '+name+' — every DMA error context','','| Error | Frame/kind/stripe | Rect/bytes | DMA pre→post us | Phase/SPI1 masks | Hash/SHA API mask |','| --- | --- | --- | --- | --- | --- |']
    for e in r['error_contexts']:
        v=e['record'];x=obs.hash_errors.get(e['index'],{});lines.append(f"| {e['index']} | {v[8]}/{v[9]}/{v[10]} | {v[11]}/{v[12]} | {e['dma_pre_us']}→{e['dma_post_us']} | {v[13]}/{v[14]} | {x.get('mask','outside armed interval or missing')} |")
    (path/'error-contexts.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'cell':name,'completed':r['completed'],'rate_valid':r['rate_valid'],'clean':r['clean'],'exposure':r.get('exposure'),'hash_rates':r.get('hash_rates'),'invalid_reasons':r['invalid_reasons']}),flush=True);return r

def main():
    p=argparse.ArgumentParser();p.add_argument('--name',required=True);p.add_argument('--core',type=int,required=True);p.add_argument('--mode',type=int,required=True);a=p.parse_args()
    with asset_server():r=run(a.name,a.core,a.mode)
    return 0 if r['rate_valid'] else 2
if __name__=='__main__':raise SystemExit(main())

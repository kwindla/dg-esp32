#!/usr/bin/env python3
"""Predeclared display off/cat/full and fetch crossovers on one feature ELF."""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import time
import numpy as np
from esp_serial import EspSerial,ROOT
from step7_support import BotServer
from ethernet_bench import ethernet_bench
from netem import Netem
from quiet_nic import QuietNic,wait_for_quiet
from arrival_trace import drain
from transport_test import perf
from step8e2b_analysis import console_windows,device_send_cadence,owner_gate,lcd_memory_gate
from uplink_continuity import link_tail
from step9a_fetch_evidence import completion as fetch_completion
from step9a_assets import asset_server
from step9a_support import setup,read,request,terminal,host_address,FIXTURES


def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def read_perf(board):
    began=time.monotonic();sample=perf(board);sample['host_before_s']=began;sample['host_after_s']=time.monotonic();return sample


def prepare_5b(board):
    board.command('disp bench hold 30')
    images=json.loads((FIXTURES/'flash-fixtures.json').read_text());fillers=[image for image in images if image['name'].startswith('fill-') or image['name'].startswith('small-fill-')];small=[image for image in images if image['name'].startswith('flash-arm-')];large=json.loads((FIXTURES/'fetch-fixture.json').read_text());owned=[]
    # Each fresh boot starts from the same declared near-full state. These setup
    # transfers/writes stay outside the call and all scored windows.
    for index,image in enumerate(fillers+small+[large]):
        request(board,'delete',{'hash':image['hash']},'5b-pre-delete-'+str(index));assert terminal(board,'5b-pre-delete-'+str(index))['error']=='none'
    for index,image in enumerate(fillers):
        request(board,'load',{'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host_address()+':7874/'+image['hash']+'.dgi'},'5b-fill-'+str(index));done=terminal(board,'5b-fill-'+str(index));assert done['error']=='none'
        if done['persisted']:owned.append(image['hash'])
        else:request(board,'delete',{'hash':image['hash']},'5b-no-space-'+str(index));assert terminal(board,'5b-no-space-'+str(index))['error']=='none'
    filled=read(board,'status','DISPLAY');assert filled['flash']['free']<large['bytes'] and filled['flash']['max_admissible_bytes']>=small[0]['bytes']
    board.command('disp defer-persistence 1');board.command('disp call-persistence 1')
    for index,image in [(4,large)]+list(enumerate(small)):
        request(board,'load',{'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host_address()+':7874/'+image['hash']+'.dgi'},'5b-ram-'+str(index));done=terminal(board,'5b-ram-'+str(index));assert done['error']=='none' and not done['persisted'];board.command('disp store-asset '+str(index)+' '+image['hash'])
    return {'owned_fillers':owned,'prepared':small+[large],'near_full':filled['flash'],'basis':'five validated RAM-only objects before call; four16KiB write/sync/close/rename/delete pairs plus one300KiB near-full rejection; autonomous worker typed path,zero asset transfers during score'}


def cleanup_5b(board,setup):
    for index,hash in enumerate(setup.get('owned_fillers',[])+[image['hash'] for image in setup.get('prepared',[])]):
        request(board,'delete',{'hash':hash},'5b-cleanup-'+str(index));assert terminal(board,'5b-cleanup-'+str(index))['error']=='none'
    board.command('disp call-persistence 0');board.command('disp defer-persistence 0')


def arm(path,elf,mode,fetch=False,persistence=False,prepare_persistence=False,flash_preload=False,prepare_flash_preload=False):
    path.mkdir();wait_for_quiet(path/'quiet-preflight.json')
    values=dict(OFFLINE_SKIP_GREETING='1',RTP_EVIDENCE_DIR=str(path/'rtp'),RTP_EVIDENCE_MAX_PACKETS='100000',OFFLINE_IDLE_TIMEOUT_SECS='2100')
    previous={k:os.environ.get(k) for k in values};os.environ.update(values)
    result=dict(mode=mode,fetch=fetch,persistence=persistence,flash_preload=flash_preload,prepare_flash_preload=prepare_flash_preload,completed=False,strata={},elf_sha256=hashlib.sha256(elf.read_bytes()).hexdigest())
    try:
        from alsa import record_room_baseline
        result['room_baseline']=record_room_baseline(path/'room-baseline',path.name)
        if result['room_baseline']['noise_contaminated']:
            result['acoustic_hold']='noise';save(path/'result.json',result);raise RuntimeError('ACOUSTIC HOLD: noise')
        with asset_server(),Netem('192.168.4.29',['loss','0%'],'both',state=path/'tc-state.json',account_both=True),QuietNic(path/'quiet-nic.json'),ethernet_bench(path/'ethernet'),BotServer(path/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=path/'serial.log',display_checks=True) as board:
            board.display_terminal_events=True
            try:
                cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                identity=board.wait_for('elf_sha256=',3,since=cursor);assert result['elf_sha256'] in identity
                board.command('server-url '+os.environ['BOT_BENCH_SERVER_URL'])
                result['display_setup']=setup(board,full=True,mode=None)
                if fetch:
                    fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text());request(board,'delete',{'hash':fixture['hash']},'remove-fetch-fixture');assert terminal(board,'remove-fetch-fixture')['error']=='none'
                if prepare_flash_preload:
                    fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text())
                    board.command('disp bench hold 30')
                    request(board,'delete',{'hash':fixture['hash']},'preload-remove');assert terminal(board,'preload-remove')['error']=='none'
                    request(board,'load',{'hash':fixture['hash'],'bytes':fixture['bytes'],'url':'http://'+host_address()+':7874/'+fixture['hash']+'.dgi'},'preload-prime')
                    result['preload_prime']=terminal(board,'preload-prime');assert result['preload_prime']['error']=='none' and result['preload_prime']['persisted']
                    board.command('disp ram-evict '+fixture['hash'])
                if prepare_persistence:
                    result['persistence_setup']=prepare_5b(board);save(path/'result.json',result)
                for cmd in ('peer-option call-timing 1','peer-option arrival-trace 1','quiet 1','mic-fixture 1','gate open','connect','audio 1'):
                    board.command(cmd,90 if cmd=='connect' else 10)
                # Start the fixed animation before every timed window. No display
                # status, assets, scene commands or bench reissues inside windows.
                board.command('disp bench '+mode+' 30');time.sleep(2)
                board.command('play-long 1',30);began=time.monotonic();time.sleep(5)
                result['display_before']=read(board,'status','DISPLAY')
                if persistence:board.command('disp store-bench 1')
                for name in ('busy','quiet'):
                    if name=='quiet':time.sleep(max(0,began+65-time.monotonic()));board.command('gate closed')
                    snapshots=[read_perf(board)];drain(board,path/'arrivals.jsonl')
                    if fetch and name=='busy':
                        fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text());request(board,'fetch',{'seq':1000000,'hash':fixture['hash'],'bytes':fixture['bytes'],'url':'http://'+host_address()+':7874/'+fixture['hash']+'.dgi'},'in-call-fetch')
                    if flash_preload and name=='busy':
                        fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text());request(board,'preload',{'seq':1000000,'hash':fixture['hash']},'in-call-preload')
                    for _ in range(4):
                        deadline=time.monotonic()+10
                        while time.monotonic()<deadline:time.sleep(min(5,deadline-time.monotonic()));drain(board,path/'arrivals.jsonl')
                        snapshots.append(read_perf(board))
                    result['strata'][name]=dict(snapshots=snapshots);save(path/'result.json',result);print(path.name+' '+name+' captured',flush=True)
                result['display_after']=read(board,'status','DISPLAY');assert result['display_after']['dma']==[0,0] and not result['display_after']['failed']
                if fetch:
                    result['fetch_result']=fetch_completion((path/'serial.log').read_text())
                    result['fetch_throughput']={k:result['fetch_result'][k] for k in ('bytes','worker_duration_us','decimal_kB_per_s','basis')}
                if flash_preload:
                    result['preload_result']=terminal(board,'in-call-preload');assert result['preload_result']['error']=='none' and result['preload_result']['persisted']
                if persistence:
                    result['store_results']=read(board,'store-results','DISPLAY_STORE_RESULTS');assert result['store_results']['steps']==9
                    for index,row in enumerate(result['store_results']['items']):assert row[3]>0 and row[1]==('flash_full' if index==8 else 'none'),(index,row)
                board.command('wifi-stats',10);drain(board,path/'arrivals.jsonl');result['completed']=True
            finally:
                board.display_cleanup=True
                for cmd in ('disp store-bench 0','audio 0','gate auto','mic-fixture 0','quiet 1','disconnect'):
                    try:board.command(cmd,40)
                    except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
                result['cleanup_snapshots']=[]
                latest=board.latest_entry(r'^PERF ')
                if latest:
                    try:result['cleanup_snapshots'].append(json.loads(latest[2].split('PERF ',1)[1]))
                    except json.JSONDecodeError as exc:result.setdefault('cleanup_errors',[]).append('Malformed cleanup PERF: '+str(exc))
                if fetch or prepare_flash_preload:
                    # Keep cat rendering through the default post-session persist;
                    # wait for durable completion, then remove only this fixture.
                    try:
                        fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text())
                        deadline=time.monotonic()+60;next_snapshot=time.monotonic()+1
                        while time.monotonic()<deadline:
                            if time.monotonic()>=next_snapshot:
                                result['cleanup_snapshots'].append(read_perf(board));next_snapshot=time.monotonic()+1
                            events=read(board,'persisted','DISPLAY_PERSISTED')
                            if any(e[1]==fixture['hash'] and e[2]=='none' for e in events['events']):break
                            time.sleep(.1)
                        else:raise RuntimeError('Post-session persistence timeout')
                        request(board,'delete',{'hash':fixture['hash']},'r2-cleanup-fetch')
                        assert terminal(board,'r2-cleanup-fetch')['error']=='none'
                    except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
                if prepare_persistence:
                    try:cleanup_5b(board,result.get('persistence_setup',{}))
                    except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
                try:
                    time.sleep(.2);result['cleanup_snapshots'].append(read_perf(board))
                    result['cleanup_display']=read(board,'status','DISPLAY')
                    for sample in result['cleanup_snapshots']:
                        heap=sample['heap']['internal']
                        assert heap['minimum_free']>=81920 and 113624-heap['minimum_free']<=27108 and heap['largest']>=32768
                        assert sample['failed_allocations']['internal']==0
                    board.command('disp bench off')
                    deadline=time.monotonic()+2
                    while time.monotonic()<deadline:
                        result['cleanup_final']=read(board,'status','DISPLAY')
                        if result['cleanup_final']['dead']:break
                        if result['cleanup_final']['revisions'][0]==result['cleanup_final']['revisions'][2]:break
                        time.sleep(.1)
                    else:raise RuntimeError('Display failed to finish its final recycled frame')
                except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
                board.display_cleanup=False
                result['display_monitor']=board.display_monitor.result()
                board.display_monitor.check()
                if result.get('cleanup_errors'):raise RuntimeError('Cleanup failures: '+repr(result['cleanup_errors']))
        # The closed raw file includes all bytes received through cleanup.
        from step9a_r2_checks import audit
        result['raw_display_audit']=audit((path/'serial.log').read_bytes())
        trace=[json.loads(l) for l in (path/'arrivals.jsonl').read_text().splitlines()];packets=[json.loads(l) for l in (path/'rtp/rtp.jsonl').read_text().splitlines()];windows=console_windows(path/'serial.log')
        for name,data in result['strata'].items():
            first,last=data['snapshots'][0],data['snapshots'][-1];start,end=first['device_us'],last['device_us'];data['device_cadence']=device_send_cadence(trace,start,end,windows,snapshots=data['snapshots']);slow=[r for r in trace if r['kind']==8 and start<=r['device_us']<=end];calls=last['peer_profile']['loop_iteration']['calls']-first['peer_profile']['loop_iteration']['calls'];data['owner']=owner_gate(slow,calls,covered=last['peer_profile']['trace']['overwritten']==0,trace=trace,snapshots=data['snapshots']);rows=[r for r in packets if r['pc_id']==last['pc_id']];data['link']=link_tail(rows,first['host_after_s'],last['host_before_s']);data['counts']={k:last[k]-first[k] for k in ('late','lost','concealed','send_errors','queue_dropped','drift_drops','drift_inserts','latency_trim','rebuffers','playout_dropped','device_silence_blocks','discontinuity')};data['stack_minima']={k:min(t['stack_free_bytes'] for sample in data['snapshots'] for t in sample['tasks'] if t['name']==k) for k in {t['name'] for sample in data['snapshots'] for t in sample['tasks']}};data['memory']=lcd_memory_gate(data['snapshots'],reconnects=None)
            cpu=defaultdict(list)
            for snapshot in data['snapshots'][1:]:
                for task in snapshot['tasks']:
                    if task['cpu_percent'] is not None:cpu[task['name']].append(task['cpu_percent'])
            data['cpu']={k:float(np.median(v)) for k,v in cpu.items()}
            data['absolute_passed']=data['device_cadence']['target_passed'] and data['device_cadence']['hard_ceiling_passed'] and data['owner']['passed'] and data['link']['passed'] and data['memory']['checks']['session_window_internal'] and data['memory']['checks']['largest_internal'] and data['memory']['checks']['failed_internal_allocations']
        result['quiet_nic']=json.loads((path/'quiet-nic.json').read_text());result['absolute_passed']=all(d['absolute_passed'] for d in result['strata'].values()) and result['quiet_nic']['valid']
        before,after=result['display_before'],result['display_after'];first=result['strata']['busy']['snapshots'][0]['device_us'];last=result['strata']['quiet']['snapshots'][-1]['device_us'];result['display_rate']={'interval_s':(last-first)/1e6,'requested':after['updates'][0]-before['updates'][0],'accepted':after['updates'][1]-before['updates'][1],'rendered':after['updates'][2]-before['updates'][2],'coalesced':after['updates'][3]-before['updates'][3],'pixels':after['pixels']-before['pixels'],'deadline_misses':after['deadline_misses']-before['deadline_misses']};result['display_rate']['achieved_fps']=result['display_rate']['rendered']/result['display_rate']['interval_s']
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}';raise
    finally:
        save(path/'result.json',result)
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
    return result


def summarize(runs):
    aa=[r for r in runs if r['label'].startswith('aa-')];output={'arms':{},'absolute_passed':all(r.get('absolute_passed') for r in runs)}
    for comparison in ('cat','full','fetch','5b'):
        selected=[r for r in runs if r['label'].startswith(comparison+'-')];pairs=list(zip(selected[::2],selected[1::2]));strata={}
        for stratum in ('busy','quiet'):
            names=set.intersection(*(set(a['strata'][stratum]['cpu']) & set(b['strata'][stratum]['cpu']) for a,b in pairs))
            floor={k:abs(aa[1]['strata'][stratum]['cpu'].get(k,0)-aa[0]['strata'][stratum]['cpu'].get(k,0)) for k in names};tasks={}
            for k in names:
                deltas=[b['strata'][stratum]['cpu'][k]-a['strata'][stratum]['cpu'][k] for a,b in pairs];median=float(np.median(deltas));threshold=max(floor[k],5 if k.startswith('IDLE') else 2);tasks[k]={'paired_deltas_pp':deltas,'median_delta_pp':median,'aa_floor_pp':floor[k],'threshold_pp':threshold,'material':median<-threshold if k.startswith('IDLE') else median>threshold}
            strata[stratum]={'cpu':tasks,'windows_per_arm':len(pairs)*4}
        output['arms'][comparison]={'strata':strata,'achieved_fps':[r['display_rate']['achieved_fps'] for r in selected[1::2]]}
    return output


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');p.add_argument('--single',choices=('off','cat','full','fetch','5b'));a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    order=[('aa-A','off',False),('aa-B','off',False)]
    for mode in ('cat','full','fetch','5b'):
        for i in range(6):order.extend([(f'{mode}-A{i+1}','cat' if mode in ('fetch','5b') else 'off',False),(f'{mode}-B{i+1}','cat' if mode in ('fetch','5b') else mode,mode=='fetch')])
    save(a.out/'predeclared-rule.json',{'order':order,'elf_sha256':hashlib.sha256(a.elf.read_bytes()).hexdigest(),'windows':'four10-second busy and four10-second gated-quiet windows per fresh boot; full fixed pack; no display status inside window','full':'30fps saturating requests, frozen after throughput characterization; report achieved rate','materiality':'median paired CPU delta beyond max(off/offAA floor,2pp) for non-idle; IDLE decrease beyond max(AAfloor,5pp)','outage_rule':'keep every failure; stop on failed absolute audio/memory/quiet-NIC or acoustic guard; no retakes','storage':'verified full-flash backup; provisioned physical LittleFS;5b prepared RAM-only inputs,near-full flash,no network during scored persistence' })
    if a.single:
        row=arm(a.out/'single',a.elf.resolve(),'cat' if a.single in ('fetch','5b') else a.single,a.single=='fetch',a.single=='5b',a.single=='5b');save(a.out/'single-summary.json',row);return 0 if row['absolute_passed'] else 1
    runs=[]
    for label,mode,fetch in order:
        print('START '+label,flush=True);row=arm(a.out/label,a.elf.resolve(),mode,fetch,label.startswith('5b-B'),label.startswith('5b-'));row['label']=label;runs.append(row);save(a.out/'progress.json',{'completed':len(runs),'order':[r['label'] for r in runs]})
        if not row['absolute_passed']:save(a.out/'blocked.json',{'label':label,'reason':'absolute gates','retained':True});return 1
    save(a.out/'summary.json',summarize(runs));return 0

if __name__=='__main__':raise SystemExit(main())

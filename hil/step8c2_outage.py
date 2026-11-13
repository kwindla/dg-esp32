#!/usr/bin/env python3
"""Six alternating 15-minute probe-only flash-mode blocks; no voice stack."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import traceback
from esp_serial import EspSerial
from phase2_campaign import flash
from step8e0_campaign import probe_stats
from step8e0_analysis import wifi_counters
from step8c2_analysis import absent_intervals,outage_verdict
import netprobe

def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')

def block(directory,arm,image,wifi_host,ethernet_host,seconds=900,patched_archive=False):
    directory.mkdir(parents=True,exist_ok=False);result=dict(arm=arm,complete=False,image=str(image),
        elf_sha256=hashlib.sha256((image/'dg-esp32.elf').read_bytes()).hexdigest(),seconds=seconds,
        voice_stack=False,probe_pps=50,echo_pps=[0,5],wifi_sample_seconds=30)
    worker=None;stop=threading.Event();errors=[];samples=[];flashed=False
    try:
        flash(image,directory/'flash.log')
        flashed=True
        with EspSerial('/dev/ttyACM0',log_path=directory/'serial.log') as esp:
            cursor=esp.cursor;esp.reset();esp.wait_for('READY transport-console',60,since=cursor)
            identity=esp.wait_for('elf_sha256=',5,since=cursor)
            if result['elf_sha256'] not in identity:raise ValueError('Wrong installed image')
            esp.command('audio 0');esp.command('disconnect');esp.command('quiet 1');esp.command('netprobe stop')
            if not patched_archive:esp.command('wifi-log 1')
            def wifi(name):
                cursor=esp.cursor;esp.command('wifi-stats',timeout=15)
                (directory/name).write_text('\n'.join(r[2] for r in esp.lines_since(cursor))+'\n')
            wifi('wifi-tx-before.log');save(directory/'device-before.json',probe_stats(esp))
            def poll():
                deadline=time.monotonic()+30
                try:
                    while not stop.wait(max(0,deadline-time.monotonic())):
                        row=dict(host_ns=time.perf_counter_ns(),stats=probe_stats(esp))
                        samples.append(row);wifi(f'wifi-{len(samples):02}.log')
                        save(directory/'device-samples.json',samples)
                        print(directory.name+f': {len(samples)*30}/{seconds}s',flush=True)
                        deadline+=30
                except BaseException:errors.append(traceback.format_exc())
            def ready():
                nonlocal worker
                esp.command(f'netprobe start {wifi_host} 47100 50 130 2 {ethernet_host} 47101')
                result['host_started_ns']=time.perf_counter_ns()
                worker=threading.Thread(target=poll);worker.start()
            def finish():
                result['host_ended_ns']=time.perf_counter_ns();stop.set();worker.join(30)
                if worker.is_alive():raise RuntimeError('Serial poller did not finish')
                esp.command('netprobe stop')
            netprobe.run_streams([f'esp-0,{wifi_host},47100',f'esp-1,{ethernet_host},47101'],seconds,50,130,
                                directory/'host.jsonl',ready=ready,finish=finish)
            after=probe_stats(esp);save(directory/'device-after.json',after)
            wifi('wifi-tx-after.log')
            cursor=esp.cursor;esp.command('netprobe dump',timeout=100)
            text='\n'.join(r[2] for r in esp.lines_since(cursor));(directory/'probe-dump.log').write_text(text+'\n')
            device=netprobe.decode_dump(text)
            analysis=netprobe.analyze_streams(directory/'host.jsonl',device_rows=device,device_stats=after)
            save(directory/'analysis.json',analysis);save(directory/'wifi-tx-delta.json',wifi_counters(directory))
            if errors:raise RuntimeError('; '.join(errors))
            if after.get('udp_mailbox_untracked') or after.get('udp_mailbox_untracked_ports'):raise ValueError('Incomplete mailbox census')
            if not all(r['downlink']['capture_complete'] and r['uplink']['local_send_failure_capture_complete'] for r in analysis.values()):
                raise ValueError('Incomplete probe capture')
            # Use one-hop path 1 as the outage decision witness. Path 0 is
            # retained separately because it also crosses the host WiFi hop.
            host=[json.loads(l) for l in (directory/'host.jsonl').read_text().splitlines()]
            up=[r for r in host if r.get('kind')=='receive' and r.get('path')=='esp-1']
            down=[r for r in device if r['path']==1 and not r.get('send_errno')]
            path=after['paths'][1]
            gaps_up=absent_intervals(up,result['host_started_ns'],result['host_started_ns']+int(seconds*1e9),'arrival_ns')
            # Score the declared 900-second active window. Stopping serial
            # polling and transmitter tail grace must not create false outages.
            # Retain both endpoint absence and actual stop timestamps separately.
            gaps_down=absent_intervals(down,path['began_us']*1000,(path['began_us']+int(seconds*1e6))*1000,'arrival_ns') if down and 'arrival_ns' in down[0] else \
                absent_intervals([dict(arrival_ns=r['arrival_us']*1000) for r in down],path['began_us']*1000,(path['began_us']+int(seconds*1e6))*1000,'arrival_ns')
            result.update(complete=True,outages_ge_1s=len(gaps_up)+len(gaps_down),
                uplink_absence_ge_1s=gaps_up,downlink_absence_ge_1s=gaps_down,
                network=analysis,wifi=wifi_counters(directory),rssi=[r['stats'].get('wifi') for r in samples],
                observations='Socket-drain absence >=1 s; each direction counts separately; no RF-causality claim')
            if not patched_archive:esp.command('wifi-log 0')
    except BaseException:
        (directory/'FAILED.txt').write_text(traceback.format_exc());raise
    finally:
        stop.set()
        if worker:worker.join(30)
        if flashed:
            try:
                with EspSerial('/dev/ttyACM0',log_path=directory/'cleanup-serial.log') as esp:
                    esp.command('netprobe stop');esp.command('audio 0');esp.command('disconnect')
                    if not patched_archive:esp.command('wifi-log 0')
                result['cleanup_complete']=True
            except Exception:
                result['cleanup_complete']=False;result['complete']=False
                (directory/'cleanup-FAILED.txt').write_text(traceback.format_exc())
        save(directory/'result.json',result)
    serial=(directory/'serial.log').read_text(errors='replace')
    log_lines=[line for line in serial.splitlines() if re.match(r'^[IWE]\s*\(\d+\)\s*[^:]+:',line)]
    result['beacon_timeout_lines']=[line for line in log_lines if re.search(r'bcn_timeout(?!\s*:)|\bbeacon\s+timeout\b',line,re.I)]
    result['rx_error_lines']=[line for line in log_lines if re.search(r'rx.*error|rx.*fail',line,re.I)]
    result['disconnect_lines']=[line for line in log_lines if re.search(r'disconnect|reconnect|reason=',line,re.I)]
    result['wifi_disconnect_lines']=[line for line in log_lines if re.search(r'wifi.*(?:disconnect|reconnect|reason=)',line,re.I)]
    result['wifi_state_transition_lines']=[line for line in log_lines if re.search(r'wifi:state:',line,re.I)]
    raw_rx={}
    for name in ('before','after'):
        text=(directory/('wifi-tx-'+name+'.log')).read_text()
        match=re.search(r'wifi: HW RX:\s*\n[^\n]*rx_suc[^\n]*\n[^\n]*wifi: ([^\n]+)',text)
        raw_rx[name]=dict(zip(('rx_suc','rx_end','full#1','full#2'),match[1].split())) if match else None
    result['raw_hw_rx']=raw_rx
    result['rx_error_count']=None
    result['rx_error_counter_note']='No labeled RX error counter is exposed by this vendor dump; raw HW RX fields and RX error log lines are retained. Neither rx_end nor absence of error lines is treated as an error count.'
    save(directory/'result.json',result);return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--qio',type=Path,required=True);p.add_argument('--dio',type=Path,required=True)
    p.add_argument('--wifi-host',required=True);p.add_argument('--ethernet-host',required=True)
    p.add_argument('--patched-archive',action='store_true',help='Archive source has INFO WiFi logging and previous-level restoration')
    a=p.parse_args();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False)
    save(a.out/'predeclared-rule.json',dict(block_seconds=900,rotation=['qio','dio']*3,
        outage_ms=1000,rule='Any outage >=1 s on QIO with none on DIO rejects QIO; else requalify with full gates',
        voice_stack=False,wifi_stats_seconds=30,probe_pps=50,echo_pps=[0,5]))
    blocks=[]
    for i,arm in enumerate(['qio','dio']*3):
        blocks.append(block(a.out/f'{i+1:02}-{arm}',arm,getattr(a,arm).resolve(),a.wifi_host,a.ethernet_host,patched_archive=a.patched_archive))
        save(a.out/'result.json',dict(blocks=blocks,decision=outage_verdict(blocks)))

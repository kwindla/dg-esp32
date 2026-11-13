#!/usr/bin/env python3
"""State-machine HIL: >=50 cycles, channel-open timeout, server death, WiFi loss."""
import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from esp_serial import EspSerial,ROOT
from step7_support import BotServer
from transport_test import perf
from step8e2b_analysis import lcd_memory_gate

def run(a):
    if a.cycles<50:raise ValueError('At least 50 cycles required')
    a.out.mkdir(parents=True,exist_ok=False);result={'passed':False,'cycles':[],'memory_samples':[]}
    def save():(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        with BotServer(a.out/'bot',ice_servers=getattr(a,'ice_servers',())) as server,EspSerial(a.port,log_path=a.out/'serial.log') as board:
            time.sleep(.2);start=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=start)
            identity=board.wait_for(r'elf_sha256=([0-9a-f]{64})',3,since=start)
            assert re.search(r'elf_sha256=([0-9a-f]{64})',identity)[1]==hashlib.sha256((a.elf or ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
            if getattr(a,'server_url',None):board.command('server-url '+a.server_url)
            for option in getattr(a,'option',()):
                name,value=option.split('=');board.command(f'peer-option {name} {int(value)}')
            for cycle in range(1,a.cycles+1):
                cursor=board.cursor;began=time.monotonic();board.command('connect',90)
                ready=board.wait_for('STATE Ready',3,since=cursor)
                failures=[l for _,_,l in board.lines_since(cursor) if 'SESSION FAIL' in l]
                result['memory_samples'].append(perf(board))
                board.command('disconnect',40);closed=board.cursor;time.sleep(.1)
                assert not any('STATE Starting' in l for _,_,l in board.lines_since(closed)),'User disconnect retried'
                row=dict(cycle=cycle,elapsed_s=time.monotonic()-began,failures=failures,retry_outcome='ready' if failures else 'not needed',ready=ready)
                result['cycles'].append(row);save();print(f'cycle {cycle}: ready, failures={len(failures)}',flush=True)
            board.command('fault-channel-next');cursor=board.cursor;board.command('connect',90)
            failure=board.wait_for('SESSION FAIL.*Data-channel-open timeout',5,since=cursor)
            states=[l for _,_,l in board.lines_since(cursor) if 'STATE ' in l]
            assert sum('STATE Starting' in l for l in states)>=2
            result['channel_timeout']={'failure':failure,'states':states,'retry_outcome':'ready'};save()
            board.command('audio 1');time.sleep(.5);cursor=board.cursor;server.stop(kill=True)
            board.wait_for('STATE Disconnected',60,since=cursor);time.sleep(2.5)
            server.start();board.wait_for('STATE Ready',90,since=cursor)
            failures=[l for _,_,l in board.lines_since(cursor) if 'SESSION FAIL' in l]
            result['server_restart']={'failures':failures,'retry_outcome':'ready'};save()
            cursor=board.cursor;board.command('wifi-drop');board.wait_for('STATE WifiConnecting',10,since=cursor)
            board.wait_for('WIFI READY',50,since=cursor);board.wait_for('STATE Ready',90,since=cursor)
            result['wifi_rejoin']={'states':[l for _,_,l in board.lines_since(cursor) if 'STATE ' in l],
                'failures':[l for _,_,l in board.lines_since(cursor) if 'SESSION FAIL' in l],'retry_outcome':'ready'};save()
            board.command('audio 0');board.command('disconnect',40);cursor=board.cursor;time.sleep(2)
            assert not any('STATE Starting' in l for _,_,l in board.lines_since(cursor))
            text='\n'.join(l for _,_,l in board.lines_since(start))
            assert len(re.findall('ESP32-S3 step 7 audio elf_sha256=',text))==1,'Unexpected reboot'
            assert not re.search(r'Guru Meditation|abort\(\)|assert failed|Task watchdog',text),'Device fault'
            result['final_perf']=perf(board)
            result['memory_samples'].append(result['final_perf'])
            result['lcd_memory']=lcd_memory_gate(result['memory_samples'],reconnects=a.cycles-1)
            result['passed']=result['lcd_memory']['passed'] is True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:save()
    return 0 if result['passed'] else 1
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--cycles',type=int,default=50);p.add_argument('--port',default='/dev/ttyACM0')
    p.add_argument('--elf',type=Path,help='Verify a saved firmware image')
    p.add_argument('--server-url',help='RAM-only board signaling endpoint')
    p.add_argument('--ice-servers',nargs='+',default=[])
    p.add_argument('--option',action='append',default=[])
    raise SystemExit(run(p.parse_args()))

#!/usr/bin/env python3
"""Actual SCTP/RTVI -> app-control -> display queue -> HTTP/hash/store smoke."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
ROOT=Path(__file__).resolve().parents[1]
os.environ['STEP9A_FIXTURES']=str(ROOT/'hil/fixtures/echoear360')
from esp_serial import EspSerial
from step9a_echoear import bench,verify,flash,check_firmware
from step9a_assets import asset_server
from step9a_support import read,request,terminal,pack,host_address
from step9a_echoear_cat import perf

@contextmanager
def bot(out,wire,mode='offline-test',*,entrypoint='bot.py'):
    with socket.socket() as s:
        if s.connect_ex(('127.0.0.1',7861))==0:raise RuntimeError('Port7861 already owned')
    out.mkdir();env=dict(os.environ,BOT_MODE=mode,SERVER_PORT='7861',OFFLINE_SKIP_GREETING='1',EVENTS_PATH=str(out/'events.jsonl'),DISPLAY_HIL_COMMANDS=str(wire),PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',DISPLAY_RETRY_BUDGET='0')
    env.pop('DG_BENCH_LOCK_FD',None)
    with (out/'server.log').open('w') as log:
        process=subprocess.Popen([str(ROOT/'server/.venv/bin/python'),str(ROOT/'server'/entrypoint),'--host','0.0.0.0','--port','7861'],cwd=ROOT/'server',env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            end=time.monotonic()+60
            while True:
                if process.poll() is not None:raise RuntimeError('Owned bot exited; inspect server.log')
                try:
                    urllib.request.urlopen('http://127.0.0.1:7861/',timeout=.5);break
                except Exception:
                    if time.monotonic()>end:raise TimeoutError('Owned bot startup')
                    time.sleep(.2)
            yield
        finally:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--flash',action='store_true');p.add_argument('--budget',type=Path,required=True);p.add_argument('--wait-ms',type=int,choices=[0,5],default=5);p.add_argument('--messages',type=int,default=100);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False);result={'passed':False}
    if not 1<=a.messages<=200:p.error('--messages must be 1..200')
    os.environ['STEP9A_BUDGET_PATH']=str(a.budget.resolve())
    import step9a_support
    step9a_support.BUDGET_PATH=a.budget.resolve()
    wire=a.out/'request.json'
    try:
        with bench():
            if a.flash:flash(a.out)
            with bot(a.out/'bot',wire),asset_server(),EspSerial(verify(a.out),log_path=a.out/'serial.log',display_checks=True) as board:
                try:
                    at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);result['elf_sha256']=check_firmware(board,at);board.command('quiet 1');board.command('audio 0');board.command('server-url http://'+host_address()+':7861')
                    result['setup']=step9a_support.setup(board,mode='cat');board.command('disp defer-persistence 1');board.command('disp wire-wait '+str(a.wait_ms))
                    from step9a_merge_checks import census_gate
                    result['census']=census_gate([line for _,_,line in board.lines_since(at)])
                    initial=perf(board);board.command('connect',90)
                    base=pack()[0]
                    def send(op,body):
                        rid='wire-'+op;packet={'op':op,'data':body,'id':rid};at=board.cursor;temp=wire.with_suffix('.tmp');temp.write_text(json.dumps(packet));temp.replace(wire)
                        line=board.wait_for('DISPLAY_WIRE .*"id":"'+rid+'"',30,since=at);accepted=json.loads(line.split('DISPLAY_WIRE ',1)[1]);assert accepted['accepted'],accepted
                        result[op]={'packet':packet,'admission':accepted,'terminal':terminal(board,rid)};assert result[op]['terminal']['error']=='none'
                    send('load',{'hash':base['hash'],'bytes':base['bytes'],'url':'http://'+host_address()+':7874/'+base['hash']+'.dgi'})
                    send('show',{'hash':base['hash'],'layer':0,'x':0,'y':0})
                    result['layers']=read(board,'status layers','DISPLAY_LAYERS');assert result['layers']['layers'][0][1]==base['hash'] and result['layers']['layers'][0][-1]
                    from step9a_merge_checks import publish_command
                    from transport_test import wait_until
                    def events():
                        path=a.out/'bot/events.jsonl'
                        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
                    ready=wait_until(lambda:next((e for e in events() if e['event']=='display-ready'),None),3)
                    result['device_published_ready']=ready
                    result['admissions']=[]
                    for i in range(a.messages):
                        rid='cat-wire-'+str(i)
                        publish_command(wire,'show',dict(hash=pack()[1]['hash'],layer=7,x=i%8,y=0),rid)
                        sent=wait_until(lambda:next((e for e in events() if e['event']=='display-sent' and e['packet']['d']['id']==rid),None),3)
                        assert sent is not None, 'Missing display-sent for '+rid
                        ack=wait_until(lambda:next((e for e in events() if e['event']=='display-result' and e['result']['seq']==sent['packet']['d']['seq']),None),3)
                        assert ack is not None, 'Missing display-result for '+rid
                        assert not ack['result'].get('parse_failed'), ack
                        result['admissions'].append(ack['result'])
                        time.sleep(.02)
                    result['wire_status']=read(board,'wire-status','DISPLAY_WIRE_STATUS')
                    result['rejection_rate']=sum(not row['accepted'] for row in result['admissions'])/a.messages
                    result['wait_ms']=a.wait_ms
                    board.command('disp bench off')
                    result['connected']=perf(board)
                    board.command('disconnect',45)
                    request(board,'persist',{'hash':base['hash']},'wire-persist');result['persist']=terminal(board,'wire-persist');assert result['persist']['error']=='none'
                    board.command('disp ram-evict '+base['hash']);request(board,'preload',{'hash':base['hash']},'wire-preload');result['preload']=terminal(board,'wire-preload');assert result['preload']['error']=='none'
                    result['status']=read(board,'status','DISPLAY');result['hash']=read(board,'r4-hash-status','DISPLAY_R4_HASH');result['final']=perf(board);result['monitor']=board.display_monitor.result()
                    snapshots=[initial,result['connected'],result['final']]
                    result['memory']={'minimum_internal':min(s['heap']['internal']['minimum_free'] for s in snapshots),'largest_internal':min(s['heap']['internal']['largest'] for s in snapshots),'failed_internal_allocations':max(s['failed_allocations']['internal'] for s in snapshots)}
                    m=result['memory'];m['passed']=m['minimum_internal']>=81920 and m['largest_internal']>=32768 and m['failed_internal_allocations']==0
                    result['passed']=result['monitor']['passed'] and result['status']['dma']==[0,0] and m['passed'] and (a.wait_ms==0 or result['rejection_rate']==0) and result['monitor'].get('app_control_min_free',0)>=2048
                finally:
                    board.display_cleanup=True
                    for command in ('audio 0','disconnect'):
                        try:board.command(command,45)
                        except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
                    result['monitor']=board.display_monitor.result();result['passed']=result['passed'] and result['monitor']['passed'] and not result.get('cleanup_errors')
    finally:(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':result['passed'],'monitor':result.get('monitor')}));return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())

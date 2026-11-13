#!/usr/bin/env python3
"""EchoEar cat cadence and real content-addressed fetch/persist/evict/preload smoke."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
os.environ['STEP9A_FIXTURES']=str(ROOT/'hil/fixtures/echoear360')
from esp_serial import EspSerial
from step9a_echoear import bench,verify,flash,check_firmware
from step9a_assets import asset_server
from step9a_support import read,request,terminal,pack,host_address
from step9a_r2_checks import DisplayMonitor

def perf(board):
    at=board.cursor;board.command('stats');return json.loads(board.wait_for(r'PERF \{',5,since=at).split('PERF ',1)[1])

def erase_storage(out):
    factory=Path('/home/khkramer/src/evidence/echoear-factory/echoear-factory-16MB.bin')
    assert factory.stat().st_size==16*1024*1024
    assert hashlib.sha256(factory.read_bytes()).hexdigest()=='4083ad414517684ba8edf2f4a8f9840aa034ca2a7e9287e000dfe3b42afe0692'
    port=verify(out)
    with (out/'erase.log').open('w') as log:
        subprocess.run([sys.executable,'-m','esptool','--chip','esp32s3','--port',port,'erase-region','0x410000','0x400000'],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=90)
    # Firmware provision-empty independently checks every byte is FF before formatting.

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--flash',action='store_true');p.add_argument('--provision',action='store_true');a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False);result={'passed':False,'snapshots':[]}
    def save():(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        with bench():
            if a.flash:flash(a.out)
            if a.provision:erase_storage(a.out)
            with asset_server(),EspSerial(verify(a.out),log_path=a.out/'serial.log') as board:
                try:
                    at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);result['elf_sha256']=check_firmware(board,at);board.command('quiet 1');board.command('disp init',45)
                    if a.provision:board.command('disp provision-empty',90)
                    assert read(board,'status','DISPLAY')['flash']['health']=='ready'
                    board.display_monitor=DisplayMonitor()
                    board.command('disp defer-persistence 1')
                    before=perf(board);result['snapshots'].append(before);h=before['heap']['psram'];budget={'budget':491520,'F':h['free'],'W':1287760,'largest':h['largest']};result['budget']=budget
                    board.command('disp arena {budget} {F} {W} {largest}'.format(**budget));host=host_address();board.command('disp origin http://'+host+':7874');images=pack()
                    for i,item in enumerate(images):
                        rid='cat-load-'+str(i);request(board,'load',{'hash':item['hash'],'bytes':item['bytes'],'url':'http://'+host+':7874/'+item['hash']+'.dgi'},rid);assert terminal(board,rid)['error']=='none';board.command('disp asset '+str(i)+' '+item['hash'])
                    board.command('disp bench cat 30');time.sleep(1)
                    def window(name,seconds,work=None):
                        before=perf(board);start=time.monotonic()
                        if work:work()
                        while time.monotonic()-start<seconds:time.sleep(.1)
                        after=perf(board);result['snapshots'] += [before,after]
                        elapsed=(after['device_us']-before['device_us'])/1e6
                        result[name]={'seconds':elapsed,'frames':after['display']['rendered']-before['display']['rendered'],'fps':(after['display']['rendered']-before['display']['rendered'])/elapsed}
                        save()
                    window('idle',10)
                    noise=images[-1]
                    def persist():
                        begin=read(board,'r3-info','DISPLAY_R3_INFO')
                        request(board,'persist',{'hash':noise['hash']},'cat-persist');done=terminal(board,'cat-persist');result['persist']=done;assert done['error']=='none' and done['persisted']
                        end=read(board,'r3-info','DISPLAY_R3_INFO');elapsed=(end['device_us']-begin['device_us'])/1e6
                        result['persist_active']={'seconds':elapsed,'frames':end['rendered']-begin['rendered'],'fps':(end['rendered']-begin['rendered'])/elapsed,'basis':'device counters immediately bracketing persist admission through terminal result'}
                    window('persist_window',10,persist)
                    board.command('disp bench off');time.sleep(.3)
                    board.command('disp ram-evict '+noise['hash']);result['after_evict']=read(board,'status','DISPLAY')
                    request(board,'preload',{'hash':noise['hash']},'cat-preload');result['preload']=terminal(board,'cat-preload');assert result['preload']['error']=='none'
                    request(board,'delete',{'hash':noise['hash']},'cat-delete');result['delete']=terminal(board,'cat-delete');assert result['delete']['error']=='none'
                    result['status']=read(board,'status','DISPLAY');result['crypto']=read(board,'r4-crypto','DISPLAY_R4_CRYPTO');result['hash']=read(board,'r4-hash-status','DISPLAY_R4_HASH');result['monitor']=board.display_monitor.result()
                    result['memory']={'minimum_internal':min(x['heap']['internal']['minimum_free'] for x in result['snapshots']),'minimum_largest_internal':min(x['heap']['internal']['largest'] for x in result['snapshots']),'failed_internal_allocations':max(x['failed_allocations']['internal'] for x in result['snapshots'])}
                    m=result['memory'];result['memory']['passed']=m['minimum_internal']>=81920 and m['minimum_largest_internal']>=32768 and m['failed_internal_allocations']==0
                    result['passed']=result['monitor']['passed'] and result['idle']['fps']>=28 and result['persist_window']['fps']>=24 and result['persist_active']['fps']>=24 and result['memory']['passed'] and result['hash']['scratch_internal_dma'] and result['crypto']['tx_pri']==1 and result['crypto']['coverage_lost']==0 and result['status']['dma_y_even_if_row8'] is False
                    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','v4l2','-input_format','mjpeg','-video_size','1280x720','-i','/dev/video0','-frames:v','1','-y',str(a.out/'cat-camera.png')],capture_output=True,timeout=15)
                finally:
                    board.display_cleanup=True
                    for command in ('disp bench off','audio 0','disconnect'):
                        try:board.command(command,45)
                        except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
                    if board.display_monitor:
                        result['monitor']=board.display_monitor.result()
                        result['passed']=result['passed'] and result['monitor']['passed']
                    result['passed']=result['passed'] and not result.get('cleanup_errors')

    finally:save()
    print(json.dumps({k:result[k] for k in ('passed','idle','persist_window','persist_active','memory','monitor')}),flush=True)
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())

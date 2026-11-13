#!/usr/bin/env python3
"""RAM/fetch failures and real decoder/compositor throughput with physical storage."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from esp_serial import EspSerial
from step7_support import BotServer
from step9a_assets import asset_server
from step9a_support import setup,read,request,terminal,host_address,pack
from transport_test import perf
from step8e2b_analysis import lcd_memory_gate


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    result={'tests':{},'snapshots':[],'passed':False,'storage':'authorized provisioned LittleFS; board-side flash tests retained separately'}
    def save():(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    def checked(name,callback):
        try:value=callback();result['tests'][name]={'passed':True,'detail':value};save()
        except Exception as error:result['tests'][name]={'passed':False,'error':f'{type(error).__name__}: {error}'};save();raise
    with asset_server(),BotServer(a.out/'bot',port=7861),EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
      try:
        at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);board.command('quiet 1');board.command('server-url http://'+host_address()+':7861')
        result['setup']=setup(board,full=True);save();images=pack();base=images[0];url='http://'+host_address()+':7874/'+base['hash']+'.dgi'
        def bad_load(name,url,hash,bytes,error):
            before=read(board,'status','DISPLAY')['ram'];request(board,'fetch',{'hash':hash,'bytes':bytes,'url':url},name);done=terminal(board,name);assert done['error']==error,done;after=read(board,'status','DISPLAY')['ram'];assert after['reserved']==0 and after['used']<=before['used'];return {'terminal':done,'before':before,'after':after,'basis':'failed reservation is released; eviction of unpinned cache objects is permitted'}
        checked('wrong-hash',lambda:bad_load('wrong-hash',url,'0'*64,base['bytes'],'hash'))
        def oversize():
            before=read(board,'status','DISPLAY')['ram']
            try:request(board,'load',{'hash':'1'*64,'bytes':524289,'url':url},'oversize')
            except RuntimeError:pass
            else:raise AssertionError('oversize request admitted')
            after=read(board,'status','DISPLAY')['ram'];assert before==after;return after
        checked('oversize-before-allocation',oversize)
        checked('missing-content-length',lambda:bad_load('no-length','http://'+host_address()+':7874/failure/no-length','2'*64,12,'fetch'))
        checked('timeout',lambda:bad_load('timeout','http://'+host_address()+':7874/failure/timeout','3'*64,307200,'fetch'))
        for index in (0,1,7,13):
            request(board,'preload',{'hash':images[index]['hash']},'restore-needed-'+str(index));assert terminal(board,'restore-needed-'+str(index))['error']=='none'
        scene={'id':'scene-atomic','v':1,'epoch':result['setup']['status']['epoch'],'seq':1000,'ops':[{'op':'show','layer':0,'hash':base['hash'],'x':0,'y':0},{'op':'show','layer':1,'hash':images[1]['hash'],'x':168,'y':192},{'op':'show','layer':2,'hash':images[7]['hash'],'x':152,'y':176},{'op':'show','layer':3,'hash':images[13]['hash'],'x':92,'y':180}]}
        def atomic():
            before=read(board,'status','DISPLAY')['revisions'][0];invalid=dict(scene);invalid['id']='bad-batch';invalid['ops']=[dict(o) for o in scene['ops']];invalid['ops'][1]['hash']='4'*64
            try:request(board,'batch',invalid)
            except RuntimeError:pass
            else:raise AssertionError('invalid batch admitted')
            assert read(board,'status','DISPLAY')['revisions'][0]==before
            request(board,'batch',scene);done=terminal(board,'scene-atomic');assert done['error']=='none';after=read(board,'status','DISPLAY')['revisions'][0]
            replay=request(board,'batch',scene);assert replay['duplicate'];time.sleep(.1);assert read(board,'status','DISPLAY')['revisions'][0]==after
            reused=dict(scene);reused['ops']=[dict(o) for o in scene['ops']];reused['ops'][0]['x']=4
            try:request(board,'batch',reused)
            except RuntimeError:pass
            else:raise AssertionError('changed id reuse admitted')
            return done
        checked('atomic-batch-and-replay',atomic)
        def in_use():
            request(board,'delete',{'hash':base['hash']},'busy-delete');done=terminal(board,'busy-delete');assert done['error']=='in-use';return done
        checked('visible-pin-delete',in_use)
        def throughput():
            at=board.cursor;board.command('disp measure 30',90);lines=[row[2] for row in board.lines_since(at) if 'display-throughput:' in row[2]];assert len(lines)==4;assert all('bounce=0 dma_errors=0 ok=1' in row for row in lines);return lines
        checked('one-versus-two-buffer-throughput',throughput)
        result['te']=read(board,'te','DISPLAY_TE');save()
        for index in range(2):
            board.command('connect',90);result['snapshots'].append(perf(board));time.sleep(10);result['snapshots'].append(perf(board))
            if index==0:
                request(board,'fetch',{'hash':'5'*64,'bytes':307200,'url':'http://'+host_address()+':7874/failure/slow'},'mid-disconnect');time.sleep(.3)
            board.command('disconnect',45)
            if index==0:
                done=terminal(board,'mid-disconnect');assert done['error']=='cancelled';time.sleep(2.2);assert read(board,'status','DISPLAY')['ram']['reserved']==0;result['tests']['mid-transfer-disconnect']={'passed':True,'detail':done}
            save()
        result['memory']=lcd_memory_gate(result['snapshots'],reconnects=1);result['cost_bytes']=113624-result['memory']['measured']['internal']['minimum_free'];assert result['memory']['passed'] and result['cost_bytes']<=27108
        result['status']=read(board,'status','DISPLAY');assert result['status']['dma']==[0,0] and not result['status']['failed'];result['passed']=True;save()
      finally:
        for cmd in ('disp bench off','audio 0','disconnect'):
            try:board.command(cmd,45)
            except Exception as error:result.setdefault('cleanup_errors',[]).append(str(error))
        save()
    print(json.dumps({'passed':result['passed'],'cost':result.get('cost_bytes'),'memory':result.get('memory')}),flush=True)

if __name__=='__main__':main()

#!/usr/bin/env python3
"""Fresh, explicitly diagnostic cat/flash boots; no acoustic stimulus or calls."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from esp_serial import EspSerial, ROOT
from step9a_assets import asset_server
from step9a_support import setup, read, request, terminal, host_address, FIXTURES


def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def pages(board, action, prefix):
    page=read(board,action+' 0',prefix);rows=list(page['items']);start=page['start']+len(rows)
    while start<page['total']:
        page=read(board,action+' '+str(start),prefix)
        if page['start']!=start:raise RuntimeError('Diagnostic ring overwritten while reading')
        rows.extend(page['items']);start+=len(page['items'])
    return {'oldest':rows[0][0] if rows else 0,'total':page['total'],'items':rows}


def arm(out,elf,kind):
    out.mkdir(exist_ok=False);result={'diagnostic':True,'kind':kind,'completed':False,'elf_sha256':hashlib.sha256(elf.read_bytes()).hexdigest()}
    fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text())
    with EspSerial('/dev/ttyACM0',log_path=out/'serial.log',display_checks=True,display_diagnostic=True) as board:
        try:
            at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at)
            assert result['elf_sha256'] in board.wait_for('elf_sha256=',3,since=at)
            board.command('quiet 1');board.command('audio 0')
            result['setup']=setup(board,mode=None)
            if kind=='persist':
                request(board,'delete',{'hash':fixture['hash']},'diag-remove');assert terminal(board,'diag-remove')['error']=='none'
                board.command('disp defer-persistence 1')
                request(board,'load',{'hash':fixture['hash'],'bytes':fixture['bytes'],'url':'http://'+host_address()+':7874/'+fixture['hash']+'.dgi'},'diag-ram')
                assert terminal(board,'diag-ram')['error']=='none'
            elif kind=='preload':
                request(board,'preload',{'hash':fixture['hash']},'diag-prime')
                assert terminal(board,'diag-prime')['error']=='none'
                board.command('disp ram-evict '+fixture['hash'])
            board.command('disp bench cat 30');time.sleep(2)
            result['before']=read(board,'status','DISPLAY');result['flash_before']=read(board,'flash-stats','DISPLAY_FLASH_STATS')
            began=time.monotonic()
            if kind in ('persist','preload'):
                at=board.cursor
                request(board,kind,{'hash':fixture['hash']},'diag-op')
                event=board.wait_for(r'DISPLAY_WORK_RESULT .*"id":"diag-op"',90,since=at)
                result['terminal']=json.loads(event.split('DISPLAY_WORK_RESULT ',1)[1])
                result['duration_s']=time.monotonic()-began
                assert result['terminal']['error']=='none'
                if kind=='preload':
                    at=board.cursor;board.command('disp metadata-walk');board.wait_for('display-flash: metadata_walk_us=',30,since=at)
            else:time.sleep(15)
            time.sleep(2)
            result['after']=read(board,'status','DISPLAY');result['flash_after']=read(board,'flash-stats','DISPLAY_FLASH_STATS')
            board.command('disp bench off');time.sleep(.2)
            result['dma_trace']=pages(board,'dma-trace','DISPLAY_DMA_TRACE')
            result['flash_trace']=pages(board,'flash-trace','DISPLAY_FLASH_TRACE')
            result['monitor']=board.display_monitor.result();result['completed']=True
            save(out/'result.json',result)
        except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}';raise
        finally:
            board.display_cleanup=True
            for cmd in ('disp bench off','audio 0','disp defer-persistence 0','disconnect'):
                try:board.command(cmd,45)
                except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
            result['monitor']=board.display_monitor.result();save(out/'result.json',result)
    print(json.dumps({'kind':kind,'duration_s':result.get('duration_s'),'before':result.get('before',{}).get('updates'),'after':result.get('after',{}).get('updates'),'flash':result.get('flash_after')}),flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');a=p.parse_args();a.out.mkdir(exist_ok=False)
    save(a.out/'predeclared.json',{'diagnostic':True,'order':['persist','preload','baseline'],'gate':'absent','bytes':306917,'no_calls':True,'retakes':False})
    with asset_server():
        for kind in ('persist','preload','baseline'):arm(a.out/kind,a.elf,kind)

if __name__=='__main__':main()

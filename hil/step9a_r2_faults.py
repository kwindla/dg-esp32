#!/usr/bin/env python3
"""Real gate-bypass underflows and bounded console-only diagnostic fault boots."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from esp_serial import EspSerial, ROOT
from step9a_assets import asset_server
from step9a_support import setup, read, request, terminal, host_address, FIXTURES
from step9a_r2_diagnose import pages, save


def work(board,kind,fixture,rid):
    at=board.cursor
    request(board,kind,{'hash':fixture['hash']},rid)
    line=board.wait_for('DISPLAY_WORK_RESULT .*"id":"'+rid+'"',90,since=at)
    return json.loads(line.split('DISPLAY_WORK_RESULT ',1)[1])


def prepare_ram(board,fixture):
    assert work(board,'delete',fixture,'fault-remove')['error']=='none'
    board.command('disp defer-persistence 1')
    request(board,'load',{'hash':fixture['hash'],'bytes':fixture['bytes'],'url':'http://'+host_address()+':7874/'+fixture['hash']+'.dgi'},'fault-ram')
    assert terminal(board,'fault-ram')['error']=='none'


def arm(out,elf,fault):
    out.mkdir();r={'diagnostic':True,'fault':fault,'passed':False,'elf_sha256':hashlib.sha256(elf.read_bytes()).hexdigest()}
    fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text())
    with EspSerial('/dev/ttyACM0',log_path=out/'serial.log',display_checks=True,display_diagnostic=True) as board:
        try:
            at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at)
            assert r['elf_sha256'] in board.wait_for('elf_sha256=',3,since=at)
            board.command('quiet 1');board.command('audio 0');r['setup']=setup(board)
            board.command('disp r2 diagnostic 1')
            if fault in ('bypass','writer-timeout'):prepare_ram(board,fixture)
            r['before']=read(board,'status','DISPLAY')
            if fault=='bypass':
                board.command('disp r2 gate-bypass 1');board.command('disp bench cat 30');time.sleep(2)
                r['attempts']=[]
                for index in range(5):
                    r['attempts'].append(work(board,'persist',fixture,'fault-persist-'+str(index)))
                    time.sleep(2)
                    if read(board,'status','DISPLAY')['dma'][1]>0:break
                    board.command('disp bench off');prepare_ram(board,fixture);board.command('disp bench cat 30');time.sleep(2)
                r['after']=read(board,'status','DISPLAY')
                assert r['after']['dma'][1]>0,'No real underflow reproduced; no software fail flags injected'
                assert r['after']['display_recovery_attempts']>0 and r['after']['display_recoveries']>0 and not r['after']['dead']
            elif fault=='writer-timeout':
                board.command('disp r2-fault writer-timeout');r['terminal']=work(board,'persist',fixture,'writer-timeout-op')
                time.sleep(1);r['after']=read(board,'status','DISPLAY')
                assert r['terminal']['error']!='none' and r['after']['gate_timeouts']>=1 and not r['after']['dead']
            else:
                board.command('disp r2-fault '+fault);board.command('disp bench cat 30');time.sleep(3.5)
                r['after']=read(board,'status','DISPLAY')
                if fault=='prequeue':
                    assert r['after']['send_failures']==1 and r['after']['display_recovery_attempts']==1 and r['after']['display_recoveries']==1 and not r['after']['dead']
                else:
                    assert r['after']['dead'] and r['after']['failed']
                    if fault=='display-timeout':assert r['after']['display_gate_timeouts']==1
                    if fault=='late':assert r['after']['late_callbacks']==1
                    before=r['after']['updates'][2];time.sleep(1);assert read(board,'status','DISPLAY')['updates'][2]==before
            board.command('disp bench off');time.sleep(.1)
            r['dma_trace']=pages(board,'dma-trace','DISPLAY_DMA_TRACE');r['frames']=pages(board,'frame-trace','DISPLAY_FRAME_TRACE')
            r['flash_stats']=read(board,'flash-stats','DISPLAY_FLASH_STATS')
            r['gate_stats']=[read(board,'gate-stats '+str(op),'DISPLAY_GATE_STATS') for op in range(5)]
            r['passed']=True
        except Exception as exc:r['error']=str(exc);raise
        finally:
            board.display_cleanup=True
            for cmd in ('disp bench off','disp r2 gate-bypass 0','audio 0','disconnect'):
                try:board.command(cmd,45)
                except Exception as exc:r.setdefault('cleanup_errors',[]).append(str(exc))
            # Lost completion retains the display token until reboot. Do not run
            # filesystem cleanup through that token; next boot owns any cleanup.
            r['monitor']=board.display_monitor.result();save(out/'result.json',r)
    print(json.dumps({'fault':fault,'passed':r['passed'],'after':{k:r['after'][k] for k in ('dma','dead','display_recovery_attempts','display_recoveries','gate_timeouts','display_gate_timeouts','send_failures','late_callbacks')}}),flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');a=p.parse_args();a.out.mkdir(exist_ok=False)
    order=['bypass','prequeue','lost','late','writer-timeout','display-timeout']
    save(a.out/'predeclared.json',{'diagnostic':True,'order':order,'retakes':False,'bypass_max_real_persists':5,'terminal_policy':'dead until reboot; no DMA buffer reuse'})
    with asset_server():
        for fault in order:arm(a.out/fault,a.elf,fault)

if __name__=='__main__':main()

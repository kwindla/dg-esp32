#!/usr/bin/env python3
"""Freeze achieved rates before the scored campaign, with the production scene path."""
import argparse,json,time,hashlib
from pathlib import Path
from esp_serial import EspSerial,ROOT
from step9a_assets import asset_server
from step9a_support import setup,read
from transport_test import perf

def main():
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--seconds',type=int,default=60);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
 result={'elf_sha256':hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest(),'request_fps':30,'duration_s':a.seconds,'arms':{},'basis':'full alternates according to accepted/rendered scene; no status or console inside each interval'}
 with asset_server(),EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
  at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);board.command('quiet 1');setup(board,full=True)
  for mode in ('cat','full'):
   board.command('disp bench '+mode+' 30');time.sleep(2);before=read(board,'status','DISPLAY');start=time.monotonic();time.sleep(a.seconds);end=time.monotonic();after=read(board,'status','DISPLAY');board.command('disp bench off')
   d={'host_interval_s':end-start,'before':before,'after':after,'requested':after['updates'][0]-before['updates'][0],'rendered':after['updates'][2]-before['updates'][2],'coalesced':after['updates'][3]-before['updates'][3]};d['achieved_fps']=d['rendered']/d['host_interval_s'];result['arms'][mode]=d
   assert after['dma']==[0,0] and not after['failed'];(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
  board.command('audio 0');board.command('disconnect',45)
 print(json.dumps({k:v['achieved_fps'] for k,v in result['arms'].items()}),flush=True)
if __name__=='__main__':main()

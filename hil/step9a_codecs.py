#!/usr/bin/env python3
"""Board composition costs for both DGI codecs using identical base pixels."""
import argparse,json
from pathlib import Path
from esp_serial import EspSerial
from step9a_assets import asset_server
from step9a_support import read,request,terminal,host_address,FIXTURES,BUDGET_PATH

def main():
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False);result={'passed':False,'basis':'production decoder plus blend/stripe composition; pure decoder full/dirty microseconds are separately retained in codec-benchmark.json; identical faceplate pixels for codecs1 and2','arms':{}}
 with asset_server(),EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
  at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);board.command('quiet 1');board.command('disp init',30);board.command('disp origin http://'+host_address()+':7874');b=json.loads(BUDGET_PATH.read_text());board.command('disp arena {budget} {F} {W} {largest}'.format(**b));rows=json.loads((FIXTURES/'codec-benchmark.json').read_text())['rows'];manifest=json.loads(Path('/home/khkramer/src/dg-esp32/assets/characters/halloween-cat/animation.json').read_text())
  for codec in (1,2):
   image=next(r for r in rows if r['source']==manifest['base'] and r['codec']==codec);rid='codec-load-'+str(codec);request(board,'load',{'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host_address()+':7874/'+image['hash']+'.dgi'},rid);assert terminal(board,rid)['error']=='none';rid='codec-show-'+str(codec);request(board,'batch',{'ops':[{'op':'fill','color':0},{'op':'show','layer':0,'hash':image['hash'],'x':0,'y':0}]},rid);assert terminal(board,rid)['error']=='none';at=board.cursor;board.command('disp measure 30',90);lines=[r[2] for r in board.lines_since(at) if 'display-throughput:' in r[2]];assert len(lines)==4 and all('bounce=0 dma_errors=0 ok=1' in row for row in lines);result['arms'][str(codec)]={'image':image,'measurements':lines};(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
  rle=next(r for r in rows if r['source']==manifest['base'] and r['codec']==1);request(board,'delete',{'hash':rle['hash']},'codec-cleanup-rle');assert terminal(board,'codec-cleanup-rle')['error']=='none';result['status']=read(board,'status','DISPLAY');board.command('audio 0');board.command('disconnect',45);result['passed']=True
 (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'passed':result['passed']}),flush=True)
if __name__=='__main__':main()

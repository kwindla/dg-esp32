#!/usr/bin/env python3
"""Unscored functional preparation/autonomous-IO check; no call or acoustic stimulus."""
import argparse,json,time
from pathlib import Path
from esp_serial import EspSerial
from step9a_assets import asset_server
from step9a_support import setup,read
from step9a_campaign import prepare_5b,cleanup_5b


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False);result={'passed':False}
    with asset_server(),EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
      try:
        at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);board.command('quiet 1');result['setup']=setup(board,full=True);result['prepare']=prepare_5b(board);board.command('disp store-bench 1');time.sleep(42);results=read(board,'store-results','DISPLAY_STORE_RESULTS');assert results['steps']==9
        for index,row in enumerate(results['items']):assert row[3]>0 and row[1]==('flash_full' if index==8 else 'none'),(index,row)
        result['results']=results;result['passed']=True
      finally:
        for command in ('disp store-bench 0','disp bench off','audio 0','disconnect'):
            try:board.command(command,45)
            except Exception as error:result.setdefault('cleanup_errors',[]).append(str(error))
        if 'prepare' in result:cleanup_5b(board,result['prepare'])
        (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':result['passed'],'results':result.get('results')}),flush=True)

if __name__=='__main__':main()

#!/usr/bin/env python3
"""Fresh feature census with the authorized, provisioned physical LittleFS tier."""
import argparse
import hashlib
import json
import socket
import time
from pathlib import Path
from esp_serial import EspSerial
from step9a_assets import asset_server
from step7_support import BotServer
from transport_test import perf
from step8e2b_analysis import lcd_memory_gate


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);parser.add_argument('--arena',type=int,default=0);parser.add_argument('--dwell',type=int,default=60);parser.add_argument('--F',type=int,default=0);parser.add_argument('--W',type=int,default=65536);parser.add_argument('--largest',type=int,default=0);args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.connect(('192.168.4.29',1234));host=sock.getsockname()[0];sock.close()
    result={'elf_sha256':hashlib.sha256(Path('/home/khkramer/src/dg-esp32/build/dg-esp32.elf').read_bytes()).hexdigest(),'arena':args.arena,'storage':'authorized physical LittleFS; mounted without automatic format','snapshots':[]}
    (args.out/'plan.json').write_text(json.dumps({'basis':'fresh boot; init full display/store/HTTP fixed allocation; two Ready sessions with ten-second snapshots and reconnect; audio disabled','comparison_baseline_min':113624,'internal_ceiling':27108},indent=2)+'\n')
    try:
        with asset_server(),BotServer(args.out/'bot',port=7861),EspSerial('/dev/ttyACM0',log_path=args.out/'serial.log',display_checks=True) as board:
            at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at)
            board.command('quiet 1');board.command('server-url http://'+host+':7861');board.command('disp init',30)
            board.command('disp origin http://'+host+':7874');at=board.cursor
            fixture=json.loads(Path('/home/khkramer/src/dg-esp32/hil/fixtures/step9a/fetch-fixture.json').read_text())
            board.command('disp probe http://'+host+':7874/'+fixture['hash']+'.dgi');board.wait_for('probe_complete ok=1 bytes=306917',45,since=at)
            if args.arena:board.command(f'disp arena {args.arena} {args.F} {args.W} {args.largest}')
            board.command('disp status')
            for _ in range(2):
                board.command('connect',90);result['snapshots'].append(perf(board))
                for _ in range(max(1,args.dwell//10)):
                    time.sleep(10);result['snapshots'].append(perf(board))
                board.command('disconnect',45)
            result['F']=min(row['heap']['psram'].get('window_session_minimum_free',row['heap']['psram']['window_minimum_free']) for row in result['snapshots']);result['largest_psram']=min(row['heap']['psram']['largest'] for row in result['snapshots'])
            result['memory']=lcd_memory_gate(result['snapshots'],reconnects=1)
            board.command('audio 0');board.command('disp status')
    finally:(args.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result.get('memory',{}),indent=2),flush=True)

if __name__=='__main__':main()

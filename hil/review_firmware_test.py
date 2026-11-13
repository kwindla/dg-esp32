#!/usr/bin/env python3
"""Small hardware regressions for bench state, lazy trace and request errors."""
import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from esp_serial import EspSerial,ROOT
from step7_support import BotServer
from transport_test import perf

def run(a):
    a.out.mkdir(parents=True,exist_ok=False)
    result=dict(passed=False,checks={})
    try:
        with BotServer(a.out/'bot') as bot,EspSerial(a.port,log_path=a.out/'serial.log') as board:
            start=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=start)
            line=board.wait_for('elf_sha256=',3,since=start)
            assert re.search(r'elf_sha256=([0-9a-f]{64})',line)[1]==hashlib.sha256(a.elf.read_bytes()).hexdigest()
            board.command('quiet 0');board.command('silence-bench',90);cursor=board.cursor;time.sleep(.4)
            reports=[line for _,_,line in board.lines_since(cursor) if line.startswith('MIC ')]
            result['checks']['quiet_restored']=bool(reports)
            result['checks']['only_native_mic_channel']=bool(reports) and all('rmsL=' not in line and 'rmsR=' in line for line in reports)
            board.command('quiet 1');board.command('silence-bench',90);cursor=board.cursor;time.sleep(.4)
            result['checks']['quiet_true_restored']=not any(line.startswith('MIC ') for _,_,line in board.lines_since(cursor))
            board.command('connect',90);stats=perf(board);result['before_perf']=stats
            result['checks']['unpinned_core_label']=any(t['core']=='any' for t in stats['tasks'])
            trace=stats['peer_profile']['trace']
            result['checks']['trace_storage_initially_unallocated']=trace.get('allocated') is False
            try:
                board.command('play-fixture nonexistent.wav',10)
                result['checks']['fixture_error_reported']=False
            except RuntimeError as exc:
                result['checks']['fixture_error_reported']='ERR #' in str(exc)
            board.command('disconnect',40)
            before=perf(board)['heap']['psram']['free']
            board.command('peer-option arrival-trace 1')
            after=perf(board)['heap']['psram']['free']
            result['trace_enable_allocation_bytes']=before-after
            # Concurrent heap frees can move a net heap delta by a few bytes.
            # Verify the actual ring state and recorded media after enabling,
            # retaining the net delta as evidence rather than an allocation size.
            board.command('connect',90)
            trace=perf(board)['peer_profile']['trace']
            result['enabled_trace']=trace
            result['checks']['trace_storage_allocated_on_enable']=trace.get('allocated') is True and trace['written']>0
            board.command('disconnect',40)
            board.command('peer-option arrival-trace 0')
            result['passed']=all(result['checks'].values())
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ['before_perf']},indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True,type=Path);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');p.add_argument('--port',default='/dev/ttyACM0')
    raise SystemExit(run(p.parse_args()))

#!/usr/bin/env python3
"""Separate diagnostic boots: missing completion stays fatal; gate delay recovers."""
import argparse
import json
import os
from pathlib import Path
import time
ROOT=Path(__file__).resolve().parents[1]
os.environ['STEP9A_FIXTURES']=str(ROOT/'hil/fixtures/echoear360')
from esp_serial import EspSerial
from step9a_echoear import bench,verify,flash,check_firmware
from step9a_assets import asset_server
from step9a_support import setup,read
from step9a_echoear_cat import perf


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--budget',type=Path,required=True);p.add_argument('--fault',choices=['lost','display-timeout'],required=True);p.add_argument('--flash',action='store_true');a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    import step9a_support
    step9a_support.BUDGET_PATH=a.budget.resolve()
    r=dict(passed=False,diagnostic=True,fault=a.fault)
    try:
        with bench():
            if a.flash:flash(a.out)
            with asset_server(),EspSerial(verify(a.out),log_path=a.out/'serial.log',display_checks=True,display_diagnostic=True) as b:
                try:
                    at=b.cursor;b.reset();b.wait_for('READY transport-console',60,since=at);r['elf_sha256']=check_firmware(b,at)
                    b.command('quiet 1');b.command('audio 0');r['setup']=setup(b)
                    b.command('disp r2 diagnostic 1');b.command('disp r2-fault '+a.fault);b.command('disp bench cat 30')
                    time.sleep(3.5);r['after']=perf(b);time.sleep(1);r['later']=perf(b)
                    first,last=r['after']['display'],r['later']['display']
                    if a.fault=='lost':
                        r['passed']=last['dead'] and last['failed'] and last['terminal_reason'] in (1,2) and last['rendered']==first['rendered']
                    else:
                        r['passed']=not last['dead'] and not last['failed'] and last['terminal_reason']==0 and last['scheduler_delays']>=1 and last['display_gate_timeouts']==1 and last['rendered']>first['rendered'] and last['display_recoveries']>=1
                    r['passed']=r['passed'] and last['dma_errors']==0 and not b.display_monitor.raw_matches
                finally:
                    b.display_cleanup=True
                    for cmd in ('disp bench off','audio 0','disconnect'):
                        try:b.command(cmd,45)
                        except Exception as e:r.setdefault('cleanup_errors',[]).append(str(e))
                    r['monitor']=b.display_monitor.result();r['passed']=r['passed'] and not r.get('cleanup_errors')
    except Exception as e:r['error']=str(e)
    finally:(a.out/'result.json').write_text(json.dumps(r,indent=2)+'\n')
    print(json.dumps({k:r.get(k) for k in ('passed','fault','error')}),flush=True)
    return 0 if r['passed'] else 1
if __name__=='__main__':raise SystemExit(main())

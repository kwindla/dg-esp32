#!/usr/bin/env python3
"""One acquisition per required 8f2 arm; stop the campaign on acoustic noise."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from esp_serial import ROOT
from ethernet_bench import ethernet_bench
from netem import Netem
from quiet_nic import QuietNic,wait_for_quiet


def interfaces():
    result={}
    for dev in Path('/sys/class/net').iterdir():
        result[dev.name]={k:int((dev/'statistics'/k).read_text()) for k in ('tx_bytes','rx_bytes')}
    return dict(monotonic_s=time.monotonic(),interfaces=result)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    results={}
    def execute(name,args):
        print('START '+name,flush=True)
        before=interfaces()
        with (out/(name+'-command.log')).open('w') as stream:
            code=subprocess.run([sys.executable,*map(str,args)],cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT).returncode
        after=interfaces()
        delta={dev:{k:after['interfaces'][dev][k]-row[k] for k in row} for dev,row in before['interfaces'].items() if dev in after['interfaces']}
        results[name]=dict(exit_code=code,interfaces_before=before,interfaces_after=after,interface_deltas=delta)
        (out/'progress.json').write_text(json.dumps(results,indent=2)+'\n')
        print('COMPLETE '+name+' exit='+str(code),flush=True)
        log=(out/(name+'-command.log')).read_text()
        if 'ACOUSTIC HOLD: noise' in log:
            print('ACOUSTIC HOLD: noise',flush=True)
            return False
        return True
    if not execute('quiet0',[ROOT/'hil/step8f_regression.py','--out',out/'quiet0','--elf',ROOT/'build/dg-esp32.elf']):return 2
    folder=out/'offline';folder.mkdir();wait_for_quiet(folder/'quiet-preflight.json')
    with Netem('192.168.4.29',['loss','0%'],'both',state=folder/'tc-state.json'),QuietNic(folder/'quiet-nic.json'),ethernet_bench(folder/'ethernet'):
        if not execute('offline',[ROOT/'hil/e2e_offline_test.py','--out',folder/'test','--runs','5','--warm','--no-retakes',
            '--elf',ROOT/'build/dg-esp32.elf','--server-url',os.environ['BOT_BENCH_SERVER_URL'],'--ice-servers',os.environ['BOT_BENCH_STUN_URL'],
            '--option','call-timing=1','--option','arrival-trace=1']):return 2
    if not execute('voice-soak',[ROOT/'hil/step8e_soak.py','--out',out/'voice-soak','--minutes','20','--forced-reconnect']):return 2
    (out/'result.json').write_text(json.dumps(dict(completed=True,runs=results),indent=2)+'\n')
    return 0 if all(r['exit_code']==0 for r in results.values()) else 1


if __name__=='__main__':raise SystemExit(main())

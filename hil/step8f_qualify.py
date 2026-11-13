#!/usr/bin/env python3
"""All final-image gates in one retained campaign, without erasing failures."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from esp_serial import ROOT
from ethernet_bench import ethernet_bench
from quiet_nic import QuietNic,wait_for_quiet
from netem import Netem
from step8e2b_analysis import lcd_memory_gate,perf_history


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--image',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);elf=a.image.resolve()/'dg-esp32.elf';results={}
    def command(name,args):
        print('START '+name,flush=True)
        with (out/(name+'-command.log')).open('w') as log:
            code=subprocess.run([sys.executable,*map(str,args)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT).returncode
        results[name]=dict(exit_code=code);(out/'progress.json').write_text(json.dumps(results,indent=2)+'\n')
        print('COMPLETE '+name+' exit='+str(code),flush=True)
    command('quiet0',[ROOT/'hil/step8f_regression.py','--out',out/'quiet0','--elf',elf])
    for name,script,extra in [('offline','e2e_offline_test.py',['--runs','5','--warm']),('reconnect','app_reconnect_test.py',['--cycles','50'])]:
        folder=out/name;folder.mkdir();wait_for_quiet(folder/'quiet-preflight.json')
        with Netem('192.168.4.29',['loss','0%'],'both',state=folder/'tc-state.json',account_both=True),QuietNic(folder/'quiet-nic.json'),ethernet_bench(folder/'ethernet'):
            command(name,[ROOT/'hil'/script,'--out',folder/'test','--elf',elf,
                '--server-url',os.environ['BOT_BENCH_SERVER_URL'],'--ice-servers',os.environ['BOT_BENCH_STUN_URL'],*extra])
        serial=folder/'test/serial.log'
        if serial.exists():
            results[name]['lcd_memory']=lcd_memory_gate(perf_history(serial),reconnects=49 if name=='reconnect' else None)
    command('overflow',[ROOT/'hil/step8e_burst.py','--out',out/'overflow','--elf',elf,'--sustained-seconds','240','--capture-seconds','300'])
    command('voice-soak',[ROOT/'hil/step8e_soak.py','--out',out/'voice-soak','--minutes','20','--forced-reconnect'])
    (out/'result.json').write_text(json.dumps(dict(completed=True,runs=results),indent=2)+'\n')


if __name__=='__main__':main()

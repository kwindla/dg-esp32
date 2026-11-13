"""No builds in measurement windows: saved step-8 image then candidate image."""
import argparse
import json
import os
import subprocess
from pathlib import Path
from esp_serial import ROOT,port_lock
from alsa import resolve_jabra,set_volumes
from step8_summary import profile

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);a=p.parse_args()
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False)
    images=[('old',a.baseline.resolve()),('new',ROOT/'build')]
    rows={}
    for label,directory in images:
        with port_lock(),(a.out/f'flash-{label}.log').open('w') as log:
            cmd=['bash','-c','. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null && python -m esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 write-flash 0x10000 "$1"','flash',str(directory/'dg-esp32.bin')]
            subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True)
        card,_=resolve_jabra();set_volumes(card,a.out/f'mixer-{label}.log')
        with (a.out/f'{label}.log').open('w') as log:
            subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/perf_test.py'),'--out',str(a.out/label),'--elf',str(directory/'dg-esp32.elf'),'--seconds','30','--no-trace'],stdout=log,stderr=subprocess.STDOUT,check=True)
        rows[label]=profile(a.out/label/'result.json')
        print(f'{label} completed',flush=True)
    summary={}
    for old,new in zip(rows['old'],rows['new']):
        def cpu(row):return next(t['cpu_percent'] for t in row['tasks'] if t['name']=='audio-playback')
        x,y=cpu(old),cpu(new);summary[old['name']]=dict(old_cpu_percent=x,new_cpu_percent=y,ratio=y/x,within_ten_percent=y<=x*1.1)
    (a.out/'comparison.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary))
if __name__=='__main__':main()

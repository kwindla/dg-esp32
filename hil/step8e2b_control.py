#!/usr/bin/env python3
"""Quiet-NIC-qualified ABAB control attribution; immutable production images."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from esp_serial import ROOT
from netem import Netem
from phase2_campaign import flash
from quiet_nic import QuietNic, wait_for_quiet


def run(out):
    out.mkdir(parents=True, exist_ok=False)
    images={'A':ROOT.parent/'evidence/step8c3/images/step8c3-xip',
            'B':ROOT.parent/'evidence/step8e2b/images/8e2a2'}
    rule=dict(order=[f'{arm}{n}' for n in range(1,5) for arm in 'AB'],
        duration_s=300,preflight_window_s=30,maximum_busy_wait_s=600,
        non_board_egress_limit_bytes_per_300s=20_000_000,
        voice_path='wifi',wifi_probes=True,wifi_stats=True,
        firmware_changed=False,images={arm:dict(path=str(p),
            elf_sha256=hashlib.sha256((p/'dg-esp32.elf').read_bytes()).hexdigest())
            for arm,p in images.items()})
    (out/'predeclared-rule.json').write_text(json.dumps(rule,indent=2)+'\n')
    # Preserve the exact evidence-only capture instrumentation used by the
    # preliminary cohort; no firmware/configuration changes between arms.
    capture=ROOT.parent/'evidence/step8e2b/control_capture.py'
    (out/'capture-source.py').write_bytes(capture.read_bytes())
    completed=[]
    try:
        for label in rule['order']:
            image=images[label[0]]
            print('PREFLIGHT '+label,flush=True)
            wait_for_quiet(out/(label+'-quiet-preflight.json'))
            flash(image,out/(label+'-flash.log'))
            print('START '+label,flush=True)
            # Owned board-flow counters provide byte attribution, with zero
            # imposed impairment. No route/interface/process changes.
            with Netem('192.168.4.29',['loss','0%'],'downlink',state=out/(label+'-tc-state.json')):
                with QuietNic(out/(label+'-quiet-nic.json'),seconds=300):
                    with (out/(label+'-capture.log')).open('w') as log:
                        subprocess.run([sys.executable,str(out/'capture-source.py'),
                            '--out',str(out/label),'--elf',str(image/'dg-esp32.elf'),
                            '--wifi-host','192.168.7.222','--ethernet-host','192.168.42.75',
                            '--voice-path','wifi','--duration','300','--cpu-window','30',
                            '--fixed-mic','--gate','open','--option','call-timing=1','--label',label],
                            stdout=log,stderr=subprocess.STDOUT,check=True,cwd=ROOT)
            completed.append(label)
            (out/'progress.json').write_text(json.dumps(dict(completed=completed),indent=2)+'\n')
            print('COMPLETE '+label,flush=True)
    finally:
        flash(images['B'],out/'restore-8e2a2-flash.log')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True,type=Path)
    run(parser.parse_args().out.resolve())

#!/usr/bin/env python3
"""The three five-minute session-fault arms on one archived production image."""
import argparse
import json
import os
from pathlib import Path
from phase2_campaign import flash
from step8e_matrix import arm


def run(out,image):
    out.mkdir(parents=True,exist_ok=False);results=[]
    flash(image,out/'flash.log')
    for name,args,direction,outage in [
        ('delay-300-jitter-60-both',['delay','300ms','60ms','distribution','normal'],'both',None),
        ('outage-10s-both',['loss','0%'],'both',10),
        ('outage-30s-uplink',['loss','0%'],'uplink',30)]:
        print('START '+name,flush=True)
        previous=os.environ.get('FLOW_EVIDENCE_DIR')
        if outage==30:os.environ['FLOW_EVIDENCE_DIR']=str(out/name/'flow')
        try:r=arm(out/name,image/'dg-esp32.elf',name,args,direction,outage,'192.168.4.29',300)
        finally:
            if previous is None:os.environ.pop('FLOW_EVIDENCE_DIR',None)
            else:os.environ['FLOW_EVIDENCE_DIR']=previous
        results.append({k:r.get(k) for k in ('name','completed','passed','error','session_survived','reconnections','audio_sample_intelligible','post_fault_ready_host_s')})
        (out/'summary.json').write_text(json.dumps(results,indent=2)+'\n')
        print('COMPLETE '+name+' '+json.dumps(results[-1]),flush=True)
        if r.get('error'):return 1
    return 0


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--image',type=Path,required=True)
    a=p.parse_args();raise SystemExit(run(a.out.resolve(),a.image.resolve()))

#!/usr/bin/env python3
"""User-approved receive-path matrix; no uplink, outage or baseline reruns."""
import argparse
import json
from pathlib import Path
from step8e_matrix import arm,e2e_arm,write

CELLS=(('control',('loss','0%')),
       ('loss-1pct-downlink',('loss','1%')),
       ('loss-3pct-downlink',('loss','3%')),
       ('loss-5pct-downlink',('loss','5%')),
       ('bursty-2pct-downlink',('loss','gemodel','0.2%','10%','100%','0%')),
       ('reorder-5pct-downlink',('delay','20ms','reorder','5%','50%')),
       ('delay-50-jitter-10-downlink',('delay','50ms','10ms','distribution','normal')))


def run(out,elf,board_ip):
    rows=[];calibration=None
    for name,args in CELLS:
        target=out/'matrix-final'/name;voice=out/'voice-final'/name
        print('START '+name,flush=True)
        if (target/'result.json').exists():
            result=json.loads((target/'result.json').read_text())
            if not result.get('completed'):raise RuntimeError('Incomplete evidence retained; use another output root: '+name)
        else:result=arm(target,elf,name,list(args),'downlink',None,board_ip,seconds=300)
        if not result.get('completed'):raise RuntimeError('Capture incomplete: '+name)
        if (voice/'result.json').exists():v=json.loads((voice/'result.json').read_text())
        else:v=e2e_arm(voice,elf,list(args),'downlink',board_ip,calibration=calibration)
        if calibration is None:calibration=voice/'voice'
        rows.append(dict(name=name,capture_completed=result['completed'],capture_error=result.get('error'),
                         strict_voice=v.get('summary'),voice_error=v.get('error')))
        write(out/'reduced-campaign.json',dict(completed=False,arms=rows))
        print('VOICE '+name+': '+str(v.get('summary',{}).get('passes'))+' '+str(v.get('error')),flush=True)
    write(out/'reduced-campaign.json',dict(completed=True,arms=rows))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--elf',type=Path,required=True);p.add_argument('--board-ip',default='192.168.4.29')
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True);run(a.out.resolve(),a.elf.resolve(),a.board_ip)

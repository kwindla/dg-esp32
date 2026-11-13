#!/usr/bin/env python3
"""Interleaved voice-policy comparison, with contemporaneous 8d controls.

All child runs and failed turns are retained. Only fixture/SPL calibration is
shared within a level (native pre-gain PCM); every child boots the same image,
opens a fresh context and fits an independent unscored acoustic response.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_conversation_test import summarize, write_json
from esp_serial import ROOT

ARMS = {
    'A+gain': dict(policy='smartturn', gain=1600, min_volume=.6, stop_secs=3),
    'A': dict(policy='smartturn', gain=200, min_volume=.6, stop_secs=3),
    'C': dict(policy='timeout', gain=200, min_volume=.4),
    'D': dict(policy='8d', gain=200, min_volume=.4),
}
PASS_RULE = ('Exactly one complete correct audible response per fixture turn; all complete TTS segments '
             'must pass acoustic matching and content. A cancelled partial below 300 ms audible is '
             'tolerated and counted separately. Unknown audible duration fails. Transport counter '
             'damage is retained and classified separately, never removed from raw pass rates.')


def run(out,elf=ROOT/'build/dg-esp32.elf',resume=False):
    out = out.resolve(); out.mkdir(parents=True, exist_ok=resume)
    result = json.loads((out/'result.json').read_text()) if resume else dict(arms=ARMS, mute='AlwaysUserMuteStrategy in every arm', pass_rule=PASS_RULE, runs=[], completed=False)
    assert result['arms']==ARMS and result.get('mute')=='AlwaysUserMuteStrategy in every arm','Resume requires the same arms and speaking-state mute'
    if result.get('error'):result.setdefault('retained_errors',[]).append(result.pop('error'))
    write_json(out/'result.json', result)
    for spl, rounds in ((65,5), (58,2)):
        calibration = None
        for index in range(rounds):
            names = list(ARMS)
            names = names[index % 4:]+names[:index % 4]
            for name in names:
                prior=next((r for r in result['runs'] if r['arm']==name and r['spl']==spl and r['round']==index+1 and len(r['observation'].get('turns',[]))>=6),None)
                if prior:
                    if calibration is None:calibration=Path(prior['path'])
                    continue
                arm = ARMS[name]
                directory = out/f'{spl}-{index+1}-{name}'
                attempt=2
                while directory.exists():
                    directory=out/f'{spl}-{index+1}-{name}-attempt{attempt}';attempt+=1
                env = {**os.environ, 'BOT_VOICE_POLICY': arm['policy'],
                       'BOT_VAD_MIN_VOLUME': str(arm['min_volume']),
                       'BOT_SMARTTURN_STOP_SECS':str(arm.get('stop_secs',3))}
                args = [sys.executable, str(ROOT/'hil/agent_conversation_test.py'),
                        '--out',str(directory),'--runs','1','--spl',str(spl),
                        '--elf',str(elf.resolve()),
                        '--mic-gain',str(arm['gain'])]
                if calibration: args.extend(['--calibration-from',str(calibration)])
                if spl == 65 and index == rounds-1: args.append('--pause-case')
                print('Starting',name,spl,index+1,flush=True)
                with directory.with_suffix('.log').open('w') as log:
                    child = subprocess.run(args,env=env,stdout=log,stderr=subprocess.STDOUT)
                observation = json.loads((directory/'result.json').read_text())
                result['runs'].append(dict(arm=name,spl=spl,round=index+1,path=str(directory),
                                          exit_code=child.returncode,observation=observation))
                if not observation.get('turns'):
                    result['error'] = observation.get('error','Child collected no turns')
                    write_json(out/'result.json',result)
                    return 2
                if calibration is None: calibration = directory
                result['summaries'] = {}
                for arm_name in ARMS:
                    result['summaries'][arm_name] = {}
                    for level in (65,58):
                        turns = [t for r in result['runs'] if r['arm']==arm_name and r['spl']==level
                                 for t in r['observation']['turns'] if t['prompt'] != 'pause']
                        result['summaries'][arm_name][str(level)] = summarize(turns)
                write_json(out/'result.json',result)
                print('Finished',name,spl,index+1,observation['summary']['passes'],
                      '/',observation['summary']['turns'],flush=True)
    result['completed'] = True
    write_json(out/'result.json',result)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf')
    parser.add_argument('--resume',action='store_true',help='Retain prior complete turns and failed calibration attempts')
    args=parser.parse_args()
    raise SystemExit(run(args.out,args.elf,args.resume))

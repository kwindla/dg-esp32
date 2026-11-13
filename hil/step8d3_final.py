#!/usr/bin/env python3
"""Final default-policy qualification; retain every original child result.

The latest user addendum withdrew automated SmartTurn selection and stopped
pause collection. No further pause or alternate-bound arms run here.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from agent_conversation_test import write_json
from ethernet_bench import ethernet_bench
from esp_serial import ROOT


def run(out,elf):
    out.mkdir(parents=True,exist_ok=False)
    result=dict(jobs=[],completed=False,smartturn_stop_secs=3,
                policy_basis='Pipecat defaults; user decision, no automated selection')
    write_json(out/'result.json',result)
    default_keys=('BOT_SMARTTURN_STOP_SECS','BOT_VAD_MIN_VOLUME','BOT_VOICE_POLICY')
    previous={key:os.environ.pop(key,None) for key in default_keys}
    try:
        with ethernet_bench(out/'ethernet-stun'):
            from step8d3_loud import run as run_loud
            loud=run_loud(out/'loud75-80',elf)
            result['jobs'].append(dict(name='loud75-80',measurement_only=True,
                passed=loud['completed'],turns=len(loud['turns']),error=loud.get('error')))
            write_json(out/'result.json',result)
            if loud.get('error') or not loud['completed']:
                result['error']='Incomplete loud measurement';return result
            for mode in ('offline-test','live-test'):
                name='silence120-'+mode
                args=[sys.executable,str(ROOT/'hil/agent_silence_test.py'),'--out',str(out/name),
                    '--elf',str(elf),'--mode',mode]
                with (out/(name+'.log')).open('w') as stream:
                    child=subprocess.run(args,stdout=stream,stderr=subprocess.STDOUT)
                silence=json.loads((out/name/'result.json').read_text())
                result['jobs'].append(dict(name=name,exit_code=child.returncode,
                    passed=silence['passed'],seconds=silence.get('seconds'),error=silence.get('error')))
                write_json(out/'result.json',result)
                if silence.get('error'):
                    result['error']='Incomplete silence measurement: '+name;return result
            for name,options,expected in [('nominal10',['--runs','2','--turn-limit','10'],10),
                    ('quiet5',['--spl','58','--turn-limit','5'],5),
                    ('soak20',['--soak-minutes','20','--fault-free'],60)]:
                args=[sys.executable,str(ROOT/'hil/agent_conversation_test.py'),'--out',str(out/name),'--elf',str(elf)]+options
                with (out/(name+'.log')).open('w') as stream:
                    child=subprocess.run(args,stdout=stream,stderr=subprocess.STDOUT)
                raw=json.loads((out/name/'result.json').read_text())
                result['jobs'].append(dict(name=name,exit_code=child.returncode,passed=raw['passed'],
                    turns=len(raw['turns']),expected_turns=expected,passes=raw['summary']['passes'],error=raw.get('error')))
                write_json(out/'result.json',result)
                if len(raw['turns'])!=expected:
                    result['error']='Incomplete qualification child: '+name
                    write_json(out/'result.json',result);return result
        result['completed']=True
        result['passed']=all(j['passed'] for j in result['jobs'])
        return result
    finally:
        for key,value in previous.items():
            if value is not None:os.environ[key]=value
        write_json(out/'result.json',result)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,required=True)
    a=p.parse_args();result=run(a.out.resolve(),a.elf.resolve())
    raise SystemExit(2 if result.get('error') else 0 if result.get('passed') else 1)

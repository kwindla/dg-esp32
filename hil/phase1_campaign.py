#!/usr/bin/env python3
"""Local bench phase-1 runner. Never commits, changes port 7860, or exposes local config."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from esp_serial import ROOT, EspSerial
from phase1_summary import summarize

EVIDENCE=ROOT/'proj-2026-10-08-0949/evidence/step6a'
PYTHON=ROOT/'hil/.venv/bin/python'

def run(argv,log):
    with log.open('w') as stream:
        proc=subprocess.run([str(x) for x in argv],cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT)
    return proc.returncode

def configure(settings,out):
    out.mkdir(parents=True,exist_ok=True)
    base=(EVIDENCE/'defaults-instrumented.txt').read_text()
    values=dict(line.split('=',1) for line in base.splitlines() if line.startswith('CONFIG_'))
    values.update(settings)
    # Keep baseline comments, replace existing entries, append new settings.
    lines=[];seen=set()
    for line in base.splitlines():
        if line.startswith('CONFIG_'):
            key=line.split('=',1)[0];seen.add(key)
            if values[key] is None: continue
            line=f'{key}={values[key]}'
        lines.append(line)
    lines+=['', '# Measured phase 1 configuration.']+[f'{k}={v}' for k,v in values.items() if k not in seen and v is not None]
    (ROOT/'sdkconfig.defaults').write_text('\n'.join(lines)+'\n')
    (out/'settings.json').write_text(json.dumps(settings,indent=2)+'\n')
    (out/'defaults.txt').write_text((ROOT/'sdkconfig.defaults').read_text())
    if run([ROOT/'scripts/idf.sh','fullclean'],out/'clean.log'): raise RuntimeError('fullclean failed')
    (ROOT/'sdkconfig').unlink(missing_ok=True)
    if run([ROOT/'scripts/idf.sh','reconfigure'],out/'configure.log'): raise RuntimeError('reconfigure failed')
    if run([sys.executable,ROOT/'scripts/check-sdkconfig.py'],out/'config-check.log'): raise RuntimeError('Exact config check failed')
    if run([PYTHON,ROOT/'hil/flash.py','--out',out/'flash'],out/'flash-helper.log'): raise RuntimeError('Build/flash failed')
    (out/'map.txt').write_bytes((ROOT/'build/dg-esp32.map').read_bytes())
    # Resolved diff only includes non-local defaults; credentials are never copied.
    from importlib.machinery import SourceFileLoader
    checker=SourceFileLoader('sdkchecker',str(ROOT/'scripts/check-sdkconfig.py')).load_module()
    actual=checker.settings(ROOT/'sdkconfig'); expected=checker.settings(ROOT/'sdkconfig.defaults')
    (out/'resolved-defaults.json').write_text(json.dumps({k:actual.get(k) for k in expected},indent=2)+'\n')
    (out/'elf-sha256.txt').write_text(hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()+'\n')

def campaign(out,boots,extras=False,elf=None):
    for index in range(1,boots+1):
        code=run([PYTHON,ROOT/'hil/transport_test.py','--campaign','--sessions','3','--out',out/f'boot{index}']+(['--elf',elf] if elf else []),out/f'boot{index}.log')
        print(f'{out.name} boot {index}: exit={code}',flush=True)
        if len(list((out/f'boot{index}').glob('[0-9]*/result.json')))!=3:
            raise RuntimeError('Incomplete media boot; inspect retained evidence')
    code=run([PYTHON,ROOT/'hil/console_throughput_test.py','--out',out/'console'],out/'console.log')
    print(f'{out.name} console: exit={code}',flush=True)
    if extras:
        for index in range(1,3):
            code=run([PYTHON,ROOT/'hil/greeting_test.py','--out',out/f'greeting{index}'],out/f'greeting{index}.log')
            print(f'{out.name} greeting {index}: exit={code}',flush=True)
        code=run([PYTHON,ROOT/'hil/audio_loop_test.py','--firmware','app','--out',out/'loop'],out/'loop.log')
        print(f'{out.name} audio loop: exit={code}',flush=True)
        for line in (out/'loop.log').read_text().splitlines():
            if line.startswith('Evidence: '):
                import shutil
                shutil.copytree(Path(line.split('Evidence: ',1)[1]),out/'loop',dirs_exist_ok=True)
    summary=summarize(out);(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary

def boot_only(out):
    with EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
        time.sleep(.2); cursor=board.cursor;board.reset();board.wait_for('READY transport-console',40,since=cursor)
        board.command('ping');board.command('stats')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('name');p.add_argument('--settings',default='{}');p.add_argument('--boots',type=int,default=1);p.add_argument('--extras',action='store_true');p.add_argument('--boot-only',action='store_true');a=p.parse_args()
    out=EVIDENCE/a.name
    if out.exists(): raise SystemExit('Refusing to overwrite an existing measurement directory')
    configure(json.loads(a.settings),out)
    if a.boot_only: boot_only(out)
    else: campaign(out,a.boots,a.extras)

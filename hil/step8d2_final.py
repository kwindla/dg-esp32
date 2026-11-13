#!/usr/bin/env python3
"""Retained final-image qualification, fault-free growth soak and input checks."""
import argparse,json,os,subprocess,sys
from pathlib import Path
from esp_serial import ROOT
from agent_conversation_test import write_json


def run(out, campaign, elf=None,resume=False):
    out=out.resolve();out.mkdir(parents=True,exist_ok=resume);campaign=campaign.resolve()
    saved=out.parent/'final-image/dg-esp32.elf'
    elf=(elf or (saved if saved.exists() else ROOT/'build/dg-esp32.elf')).resolve()
    env={**os.environ,'BOT_VOICE_POLICY':'smartturn','BOT_VAD_MIN_VOLUME':'0.6','BOT_SMARTTURN_STOP_SECS':'1'}
    # Comparison overrides must not alter the pre-existing other-mode floor.
    ordinary={k:v for k,v in env.items() if k not in {'BOT_VAD_MIN_VOLUME','BOT_VOICE_POLICY','BOT_SMARTTURN_STOP_SECS'}}
    comparison=json.loads((campaign/'result.json').read_text())
    def calibration(level):
        return Path(next(r['path'] for r in comparison['runs'] if r['spl']==level and r['arm']=='A+gain' and r['observation'].get('turns')))
    common=['--calibration-from',str(calibration(65))]
    quiet=['--calibration-from',str(calibration(58))]
    jobs=[('qualification','agent_conversation_test.py',['--runs','10','--pause-case']+common,env),
          ('quiet','agent_conversation_test.py',['--runs','2','--spl','58','--pause-case']+quiet,env),
          ('overlap','agent_conversation_test.py',['--interruptions']+common,env),
          ('soak20','agent_conversation_test.py',['--soak-minutes','20','--fault-free']+common,env),
          ('offline65','e2e_offline_test.py',['--runs','5','--spl','65'],ordinary),
          ('live65','e2e_voice_test.py',['--runs','5','--spl','65'],ordinary),
          ('offline58','e2e_offline_test.py',['--runs','5','--spl','58'],ordinary),
          ('live58','e2e_voice_test.py',['--runs','5','--spl','58'],ordinary),
          ('silence120','agent_silence_test.py',[],env)]
    value=json.loads((out/'result.json').read_text()) if resume else dict(policy='smartturn',gain_percent=1600,stop_secs=1,min_volume=.6,
        mute='AlwaysUserMuteStrategy',jobs=[],completed=False)
    write_json(out/'result.json',value)
    for name,script,args,child_env in jobs:
        if any(j['name']==name for j in value['jobs']):continue
        path=out/name
        if path.exists():
            raw=json.loads((path/'result.json').read_text())
            expected={'qualification':61,'quiet':13,'overlap':2,'soak20':60}.get(name)
            assert resume and not raw.get('error') and expected is not None and len(raw.get('turns',[]))==expected,'Existing incomplete child must be retained and diagnosed before resuming'
            value['jobs'].append(dict(name=name,exit_code=None,path=str(path),passed=raw.get('passed',False),resumed_existing_observation=True))
            write_json(out/'result.json',value);print('Retained completed',name,flush=True);continue
        print('Starting',name,flush=True)
        with (out/(name+'.log')).open('w') as log:
            result=subprocess.run([sys.executable,str(ROOT/'hil'/script),'--out',str(path),
                '--elf',str(elf)]+args,
                env=child_env,stdout=log,stderr=subprocess.STDOUT)
        raw=json.loads((path/'result.json').read_text())
        value['jobs'].append(dict(name=name,exit_code=result.returncode,path=str(path),passed=raw.get('passed',False)))
        write_json(out/'result.json',value)
        print('Finished',name,'exit',result.returncode,flush=True)
        if raw.get('error') and not raw.get('turns') and name in {'qualification','quiet','soak20'}:
            value['error']=raw['error'];write_json(out/'result.json',value);return 2
    value['completed']=True;value['passed']=all(j['passed'] for j in value['jobs'])
    write_json(out/'result.json',value)
    return 0 if value['passed'] else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--campaign',type=Path,required=True);p.add_argument('--elf',type=Path)
    p.add_argument('--resume',action='store_true');a=p.parse_args()
    raise SystemExit(run(a.out,a.campaign,a.elf,getattr(a,'resume',False)))

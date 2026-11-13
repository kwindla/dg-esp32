#!/usr/bin/env python3
"""One diagnostic off/full pair; XIP-off attribution does not replace production."""
import argparse
import hashlib
import json
from pathlib import Path
from step9a_campaign import arm,save
from step9a_continue_controls import allowed_control


def main():
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
 save(a.out/'plan.json',{'arms':['off','full'],'fresh_boots':2,'elf_sha256':hashlib.sha256(a.elf.read_bytes()).hexdigest(),'purpose':'single XIP-off attribution pair after material full arm; original ISR placement,staging,40MHz,assets,30fps saturating requests and audio gates unchanged','filesystem':'disabled in diagnostic image; never perform cache-disabled filesystem IO on PSRAM worker','windows':'same four10-second busy/four10-second quiet windows per boot as retained campaign','retakes':False,'adoption':'XIP remains enabled in production; restore frozen image after diagnostic'})
 rows=[]
 try:
  for name,mode in (('off','off'),('full','full')):
   print('START diagnostic '+name,flush=True);row=arm(a.out/name,a.elf.resolve(),mode);rows.append(row)
   if not row['absolute_passed']:
    if allowed_control(row):print('RETAIN FAILED OFF DIAGNOSTIC CONTROL',flush=True)
    else:save(a.out/'blocked.json',{'reason':'diagnostic absolute gates','arm':name});return 1
 except Exception as error:
  save(a.out/'blocked.json',{'reason':str(error)})
  if str(error).startswith('ACOUSTIC HOLD'):print(str(error),flush=True);return 2
  raise
 summary={'absolute_passed':all(r['absolute_passed'] for r in rows),'retained_failed_controls':[name for name,row in zip(('off','full'),rows) if not row['absolute_passed']],'ruling_accepted':all(r['absolute_passed'] or allowed_control(r) for r in rows),'achieved_fps':rows[1]['display_rate']['achieved_fps'],'strata':{}}
 for s in ('busy','quiet'):
  x,y=[r['strata'][s]['cpu'] for r in rows];summary['strata'][s]={'cpu_delta_pp':{k:y[k]-x[k] for k in x.keys()&y.keys()},'off_device_cadence':rows[0]['strata'][s]['device_cadence'],'full_device_cadence':rows[1]['strata'][s]['device_cadence'],'basis':'one diagnostic pair; attribution only, no six-pair materiality claim'}
 save(a.out/'summary.json',summary);print(json.dumps({'absolute_passed':summary['absolute_passed'],'fps':summary['achieved_fps']}),flush=True);return 0
if __name__=='__main__':raise SystemExit(main())

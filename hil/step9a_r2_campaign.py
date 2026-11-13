#!/usr/bin/env python3
"""One frozen R2 ELF; fresh AA/fetch/5b/preload/cat spot-check pairs, strict stops."""
import argparse,hashlib,json
from pathlib import Path
from step9a_campaign import arm,save
from step9a_continue_controls import comparison
from step9a_r2_checks import audit
from esp_serial import ROOT


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');a=p.parse_args();a.out.mkdir(exist_ok=False)
    order=[('aa-A','off',False,False,False,False,False),('aa-B','off',False,False,False,False,False)]
    for mode in ('fetch','5b'):
        for i in range(1,7):
            for side in ('A','B'):order.append((f'{mode}-{side}{i}','cat',mode=='fetch' and side=='B',mode=='5b' and side=='B',mode=='5b',False,False))
    order.extend([('preload-A1','cat',False,False,False,False,True),('preload-B1','cat',False,False,False,True,True)])
    for i in range(1,3):order.extend([(f'cat-A{i}','off',False,False,False,False,False),(f'cat-B{i}','cat',False,False,False,False,False)])
    frozen=hashlib.sha256(a.elf.read_bytes()).hexdigest();save(a.out/'predeclared.json',{'elf_sha256':frozen,'order':order,'all_boots':'fresh quiet-NIC30s/acoustic3s/reset; zero raw driver errors/recoveries/timeouts/dead; rendered progress each snapshot; unchanged audio/memory/NIC gates','windows':'original four10s busy/four10s quiet per arm plus captured/scored memory and rendered liveness through post-session persist/owned cleanup/final recycle','retakes':False,'full_pairs':'deferred to5fps cap step','cat_spots':'two fresh pairs vs retained six-pair medians with max(AA floor,2pp nonidle/5pp idle) thresholds','preload':'both arms prepare a durable306917-byte file and evict its RAM entry; B uses typed production preload during cat/call; both remove owned file during cleanup'})
    rows=[]
    try:
        for label,mode,fetch,persistence,prepare,preload,prep_preload in order:
            assert hashlib.sha256(a.elf.read_bytes()).hexdigest()==frozen
            print('START '+label,flush=True)
            r=arm(a.out/label,a.elf,mode,fetch,persistence,prepare,preload,prep_preload);r['label']=label;r['evidence_path']=str(a.out/label);rows.append(r)
            assert r['absolute_passed'], 'absolute gate failure at '+label
            # Closed-byte audit also sees any final unterminated driver message.
            audit((a.out/label/'serial.log').read_bytes())
            save(a.out/'progress.json',{'completed':len(rows),'order':[r['label'] for r in rows],'evidence_paths':[r['evidence_path'] for r in rows]})
        summary={'passed':True,'rows':rows,'arms':{mode:comparison(rows,mode) for mode in ('fetch','5b','preload','cat')}}
        save(a.out/'summary.json',summary)
    except Exception as exc:
        save(a.out/'blocked.json',{'label':label,'reason':str(exc),'retained':True,'completed':len(rows),'no_retakes':True});raise

if __name__=='__main__':main()

#!/usr/bin/env python3
import json,os,subprocess
from pathlib import Path
from esp_serial import ROOT
from step9a_r2_diagnose import save
from step8e2b_analysis import lcd_memory_gate
OUT=Path('/home/khkramer/src/evidence/step9a/r4');REPORT=ROOT/'proj-2026-10-08-0949/codex-jobs/step-9a-r4-report.md'
def main():
    os.environ['STEP9A_BUDGET_PATH']=str(OUT/'arena-budget.json');os.environ['OFFLINE_R4_CIPHER_AUDIT']='1';os.environ['STEP9A_R4_ELF']=str(OUT/'image-candidate/dg-esp32.elf');guard=json.loads((OUT/'guard-summary.json').read_text())['selected'];assert guard;os.environ['STEP9A_R4_GUARD']=str(guard)
    plan=[('confirmation-A','natural',['--kind','A','--cycles','60','--min-stripes','100000']),('confirmation-B','natural',['--kind','B','--dumps','1050']),('confirmation-C','natural',['--kind','C','--cycles','40']),('confirmation-SHA','hash',['--core','1','--mode','0']),('confirmation-flood','inventory',['--kind','flood','--guard',str(guard)])];rows=[];all_clean=False
    try:
        for name,script,args in plan:
            print('START '+name,flush=True);save(OUT/'confirmation-progress.json',{'active':name,'finished':[r['name'] for r in rows]})
            with (OUT/(name+'-run.log')).open('x') as f:proc=subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/('hil/step9a_r4_'+script+'.py')),'--name',name]+args,stdout=f,stderr=subprocess.STDOUT,cwd=ROOT)
            r=json.loads((OUT/name/'result.json').read_text());rows.append(r)
            if not (OUT/name/'serial.log').exists():
                room=r.get('room_baseline',{})
                if room.get('noise_contaminated'):raise RuntimeError(f'ACOUSTIC HOLD before {name}: {room["baseline_dbfs"]:.6f} dBFS > -50 dBFS; device arm not started')
                raise RuntimeError(f'{name}: pre-arm failure; no serial acquisition')
            snapshots=[];parse_failures=[]
            for i,line in enumerate((OUT/name/'serial.log').read_text().splitlines()):
                if line.startswith('PERF '):
                    try:snapshots.append(json.loads(line[5:]))
                    except Exception as e:parse_failures.append({'line':i+1,'reason':str(e)})
            memory=lcd_memory_gate(snapshots,reconnects=59 if name=='confirmation-A' else 1 if name in ('confirmation-B','confirmation-flood') else None)
            save(OUT/name/'memory-audit.json',{'memory':memory,'parse_failures':parse_failures,'role':'four memory floors mandatory oncall cells; Idle/no-reconnect cells report boot trough without pretending forced reconnect'})
            memory_ok=(memory['passed'] if name in ('confirmation-A','confirmation-B','confirmation-flood') else all(v is not False for v in memory['checks'].values()))
            r['confirmation_memory_ok']=memory_ok;r['confirmation_clean']=r['clean'] and memory_ok and not parse_failures
            if name=='confirmation-flood':
                last=max((s.get('drop_reasons',{}).get('message_queue',0) for s in snapshots),default=0);r['confirmation_message_queue_drops']=last;r['confirmation_clean'] &= last==0
            save(OUT/'confirmation-summary.json',{'complete':False,'all_clean':False,'cells':rows})
            with REPORT.open('a') as f:f.write(f"\nStep4 cell progress `{name}`: complete={r['completed']}, clean={r['clean']}, fourfloor audit={memory['checks']}, acceptedconfirmation={r['confirmation_clean']}; exposure {r.get('exposure')}; error {r.get('error')}. Frozen candidate image, selectedguard{guard},40MHz; complete raw/cleanup retained; no retakes.\n")
            print(json.dumps({'name':name,'returncode':proc.returncode,'confirmation_clean':r['confirmation_clean'],'exposure':r.get('exposure'),'error':r.get('error')}),flush=True)
            if not r['confirmation_clean']:raise RuntimeError('Mandatory stop: '+name+' not clean')
        all_clean=True;save(OUT/'confirmation-summary.json',{'complete':True,'all_clean':True,'guard':guard,'cells':rows});return 0
    except Exception as e:save(OUT/'confirmation-blocked.json',{'reason':str(e),'completed_cells':[r['name'] for r in rows]});raise
    finally:
        with REPORT.open('a') as f:f.write('\nProgress note after numbered step4: '+('all five40MHz confirmation producers clean throughcleanup; proceedperformance.' if all_clean else 'mandatory stop at first unclean candidate cell; remaining confirmation/performance gated. Every failure retained, no retake.')+' No commit/push.\n')
if __name__=='__main__':raise SystemExit(main())

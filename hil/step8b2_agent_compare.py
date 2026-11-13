"""Three old/new conversation pairs, interleaved by exact firmware image.

Image switching cold-boots the board, so each conversation is cold. Each child
uses the unchanged six-turn agent conversation test and a newly owned voice bot.
Failures are retained and never replaced. The final image is always restored.
"""
import hashlib
import json
import subprocess
from pathlib import Path
from esp_serial import ROOT
from step8b2_listen import flash_image
from playback_quality import classify_turn

def run_comparison(a):
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    images={'old':(a.compare_old_bin.resolve(),a.compare_old_elf.resolve()),
            'new':(ROOT/'build/dg-esp32.bin',a.elf.resolve())}
    result=dict(completed=False,pairs=a.runs,temperature='Every conversation cold after exact-image flash',order=[],runs=[],summary={})
    prior={}
    def save():
        for level in ('old','new'):
            turns=[t for r in result['runs'] if r['level']==level for t in r.get('result',{}).get('turns',[])]
            scored=[t for t in turns if classify_turn(t)['passed']]
            eligible=[t for t in scored if t.get('checks',{}).get('llm_content',False)]
            result['summary'][level]=dict(turns=len(turns),acoustic_scored_turns=len(scored),
                transport_damaged_turns=sum(classify_turn(t)['classification']=='transport-damaged' for t in turns),
                acoustic_content_passed=sum(t.get('checks',{}).get('acoustic_content',False) for t in scored),
                correct_llm_turns=len(eligible),correct_llm_acoustic_content_passed=sum(t.get('checks',{}).get('acoustic_content',False) for t in eligible),
                all_checks_passed=sum(t.get('passed',False) for t in turns))
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        for index in range(a.runs):
            order=('old','new') if index%2==0 else ('new','old')
            for level in order:
                label=f'pair{index+1}-{level}';result['order'].append(label)
                image,elf=images[level];flash_image(image,out/f'{label}-flash.log')
                directory=out/label
                cmd=[str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/agent_conversation_test.py'),
                    '--out',str(directory),'--runs','1','--spl',str(a.spl),'--elf',str(elf)]
                if level in prior:cmd+=['--calibration-from',str(prior[level])]
                with (out/f'{label}.log').open('w') as log:
                    child=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=900)
                row=dict(pair=index+1,level=level,returncode=child.returncode,elf_sha256=hashlib.sha256(elf.read_bytes()).hexdigest())
                path=directory/'result.json'
                if path.exists():
                    row['result']=json.loads(path.read_text())
                    if row['result'].get('calibration'):prior[level]=directory
                result['runs'].append(row);save();print(label,result['summary'][level],flush=True)
        result['completed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        flash_image(images['new'][0],out/'restore-new.log');save()
    return 0 if result['completed'] else 1

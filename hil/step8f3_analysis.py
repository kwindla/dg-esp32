#!/usr/bin/env python3
"""Paired all-turn rescoring of retained 8f/8f2; no hardware or API calls."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
from esp_serial import ROOT
from step8f2_analysis import rescore_voice_content,voice_gates,write


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--baseline-ref',default='2af1d9cebaba5edae99e37d38d2337e83259cddc',help='Frozen step-8f3 base for reproducible paired comparisons')
    p.add_argument('--evidence-root',type=Path,default=ROOT.parent/'evidence')
    a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    prior=a.out/'playback-quality-base.py'
    prior.write_bytes(subprocess.check_output(['git','show',a.baseline_ref+':hil/playback_quality.py'],cwd=ROOT))
    write(a.out/'baseline-identity.json',dict(commit=subprocess.check_output(['git','rev-parse',a.baseline_ref],cwd=ROOT,text=True).strip(),
        scorer_sha256=__import__('hashlib').sha256(prior.read_bytes()).hexdigest()))
    spec=importlib.util.spec_from_file_location('base_quality',prior)
    base_quality=importlib.util.module_from_spec(spec);spec.loader.exec_module(base_quality)
    evidence=a.evidence_root;summary={}
    for name,folder in [('8f',evidence/'step8f/voice-soak-approved01/turns'),
                        ('8f2',evidence/'step8f2/qualification01/voice-soak/turns')]:
        baseline=rescore_voice_content(folder,matcher=base_quality.decoded_match,level_centers=base_quality.REPLY_SPL_CENTERS)
        matcher_only=rescore_voice_content(folder,level_centers=base_quality.REPLY_SPL_CENTERS)
        final=rescore_voice_content(folder)
        write(a.out/(name+'-baseline.json'),baseline)
        write(a.out/(name+'-matcher-only.json'),matcher_only)
        write(a.out/(name+'-content.json'),final)
        write(a.out/(name+'-device-gates.json'),voice_gates(folder))
        movements=[];level_movements=[]
        for before,middle,after in zip(baseline['rows'],matcher_only['rows'],final['rows']):
            changed={key:dict(before=value,after=after['corrected_checks'].get(key))
                for key,value in before['corrected_checks'].items() if value!=after['corrected_checks'].get(key)}
            if changed or before['corrected_passed']!=after['corrected_passed']:
                movements.append(dict(turn=after['turn'],baseline_passed=before['corrected_passed'],
                    matcher_only_passed=middle['corrected_passed'],final_passed=after['corrected_passed'],changed_checks=changed))
            for old_level,new_level in zip(before.get('level_gates',[]),after.get('level_gates',[])):
                if old_level and new_level and old_level.get('passed')!=new_level.get('passed'):
                    level_movements.append(dict(turn=after['turn'],spl_db=new_level['board_mic_tone_transfer_spl'],
                        before=old_level['passed'],after=new_level['passed'],window_db=new_level['spl_window_db']))
        summary[name]=dict(acquisition_passes=final['original_passes'],baseline_passes=baseline['corrected_passes'],
            matcher_only_passes=matcher_only['corrected_passes'],final_passes=final['corrected_passes'],
            turns=final['turns'],movements=movements,level_movements=level_movements,
            remaining_failures=[dict(turn=r['turn'],checks=[k for k,v in r['corrected_checks'].items() if v is False])
                for r in final['rows'] if not r['corrected_passed']])
    write(a.out/'verdict-movements.json',summary)
    print(__import__('json').dumps(summary,indent=2))


if __name__=='__main__':main()

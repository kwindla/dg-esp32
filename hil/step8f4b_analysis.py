#!/usr/bin/env python3
"""Offline 8f4b rescoring against the frozen entry revision; no acquisition."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import numpy as np
from esp_serial import ROOT
from playback_quality import decoded_match
from step7_support import pcm
from step8f2_analysis import load,write
from uplink_continuity import link_tail,recorded_audio_idle_windows

BASELINE_REF='45f90c1df10757d2257118e0336b5a9a0cec35f2'
RETAINED_INPUTS=(
    'step8f/voice-soak-approved01/turns/result.json',
    'step8f2/qualification01/voice-soak/turns/result.json',
    'step8f3/offline/test/result.json',
    'step8f3/offline/test/serial.log',
    'step8f3/offline/test/serial.audio.jsonl',
    'step8f3/offline/test/rtp/rtp.jsonl',
)


def frozen_module(out,ref,name):
    path=out/(name+'-base.py')
    path.write_bytes(subprocess.check_output(['git','show',ref+':hil/'+name+'.py'],cwd=ROOT))
    spec=importlib.util.spec_from_file_location(name+'_base',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--baseline-ref',default=BASELINE_REF)
    p.add_argument('--evidence-root',type=Path,default=ROOT.parent/'evidence')
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    def hashes():
        return {name:hashlib.sha256((a.evidence_root/name).read_bytes()).hexdigest()
                for name in RETAINED_INPUTS}
    before_hashes=hashes()
    write(a.out/'retained-input-sha256.json',before_hashes)
    subprocess.run([sys.executable,str(ROOT/'hil/step8f4_analysis.py'),
        '--baseline-ref',a.baseline_ref,'--evidence-root',str(a.evidence_root),
        '--out',str(a.out/'rescored')],check=True)
    baseline=frozen_module(a.out,a.baseline_ref,'playback_quality')
    baseline_link=frozen_module(a.out,a.baseline_ref,'uplink_continuity')
    source=pcm(ROOT/'hil/fixtures/step8f3-tts-reference.wav');regressions={}
    reference=np.r_[source,np.zeros(4800)]
    for missing in (1,160,320):
        decoded=reference[:-missing]
        regressions[f'trailing-quiet-missing-{missing}']=dict(
            before=baseline.decoded_match(decoded,reference),after=decoded_match(decoded,reference))
    reference=np.r_[source,np.zeros(1600-len(source)%1600+160)]
    regressions['empty-trailing-quiet']=dict(
        before=baseline.decoded_match(reference[:-160],reference),
        after=decoded_match(reference[:-160],reference))
    packets=[dict(pc_id='regression',ssrc=2,seq=65535,timestamp=2**32-960,observed_ms=9970),
             dict(pc_id='regression',ssrc=2,seq=1,timestamp=960,observed_ms=20030)]
    windows=[dict(start_s=10,end_s=20,category='idle')]
    regressions['contained-single-loss']=dict(
        before=baseline_link.link_tail(packets,9.97,20.03,startup_seconds=0,excluded_windows=windows),
        after=link_tail(packets,9.97,20.03,startup_seconds=0,excluded_windows=windows))
    write(a.out/'regressions.json',regressions)
    offline=a.evidence_root/'step8f3/offline/test';result=load(offline/'result.json')
    reply=pcm(ROOT/'server/fixtures/reply_pineapple.wav')
    offline_pcm=[]
    for index,row in enumerate(result['runs'],1):
        decoded=pcm(offline/f'run{index}/rx.wav')
        offline_pcm.append(dict(run=index,acquisition_passed=row['passed'],
            before=baseline.decoded_match(decoded,reply),after=decoded_match(decoded,reply)))
    write(a.out/'offline-pcm-comparison.json',offline_pcm)
    packets=[json.loads(line) for line in (offline/'rtp/rtp.jsonl').read_text().splitlines()]
    start=min(t['prompt_start'] for t in result['runs'] if 'prompt_start' in t)
    end=max(t['reply_window_transport']['end'] for t in result['runs'] if 'reply_window_transport' in t)
    windows=recorded_audio_idle_windows(offline/'serial.log',end=end)
    old=baseline_link.link_tail(packets,start,end,excluded_windows=windows)
    new=load(a.out/'rescored/offline-gates.json')['link']
    changed=[dict(before=b,after=c) for b,c in zip(old['gaps'],new['gaps'])
             if b['category']!=c['category'] or b['gap_ms']!=c['gap_ms']]
    write(a.out/'offline-link-comparison.json',dict(before=old,after=new,changed_intervals=changed))
    summary=load(a.out/'rescored/paired-analysis/verdict-movements.json')
    for name in ('8f','8f2'):
        final=load(a.out/'rescored/paired-analysis'/f'{name}-content.json')
        quiet=[b for r in final['rows'] for m in r['decoded_matches'] for b in m.get('quiet_blocks',[])]
        summary[name]['quiet']=dict(total=len(quiet),checked=sum(b['passed'] is not None for b in quiet),
            unavailable=sum(b['passed'] is None for b in quiet),failures=sum(b['passed'] is False for b in quiet))
    fields=('passed','intervals','over_threshold','over_extreme_threshold','eligible_maximum_ms','whole_capture_maximum_ms')
    summary['offline']=dict(original_passes=result['pass_count'],
        before={k:old[k] for k in fields},after={k:new[k] for k in fields},changed_intervals=changed,
        decoded_before_passes=sum(r['before']['passed'] for r in offline_pcm),
        decoded_after_passes=sum(r['after']['passed'] for r in offline_pcm),
        decoded_verdict_movements=[r['run'] for r in offline_pcm if r['before']['passed']!=r['after']['passed']],
        cadence=load(a.out/'rescored/offline-gates.json')['cadence'])
    summary['regressions']={name:{phase:{k:gate.get(k) for k in ('passed','correlation','quiet_block_failures','eligible_maximum_ms')}
        for phase,gate in rows.items()} for name,rows in regressions.items()}
    summary['retained_inputs_unchanged']=hashes()==before_hashes
    if not summary['retained_inputs_unchanged']:raise RuntimeError('Retained acquisition inputs changed')
    write(a.out/'summary.json',summary)
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()

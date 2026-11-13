#!/usr/bin/env python3
"""Offline contention regression controls; execute under build_locked.sh."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
MUTANTS=(
 ('old-bridge-send',"display_sent(events(offset),session,rid)","next((e for e in events(offset) if e.get('event')=='display-sent' and e['packet']['d']['id']==rid),None)"),
 ('wrong-connection-ack',"display_result(events(offset),session,packet)","next((e for e in events(offset) if e.get('event')=='display-result' and e['result']['epoch']==packet['epoch'] and e['result']['seq']==packet['seq']),None)"),
 ('demo-touch-enable',"board.command('touch-enable 0')","board.command('demo touch-enable 0')"),
 ('demo-touch-status',"board.command('touch-status')","board.command('demo touch-status')"),
 ('allow-dead-p4',"require(board=='echoear', 'mic capture counter not live:","require(True, 'mic capture counter not live:"),
 ('fake-live-completions','counter_live=completions_delta>0 if require_live_capture else None','counter_live=True if require_live_capture else None'),
 ('skip-score-liveness',"resources(samples,board=acquisition['board'])","resources(samples,board=acquisition['board'],require_live_capture=False)"),
 ('s3-dead-as-zero',"overflow_result='unmeasured (report-only)'\n        measured_overflows=None","overflow_result='measured'\n        measured_overflows=0"),

 ('drop-pair-capture-status','capture_overflow_evidence=capture_evidence','capture_overflow_evidence=[]'),
 ('skip-measured','timing = measured(row, bounds)',"timing = dict(p99_us=20000,median_us=20000,window_us=150000000)"),
 ('only-one-reference','max(deltas) <= 7000','min(deltas) <= 7000'),
 ('restart-window',"RESTART_AT <= restart['at_s'] < RESTART_AT+PERIOD*2",'True'),
 ('generation-increase','gens[1] > gens[0]','True'),
 ('frozen-once','len(capture.intervals) == 1','True'),
 ('two-sessions',"len(acquisition['sessions']) == 2", "len(acquisition['sessions']) >= 1"),
 ('touch-interrupts',"all(t['driver']['interrupts']==0 for t in touch)",'True'),
 ('touch-enabled',"t.get('enabled') is False",'True'),
 ('tts',"any(e.get('event') == 'first-tts-byte' for e in events)",'True'),
 ('delete',"c['deleted'] is True",'True'),
 ('bench',"status['bench'][:2]==[1,30]",'True'),
 ('perf-failed',"not display.get('failed')",'True'),
 ('perf-dead',"not display.get('dead')",'True'),
 ('status-failed',"not status['failed']",'True'),
 ('status-dead',"not status['dead']",'True'),
 ('middle-render',"all(b['updates'][2] > a['updates'][2] for a,b in zip(display,display[1:]))",'True'),
 ('first-cycle',"cycles[0]['at_s'] < session['started_s']+PERIOD*2",'True'),
 ('last-cycle',"cycles[-1]['at_s'] > session['ended_s']-PERIOD*3",'True'),
 ('fixed-tuple','(bounds.stall_us, bounds.p99_delta_us, bounds.coverage) == (68411, 7000, .95)','True'),
 ('later-underrun',"require(max(underruns) == 0, 'playback underrun')", "require(underruns[0] == 0, 'playback underrun')"),
 ('pc-id',"all(s['pc_id']==pc_id for s in samples)",'True'),
 ('live-mode',"all(e.get('mode')=='live-test' for e in events)",'True'),
 ('pre-window-counters','capture_counters(first); resources([first,first],board=args.board,require_cpu=False,require_live_capture=False)','pass'),
)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if os.environ.get('DG_BUILD_LOCK_HELD')!='1':raise RuntimeError('Use scripts/build_locked.sh')
    args.out.mkdir(parents=True,exist_ok=False)
    source=(ROOT/'hil/p4_contention.py').read_text()
    results=[]
    with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'],prefix='contention-mutants-') as directory:
        for name,old,new in (('control',None,None),*MUTANTS):
            if old is not None and source.count(old)!=1:raise ValueError('anchor drift: '+name)
            candidate=Path(directory)/(name+'.py')
            code=source.replace(old,new) if old else source
            code=code.replace('ROOT = Path(__file__).resolve().parents[1]',f'ROOT = Path({str(ROOT)!r})')
            candidate.write_text(code)
            environment=dict(os.environ,DG_P4_CONTENTION_SOURCE=str(candidate),PYTHONPATH='hil:server',
                OPENBLAS_NUM_THREADS='1',PYTHONDONTWRITEBYTECODE='1')
            command=[sys.executable,'-m','pytest','-q','hil/test_p4_contention.py','hil/test_p4_contention_live.py',
                     '--basetemp',str(Path(directory)/('pytest-'+name))]
            with (args.out/(name+'.log')).open('w') as log:
                status=subprocess.run(command,cwd=ROOT,env=environment,stdout=log,stderr=subprocess.STDOUT).returncode
            expected=0 if old is None else 1
            if status!=expected:raise RuntimeError(f'{name}: expected {expected}, got {status}')
            results.append(dict(case=name,exit=status,killed=old is not None))
            print(name+(': PASS' if old is None else ': killed'),flush=True)
    (args.out/'results.json').write_text(json.dumps(dict(passed=True,cases=results),indent=2)+'\n')


if __name__=='__main__':main()

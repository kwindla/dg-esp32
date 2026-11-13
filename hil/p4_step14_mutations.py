#!/usr/bin/env python3
"""Reproduce the review's 66 offline mutants in an isolated source overlay.

Never mutate the worktree or the reviewer's scratch copies. A failed test kills
a mutant; collection/tool failures do not. Keep individual logs and survivors.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--extra-cases',type=Path,help='additional numbered regression mutants')
    args=parser.parse_args(argv);args.out.mkdir(parents=True,exist_ok=False)
    definition=ROOT/'hil/fixtures/step14_mutants.json';cases=json.loads(definition.read_text())['cases']
    if len(cases)!=66:raise ValueError('exactly 66 Opus review cases required')
    if args.extra_cases:cases+=json.loads(args.extra_cases.read_text())['cases']
    modified={case['file'] for case in cases};results=[]
    with tempfile.TemporaryDirectory(prefix='step14-mutants-',dir=os.environ['TMPDIR']) as work:
        work=Path(work);(work/'hil').mkdir()
        for source in (ROOT/'hil').iterdir():
            target=work/'hil'/source.name
            if source.name in ('__pycache__','.pytest_cache'):continue
            if str(source.relative_to(ROOT)) in modified or source.name in ('test_p4_step14.py','test_p4_liveness.py'):
                shutil.copyfile(source,target)
            else:target.symlink_to(source,target_is_directory=source.is_dir())
        for name in ('scripts','main','server','components','spikes'):(work/name).symlink_to(ROOT/name,target_is_directory=True)
        environment=dict(os.environ,OPENBLAS_NUM_THREADS='1',PYTHONDONTWRITEBYTECODE='1',
                         PYTHONPATH=str(work/'hil')+':'+str(ROOT/'server'))
        command=[sys.executable,'-m','pytest','-p','no:cacheprovider','-q','-x',
                 'hil/test_p4_step14.py','hil/test_p4_liveness.py','--basetemp',str(work/'pytest')]
        def run(label):
            with (args.out/(label+'.log')).open('w') as log:
                return subprocess.run(command,cwd=work,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=90).returncode
        if run('control')!=0:raise RuntimeError('unmutated control failed; see control.log')
        for case in cases:
            path=work/case['file'];original=path.read_text()
            if original.count(case['old'])!=1:raise ValueError('anchor drift: '+str(case['id']))
            path.write_text(original.replace(case['old'],case['new']))
            try:code=run(f"case-{case['id']:02d}")
            finally:path.write_text(original)
            status='killed' if code==1 else 'survived' if code==0 else 'invalid'
            results.append(dict(id=case['id'],file=case['file'],status=status,exit=code))
            print(str(case['id'])+': '+status,flush=True)
    report=dict(cases=results,survivors=[r['id'] for r in results if r['status']=='survived'],
                invalid=[r['id'] for r in results if r['status']=='invalid'],
                killed=sum(r['status']=='killed' for r in results))
    (args.out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    return 0 if not report['survivors'] and not report['invalid'] else 1


if __name__=='__main__':raise SystemExit(main())

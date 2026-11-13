"""Read-only v8.5 retained-evidence replay. Missing capture-end is never invented."""
import argparse
import contextlib
import io
import json
from pathlib import Path
from echoear_aec_bench import sha
from echoear_aec_campaign_analysis import analyze_hold,campaign_verdict
from echoear_aec_v85 import TEMPLATE
from echoear_aec_v85_tts import inspect
from echoear_aec_accept import rows


def replay(base,output):
    base,output=Path(base).resolve(),Path(output).resolve()
    if output==base or base in output.parents:raise ValueError('Output must be outside retained evidence')
    repo=Path(__file__).resolve().parent.parent
    p=json.loads((repo/TEMPLATE).read_text())
    inputs={};code={str(f):sha(f) for f in (repo/'hil').glob('*.py')}
    code[str(repo/TEMPLATE)]=sha(repo/TEMPLATE)
    output.mkdir(parents=True,exist_ok=False)
    reports=[]
    for name in ('campaign-07','campaign-08','campaign-09'):
        run=base/name;root=run/'qualification-01'
        files={str(f):sha(f) for f in run.rglob('*') if f.is_file()};inputs.update(files)
        with contextlib.redirect_stdout(io.StringIO()):h=analyze_hold(root,p)
        h.update(id='qualification-01',run_root=str(run),acceptance_credit=False)
        verdict=campaign_verdict(p,[h]);assert not verdict['passed'] and not verdict['qualification']
        (output/(name+'-analysis.json')).write_text(json.dumps(dict(report_only=True,acceptance_credit=False,hold=h,verdict=verdict),indent=2)+'\n')
        original=json.loads((run/'analysis.json').read_text())
        events=rows(root/'evaluation/events.jsonl');recorded=[e for f in (root/'sessions').glob('*/events.jsonl') for e in rows(f)]
        ids=sorted({e['case_id'] for e in events if e.get('case_id') and e['event']=='tts-generation-metric-start'})
        long=[]
        for cid in ids:
            witness=inspect(events,recorded,[cid])
            if witness['long_generations']:long.append(dict(id=cid,witness=witness,admitted_retrospectively=False))
        reports.append(dict(campaign=name,original_passed=original.get('passed',original.get('summary',{}).get('passed')),
            hypothetical_passed=False,acceptance_credit=False,qualification=False,cases=len(h['cases']),
            strict=sum(r.get('opportunity')=='eligible' for r in h['cases']),
            lull=sum(r.get('opportunity')=='ongoing-reply-lull' for r in h['cases']),
            cancelled_lull_check=verdict['cancelled_lull_check'],long_tts=long,
            missing_capture_endpoints=sorted({m for e in h['epoch_checks'] for m in e['checks']['transport'].get('missing_capture_endpoints',[])}),
            export_scoring='unavailable: old acquisitions did not record capture-end PERF; no interpolation/no retrospective credit'))
    if any(sha(f)!=v for f,v in {**inputs,**code}.items()):raise ValueError('Replay changed retained input/code')
    result=dict(report_only=True,acceptance_credit=False,qualification=False,campaigns=reports,
                input_sha256=inputs,code_sha256=code)
    (output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('base',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();value=replay(args.base,args.output)
    print(json.dumps(dict(report_only=True,acceptance_credit=False,campaigns=[{k:r[k] for k in ('campaign','cases','strict','lull','hypothetical_passed')} for r in value['campaigns']]),indent=2))

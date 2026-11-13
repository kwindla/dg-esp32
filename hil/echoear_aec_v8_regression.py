"""Campaign-04 replay under prospective v8.1 rules: report-only, never credit."""
import argparse
import copy
import json
from pathlib import Path
from echoear_aec_bench import ROOT,EVIDENCE,sha,write
from echoear_aec_campaign_analysis import analyze_hold,campaign_verdict


def replay():
    root=EVIDENCE/'step-9/campaign-04'
    before={str(f):sha(f) for f in root.rglob('*') if f.is_file()}
    original=json.loads((root/'declaration.json').read_text())
    prospective=json.loads((ROOT/'hil/plans/echoear-aec-accept-v1.json').read_text())
    p=copy.deepcopy(original)
    for k in ['gates','fixture_policy','spares','opportunity','time_estimate']:p[k]=prospective[k]
    # Pin the pre-v8.3.1 playback scorer for this historical regression.
    for key in ('source_template_rule','source_template_basis'):p['gates']['missing_playback'].pop(key,None)
    hold=analyze_hold(root/'qualification-01',p)
    hold.update(id='qualification-01',run_root=str(root),acceptance_credit=False)
    cases={c['id']:c for c in hold['cases']}
    for short in ['stop-02','stop-08','sentence-02']:
        c=cases['qualification-01-'+short+'-a1']
        assert c['flush']['callback_witness']['passed'] and c['flush']['passed'] and c['barge_success'],short
        assert type(c['flush']['stale_frames_after_mute']) is int
    # Decisions14: all eight eligible Stops and both sentences now pass the
    # amended 300ms callback witness; this replay never awards acceptance credit.
    for kind,expected in [('stop',8),('sentence',2)]:
        eligible=[c for c in cases.values() if c['kind']==kind and c.get('opportunity')=='eligible']
        assert len(eligible)==expected and all(c['barge_success'] for c in eligible),kind
    stop09=cases['qualification-01-stop-09-a1']
    assert stop09['flush']['callback_witness']['pre_callback_upper_age_us']<=300000
    assert hold['proactive']['passed']
    summary=campaign_verdict(p,[hold]);assert not summary['passed'] and not summary['qualification']
    assert all(sha(Path(path))==digest for path,digest in before.items())
    assert not (root/'analysis.json').exists()
    return dict(report_only=True,acceptance_credit=False,original_declaration_sha256=sha(root/'declaration.json'),
        prospective_template_sha256=sha(ROOT/'hil/plans/echoear-aec-accept-v1.json'),
        preserved_files=len(before),input_sha256=before,summary=summary,holds=[hold])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path);a=parser.parse_args()
    if a.output.exists():raise FileExistsError('Fresh report-only output required')
    value=replay();write(a.output,value)
    print(json.dumps(dict(passed=True,report_only=True,acceptance_credit=False,cases=len(value['holds'][0]['cases']),preserved_files=value['preserved_files'])))

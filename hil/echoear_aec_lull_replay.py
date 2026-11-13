"""Read-only retained-campaign lull replay. No acceptance credit.

Output MUST be outside the evidence tree. Historical rows are retained verbatim;
an independent rerun is checked against them before hypothetical annotations.
The unsealed control is listed explicitly and never added to a denominator.
"""
import argparse
import contextlib
import io
import json
import re
from pathlib import Path
import tempfile

from echoear_aec_accept_analysis import main as block_analysis, summarize
from echoear_aec_bench import sha
from echoear_aec_lull import RULE, cancelled_check


def replay(base, output, campaigns=('campaign-04', 'campaign-06')):
    base, output = Path(base).resolve(), Path(output).resolve()
    if output == base or base in output.parents:
        raise ValueError('Replay output must be outside evidence')
    campaigns=tuple(campaigns)
    if not campaigns or len(campaigns)!=len(set(campaigns)) or any(not re.fullmatch(r'campaign-[0-9]{2}',c) for c in campaigns):
        raise ValueError('Distinct retained campaign-NN identities required')
    inputs = {}
    code_files=('echoear_aec_lull.py','echoear_aec_lull_replay.py','echoear_aec_accept_analysis.py','echoear_aec_campaign_analysis.py','echoear_aec_campaign.py','echoear_aec_campaign_resume.py')
    code_sha256={str(Path(__file__).with_name(name)):sha(Path(__file__).with_name(name)) for name in code_files}

    def track(path):
        path = Path(path).resolve()
        inputs[str(path)] = sha(path)
        return path

    def read(path):
        return json.loads(track(path).read_text())

    results = []
    for campaign in campaigns:
        root = base/campaign/'qualification-01'
        saved = read(root/'result.json')
        frozen = read(root/'accept-analysis.json')
        cases = list(saved['cases'])
        sealed = {c['run_id'] for c in cases}
        extra = []
        for path in sorted(root.glob('*/result.json')):
            for c in read(path).get('cases', []):
                if c.get('kind') in ('stop', 'sentence') and c['run_id'] not in {x['run_id'] for x in cases+extra}:
                    extra.append(c)
        cases += extra
        # The block analyzer also visits echo/probe rows. Bind their inputs too,
        # even though this report scores only Stop/sentence rows.
        for c in cases:
            folder=root/c.get('capture_dir',c['run_id'])
            track(folder/'capture.npz')
            for path in sorted(folder.glob('*.json')):track(path)
            if c.get('fixture'):track(c['fixture'])
            aggregate=root/'evaluation'/(c['run_id']+'.pcm')
            if aggregate.exists():track(aggregate)
            for context in [e for e in c['events'] if e.get('event')=='tts-context-pcm']:
                track(root/'evaluation'/context['path'])
        # block_analysis uses unchanged historical measurements; only its
        # explicit temporary output is writable, never accept-analysis.json.
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            reproduced = block_analysis(root, Path(temporary)/'strict.json', additional_cases=extra)
            prospective = block_analysis(root, Path(temporary)/'prospective.json', additional_cases=extra,lull_policy=RULE)
        prior = {r['id']:r for r in frozen['cases']}
        index = {r['id']:r for r in reproduced['cases']}
        proposed_index = {r['id']:r for r in prospective['cases']}
        track(root/'quiet.npz')
        for path in sorted((root/'sessions').glob('*/events.jsonl')):
            track(path)
        for path in (root/'declaration.json', root/'identity.json', root/'candidate-serial.log'):
            track(path)
        for c in cases:
            if c['kind'] not in ('stop','sentence'):
                continue
            row = index[c['run_id']]
            original = prior.get(c['run_id'])
            if original is not None:
                for key in ('opportunity','mic3_state','recognized','credited_barge_in','near_onset'):
                    if row.get(key) != original.get(key):
                        raise ValueError(f'Historical {key} failed to reproduce: {campaign}/{c["run_id"]}')
            hypothetical = proposed_index[c['run_id']]
            proof = hypothetical.get('lull_witness', {})
            witness = proof.get('pre_near_source',dict(passed=False,reason='Strict/upstream path; no lull evaluation'))
            measurement = proof.get('measurement',dict(passed=False,reason='Strict/upstream path; no lull evaluation'))
            for context in [e for e in c['events'] if e.get('event')=='tts-context-pcm']:
                track(root/'evaluation'/context['path'])
            results.append(dict(campaign=campaign,id=c['run_id'],sealed_in_hold=c['run_id'] in sealed,
                                original=original,strict_reproduced=row,pre_near_source=witness,
                                measured_mic3=measurement,hypothetical=hypothetical,
                                acceptance_credit=False))
    # Verify every read input remains byte-identical. No output is a retained
    # accept-analysis.json or a campaign verdict; success is descriptive only.
    if any(sha(path)!=digest for path,digest in {**inputs,**code_sha256}.items()):
        raise ValueError('Input changed during retained replay')
    result = dict(rule=RULE,status='preparation-only',report_only=True,acceptance_credit=False,
                  qualification=False,
                  limitation='Option (a), measured span within a fixed horizon with cancellation censoring. Retained replay is diagnostic only; no retrospective acceptance credit.',
                  cancelled_lull_check=cancelled_check([r['hypothetical'] for r in results]),
                  success_basis='Recognition plus an observed interruption only; campaign latency/flush success gates are not re-scored or credited.',
                  cases=results,
                  summary={campaign:summarize([r['hypothetical'] for r in results if r['campaign']==campaign and r['sealed_in_hold']],lull_policy=RULE) for campaign in campaigns},
                  input_sha256=inputs,code_sha256=code_sha256)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-base',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--campaigns',nargs='+',default=['campaign-04','campaign-06'])
    args=parser.parse_args()
    result=replay(args.evidence_base,args.output,args.campaigns)
    print(json.dumps(result['summary'],indent=2))

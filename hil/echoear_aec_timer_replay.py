"""Read-only c09/c10 timer replay. Writes diagnostics only to a new output folder."""
import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from echoear_aec_event_tail import JsonlTail
from echoear_aec_stimulus import (TransportEvents, active_transport_witness,
                                  schedule_from_tts, schedule_active_window)


class Clock:
    def __init__(self, start): self.now = start
    def __call__(self): return self.now
    def is_set(self): return False
    def wait(self, seconds): self.now += seconds


def replay(root, out):
    out.mkdir(exist_ok=False, parents=True)
    inputs = {}; results = []; row_checks = []
    def read(path):
        inputs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return json.loads(path.read_text())
    def events(path):
        # Compare exactly the complete-row sequence, not only selected writes.
        raw = hashlib.sha256(); count = 0
        def update(h, row):h.update(json.dumps(row,sort_keys=True,separators=(',',':')).encode()+b'\n')
        with path.open() as stream:
            for line in stream:
                if line.endswith('\n'):
                    update(raw,json.loads(line));count+=1
        tail = JsonlTail([path]); actual = hashlib.sha256(); seen = 0; writes = []; evaluation=[]
        while True:
            for row in tail.poll():
                update(actual,row);seen+=1
                if row.get('kind')=='tts-transport-write':writes.append(row)
                if row.get('event')=='tts-generation-metric-start':evaluation.append(row)
            if not tail.backlog:break
        assert seen==count and actual.digest()==raw.digest()
        inputs[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
        row_checks.append(dict(path=str(path),rows=seen,identical=True))
        return writes,evaluation
    for campaign in ('campaign-09','campaign-10'):
        hold=root/campaign/'qualification-01'
        saved=read(hold/'result.json'); writes=[]
        analysis=read(root/campaign/'analysis.json')
        indexed={r['id']:r for h in analysis['holds'] for r in h['cases']}
        for path in sorted((hold/'sessions').glob('*/events.jsonl')):
            writes.extend(events(path)[0])
        evaluation=events(hold/'evaluation/events.jsonl')[1]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'writes.jsonl'
            for case in saved['cases']:
                old=case.get('stimulus_timer')
                if not old:continue
                owner=old['active_tts_witness']['latest']
                subset=[w for w in writes if w['monotonic_ns']>=case['start_ns'] and
                        (w.get('pc_id'),w.get('context_id'))==(owner['pc_id'],owner['context_id'])]
                path.write_text(''.join(json.dumps(w)+'\n' for w in subset))
                identity=dict(pc_id=owner['pc_id'],context_id=owner['context_id'])
                point=active_transport_witness([path],case['start_ns'],old['timer_fired_s'],owner=identity)
                assert point['latest']==old['active_tts_witness']['latest']
                assert point['active']==old['active_tts_witness']['active']
                clock=Clock(case['start_ns']/1e9)
                new=schedule_from_tts(lambda:[path],case['start_ns'],case['stop_offset_s'],
                    lambda:dict(request_monotonic_s=clock()),identity=lambda:identity,
                    require_active=True,clock=clock,cancel=clock)
                assert new['anchor_s']==old['anchor_s']
                actual=old['jabra']['request_monotonic_s'] if old.get('jabra') else None
                near=indexed.get(case['run_id'],{}).get('near_onset',{})
                bracket=near.get('host_interval');wide=near.get('host_interval_with_onset_uncertainty')
                results.append(dict(campaign=campaign,id=case['run_id'],kind='ordinary',
                    near_onset_bracket_width_ms=(bracket[1]-bracket[0])*1000 if bracket else None,
                    near_onset_bracket_with_uncertainty_width_ms=(wide[1]-wide[0])*1000 if wide else None,
                    anchor_identical=True,retained_fired_selection_identical=True,
                    efficient_poll_skipped=new['skipped'],retained_skipped=old['skipped'],
                    efficient_poll_error_ms=new.get('actual_minus_requested_ms'),
                    historical_actual_error_ms=(actual-old['deadline_s'])*1000 if actual else None,
                    historical_over_100ms=bool(actual and abs(actual-old['deadline_s'])>.1)))
            contexts=[e for e in evaluation if e.get('case_id')=='reconnect-01-a1-old']
            assert len({(e['pc_id'],e['context_id']) for e in contexts})==1
            context=contexts[0];identity=dict(pc_id=context['pc_id'],context_id=context['context_id'])
            subset=[w for w in writes if (w.get('pc_id'),w.get('context_id'))==tuple(identity.values())]
            path.write_text(''.join(json.dumps(w)+'\n' for w in subset))
            start=int(context['t']*1e9);clock=Clock(context['t'])
            new=schedule_active_window(lambda:[path],start,lambda:dict(request_monotonic_s=clock()),
                lambda:identity,clock=clock,cancel=clock)
            rec=dict(campaign=campaign,id='reconnect-01-a1',kind='reconnect',
                     efficient_window_skipped=new['skipped'],efficient_offset_s=new.get('actual_invocation_offset_s'),
                     efficient_selected=new['active_tts_witness']['latest'])
            old_path=hold/'reconnect-01-a1/timer.json'
            if old_path.exists():
                old=read(old_path)
                point=active_transport_witness([path],start,old['timer_fired_s'],owner=identity)
                rec.update(retained_skipped=old['skipped'],retained_fired_latest_age_s=point['age_s'],
                    stale_selected_age_s=old['active_tts_witness']['age_s'])
                assert new['anchor_s']==old['anchor_s'] and not new['skipped'] and old['skipped']
            else:
                read(hold/'reconnect-01-a1/acquisition-failure.json')
                anchor=min(w['monotonic_ns']/1e9 for w in subset if w.get('non_silent'))
                point=active_transport_witness([path],start,anchor+1,owner=identity)
                rec['historical_fixed_1s_active']=point['active']
                assert not point['active']
            results.append(rec)
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in inputs.items())
    ordinary=[r for r in results if r['kind']=='ordinary']
    summary=dict(report_only=True,qualification_credit=False,complete_row_checks=row_checks,
                 ordinary_count=len(ordinary),ordinary_retained_selections_identical=True,
                 historical_over_100ms=sum(r['historical_over_100ms'] for r in ordinary),
                 counterfactual_decision_differences=[r['id'] for r in ordinary if r['efficient_poll_skipped']!=r['retained_skipped']],
                 results=results,input_sha256=inputs,
                 limitation='Virtual-time counterfactual only; no simulated callback is a live stimulus or qualification. Real elapsed-time performance is tested separately.')
    (out/'replay.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in ('results','input_sha256','complete_row_checks')}))
    print(json.dumps([r for r in results if r['kind']=='reconnect']))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();replay(args.root,args.out)

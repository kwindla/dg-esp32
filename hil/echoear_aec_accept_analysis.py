"""Barge-in block analysis with explicit eligible, in-window and no-opportunity rows.

Independent raw/MIC3 witnesses, fixed 1024-sample AFE delay. Unknown evidence
never becomes a witnessed pause or a successful acceptance denominator.
"""
import sys,json,re,wave
from pathlib import Path
import numpy as np
from scipy.signal import correlate,resample_poly

from echoear_aec_bench import load_capture,write,analyze,sha
from echoear_aec_metrics import activity,frame_power,interval_latency
from echoear_aec_timing import sample_interval

def rows(p):return [json.loads(x) for x in p.read_text().splitlines() if x] if p.exists() else []
def read(p):return json.loads(p.read_text())
def match(target,template):
 target=np.asarray(target,float);template=np.array(template,dtype=float,copy=True);template-=template.mean();n=len(template)
 if n<160 or len(target)<n or np.sum(template**2)==0:return dict(valid=False)
 dots=correlate(target,template,mode='valid',method='fft');ones=np.ones(n)
 energy=np.maximum(correlate(target**2,ones,mode='valid',method='fft')-correlate(target,ones,mode='valid',method='fft')**2/n,1e-12)
 values=dots/np.sqrt(energy*np.sum(template**2));at=int(np.argmax(np.abs(values)));peak=float(abs(values[at]));other=np.abs(values).copy();other[max(0,at-640):at+641]=0;alternative=float(other.max())
 return dict(valid=peak>=.25 and peak>=1.1*alternative,at=at,correlation=float(values[at]),alternative_peak=alternative)
def normalized(text):return re.findall(r"[a-z0-9]+",(text or '').lower())
def wer(expected,actual):
 a,b=normalized(expected),normalized(actual);prev=list(range(len(b)+1))
 for i,x in enumerate(a,1):
  now=[i]
  for j,y in enumerate(b,1):now.append(min(now[-1]+1,prev[j]+1,prev[j-1]+(x!=y)))
  prev=now
 return prev[-1]/max(1,len(a))
def classify_opportunity(*, clean, delivered, near_valid, gate, mic3, future,
                         lull=None, lull_policy=None, integrity_passed=False):
    if lull_policy is not None:
        from echoear_aec_lull import RULE
        if lull_policy != RULE:raise ValueError('Unknown prospective lull policy')
    if not clean:return 'excluded-noise-or-acquisition'
    if not delivered:return 'not-delivered-no-active-tts'
    if not near_valid:return 'unscorable-near-onset'
    if gate is None:return 'unscorable-gate'
    if gate:return 'in-window'
    if mic3 == 'silent':return 'no-opportunity'
    if mic3 == 'uncertain' and lull_policy is not None and integrity_passed and lull:
        if lull.get('cancelled') is True:return 'unscorable-lull-cancelled'
        if lull.get('passed') is True:return 'ongoing-reply-lull' if future else 'unscorable-future'
    if mic3 != 'active':return 'unscorable-mic3'
    if not future:return 'unscorable-future'
    return 'eligible'


def gate_at_near(serial, blocks, onset, uncertainty=320):
    # Gate log source_lower_us is the unchanged AFE source clock, so compare
    # it with raw near-onset blocks rather than shifting a wall-clock guess.
    changes=[(int(s), state=='closed') for state,t,s,v in re.findall(
        r'GATE (closed|open) t=(\d+) source_lower_us=(-?\d+) source_valid=(\d)',serial) if v=='1']
    states=[]
    for block in blocks:
        if block[1]<=onset-uncertainty or block[0]>onset+uncertainty:continue
        prior=[state for source,state in changes if source<=block[2]]
        if not prior:return None
        states.append(prior[-1])
    return any(states) if states else None


def summarize(cases, *, lull_policy=None):
    from echoear_aec_lull import RULE, stratum_summary
    if lull_policy is not None and lull_policy != RULE:raise ValueError('Unknown prospective lull policy')
    result={}
    for volume in (0,-6):
        for kind in ('stop','sentence'):
            group=[r for r in cases if r['kind']==kind and r['volume_db']==volume]
            eligible=[r for r in group if r['opportunity']=='eligible' or lull_policy == RULE and r['opportunity']=='ongoing-reply-lull']
            counts={name:sum(r['opportunity']==name for r in group) for name in sorted({r['opportunity'] for r in group})}
            result[f'{volume}:{kind}']=dict(total=len(group),classes=counts,
                eligible=len(eligible),recognized=sum(r['recognized'] for r in eligible),
                credited=sum(r['credited_barge_in'] for r in eligible),
                in_window=[dict(id=r['id'],recognized=r['recognized'],wer=r.get('wer')) for r in group if r['opportunity']=='in-window'])
            if lull_policy is not None:result[f'{volume}:{kind}']['strata']=stratum_summary(group)
    return result


def check_render_ledger(snapshots):
    """PERF freezes the pre-write RenderTick, not the post-write DMA cursor."""
    rows=[]
    for p in snapshots:
        c=p.get('playout',{}).get('clock',{})
        completed,submitted=c.get('completed_samples'),c.get('submitted_samples')
        valid=type(completed) is int and type(submitted) is int and completed>0
        expected=640-completed%320 if valid else None
        actual=submitted-completed if valid else None
        rows.append(dict(device_us=p.get('device_us'),actual=actual,expected=expected,
                         passed=valid and actual==expected))
    return dict(passed=bool(rows) and all(r['passed'] for r in rows),snapshots=rows)


def disconnect_activity(blocks, active, anchors, fired_s):
    before=[a for a in anchors if a['upper_s']<=fired_s]
    after=[a for a in anchors if a['lower_s']>=fired_s]
    if not before or not after:return dict(passed=False,reason='unbracketed disconnect')
    lo,hi=before[-1],after[0]
    candidates=[i for i,b in enumerate(blocks) if b[3]>=lo['coordinate'] and b[2]<=hi['coordinate']]
    width=hi['upper_s']-lo['lower_s']
    return dict(passed=bool(candidates) and width<=.1 and all(bool(active[i]) and not blocks[i][4] for i in candidates),
                host_bracket=[lo['lower_s'],hi['upper_s']],device_bracket=[lo['coordinate'],hi['coordinate']],
                width_ms=width*1000,frames=len(candidates),active_frames=sum(bool(active[i]) for i in candidates))


def main(root, output_path=None, *, additional_cases=(), lull_policy=None):
    root=Path(root);saved=read(root/'result.json');plan=read(root/'declaration.json');identity=read(root/'identity.json');serial=(root/'candidate-serial.log').read_text();events=[e for p in (root/'sessions').glob('*/events.jsonl') for e in rows(p)];qref=load_capture(root/'quiet.npz')[2];result=[]
    from echoear_aec_lull import declared_rule, eligible as opportunity_eligible, case_witness, cancelled_check
    if lull_policy is not None and output_path is None:raise ValueError('Diagnostic policy override requires explicit output')
    lull_policy=lull_policy or declared_rule(plan)
    opportunity_eligible(None,lull_policy)
    if additional_cases and output_path is None:raise ValueError('Diagnostic additional cases require an explicit output path')
    for c in saved['cases']+list(additional_cases):
     folder=root/c.get('capture_dir',c['run_id']);raw,afe,ref,blocks=load_capture(folder/'capture.npz');anchors=read(folder/'anchors.json');mask=activity(ref,qref,[(0,len(ref)-1024)])
     transcripts=[e for e in c['events'] if e.get('case_id')==c['run_id'] and e.get('frame')=='TranscriptionFrame'];text=' '.join(dict.fromkeys(e.get('text') or '' for e in transcripts))
     row=dict(id=c['run_id'],cell_id=c['id'],expected_class=c.get('expected_class'),capture_dir=c.get('capture_dir',c['run_id']),kind=c['kind'],volume_db=c['volume_db'],clean=c['noise_gate_passed'] and c['quiet_windows_valid'],requested_offset_s=c['stop_offset_s'],transcript=text,resources=c['resources'],admission=c['admission'])
     selected=[e for e in events if e['kind']=='session-start' or anchors[0]['lower_s']<=e['monotonic_ns']/1e9<=anchors[-1]['upper_s']]
     row['playback']=analyze('live',folder/'capture.npz',root/'quiet.npz',dict(host_boot_id=identity['host_boot_id']),events=selected,anchors=anchors)
     if c['stop_offset_s'] is not None and not c.get('stimulus_skipped'):
      fixture=Path(c.get('fixture',plan['stop_fixture']))
      with wave.open(str(fixture)) as w:source=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(float);rate=w.getframerate()
      if rate!=16000:source=resample_poly(source,16000,rate)
      power=frame_power(source);active=np.flatnonzero(power>max(1,float(power.max())*.001));first=int(active[0])*320;template=source[first:min(len(source),first+8000)]
      j=c['jabra'];start=j['request_monotonic_s'];end=start+j['elapsed_seconds'];eligible=[int(b[0]) for b in blocks if (lambda t:t and t[1]>=start-.2 and t[0]<=end+.3)(sample_interval(int(b[0]),blocks,anchors))]
      near=match(afe[min(eligible)+1024:min(len(afe),max(eligible)+320+1024)],template) if eligible else dict(valid=False)
      if near.get('at') is not None:
       onset=min(eligible)+near['at'];near.update(raw_onset_sample=onset,host_interval=sample_interval(onset,blocks,anchors),uncertainty_samples=320)
       left=sample_interval(max(0,onset-320),blocks,anchors);right=sample_interval(min(len(raw)-1,onset+320),blocks,anchors)
       near['host_interval_with_onset_uncertainty']=[left[0],right[1]] if left and right else None
       first_frame=max(0,(onset-320)//320);last_frame=min(len(mask),(onset+320)//320+1)
       window=mask[first_frame:last_frame]
       row['mic3_state']='active' if len(window) and window.all() else ('silent' if len(window) and not window.any() else 'uncertain')
       row['mic3_active_at_near_onset']=bool(near['valid'] and row['mic3_state']=='active')
       row['in_window']=gate_at_near(serial,blocks,onset)
       # Match the independently recorded pre-onset electrical loopback to source.
       n=min(3200,onset);pcm=root/'evaluation'/(c['run_id']+'.pcm');tts=np.fromfile(pcm,dtype='<i2').astype(float) if pcm.exists() else np.array([])
       far=match(tts,ref[onset-n:onset]) if n else dict(valid=False)
       if far.get('at') is not None:
        position=far['at']+n;future=tts[position:];pp=frame_power(tts);fp=frame_power(future)
        count=int(np.sum(fp>max(1,float(pp.max())*.001))) if len(pp) and len(fp) else 0
        far.update(source_onset_sample=position,future_active_ms=count*20,verified=bool(far['valid'] and count>=5))
       row['future_speech']=far
      bracket=near.get('host_interval_with_onset_uncertainty')
      raw_bracket=near.get('host_interval')
      row['near_onset_bracket_width_ms']=(raw_bracket[1]-raw_bracket[0])*1000 if raw_bracket else None
      row['near_onset_bracket_with_uncertainty_width_ms']=(bracket[1]-bracket[0])*1000 if bracket else None
      row['near_onset']=near;row['timer']=c.get('stimulus_timer')
      row['recognized']=bool(re.search(r'\bstop\b',text,re.I)) if c['kind']=='stop' else wer(c['expected_text'],text)<=.2
      if c['kind']=='sentence':row.update(expected_text=c['expected_text'],wer=wer(c['expected_text'],text))
      interrupts=[e for e in c['events'] if e.get('case_id')==c['run_id'] and e.get('frame')=='InterruptionFrame' and e['t']>=start]
      row['interruption_after_stimulus']=bool(interrupts)
      row['near_onset_to_interruption']=interval_latency(near.get('host_interval_with_onset_uncertainty') if near['valid'] else None,[interrupts[0]['t'],interrupts[0]['t']] if interrupts else None)
      row['credited_barge_in']=bool(row['recognized'] and row['interruption_after_stimulus'] and row.get('mic3_active_at_near_onset') and row.get('future_speech',{}).get('verified'))
     if c['stop_offset_s'] is not None:
      row['post_stop_render_ledger']=check_render_ledger([read(p) for p in sorted(folder.glob('after-boundary-*.json'))])
      near=row.get('near_onset',{})
      far=row.get('future_speech',{})
      row.setdefault('recognized',False)
      if lull_policy and near.get('valid') and row.get('mic3_state')=='uncertain':
       row['lull_witness']=case_witness(root,c,row,ref,qref,blocks,anchors,events,serial)
      row['opportunity']=classify_opportunity(clean=row['clean'],delivered=not c.get('stimulus_skipped'),
          near_valid=near.get('valid',False),gate=row.get('in_window'),mic3=row.get('mic3_state'),
          future=far.get('verified',False) if far.get('valid') else None,
          lull=row.get('lull_witness'),lull_policy=lull_policy,
          integrity_passed=row['resources']['all_passed'] and row['admission']['passed'])
      row['observed_barge_in']=row.get('credited_barge_in',False)
      row['credited_barge_in']=bool(opportunity_eligible(row['opportunity'],lull_policy) and row['recognized'] and row.get('interruption_after_stimulus') and far.get('valid') and far.get('verified'))
     else:row['opportunity']='echo-only'
     row['source_sha256']={name:sha(folder/name) for name in ['capture.npz','anchors.json','result.json']};result.append(row)
    summary=summarize(result,lull_policy=lull_policy)
    missing=[c['id'] for c in plan['cases'] if not any(r['id']==c['id']+'-a1' for r in result)]
    output=dict(summary=summary,cases=result,qualification=False,
        acquisition_completed=saved['completed'],declared_cells=len(plan['cases']),
        acquired_cells=len(result),missing_cells=missing,
        integrity_failures=[r['id'] for r in result if not r['clean'] or not r['resources']['all_passed'] or not r['admission']['passed'] or not r.get('post_stop_render_ledger',{'passed':True})['passed']],
        limitation='Barge-in block only; full step-9 campaign and reviewed eligible-count gates remain required',
        policy=plan.get('near_onset_witness'),analyzer_sha256=sha(Path(__file__)))
    if lull_policy:output.update(lull_policy=lull_policy,cancelled_lull_check=cancelled_check(result))
    control=root/'silent-stop-control'
    if control.exists():
        output['silent_stop_render_ledger']=check_render_ledger([read(p) for p in sorted(control.glob('after-boundary-*.json'))])
        if not output['silent_stop_render_ledger']['passed']:output['integrity_failures'].append('silent-stop-control')
    write(Path(output_path) if output_path else root/'accept-analysis.json',output)
    print(json.dumps(summary))
    return output

if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('root',type=Path);parser.add_argument('--output',type=Path)
 args=parser.parse_args();main(args.root,args.output)

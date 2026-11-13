"""Host-only continuity witness for the frozen far-end source, at 16 kHz S16LE."""
import json
from pathlib import Path
import numpy as np
from echoear_aec_bench import sha,write
from echoear_aec_metrics import frame_power,FRAME,RATE


def measure(samples):
    """20 ms source-power frames >1024 LSB²; bridge silence of at most100 ms.

    Leading/trailing silence is not speech span. Report both the span beginning
    at first speech and the longest span; only the former qualifies the initial
    source used by fixed-offset stimuli. Incomplete final frames are excluded.
    """
    active=frame_power(samples)>1024
    positions=np.flatnonzero(active)
    runs=[];start=None
    for i,value in enumerate(active):
        if not value and start is None:start=i
        if value and start is not None:runs.append((start,i));start=None
    if start is not None:runs.append((start,len(active)))
    spans=[]
    if len(positions):
        first=previous=int(positions[0])
        for position in positions[1:]:
            position=int(position)
            if position-previous-1>5:
                spans.append((first,previous+1));first=position
            previous=position
        spans.append((first,previous+1))
    seconds=lambda count:count*FRAME/RATE
    initial=seconds(spans[0][1]-spans[0][0]) if spans else 0.
    longest=max((seconds(b-a) for a,b in spans),default=0.)
    speech_runs=[b-a for a,b in runs if len(positions) and a>positions[0] and b<=positions[-1]]
    return dict(rate=RATE,samples=len(samples),frame_ms=20,activity_power_lsb2=1024,
                silent_run_allowed_ms=100,speech_onset_s=seconds(int(positions[0])) if len(positions) else None,
                continuous_active_seconds=initial,longest_span_seconds=longest,
                max_silent_run_ms=1000*seconds(max((b-a for a,b in runs),default=0)),
                max_internal_silent_run_ms=1000*seconds(max(speech_runs,default=0)),
                spans_s=[[seconds(a),seconds(b)] for a,b in spans],passed=initial>=4)


def preflight(pcm_path,source_declaration,template):
    pcm_path=Path(pcm_path);source_declaration=Path(source_declaration)
    original=json.loads(source_declaration.read_text())
    if original['text']!=template['text']:raise ValueError('Retained source text differs from frozen campaign')
    data=pcm_path.read_bytes()
    if len(data)%2:raise ValueError('Incomplete S16LE source sample')
    value=measure(np.frombuffer(data,dtype='<i2'))
    value.update(pcm_path=str(pcm_path.resolve()),pcm_sha256=sha(pcm_path),
                 source_declaration_path=str(source_declaration.resolve()),source_declaration_sha256=sha(source_declaration),
                 text=original['text'],timing_contract=template['preflight_contract'],helper_sha256=sha(__file__))
    return value


def validate_source(p,witness):
    if sha(witness['path'])!=witness['sha256']:raise ValueError('Source preflight hash mismatch')
    saved=json.loads(Path(witness['path']).read_text())
    actual=preflight(saved['pcm_path'],saved['source_declaration_path'],p)
    if saved!=actual:raise ValueError('Source witness does not match retained PCM/metric')
    if not actual['passed'] or witness.get('continuous_active_seconds')!=actual['continuous_active_seconds']:
        raise ValueError('Source continuity below4s or declared span mismatch')
    return actual


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('pcm','source_declaration','template','output'):parser.add_argument(name,type=Path)
    args=parser.parse_args()
    value=preflight(args.pcm,args.source_declaration,json.loads(args.template.read_text()))
    write(args.output,value)
    print(json.dumps(value,indent=2))
    raise SystemExit(0 if value['passed'] else 1)

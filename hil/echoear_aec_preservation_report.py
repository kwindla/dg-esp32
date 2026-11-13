"""Read retained evidence; write only a new report directory. No bench access."""
import argparse
import hashlib
import json
from pathlib import Path
from echoear_aec_bench import load_capture,write
from echoear_aec_preservation import preservation,propose_tolerances


def main(root,out):
    root,out=Path(root),Path(out)
    out.mkdir(parents=True,exist_ok=False)
    rows=[];hashes={}
    def read(path):return json.loads(path.read_text())
    for hold in read(root/'analysis.json')['holds']:
        folder=Path(hold['run_root'])/hold['id'];indexed={r['id']:r for r in hold['cases']}
        for c in read(folder/'result.json')['cases']:
            if not c.get('fixture'):continue
            capture=folder/c.get('capture_dir',c['run_id'])/'capture.npz'
            raw,afe,_,blocks=load_capture(capture)
            anchors=read(capture.with_name('anchors.json'))
            row=indexed.get(c['run_id'],{})
            value=preservation(c,row,raw,afe,blocks=blocks,anchors=anchors)
            value.update(id=c['run_id'],voice_class=c.get('voice_class','firm'),kind=c['kind'],
                         clean=c['noise_gate_passed'] and c['quiet_windows_valid'])
            rows.append(value)
            for p in (capture,Path(c['fixture']),capture.with_name('anchors.json')):
                hashes[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
    write(out/'rows.json',rows);write(out/'input-sha256.json',hashes)
    proposal=propose_tolerances(rows)
    proposal['by_kind']={kind:propose_tolerances([r for r in rows if r['kind']==kind]) for kind in ('stop','sentence','near')}
    write(out/'proposal.json',proposal)
    print(json.dumps(dict(rows=len(rows),scorable=sum(r['scorable'] for r in rows),proposal=proposal)))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('output')
    a=p.parse_args();main(a.root,a.output)

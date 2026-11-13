"""Offline provenance for the two observed fault-bot processes and control extents."""
import json
from pathlib import Path
import re

from p4_parity import load,digest,require,write_new,checked_plan


def boundary(path):
    data=Path(path).read_bytes()
    return data.rfind(b'\n')+1


def capture_controls(paths,starts,identities,out):
    bindings=[];combined=[]
    for i,(path,start,identity) in enumerate(zip(paths,starts,identities)):
        path=Path(path).resolve()
        with path.open('rb') as stream:stream.seek(start);data=stream.read()
        data=data[:data.rfind(b'\n')+1]
        copy=Path(out)/('control-source-'+str(i)+'.jsonl')
        with copy.open('xb') as stream:stream.write(data)
        bindings.append(dict(original_file=str(path),start_byte=start,end_byte=start+len(data),copy_file=copy.name,
            sha256=digest(copy),identity=identity))
        combined.extend(dict(json.loads(line),bot_instance=identity['bot_instance']) for line in data.splitlines())
    combined.sort(key=lambda row:row['monotonic_ns'])
    with (Path(out)/'control.jsonl').open('x') as stream:
        for row in combined:stream.write(json.dumps(row)+'\n')
    return bindings


def provenance(directory,a,plan=None):
    from wire_contract import identity_schema
    directory=Path(directory).resolve()
    require(digest(a['plan_file'])==a['plan_sha256'],'liveness plan hash differs')
    plan=checked_plan(a['plan_file']) if plan is None else plan;policy=plan['fault_bot_policy']
    require(a.get('source_sha256')==plan['source_sha256'],'liveness source bindings differ')
    for name,sha in a['source_sha256'].items():require(digest(Path(__file__).parent/name)==sha,'liveness runtime source changed')
    require(a.get('helper_sha256')==digest(Path(__file__).with_name('p4_parity_liveness.py')) and
        a.get('observer_sha256')==policy['observer_sha256']==digest(Path(__file__).with_name('p4_liveness_bot.py')),
        'liveness helper/observer provenance differs')
    ready=[load(directory/'initial-ready.json'),load(directory/'replacement-ready.json')]
    identities=[load(directory/'identity.json'),load(directory/'replacement-identity.json')]
    require(a.get('bot_identities')==identities,'recorded fault identities differ')
    pids=[]
    for i,(identity,marker) in enumerate(zip(identities,ready)):
        identity_schema(identity)
        match=re.fullmatch(r'[^:]+:(\d+):(\d+)',identity['bot_instance'])
        require(match is not None,'typed fault-bot process identity required');pids.append(match[1])
        require(identity==marker.get('identity') and marker.get('observer_sha256')==policy['observer_sha256'] and
            type(marker.get('up_monotonic_ns')) is int and marker['up_monotonic_ns']>0,'observer readiness witness differs')
        require(all(identity[k]==policy[k] for k in ('bot_source_sha','policy_source_sha','profile_manifest')),
            'fault-bot source/profile differs from approved policy')
        require(identity['bot_instance']!=policy['distinct_from_product_instance'],'fault bot must be distinct from product bot')
    require(identities[0]['bot_instance']!=identities[1]['bot_instance'] and pids[0]!=pids[1],
        'replacement must be a distinct process')
    request=load(directory/'restart-request.json');requested=request.get('request_monotonic_ns')
    require(request.get('bot_instance')==identities[0]['bot_instance'] and type(requested) is int and
        ready[0]['up_monotonic_ns']<requested<ready[1]['up_monotonic_ns'] and
        ready[1]['up_monotonic_ns']-requested<=policy['handoff_seconds']*10**9,
        'restart/startup order or 180 s handoff cap differs')
    require(request.get('replacement_dir')==a['replacement_dir'],'restart replacement directory differs')
    sources={};combined=[];bindings=a.get('control_sources')
    require(isinstance(bindings,list) and len(bindings)==2,'two control source bindings required')
    for i,binding in enumerate(bindings):
        require(binding['identity']==identities[i] and binding['copy_file']=='control-source-'+str(i)+'.jsonl',
            'control source process/order differs')
        original=Path(binding['original_file']);expected_dir=Path(a['initial_bot_dir'] if i==0 else a['replacement_dir'])
        require(original.resolve()==(expected_dir/'dc-events.jsonl').resolve(),'control source directory differs')
        start,end=binding['start_byte'],binding['end_byte']
        require(type(start) is int and type(end) is int and 0<=start<end<=original.stat().st_size,'control source extent differs')
        with original.open('rb') as stream:stream.seek(start);data=stream.read(end-start)
        copy=directory/binding['copy_file']
        require(data==copy.read_bytes() and data.endswith(b'\n') and digest(copy)==binding['sha256'],
            'raw control extent/copy/hash differs')
        events=[json.loads(line) for line in data.splitlines()]
        require(events and all(type(e.get('monotonic_ns')) is int and e['monotonic_ns']>=ready[i]['up_monotonic_ns'] and
            str(e.get('channel','')).startswith(pids[i]+':') for e in events),'control event process/startup differs')
        combined.extend(dict(e,bot_instance=identities[i]['bot_instance']) for e in events)
        sources[str(copy)]=digest(copy)
        for local,name in (('initial-ready.json' if i==0 else 'replacement-ready.json','ready.json'),
                           ('identity.json' if i==0 else 'replacement-identity.json','identity.json')):
            raw=expected_dir/name
            require(load(raw)==load(directory/local),'raw observer ready/identity differs from retained copy')
            sources[str(raw.resolve())]=digest(raw)
    combined.sort(key=lambda e:e['monotonic_ns'])
    require([json.loads(line) for line in (directory/'control.jsonl').read_text().splitlines()]==combined,
        'combined control stream dropped/swapped/changed source records')
    after=False
    for session in a['sessions']:
        if session['label']=='bot-after':after=True
        i=int(after)
        require(session.get('bot_instance')==identities[i]['bot_instance'] and session['ready_ns']>=ready[i]['up_monotonic_ns'],
            'session bound to wrong fault-bot process/startup')
        require(any(e['channel']==session['channel'] and e['kind']=='display-ready' and e.get('epoch')==session['epoch'] and
            session['ready_ns']-5*10**9<=e['monotonic_ns']<=session['ended_ns'] for e in combined
            if e['bot_instance']==identities[i]['bot_instance']),'session/channel missing from bound process controls')
    before=next(s for s in a['sessions'] if s['label']=='bot-before')
    following=next(s for s in a['sessions'] if s['label']=='bot-after')
    require(before['ready_ns']<requested and following['ready_ns']>=ready[1]['up_monotonic_ns'] and
        following['ready_ns']-ready[1]['up_monotonic_ns']<=60*10**9,'restart Ready timing differs')
    for name in ('initial-ready.json','replacement-ready.json','identity.json','replacement-identity.json',
                 'restart-request.json','control.jsonl'):
        sources[str(directory/name)]=digest(directory/name)
    return dict(identities=identities,policy=policy,sources=sources)

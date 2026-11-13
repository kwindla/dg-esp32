"""Read-only, hash-bound resume state for a separately declared campaign hold."""
import json
from pathlib import Path
from echoear_aec_bench import sha
from echoear_aec_lull import declared_rule


def run_chain(declaration,seen=None):
    """Oldest first. Bind every retained file, not just a mutable result summary."""
    seen=set() if seen is None else seen
    ref=declaration.get('resume_from')
    if ref is None:return []
    root=Path(ref['path']).resolve()
    if root in seen:raise ValueError('Cyclic resume chain')
    seen.add(root)
    files={str(f.relative_to(root)):sha(f) for f in root.rglob('*') if f.is_file()}
    if not files or files!=ref['files_sha256']:raise ValueError('Prior run evidence hash mismatch')
    old=json.loads((root/'declaration.json').read_text())
    if files.get('declaration.json')!=ref['declaration_sha256']:raise ValueError('Prior declaration hash mismatch')
    # Resume cannot quietly change gates, source, candidate, or the reviewed code.
    for key in ('reviewed_spec_sha256','candidate','preflight_contract','source_preflight','cases','spares','controls','reconnects','blocks','sessions','gates','noise_gate','bot_config','failure_policy','source_preflight_contract','ongoing_reply_lull'):
        if old.get(key)!=declaration.get(key):raise ValueError('Resume contract changed: '+key)
    return run_chain(old,seen)+[root]


def resume_state(declaration):
    chain=run_chain(declaration);state={}
    for root in chain:
        for session in declaration['sessions']:
            folder=root/session['id']
            if not folder.exists():continue
            result=json.loads((folder/'result.json').read_text())
            # A controlled environmental pause finishes the active integrity audit
            # and restores before release. An unclassified/crashed hold cannot be
            # rescued by dropping its bad evidence from a resumed campaign.
            if not result.get('completed') or not result.get('lock_released') or not result.get('mixer_equal') or result.get('cleanup_errors'):
                raise ValueError('Prior hold has no clean completed integrity/cleanup audit')
            from echoear_aec_bench import NORMAL_HASH
            from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore
            if result.get('restored_elf_sha256')!=NORMAL_HASH:raise ValueError('Prior normal image restoration missing')
            if result.get('error') and result.get('error_class') not in ('environmental-pause','serial-or-host') and not (result.get('environmental_pause') and not result.get('error_class')):
                raise ValueError('Hard prior hold failure cannot be retried')
            s=state.setdefault(session['id'],dict(reconnects={},probes={}))
            for name in ('schedule','soak-state'):
                path=folder/(name+'.json')
                if path.exists():s[name]=json.loads(path.read_text())
            if (folder/'soak.json').exists():s['soak_complete']=json.loads((folder/'soak.json').read_text()).get('passed',False)
            for key in ('reconnects','probes'):
                for row in result.get(key,[]):
                    cid=row.get('cell_id',row['id'])
                    old=s[key].get(cid)
                    if old and row['attempt']<=old['attempt']:raise ValueError('Regressed/reused lifetime attempt')
                    s[key][cid]=row
    # Validate lifetime journals before the caller can mkdir or take a lock.
    from echoear_aec_campaign import Schedule,soak_cells
    for sid,entry in state.items():
        if 'schedule' in entry:
            cases=[c for c in declaration['cases']+declaration['controls'] if c['session_id']==sid and c['kind']!='probe']
            spares=[c for c in declaration['spares'] if c['session_id']==sid]
            Schedule(cases,spares,declaration['noise_gate'],entry['schedule'],lull_policy=declared_rule(declaration))
        if 'soak-state' in entry:
            prior=entry['soak-state']
            if prior.get('inflight') or prior.get('exhausted'):raise ValueError('Hard/exhausted prior soak')
            completed=set();last={}
            for r in prior['attempts']:
                if r['id'] in completed or r['attempt']!=last.get(r['id'],0)+1:raise ValueError('Soak attempt regression')
                last[r['id']]=r['attempt']
                if r.get('noise_gate_passed',r.get('noise')) is True:completed.add(r['id'])
            expected={c['id']:last.get(c['id'],0)+1 for c in soak_cells(declaration) if c['id'] not in completed}
            if {r['id']:r['attempt'] for r in prior['pending']}!=expected or any(a>3 for a in expected.values()):raise ValueError('Soak pending budget mismatch')
    return state


def remaining(cells,previous,kind):
    answer=[]
    for c in cells:
        row=previous.get(c['id'])
        if row:
            clean=row.get('noise_passed') if kind=='reconnects' else row.get('noise')
            if clean:continue # includes a scored performance miss; never reacquire it
            if row['attempt']>=3:raise ValueError('Lifetime retry budget exhausted: '+c['id'])
            c=dict(c,start_attempt=row['attempt']+1)
        answer.append(c)
    return answer


def resolve_host_interruption(hold):
    """Called only after a successful final audit; consume incomplete attempts."""
    from echoear_aec_bench import write
    from echoear_aec_campaign import Schedule
    path=hold.out/'schedule.json'
    if path.exists():
        state=json.loads(path.read_text());inflight=state.get('inflight')
        if inflight:
            state['pending'].insert(0,inflight);state['inflight']=None
            cases=[c for c in hold.p['cases']+hold.p['controls'] if c['session_id']==hold.s['id'] and c['kind']!='probe']
            spares=[c for c in hold.p['spares'] if c['session_id']==hold.s['id']]
            queue=Schedule(cases,spares,hold.p['noise_gate'],state,lull_policy=declared_rule(hold.p));group,attempt=queue.next()
            saved={c['id']:c for c in hold.result['cases'] if c['attempt']==attempt and c['id'] in {c['id'] for c in group['cells']}}
            if len(saved)==len(group['cells']):
                from echoear_aec_accept_analysis import main as analyze
                analysis=analyze(hold.out);classes={c['cell_id']:c['opportunity'] for c in analysis['cases'] if c['id'] in {r['run_id'] for r in saved.values()}}
                queue.finish(group,attempt,all(c['noise_gate_passed'] for c in saved.values()),classes)
            else:
                queue.finish(group,attempt,False,{},dict(kind='serial-or-host',retry_safe=True,error=hold.result['error']))
            write(path,queue.state())
    path=hold.out/'soak-state.json'
    if path.exists():
        state=json.loads(path.read_text());inflight=state.get('inflight')
        if inflight:
            cid,attempt=inflight['id'],inflight['attempt']
            saved=next((c for c in hold.result['cases'] if c['id']==cid and c['attempt']==attempt),None)
            clean=saved['noise_gate_passed'] if saved else False
            if not clean:
                if attempt<3:state['pending'].append(dict(id=cid,attempt=attempt+1))
                else:state['exhausted'].append(cid)
            state['attempts'].append(dict(id=cid,attempt=attempt,noise=clean,action='accepted' if clean else 'host-interrupted',error_class='serial-or-host'))
            state['inflight']=None;write(path,state)
    aux=hold.result.pop('inflight_aux',None)
    if aux:
        cid,attempt=aux['cell_id'],aux['attempt']
        value=dict(id=cid if aux['kind']=='probes' else f'{cid}-a{attempt}',cell_id=cid,attempt=attempt,noise=False,noise_passed=False,passed=False,error_class='serial-or-host')
        hold.result.setdefault(aux['kind'],[]).append(value)

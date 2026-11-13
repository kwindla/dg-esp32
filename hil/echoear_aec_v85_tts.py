"""Bounded vendor-generation retakes; never a transport/performance waiver."""
POLICY=dict(rule='long-tts-v8.5',lead_s_gte=.8,max_campaign_retakes=2,
    shared_cell_retries=2,noise_streak='neutral',unit='whole-capture-group-or-reconnect-cycle',
    soak_retry='after-original-600s',incomplete_witness='stop',
    interpretation='coordinator interpretation of user decisions 3–5, 2026-10-18')


def inspect(events,recorded,case_ids):
    from echoear_aec_campaign_analysis import generation_audit
    own=[e for e in events if e.get('case_id') in case_ids]
    starts=[e for e in own if e['event']=='tts-generation-metric-start']
    keys={(e.get('pc_id'),e.get('context_id')) for e in starts}
    writes=[e for e in recorded if (e.get('pc_id'),e.get('context_id')) in keys]
    audit=generation_audit(own,writes)
    generations=[g for s in audit['sessions'] for g in s['generations']]
    long=[g for g in generations if isinstance(g.get('lead_s'),(int,float)) and g['lead_s']>=.8]
    closed=all(sum(e['event'] in ('tts-generation-stopped','tts-interruption-stopped') and
                   (e.get('pc_id'),e.get('context_id'))==key for e in own)==1 for key in keys)
    complete=bool(generations) and set(case_ids).issubset({g['case_id'] for g in generations}) and closed and all(g['complete'] for g in generations) and all(not s['problems'] and not s['active_errors'] for s in audit['sessions'])
    return dict(complete=complete,long_generations=long,case_ids=list(case_ids),audit=audit)


def admit(saved,attempt,unit,witness,integrity):
    """Caller persists even refused admission before raising; no score inputs."""
    previous=saved.setdefault('long_tts_retries',[])
    used=sum(r['admitted'] for r in previous)
    ok=bool(witness['long_generations'] and witness['complete'] and integrity and attempt<=2 and used<2)
    record=dict(id=unit,attempt=attempt,kind='long-tts',admitted=ok,retry_safe=ok,
                campaign_retakes_before=used,noise_streak='neutral',witness=witness,
                integrity_passed=bool(integrity))
    previous.append(record)
    return record


def retry(queue,trial,attempt):
    # Neutral for the consecutive noise streak; same lifetime attempt number.
    if attempt>queue.policy['max_retries']:
        queue.exhausted.append(trial['run_id']);return 'retries-exhausted'
    queue.pending.append((trial,attempt+1));return 'long-tts-retry-queued'


def exclusions(saved,events=None,recorded=None,root=None):
    records=saved.get('long_tts_retries',[])
    accepted=[r for r in records if r.get('admitted')]
    if len(accepted)>2 or any(not r.get('retry_safe') or not r.get('integrity_passed') or r['attempt']>2 or not r['witness']['complete'] for r in accepted):
        raise ValueError('Invalid long-TTS exclusion journal')
    if events is not None:
        import json
        from pathlib import Path
        for r in accepted:
            actual=inspect(events,recorded,r['witness']['case_ids'])
            if not actual['complete'] or actual['long_generations']!=r['witness']['long_generations'] or [[g for g in s['generations']] for s in actual['audit']['sessions']]!=[[g for g in s['generations']] for s in r['witness']['audit']['sessions']]:raise ValueError('Long-TTS generation journal differs from retained events')
            folder=Path(root)/(r['id']+'-a'+str(r['attempt']))
            path=folder/'acquisition-failure.json'
            if path.exists():
                proof=json.loads(path.read_text())
                clean=(proof.get('retry_safe') and proof.get('resources',{}).get('all_passed') and
                       all(proof.get(k,{}).get('passed') for k in ('transport','delivery','admission')) and proof.get('observer_passed'))
            else:
                proof=json.loads((folder/'result.json').read_text())
                clean=proof.get('passed') and len(proof.get('epochs',[]))==2 and all(e['passed'] for e in proof['epochs'])
            if not clean:raise ValueError('Missing clean whole-unit long-TTS integrity evidence')
    return {(g['case_id'],g['context_id']) for r in accepted for s in r['witness']['audit']['sessions'] for g in s['generations']}

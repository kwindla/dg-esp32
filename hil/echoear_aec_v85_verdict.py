"""Single-hold conjunctive acceptance; report-only exposure never fills a gate."""

def verdict(p,holds):
    from echoear_aec_lull import eligible,cancelled_check,stratum_summary,declared_rule
    policy=declared_rule(p)
    rows=[r for h in holds if h.get('acceptance_credit',False) for r in h.get('cases',[])]
    clean=[r for r in rows if r.get('clean')];gated=[r for r in clean if not r.get('report_only')]
    required={c['id'] for c in p['cases']+p['controls'] if c['kind']!='probe'}
    seen={r.get('cell_id') for r in clean};scores={}
    for kind in ('stop','sentence'):
        selected=[r for r in gated if r['kind']==kind and r.get('expected_class')=='eligible' and eligible(r.get('opportunity'),policy)]
        success=sum(r.get('barge_success',False) for r in selected)
        scores[kind]=dict(eligible=len(selected),successes=success,required_eligible=10,passed=len(selected)==10 and success>=9)
    echo=[r for r in gated if r['kind']=='echo'];quick=sum(r.get('quick_gap',{}).get('witnessed',False) for r in echo)
    reconnect=[r for h in holds for r in h.get('reconnects',[]) if r.get('noise_passed',True) and not r.get('long_tts_excluded')]
    probes=[r for h in holds for r in h.get('probes',[]) if r.get('noise')]
    soak=[h.get('soak') for h in holds if h.get('soak')]
    cancel=cancelled_check([r for h in holds for r in h.get('cases',[])])
    unique=[r.get('cell_id',r['id']) for r in clean]
    complete=bool(not required-seen and len(unique)==len(set(unique)) and all(s['passed'] for s in scores.values()) and
        len(echo)==10 and quick>=1 and sum(r['kind']=='near' for r in gated)==4 and sum(r['kind']=='silent-cancel' for r in gated)==1 and
        sum(r['kind']=='soak-echo' for r in gated)==10 and len(reconnect)==2 and all(r['passed'] for r in reconnect) and
        len(probes)==1 and all(r['passed'] for r in probes) and len(soak)==1 and soak[0]['passed'])
    passed=bool(len(holds)==1 and holds[0].get('id')=='qualification-01' and holds[0].get('passed') and complete and cancel['passed'])
    # Strata are descriptive across all retained rows, including a failed hold.
    allrows=[r for h in holds for r in h.get('cases',[])]
    exports=[dict(hold=h.get('id'),pc_id=e['pc_id'],generation=e['generation'],**r) for h in holds for e in h.get('epoch_checks',[]) for r in e.get('checks',{}).get('transport',{}).get('intervals',[]) if r.get('scope')=='export']
    exposure=sum((r['after_device_us']-r['before_device_us'])/1e6 for r in exports)
    events=sum(not r['transport']['passed'] or not r['delivery']['passed'] for r in exports)
    return dict(export_event_rate=dict(report_only=True,intervals=exports,exposure_seconds=exposure,event_intervals=events,event_intervals_per_hour=events*3600/exposure if exposure else None),passed=passed,qualification=passed and p.get('qualification') is True,qualified_volumes_db=[-6] if passed else [],
        complete_denominators=complete,barge_in=scores,missing_declared_ids=sorted(required-seen),quick_pairs_witnessed={'-6':quick},
        cancelled_lull_check=cancel,opportunity_strata=stratum_summary(allrows),default_changed=False,
        report_only_exposures=[r for r in allrows if r.get('report_only')],
        long_tts_retakes=[r for h in holds for r in h.get('cleanup',{}).get('long_tts_retries',[])])

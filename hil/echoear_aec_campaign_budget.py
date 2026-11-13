"""Derived v8.3.1 schedule/retention budgets; no new eligibility or retry rules."""
import math


def budget(p):
    from echoear_aec_campaign import capture_groups
    attempts=1+p['noise_gate']['max_retries'];sessions={}
    for s in p['sessions']:
        sid=s['id']
        cells=[c for c in p['cases']+p['spares']+p['controls'] if c['session_id']==sid and c['kind']!='probe']
        groups=len(capture_groups(cells))
        probes=sum(c['session_id']==sid and c['kind']=='probe' for c in p['controls'])
        reconnects=sum(c['session_id']==sid for c in p['reconnects'])
        soak=10 if s['role']=='soak' else 0
        # Reconnect trials contain two independently exported capture legs.
        captures=attempts*(groups+probes+2*reconnects+soak)
        # 90 s remains the prior planning allowance per capture. Max-attempt
        # planning is not a guaranteed wall-clock bound (analysis/I/O unbounded).
        planned=captures*90+600+240+(600 if soak else 0)
        # Conservative timeout/overhead envelope, not a new runtime stop:
        # 28 capture +150 export +5 idle +60 boundary I/O +30 host analysis;
        # 10 s recovery allowance for each admitted retry, plus baseline attempts.
        envelope=captures*273+(attempts-1)*(groups+probes+reconnects+soak)*10+600+240+3*(5+6+150)+(600 if soak else 0)
        # These observer caps are lifetime per association, NOT drained rings.
        # 100 records/s covers 50Hz uplink plus DTLS/control metadata allowance.
        cap=max(200000,math.ceil(envelope*100/100000)*100000)
        sessions[sid]=dict(capture_groups=groups,probe_ids=probes,reconnect_ids=reconnects,soak_ids=soak,
            max_attempts_per_id=attempts,max_capture_legs=captures,planning_all_attempts_s=planned,
            timeout_overhead_envelope_s=envelope,rtp_evidence_max_packets=cap)
    return dict(rule='spare-budget-v8.3.1',sessions=sessions,
        total_planning_all_attempts_s=sum(v['planning_all_attempts_s'] for v in sessions.values()),
        total_timeout_overhead_envelope_s=sum(v['timeout_overhead_envelope_s'] for v in sessions.values()),
        hard_wallclock_bound_s=None,
        limitation='No finite hard wall-clock maximum: host analysis/I/O, environmental holds/resumes and restore-failure lock retention are not bounded. Envelope is explicit planning, not a timeout guarantee.',
        available_0415_to_next_0300_s=81900,
        idle_probe_s=294,per_capture_cap_s=28,ordinary_export_timeout_s=150,
        noise_harness_retries_per_id=p['noise_gate']['max_retries'],pause_distinct_consecutive=p['noise_gate']['pause_consecutive_cells'],
        environmental_caps=[p['failure_policy']['transport_environmental']['max_per_hold'],p['failure_policy']['transport_environmental']['max_per_campaign']])


def validate_budget(p):
    declared=p.get('time_estimate',{}).get('v831_budget')
    if declared is not None and declared!=budget(p):raise ValueError('Derived schedule/retention budget changed')

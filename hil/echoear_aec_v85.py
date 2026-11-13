"""Prospective single-hold v8.5 contract and budget. Import is host-only."""
import copy
import math
from pathlib import Path
from echoear_aec_v85_transport import POLICY as TRANSPORT
from echoear_aec_v85_tts import POLICY as TTS

POLICY=dict(rule='single-hold-v8.5',transport=TRANSPORT,long_tts=TTS,
    reconnect_window_s=[1.0,1.5],reconnect_new_reply='requested-completion-plus-2.3s-within-28s',capture_end_perf=True,host_boundary_not_reached=True,
    no_link_retakes=True,resume_allowed=False,
    rationale='coordinator interpretation of user decisions 3–5, 2026-10-18')
TEMPLATE='hil/plans/echoear-aec-accept-v8.5.json'


def budget(p):
    from echoear_aec_campaign import capture_groups
    core=len(capture_groups(p['cases']+ [c for c in p['controls'] if c['kind']!='probe']))
    spare=len(capture_groups(p['spares']))
    # 65 s per ordinary capture reflects retained c09 capture+export cadence;
    # planning only. Two reconnect cycles use 90 s each, canonical probe 45 s.
    nominal=core*65+2*90+45+600+240+300
    max_attempts=3*(core+spare+2+1+10)
    # 28 capture + 150 export + 60 boundaries + 30 analysis + 5 idle + 10 recovery.
    # Long-TTS retakes consume the same max-three attempts; not extra attempts.
    envelope=max_attempts*283+600+240+300+3*(5+6+150)
    cap=max(200000,math.ceil(envelope*100/100000)*100000)
    return dict(rule='single-hold-budget-v8.5',nominal_s=nominal,nominal_capture_s=65,
        ordinary_groups=core,spare_groups=spare,reconnect_cycles=2,canonical_probes=1,
        soak_seconds=600,max_attempts=max_attempts,timeout_overhead_envelope_s=envelope,
        hard_wallclock_bound_s=None,rtp_evidence_max_packets=cap,
        limitation='Planning only; analysis, I/O, and failed-restore lock retention are not hard-bounded. The same per-association retention cap applies to uplink/downlink/SCTP; no drained-ring assumption.')


def make_plan(base):
    from echoear_aec_host_gate import V85_POLICY
    p=copy.deepcopy(base);p['v85']=copy.deepcopy(POLICY)
    p['bot_config']['ECHOEAR_STT_PROFILE']='dev'
    firm=[c for c in p['cases'] if c['session_id']=='qualification-01' and c.get('expected_class')!='report-actual-guard-overlap']
    echo=[dict(c,report_only=True) for c in p['cases'] if c['volume_db']==0 and c['kind']=='echo'][:3]
    normal=[]
    for kind in ('stop','sentence'):
        normal += [dict(c,report_only=True) for c in p['cases'] if c.get('voice_class')=='normal' and c['kind']==kind][:5]
    p['cases']=firm+echo+normal
    p['spares']=[c for c in p['spares'] if c['volume_db']==-6 and c.get('voice_class','firm')=='firm']
    p['controls']=[c for c in p['controls'] if c['session_id']=='qualification-01']
    p['reconnects']=p['reconnects'][:2]
    for key in ('cases','spares','controls','reconnects'):
        for c in p[key]:c['session_id']='qualification-01'
    for c in p['reconnects']:
        c['active_speech_window_s']=[1.,1.5]
        c.pop('second_reply_window_seconds',None)
        c['second_reply_completion']='requested-completion-plus-2.3s-within-28s'
    p['sessions']=[dict(p['sessions'][0],role='combined',cold_definition='One candidate boot; first -6 echo cold. Report-only 0dB and normal voice warm; existing soak follows in same restorable hold.')]
    soak=copy.deepcopy(next(b for b in p['blocks'] if b['id']=='soak'));soak['session_id']='qualification-01'
    p['blocks']=[dict(id='combined',order=['gated-firm','reported-exposure','reconnect','soak','finish','canonical-probe'],
        design='Canonical probe remains after transport disconnect; one flash, idle link probe and restore. No reflash before soak.'),soak]
    p['host_environment']=copy.deepcopy(V85_POLICY)
    p['gates']['barge_in']['gated_voice_classes']=['firm']
    p['gates']['barge_in']['voice_class_rule']='Only -6 dB firm requires ten eligible jointly across strict/lull strata and nine successes per kind. Five normal-voice acquisitions per kind are reported, not replenished. Cancelled lull count remains zero.'
    p['gates']['false_turns']['minimum_core_echo_per_volume']={'-6':10}
    p['gates']['false_turns']['scope']='Gated -6 dB firm core and soak echo only; three 0 dB echoes are report-only and never replace core cells.'
    p['gates']['reconnect']['cycles']=2
    p['gates']['reconnect']['second_reply']='Requested new reply finishes plus 2.3 seconds quiet, within unchanged 28-second cap. Coordinator acquisition fix, no threshold change (freeze-answers item 3).'
    p['gates']['reconnect']['active_tts_offset_s']=[1.,1.5]
    p['gates']['reconnect']['active_disconnect']='First fresh non-silent requested-context/peer write in +1.0..+1.5s; 120ms freshness. Independent active MIC3 bracket <=100ms unchanged; skipped stimulus stops without retry.'
    p['gates']['proactive_tts']['long_generations_max']='at most two admitted whole-unit retakes; all generations retained'
    p['gates']['proactive_tts']['metric_coverage']='Every generation complete; lead >=0.8s is excluded only by a clean, complete, budgeted long-tts admission journal. All other lifecycle gates unchanged.'
    p['failure_policy']['acquisition_errors']+=' v8.5 long-TTS retakes follow v85.long_tts; stimulus-precondition stops without retry.'
    p['failure_policy']['retakes']='Shared <=2 noise/harness/long-TTS re-attempts per cell; <=2 long-TTS admissions campaign-wide, neutral for noise streak. Firm Stop/sentence spares 7/6. No link-event retakes. Report-only cells have no opportunity spares.'
    p['failure_policy']['harness_retries']['classes'].append('long-tts')
    p['failure_policy']['transport_environmental']['enabled']=False
    p['time_estimate']=dict(v85_budget=budget(p))
    p['declaration_id']='step-9-acceptance-10-v8.5'
    p['status']='draft';p['qualification']=False;p['campaign_authorized']=False;p['resume_from']=None
    p['implementation_status']='v8.5 single-hold runner and measurement policy; no firmware change; freeze prepared only, never launched by freeze helper.'
    p['declaration_policy']='User decisions 1–6 plus adopted reviewed ongoing_reply_lull; coordinator interpretation of user decisions 3–5, 2026-10-18. No retrospective credit.'
    return p


def validate(p):
    from echoear_aec_host_gate import V85_POLICY,CADENCE
    from echoear_aec_lull import declared_rule,RULE
    from echoear_aec_noise_gate import validate_noise_gate
    from echoear_aec_transport_policy import validate_policy
    if p.get('v85')!=POLICY:raise ValueError('v8.5 policy changed')
    if declared_rule(p)!=RULE:raise ValueError('Exact adopted lull rule required')
    validate_noise_gate(p['noise_gate']);validate_policy(p['gates']['transport'])
    if p['host_environment']!=V85_POLICY or p['first_hold_periodicity']!=CADENCE:raise ValueError('Host policy changed')
    if p['boot_ref_gain_db']!=15 or p['gain_db']!=15 or p['capture_cap_seconds']!=28 or p['gated_volume_db']!=-6:raise ValueError('Candidate gain/capture/volume changed')
    if [(s['id'],s['role']) for s in p['sessions']]!=[('qualification-01','combined')]:raise ValueError('Single combined hold required')
    ids=[c['id'] for key in ('cases','spares','controls','reconnects') for c in p[key]]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate IDs')
    for kind in ('stop','sentence'):
        firm=[c for c in p['cases'] if c['kind']==kind and not c.get('report_only')]
        normal=[c for c in p['cases'] if c['kind']==kind and c.get('report_only')]
        spare=[c for c in p['spares'] if c['kind']==kind]
        if (len(firm),len(normal),len(spare))!=(10,5,7 if kind=='stop' else 6):raise ValueError('v8.5 voice count/spares changed')
    if [(v,sum(c['kind']=='echo' and c['volume_db']==v for c in p['cases'])) for v in (-6,0)]!=[(-6,10),(0,3)]:raise ValueError('Echo counts changed')
    if sorted(c['kind'] for c in p['controls'] if c['kind']!='host-interrupt')!=['near']*4+['probe','silent-cancel']:raise ValueError('Controls changed')
    from echoear_aec_host_interrupt import validate as validate_host_interrupt
    for c in p['controls']:
        if c['kind']=='host-interrupt':validate_host_interrupt(c)
    if len(p['reconnects'])!=2 or any((c.get('active_speech_window_s')!=[1,1.5] or c.get('second_reply_completion')!=POLICY['reconnect_new_reply'] or 'second_reply_window_seconds' in c) for c in p['reconnects']):raise ValueError('Reconnect contract changed')
    if any(c['session_id']!='qualification-01' or c['guard_ms']!=400 or c['volume_db'] not in (-6,0) for key in ('cases','spares','controls','reconnects') for c in p[key]):raise ValueError('Cell hold/guard/volume changed')
    soak=next(b for b in p['blocks'] if b['id']=='soak')
    if soak['duration_s']!=600 or len(soak['prompts'])!=10:raise ValueError('Soak changed')
    if p['time_estimate']!={'v85_budget':budget(p)}:raise ValueError('v8.5 budget mismatch')
    if p.get('resume_from') is not None:raise ValueError('v8.5 resume not authorized')
    return True

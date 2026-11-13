"""Offline gates for overnight item4; no hardware or network on import.

ERLE inputs must come from the same capture with an independently measured
AFE delay. The scorer never estimates a delay that maximizes cancellation.
Default half-duplex/muted captures cannot qualify echo or barge-in.
"""
import math
import re
import numpy as np


def erle(microphone,processed,reference,*,delay_samples=None,rate=16000):
    arrays=[np.asarray(x,dtype=float).reshape(-1) for x in (microphone,processed,reference)]
    invalid=(type(delay_samples) is not int or delay_samples<0 or delay_samples>rate or rate!=16000
             or len({len(x) for x in arrays})!=1 or not len(arrays[0])
             or any(not np.isfinite(x).all() for x in arrays))
    if invalid:return dict(available=False,passed=False,reason='Aligned finite 16 kHz captures and measured delay required')
    mic,out,ref=arrays
    if delay_samples:
        mic,ref,out=mic[:-delay_samples],ref[:-delay_samples],out[delay_samples:]
    size=len(mic)//320
    if not size:return dict(available=False,passed=False,reason='No complete 20 ms frame')
    mic,out,ref=[x[:size*320].reshape(size,320) for x in (mic,out,ref)]
    power=lambda x:np.mean(x*x,axis=1)
    active=power(ref)>32768**2*10**(-45/10)
    if np.count_nonzero(active)<5:return dict(available=False,passed=False,reason='Insufficient reference-active frames')
    # One LSB power is a finite lower bound for an all-zero residual.
    values=10*np.log10(np.maximum(power(mic)[active],1)/np.maximum(power(out)[active],1))
    median=float(np.median(values))
    saturated=int(np.count_nonzero(np.abs(mic)>=32767)+np.count_nonzero(np.abs(out)>=32767)+np.count_nonzero(np.abs(ref)>=32767))
    return dict(available=True,passed=median>=20 and saturated==0,median_db=median,
                active_frames=int(active.sum()),delay_samples=delay_samples,saturated_samples=saturated,
                residual_floor_lsb_squared=1,target_db=20)


def _full_duplex(row):
    return (all(row.get(name) is True for name in ('aec_enabled','bot_full_duplex','firmware_full_duplex'))
            and all(row.get(name) is False for name in ('ns_enabled','agc_enabled'))
            and row.get('gain_matched') is True)


def _integrity(row):
    return all(row.get(name,{}).get('passed') is True for name in ('transport','clips','uplink','admission'))


def echo_campaign(rows,expected_ids):
    identities=[r.get('id') for r in rows]
    complete=(len(expected_ids)>=10 and len(set(expected_ids))==len(expected_ids)
              and len(identities)==len(expected_ids) and set(identities)==set(expected_ids))
    available=complete and all(r.get('erle',{}).get('available') is True
        and type(r['erle'].get('median_db')) in (int,float) and math.isfinite(r['erle']['median_db']) for r in rows)
    median=float(np.median([r['erle']['median_db'] for r in rows])) if available else None
    checks=dict(complete=complete,full_duplex=all(_full_duplex(r) for r in rows),
                no_false_turns=all(type(r.get('false_user_turns')) is int and r['false_user_turns']==0 for r in rows),
                no_saturation=all(r.get('erle',{}).get('saturated_samples')==0 for r in rows),
                integrity=all(_integrity(r) for r in rows),
                median_erle=median is not None and math.isfinite(median) and median>=20)
    return dict(passed=all(checks.values()),checks=checks,median_reply_erle_db=median,
                replies=len(rows),target_db=20)


def barge_campaign(rows,expected_ids):
    words=lambda s:re.findall(r'\w+',s.lower())
    correct=[bool(words(r.get('expected',''))) and words(r.get('transcript',''))==words(r['expected']) for r in rows]
    latencies=[]
    for r in rows:
        begin,end=r.get('jabra_user_onset_s'),r.get('jabra_bot_end_s')
        value=end-begin if type(begin) in (int,float) and type(end) in (int,float) else None
        latencies.append(value)
    checks=dict(complete=len(rows)==len(expected_ids)==5 and len(set(expected_ids))==5
                    and {r.get('id') for r in rows}==set(expected_ids),
                full_duplex=all(_full_duplex(r) for r in rows),
                acoustic_stop=all(x is not None and math.isfinite(x) and 0<=x<=1 for x in latencies),
                transcription=sum(correct)>=4,integrity=all(_integrity(r) for r in rows))
    return dict(passed=all(checks.values()),checks=checks,stop_latencies_s=latencies,
                correct=sum(correct),required_correct=4,trials=len(rows),stop_limit_s=1)


def admission_counters(before, after):
    """Counter integrity only; does not establish end-to-end delivery."""
    initial_received = before.get('data_admission', {}).get('received')
    initial_dispatched = before.get('data_admission', {}).get('dispatched')
    a = before.get('drop_reasons', {}).get('admission_budget')
    b = after.get('drop_reasons', {}).get('admission_budget')
    received = after.get('data_admission', {}).get('received')
    dispatched = after.get('data_admission', {}).get('dispatched')
    valid = (all(type(v) is int and v >= 0 for v in (a,b,received,dispatched,initial_received,initial_dispatched))
             and before.get('generation') == after.get('generation')
             and before.get('pc_id') is not None
             and before.get('generation') is not None
             and before.get('pc_id') == after.get('pc_id') and b >= a
             and received >= initial_received and dispatched >= initial_dispatched)
    delta = b-a if valid else None
    received_delta = received-initial_received if valid else None
    dispatched_delta = dispatched-initial_dispatched if valid else None
    return dict(passed=bool(valid and delta == 0 and received == dispatched),
                received_delta=received_delta, dispatched_delta=dispatched_delta,
                admission_budget_delta=delta, received=received, dispatched=dispatched,
                same_session=before.get('generation') == after.get('generation')
                    and before.get('pc_id') == after.get('pc_id'))


def live_admission(before, after, *, recorder_messages=None):
    """Quiescent per-reply check; missing recorder counts fail closed.

    Count outgoing RTVI within the same host-time window. Only an explicitly
    quiet window may supply zero. Extra device messages are allowed; a short
    receive or dispatch delta fails even when the counters equal each other.
    """
    result = admission_counters(before, after)
    delivery = (result['passed'] and type(recorder_messages) is int
                and recorder_messages >= 0
                and result['received_delta'] >= recorder_messages
                and result['dispatched_delta'] >= recorder_messages)
    return dict(result, passed=bool(delivery), recorder_messages=recorder_messages,
                delivery_checked=recorder_messages is not None)


def outgoing_rtvi_count(events, start_ns, end_ns):
    """Count recorder output, excluding input copies and duplicate observers."""
    return sum(e.get('kind') == 'rtvi-output' and e.get('source') == 'RTVIProcessor'
               and e.get('direction') == 'DOWNSTREAM'
               and start_ns <= e.get('monotonic_ns', -1) < end_ns for e in events)

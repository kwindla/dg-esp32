"""Step 15d validation rules and offline statistics; no device IO."""
import numpy as np
from scipy.stats import beta

import echoear_aec_alignment as alignment
import p4_aec_policy as policy
from p4_aec_reselect import direction_prompt_score
from echoear_hot_stt import word_errors
from echoear_aec_scoring import reply_erle
from wire_contract import require

RULES=dict(id='step-15d-validation-v1',qualification=False,
    delay=dict(repetitions=3,minimum_passing=2,adopted_samples=1024,tolerance_samples=16,
        search_samples=[0,4096],minimum_correlation=.6,minimum_separation=.05,
        extra_capture_samples=16000,method='unchanged independent S3 delay_score; all source/silence/clip gates retained'),
    guard=dict(repetitions_per_condition=5,guard_ms=400,rearm_ms=2000,frame_ms=20,
        conditions=['in-guard','outside-guard','gap-1800','gap-2200'],pass_rule='every repetition matches; no replacement'),
    double_talk=dict(sets=3,trials_per_prompt_per_set=5,prompts=['france','speech'],reference_gain_db=30,
        report_only=True,reselection=False,stt_trial_accuracy_min=.9,
        missed_injections='report count and sensitivity treating authenticated missed injections as failures; other unavailable trials remain separate',
        intervals='two-sided 95% Clopper-Pearson on trial hits; individual, binomial independence assumed',
        caveat='P4 placement unchanged; localized near-end weakness is carried into these results'))


def schedule():
    rows=[]
    for rep in range(3):rows.append(dict(id=f'delay-{rep+1:02d}',part=f'delay-{rep}',kind='delay'))
    for group in range(3):
        for rep in range(5):
            # Alternate the first prompt between sets and pairs; always five of each.
            prompts=('france','speech') if (group+rep)%2==0 else ('speech','france')
            for prompt in prompts:rows.append(dict(id=f'set-{group+1}-{rep+1}-{prompt}',part=f'double-talk-{group}',
                kind='double-talk',prompt=prompt,set=group+1,repetition=rep+1))
    return rows


def delay_trial(dac,near,source):
    minimum=RULES['delay']['extra_capture_samples']+4096
    coverage=dict(dac_samples=len(dac),near_samples=len(near),
        dac_required=len(alignment.probe_pcm())+minimum,near_required=len(source)+minimum)
    if len(dac)<coverage['dac_required'] or len(near)<coverage['near_required']:
        return dict(available=False,passed=False,measured_delay_samples=None,coverage=coverage,
            reason='full source plus complete 0..4096 search and one second margin required')
    return dict(policy.delay_score(dac,near,source),coverage=coverage)


def delay_summary(trials):
    require(len(trials)==3,'exactly three declared delay observations required (missing is unavailable)')
    passing=[]
    for row in trials:
        if row.get('available') is True:
            value=row.get('measured_delay_samples')
            require(row.get('passed') is True and type(value) is int and 0<value<4096 and
                type(row.get('correlation')) in (int,float) and np.isfinite(row['correlation']) and row['correlation']>=.6 and
                type(row.get('separation')) in (int,float) and np.isfinite(row['separation']) and row['separation']>=.05,
                'invalid passing delay observation')
            passing.append(value)
    consumed=sum(row.get('started',True) is True for row in trials)
    median=float(np.median(passing)) if consumed==3 and len(passing)>=2 else None
    outcome=('unavailable' if median is None else
        'measured-confirmed' if abs(median-1024)<=16 else 'measured-different')
    return dict(outcome=outcome,delay_samples=median,passing_repetitions=len(passing),planned_repetitions=3,consumed_repetitions=consumed,
        stop_for_user_decision=outcome=='measured-different',trials=trials,qualification=False,
        basis='median of passing repetitions; no ERLE optimization; firmware delay unchanged')


def exact_rate(hits,total):
    require(type(hits) is int and type(total) is int and 0<=hits<=total,'integer binomial counts required')
    return dict(hits=hits,total=total,rate=hits/total if total else None,
        interval_95=[float(beta.ppf(.025,hits,total-hits+1)) if hits else 0.,
            float(beta.ppf(.975,hits+1,total-hits)) if hits<total else 1.] if total else None,
        method='two-sided 95% Clopper-Pearson; trial is unit; independent identical binomial observations assumed')


def missed_injection(original):
    playback=original.get('near_playback')
    if playback is None or original.get('injection_status')=='missed-bot-playback':return True
    request=playback.get('request_monotonic_s');start=original.get('bot_started_ns');stop=original.get('bot_stopped_ns')
    known=all(type(value) in (int,float) and np.isfinite(value) for value in (request,start,stop))
    return bool(known and start>0 and stop>start and not start<request*1e9<stop)


def double_talk_trial(spec,original,pcm,quiet):
    policy.reference_gain(original['before'],original['after'],30)
    playback=original.get('near_playback')
    prompt=direction_prompt_score(spec,original['events'],playback or dict(request_monotonic_s=0),
        original.get('bot_started_ns') or 0,original.get('bot_stopped_ns') or 0)
    import board_smoke as smoke
    smoke.downstream_frames(original['events'])
    events=[e for e in original['events'] if e.get('kind')!='frame' or e['direction']=='DOWNSTREAM']
    request=playback['request_monotonic_s']*1e9 if playback else None
    # An interruption must belong to a bot-active injected turn, not its setup or a later reply.
    stopped=original.get('bot_stopped_ns')
    interrupted=bool(request is not None and stopped is not None and any(e.get('kind')=='frame' and
        e.get('frame')=='InterruptionFrame' and e.get('bot_active') is True and request<=e['monotonic_ns']<=stopped
        for e in events))
    try:
        require(original['complete'] is True,'incomplete declared reply capture')
        erle=reply_erle(pcm,quiet_reference=quiet[:,2],delay_samples=1024,reply_start=0,reply_end=len(pcm)-1024)
    except ValueError as exc:erle=dict(available=False,median_db=None,error=str(exc))
    clips=policy.clip_evidence(original['before'],original['after'],pcm)
    # A missed injection is an observed protocol mismatch, not a successful speech trial.
    protocol=bool(playback and playback.get('duration_valid') is True and prompt['playback_overlap'])
    return dict(observed=protocol,protocol_matched=protocol,
        missed_injection=missed_injection(original),interruption=interrupted if protocol else None,
        stt_hit=prompt['accuracy']>=.9 if protocol else None,exact_transcript=prompt['available'] and word_errors(spec['expected'],prompt['received'])['edits']==0 if protocol else None,
        word_accuracy=prompt['accuracy'] if protocol else None,transcript=prompt['received'],prompt=prompt,
        erle=erle,median_erle_db=erle.get('median_db'),clips=clips,report_only=True,
        erle_scope='unchanged reply_erle on echo-active capture frames; includes double-talk mixture, not isolated echo',
        delay_kind='decision-backed 1024; V1 validation is reported separately',
        overlap_basis='existing host playback-request versus bot-event timing; physical acoustic overlap is not independently established')


def double_talk_summary(trials,planned):
    require(len(trials)==planned,'include all planned trials; missing observations stay explicit')
    seen=[r for r in trials if r.get('observed') is True]
    missed=[r for r in trials if r.get('observed') is not True and r.get('missed_injection') is True]
    sensitivity_total=len(seen)+len(missed)
    sensitivity=dict(interruption=exact_rate(sum(r['interruption'] is True for r in seen),sensitivity_total),
        stt_accuracy_ge_90_percent=exact_rate(sum(r['stt_hit'] is True for r in seen),sensitivity_total),
        exact_transcript=exact_rate(sum(r['exact_transcript'] is True for r in seen),sensitivity_total),
        missed_injections_counted_as_failures=len(missed),
        other_unavailable_excluded=planned-sensitivity_total,
        basis='observed trials plus authenticated missed injections; missed injections count as zero hits')
    return dict(planned=planned,observed=len(seen),unavailable=planned-len(seen),complete=len(seen)==planned,
        missed_injections=len(missed),sensitivity_missed_injections_as_failures=sensitivity,
        interruption=exact_rate(sum(r['interruption'] is True for r in seen),len(seen)),
        stt_accuracy_ge_90_percent=exact_rate(sum(r['stt_hit'] is True for r in seen),len(seen)),
        exact_transcript=exact_rate(sum(r['exact_transcript'] is True for r in seen),len(seen)),
        mean_word_accuracy=float(np.mean([r['word_accuracy'] for r in seen])) if seen else None,
        word_accuracy_interval=None,word_accuracy_note='descriptive mean; no exact binomial interval for a continuous WER-derived mean',
        trials=trials,report_only=True,qualification=False,reselection=False,
        interval_limits='Individual trial-hit intervals, not simultaneous or cluster-adjusted; within-set dependence and placement are not covered')


def guard_design():
    return dict(available=False,outcome='partially-available-not-run',physically_validated_in_window=False,
        reason='In-guard and outside-guard are feasible with existing bot playback onset, AEC_GUARD trigger, steady Jabra stimulus and post-gate recorder uplink.wav; the re-arm boundary needs new tooling for scheduled accepted playback gaps. V2 has not been run.',
        conditions=[dict(id=name,repetitions=5,expectation=expectation) for name,expectation in (
            ('in-guard','Near stimulus active before playback onset and through +800 ms; suppressed until +400 ms then present'),
            ('outside-guard','Near stimulus begins at measured playback onset +600 ms or later and is present'),
            ('gap-1800','Second accepted playback after 1800 ms silence; no new guard and near-end remains present'),
            ('gap-2200','Second accepted playback after 2200 ms silence; new guard, suppression through +400 ms then reopening'))],
        hooks=dict(pre_gate='aec-record-slots: simultaneous raw MIC1/MIC3/processed, before pipeline gate',
            console='AEC_GUARD only logs armed writes; GATE transitions include source_lower_us; MIC_GATE is one in five enabled capture frames',
            playback='tone/aec-probe reject active media; demo_bot has no play-fixture request handler',
            recorder='Existing demo recorder retains post-network uplink, but ordinary bot reply timing cannot prescribe accepted playback gaps'),
        timing='Use accepted device playback first/end and source sample times, not bot-start or serial receipt. Tolerance = one 20 ms frame plus measured alignment bound; unknown bounds are unavailable.',
        evidence='Require nonzero raw near stimulus, silent-vs-active reference boundaries, post-gate suppression and reopening, bracketed clocks and actual gap intervals wholly on the declared side of 2000 ms. All five must match; retain every mismatch.',
        smallest_alternative='Add a standalone host fixture WebRTC peer in new hil modules, using the existing transport protocol to send a scheduled two-burst PCM and record uplink. No firmware/server edit. Validate its clock mapping and source isolation first; raw AEC output and gate logs alone remain diagnostic.',
        partial_alternative='Console tone pairs with a continuous Jabra stimulus can check onset/re-arm logs and raw reference gaps offline while disconnected. They cannot independently prove post-gate uplink suppression.',
        pass_rule=RULES['guard']['pass_rule'],qualification=False)

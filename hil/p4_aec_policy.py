"""Predeclared P4 AEC calibration policy. Pure rules; no device IO."""
import json
import math
from pathlib import Path
import re

import numpy as np
import echoear_aec_alignment as alignment
import echoear_aec_scoring as scoring
from echoear_aec_metrics import reduction
from make_bundle import digest,verify_bundle
from playback_quality import microphone_clip_gate
from wire_contract import require

ROOT=Path(__file__).resolve().parents[1]
GAINS=tuple(range(0,34,3))
GUARDS=tuple((guard,rearm) for rearm in (2000,3000,4000) for guard in (400,600,800,1000))
ECHOES=10
GAIN_PARTS=(tuple(range(3)),tuple(range(3,6)),tuple(range(6,9)),tuple(range(9,13)))
POLICY=dict(id='p4-aec-measurement-v2',gain_db=list(GAINS),echo_replies=ECHOES,
    stt_accuracy_min=.90,clips_max=0,false_user_turns_max=0,erle_tie_db=1.,
    erle_campaign_target_db=20,delay_min_correlation=.6,delay_min_separation=.05,
    delay_search_samples=[0,4096],guard_candidates=[list(v) for v in GUARDS],
    guard_self_interruptions_max=0,guard_long_replies_min=10,
    max_capture_seconds=26,room_gate='unchanged alsa.record_room_baseline; failed preflight is unscored',
    state='fresh boot per declared part; fixed order; single acquired scored original; unscored errors retryable',
    inside_guard_timing='report-only',bot_content='report-only for sweep and guard')
S3_REFERENCE=dict(scope='report-only; not a transferred P4 calibration',campaign='nova-live-02',
                  reply_medians_db=[14.3,24.8,30.5])


def write_new(path,value):
    with Path(path).open('x') as stream:stream.write(json.dumps(value,indent=2,allow_nan=False)+'\n')


def fixtures():
    from p4_defaults_validate import prompts
    manifest=json.loads((ROOT/'hil/fixtures/agent_conversation/manifest.json').read_text())['turns']
    long=next(p for p in manifest if p['id']=='long')
    path=ROOT/'hil/fixtures/agent_conversation'/long['file']
    require(digest(path)==long['sha256'],'long prompt fixture changed')
    return [dict(id='long',path=str(path),expected=long['text'],sha256=digest(path))]+[
        dict(id=p['id'],path=str(p['path']),expected=p['expected'],sha256=p['source_sha256']) for p in prompts()]


def make_plan(bundle):
    bundle=Path(bundle).resolve();manifest,_=verify_bundle(bundle,'p4xc')
    require(manifest['profile'] in ('aec-normal','aec-demo') and manifest['aec_enabled'],'P4 AEC calibration/report bundle required')
    return dict(schema=1,kind='p4-aec-calibration-plan',qualification=False,policy=POLICY,
        bundle=str(bundle),profile=manifest['profile'],manifest_sha256=digest(bundle/'manifest.json'),elf_sha256=manifest['elf_sha256'],
        fixtures=fixtures(),gain_parts=[list(p) for p in GAIN_PARTS],s3_reference=S3_REFERENCE,
        source_sha256={name:digest(ROOT/'hil'/name) for name in (
            'p4_aec_policy.py','p4_aec_measure.py','echoear_aec_alignment.py','echoear_aec_gain_sweep.py',
            'echoear_aec_scoring.py','echoear_aec_metrics.py','echoear_aec_capture.py','p4_aec_product.py',
            'board_smoke.py','echoear_aec_timing.py','dump_verify.py','playback_quality.py','echoear_hot_stt.py',
            'p4_step14.py','p4_product.py','p4_audio_fix4.py','alsa.py')})


def validate_plan(plan):
    require(plan==make_plan(Path(plan['bundle'])),'plan, bundle, rules, fixture or helper hashes changed')
    return plan


def capture(path,expected_sha):
    path=Path(path);require(digest(path)==expected_sha,'capture hash mismatch')
    with np.load(path,allow_pickle=False) as data:
        pcm=data['pcm'].copy();timing=data['timing'].copy()
        require(int(data['schema'])==2 and int(data['rate'])==16000 and
            list(data['channels'])==['mic1','mic2','mic3','mic4','processed'],'schema2 16 kHz raw-slot capture required')
    require(pcm.dtype==np.dtype('<i2') and pcm.ndim==2 and pcm.shape[1]==5 and len(pcm)>=8192 and np.isfinite(pcm).all(),
            'complete finite raw-slot/AFE capture required')
    require(timing.ndim==2 and timing.shape[1]==5 and len(timing)>0 and timing[0,0]==0 and
        timing[-1,1]==len(pcm) and np.all(timing[:,4]==0) and np.all(timing[:,2]<=timing[:,3]) and
        np.all(timing[:,0]<timing[:,1]) and np.all(timing[1:,0]==timing[:-1,1]),'complete gap-free capture timing required')
    return pcm,timing


def delay_score(dac,near,source):
    result=alignment.analyze(dac,near,source)
    if 'independent_nearend_afe_delay' not in result:
        return dict(passed=False,available=False,measured_delay_samples=None,analysis=result,qualification=False)
    afe=result['independent_nearend_afe_delay']
    # Reference-to-mic physical path correlation is diagnostic, not the AFE
    # delay. A distributed acoustic path must not veto independent near-end delay.
    valid=(result['known_source_to_reference']['available'] and result['known_nearend_source_to_mic']['available']
        and result['nearend_excitation_verified'] and result['reference_silent_for_independent_afe']
        and result['reference_dac_vs_nearend_db'] is not None and result['reference_dac_vs_nearend_db']>=10
        and afe.get('available') is True and scoring.saturation(dac)['passed'] and scoring.saturation(near)['passed'])
    correlation=abs(afe.get('signed_correlation',0));separation=correlation-afe.get('secondary_peak',correlation)
    valid=bool(valid and correlation>=.6 and separation>=.05 and type(afe.get('lag_samples')) is int and 0<afe['lag_samples']<4096)
    return dict(passed=valid,available=valid,measured_delay_samples=afe.get('lag_samples') if valid else None,
        correlation=correlation,separation=separation,analysis=result,qualification=False,
        basis='independent DAC-silent raw MIC1 to AFE; never optimized against ERLE')


def clip_evidence(before,after,pcm):
    require(before.get('generation')==after.get('generation') and before.get('pc_id')==after.get('pc_id'),
            'measurement session changed')
    gate=microphone_clip_gate(before,after)
    require(gate['available'],'ADC/gain clip evidence unavailable')
    counters={}
    for key in ('errors','reference_clips','output_clips'):
        values=[r.get('aec',{}).get(key) for r in (before,after)]
        require(all(type(v) is int and v>=0 for v in values) and values[1]>=values[0], 'AEC counters missing/reset')
        counters[key]=values[1]-values[0]
    saturation=scoring.saturation(pcm)
    return dict(passed=gate['passed'] and saturation['passed'] and not any(counters.values()),
                microphone=gate,raw=saturation,aec_delta=counters)


def reference_gain(before,after,gain):
    values=[row.get('aec',{}).get('reference_health',{}).get('gain_db') for row in (before,after)]
    require(type(gain) is int and gain in GAINS and all(type(v) is int and v==gain for v in values),
            'PERF aec.reference_health.gain_db missing, invalid or differs')
    return gain


def echo_score(pcm,quiet,delay,events,complete):
    from board_smoke import turn_checks
    erle=scoring.reply_erle(pcm,quiet_reference=quiet[:,2],delay_samples=delay,reply_start=0,reply_end=len(pcm)-delay)
    try:turn=turn_checks(events,'long',True,calibration=True)
    except ValueError as exc:turn=dict(passed=False,error=str(exc))
    false=[e for e in events if e.get('kind')=='frame' and e.get('bot_active') is True and
           e.get('frame') in ('UserStartedSpeakingFrame','InterruptionFrame')]
    require(complete is True,'reply incomplete or exceeded declared 26 s capture; retained, not retaken')
    residual=reduction(pcm[:,0],pcm[:,4],np.asarray([r['active'] for r in erle.get('frames',[])],dtype=bool),delay=delay) if erle['available'] else None
    return dict(available=erle['available'] and turn['passed'],median_erle_db=erle.get('median_db'),erle=erle,
        false_user_turns=len(false),turn=turn,residual=residual,s3_reference=S3_REFERENCE,
        erle_vs_s3_db=[erle['median_db']-v for v in S3_REFERENCE['reply_medians_db']] if erle['available'] else None,
        # ERLE target is a separate campaign report; sweep eligibility is the
        # user's clip/STT/false-turn rule, not a newly imposed >=20 per reply.
        campaign_reply_target_passed=erle['passed'])


def prompt_score(spec,events,playback,bot_started_ns,bot_stopped_ns):
    from echoear_hot_stt import word_errors
    started=playback['request_monotonic_s']*1e9
    overlap=bot_started_ns<started<bot_stopped_ns
    received=[e for e in events if e.get('kind')=='aggregator-user-turn-stopped' and e['monotonic_ns']>=started]
    available=len(received)==1 and isinstance(received[0].get('text'),str)
    text=received[0]['text'] if available else ''
    accuracy=word_errors(spec['expected'],text)['accuracy'] if available else 0.
    false=[e for e in events if e.get('kind')=='frame' and e.get('bot_active') is True and
           e.get('frame') in ('UserStartedSpeakingFrame','InterruptionFrame') and e['monotonic_ns']<started]
    starts=[e for e in events if e.get('kind')=='frame' and e.get('frame')=='UserStartedSpeakingFrame']
    intentional=[e for e in starts if e['monotonic_ns']>=started]
    witnessed=len(starts)==2 and len(intentional)==1 and intentional[0].get('bot_active') is True
    extra=max(0,len(starts)-2)
    return dict(passed=available and overlap and witnessed and accuracy>=.90 and not false and not extra,available=available,
        accuracy=accuracy,received=text,expected=spec['expected'],playback_overlap=overlap,
        intentional_user_turn_witnessed=witnessed,false_user_turns=len(false)+extra)


def select_gain(points):
    require(len(points)==12 and all(type(p['gain_db']) is int for p in points) and [p['gain_db'] for p in points]==list(GAINS),'complete ordered 12-point sweep required')
    eligible=[]
    for point in points:
        replies=point['echoes'];prompts=point['prompts']
        require(len(replies)==10 and [r['id'] for r in prompts]==['france','italy','speech'],'ten replies and every standard prompt required')
        available=all(r.get('available') is True and type(r.get('median_erle_db')) in (int,float) and math.isfinite(r['median_erle_db']) for r in replies)
        okay=available and point.get('clips_passed') is True and all(r.get('false_user_turns')==0 for r in replies) and all(
            p.get('passed') is True and type(p.get('accuracy')) in (int,float) and math.isfinite(p['accuracy']) and
            .90<=p['accuracy']<=1 and p.get('false_user_turns')==0 for p in prompts)
        median=float(np.median([r['median_erle_db'] for r in replies])) if available else None
        if okay:eligible.append(dict(gain_db=point['gain_db'],median_erle_db=median))
    if not eligible:return dict(selected_gain_db=None,passed=False,reason='none eligible; stop',eligible=[])
    best=max(p['median_erle_db'] for p in eligible)
    selected=min((p for p in eligible if best-p['median_erle_db']<=1),key=lambda p:p['gain_db'])
    return dict(passed=True,selected_gain_db=selected['gain_db'],median_erle_db=selected['median_erle_db'],eligible=eligible,
        ties='within 1 dB of global best; lower gain wins',campaign_erle_target_passed=selected['median_erle_db']>=20,
        s3_reference=S3_REFERENCE,qualification=False)


GUARD_RE=re.compile(r'AEC_GUARD onset_upper_us=(\d+) source_deadline_us=(\d+) guard_ms=(\d+) rearm_ms=(\d+) previous_end_us=(-?\d+)')


def guard_score(index,long_turns,onsets,inside,after):
    require(type(index) is int and 0<=index<len(GUARDS),'declared guard candidate required')
    guard,rearm=GUARDS[index]
    require(len(long_turns)>=10,'at least ten original long replies required')
    events=[dict(zip(('onset_us','deadline_us','guard_ms','rearm_ms','previous_end_us'),map(int,m.groups())))
            for line in onsets for m in [GUARD_RE.search(line)] if m]
    observed=bool(events) and all(e['guard_ms']==guard and e['rearm_ms']==rearm and
        e['deadline_us']==e['onset_us']+guard*1000 for e in events)
    rearmed=any(e['previous_end_us']>0 and e['onset_us']-e['previous_end_us']>=rearm*1000 for e in events)
    no_false=all(t.get('passed') is True and t.get('user_starts_during_bot')==0 and t.get('interruptions_during_bot')==0
                 and t.get('user_turn_starts')==1 for t in long_turns)
    passed=observed and rearmed and no_false and after.get('detected') is True and after.get('after_rearm') is True
    return dict(passed=passed,guard_ms=guard,rearm_ms=rearm,events=events,observed_onset=observed,
        rearmed=rearmed,zero_self_interruptions=no_false,inside_probe=inside,inside_timing_report_only=True,
        bot_content_findings=[f for t in long_turns for f in t.get('bot_content_findings',[])],
        after_rearm_probe=after,qualification=False)


def select_guard(rows):
    require(rows and len(rows)<=len(GUARDS),'ordered guard results required')
    for index,row in enumerate(rows):
        require((row['guard_ms'],row['rearm_ms'])==GUARDS[index],'guard candidates skipped or reordered')
        if row['passed']:
            require(index==len(rows)-1,'changed guard after acceptance')
            return dict(passed=True,candidate_index=index,guard_ms=row['guard_ms'],rearm_ms=row['rearm_ms'],qualification=False)
    return dict(passed=False,reason='next declared candidate' if len(rows)<len(GUARDS) else 'none accepted; stop')

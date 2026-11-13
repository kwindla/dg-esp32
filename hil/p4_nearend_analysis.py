"""Step 15d predeclared word-local transfer analysis; no hardware or server imports."""
import re
import numpy as np
from scipy import signal, stats

RATE=16000
SPLIT=12480  # 0.780 s: source's internal pause before Italy, fixed before acquisition.
RULES=dict(band_hz=[300,3500],block_samples=320,activity_db=-30,split_sample=SPLIT,
    bootstrap_resamples=10000,bootstrap_seed=151504,confidence=.95,attribution_db=2.,
    raw_match_min=.10,envelope_match_min=.65,uplink_match_min=.60,
    match_energy_floor_ratio=1e-12,
    afe_delay_samples=1024,delay_kind='decision-backed; not remeasured',
    mixture='Arm B levels include simultaneous echo; not source-separated near speech',
    stage_labels=dict(acoustic='acoustic, including placement',
        double_talk='double_talk (includes echo-removal confound)',
        double_talk_output='double_talk_output (includes residual-echo confound)'),
    double_talk_output='Paired B minus A output; mixture difference, not near-end localization by itself',
    residual=dict(pre_onset_margin_ms=40,min_window_ms=100,min_segment_to_residual_power=2.,min_pairs=15,
        p4_within_board_loss_db=2.),
    assumptions=['Residual echo is roughly stationary across the pre-onset window and lexical segment',
        'Near speech and residual echo add in power',
        'Bootstrap treats trials, including trials within a continuous-AFE boot part, as independent/exchangeable',
        'Clopper-Pearson assumes independent Bernoulli trials; intervals are individual, not simultaneous or multiplicity-adjusted',
        'Arm A silence threshold is mean 300-3500 Hz MIC3 power over source-active samples, not per-sample silence'],
    delivery='AEC-to-uplink includes HPF, digital gain, limiter, gate, Opus and delivery',
    packet_loss='unmeasured: unchanged bot recorder has no RTP sequence numbers')


def check(value,message):
    if not value:raise ValueError(message)


def mono(x):
    x=np.asarray(x,dtype=float)
    check(x.ndim==1 and len(x)>0 and np.isfinite(x).all(),'finite nonempty mono samples required')
    return x


def band(x):
    return signal.sosfilt(signal.butter(4,RULES['band_hz'],fs=RATE,btype='bandpass',output='sos'),mono(x))


def segments(source):
    source=mono(source);check(len(source)>SPLIT+320,'complete Italy fixture required')
    blocks=len(source)//320
    powers=np.mean(source[:blocks*320].reshape(-1,320)**2,axis=1)
    check(powers.max()>0,'silent stimulus')
    active=np.r_[np.repeat(powers>powers.max()*.001,320),np.zeros(len(source)%320,dtype=bool)]
    indices=np.arange(len(source))
    result={'phrase':np.flatnonzero(active & (indices<SPLIT)),
            'word':np.flatnonzero(active & (indices>=SPLIT))}
    check(all(len(v)>=320 for v in result.values()),'both source-defined lexical segments required')
    return result


def locate(source,target,minimum=.60):
    """Normalized complete-template correlation. Never use absolute WAV offsets as a clock."""
    a,b=band(source),band(target)
    check(len(b)>len(a)+2,'target must retain complete template and margins')
    energy=np.r_[0.,np.cumsum(b*b)]
    window_power=np.maximum(energy[len(a):]-energy[:-len(a)],0)
    norm=np.sqrt(np.maximum(window_power*np.dot(a,a),1e-30))
    values=signal.correlate(b,a,mode='valid',method='fft')/norm
    # FFT/cumulative-sum roundoff in exact silence must not win a normalized peak.
    values[window_power<=max(1e-30,float(window_power.max())*RULES['match_energy_floor_ratio'])]=0
    peak=int(np.argmax(np.abs(values)));corr=float(abs(values[peak]))
    check(0<peak<len(values)-1 and corr>=minimum,'stage alignment weak or at search boundary')
    # Envelope supplies an independent window check, not a physical-delay measurement.
    n=len(a)//320
    ea=np.sqrt(np.mean(a[:n*320].reshape(-1,320)**2,axis=1))
    eb=np.sqrt(np.mean(b[peak:peak+n*320].reshape(-1,320)**2,axis=1))
    envelope=float(np.corrcoef(ea,eb)[0,1]) if min(np.std(ea),np.std(eb))>0 else 0.
    check(np.isfinite(envelope) and envelope>=RULES['envelope_match_min'],'stage envelope alignment failed')
    rivals=np.abs(values[np.abs(np.arange(len(values))-peak)>320])
    check(not len(rivals) or corr-float(rivals.max())>=.02,'ambiguous stage alignment')
    return dict(offset_samples=peak,correlation=corr,envelope_correlation=envelope)


def level(filtered,indices):
    check(len(indices)>0 and indices.min()>=0 and indices.max()<len(filtered),'incomplete lexical window')
    power=float(np.mean(filtered[indices]**2))
    check(np.isfinite(power) and power>0,'zero/nonfinite lexical power; level unavailable')
    return float(10*np.log10(power/32768**2))


def stage_levels(source,pcm,uplink=None):
    source=mono(source);pcm=np.asarray(pcm)
    check(pcm.ndim==2 and pcm.shape[1]==5 and np.isfinite(pcm).all(),'five synchronized capture channels required')
    masks=segments(source);raw=locate(source,pcm[:,0],RULES['raw_match_min'])
    onset=raw['offset_samples'];processed=onset+RULES['afe_delay_samples']
    stages={'raw':(pcm[:,0],onset),'aec':(pcm[:,4],processed)}
    matches={'raw':raw,'aec':dict(offset_samples=processed,kind=RULES['delay_kind'],measured_pass=False)}
    errors={}
    if uplink is not None:
        try:
            match=locate(pcm[processed:processed+len(source),4],uplink,RULES['uplink_match_min'])
            stages['uplink']=(uplink,match['offset_samples']);matches['uplink']=match
        except ValueError as exc:errors['uplink']=str(exc)
    result={};validity={s:False for s in ('raw','aec','uplink')}
    for name,(samples,offset) in stages.items():
        try:
            check(offset+len(source)<=len(samples),'capture ends before complete delayed final word')
            filtered=band(samples)
            levels={part:level(filtered,offset+indices) for part,indices in masks.items()}
            result[name]=levels;validity[name]=True
        except ValueError as exc:errors[name]=str(exc)
    return dict(levels=result,alignment=matches,stage_validity=validity,unavailable=errors)


def valid_stage(row,stage):
    if row.get('control_valid') is not True or row.get('stage_validity',{}).get(stage) is not True:return False
    levels=row.get('levels',{}).get(stage,{})
    return all(isinstance(levels.get(s),(int,float)) and np.isfinite(levels[s]) for s in ('phrase','word'))


def corrected_power(segment_power,residual_power):
    check(np.isfinite([segment_power,residual_power]).all() and segment_power>=0 and residual_power>=0,
        'finite nonnegative segment/residual powers required')
    near=segment_power-residual_power
    valid=segment_power>=RULES['residual']['min_segment_to_residual_power']*residual_power and near>0
    return dict(valid=bool(valid),segment_power=float(segment_power),residual_power=float(residual_power),
        near_power=float(near) if valid else None,
        near_dbfs=float(10*np.log10(near/32768**2)) if valid else None,
        reason=None if valid else 'segment power below twice residual or nonpositive corrected power')


def median_ci(values):
    values=np.asarray(values,dtype=float)
    check(values.ndim==1 and np.isfinite(values).all(),'finite bootstrap observations required')
    if not len(values):return dict(n=0,median=None,ci95=None)
    rng=np.random.default_rng(RULES['bootstrap_seed'])
    medians=np.median(values[rng.integers(0,len(values),(RULES['bootstrap_resamples'],len(values)))],axis=1)
    return dict(n=len(values),median=float(np.median(values)),ci95=np.quantile(medians,[.025,.975]).tolist())


def binomial(hits,total):
    check(type(hits) is int and type(total) is int and 0<=hits<=total,'integer binomial counts required')
    if not total:return dict(hits=0,n=0,rate=None,ci95=None)
    return dict(hits=hits,n=total,rate=hits/total,ci95=[
        float(stats.beta.ppf(.025,hits,total-hits+1)) if hits else 0.,
        float(stats.beta.ppf(.975,hits+1,total-hits)) if hits<total else 1.])


def attribution(p4,s3):
    if p4['ci95'] is None or s3['ci95'] is None:return dict(responsible=False,available=False)
    difference=p4['median']-s3['median']
    separated=p4['ci95'][1]<s3['ci95'][0] or s3['ci95'][1]<p4['ci95'][0]
    return dict(available=True,p4_minus_s3_db=difference,intervals_nonoverlap=separated,
        responsible=bool(abs(difference)>=2 and separated),deficit=bool(difference<=-2 and separated))


def italy_hit(transcripts):
    return any('italy' in re.findall(r"[a-z]+",str(t).lower()) for t in transcripts)


def near_end_claim(p4,s3,corrected,mixture):
    criteria=dict(corrected_deficit=corrected.get('deficit') is True,
        mixture_deficit_same_direction=mixture.get('deficit') is True,
        minimum_pairs=p4['n']>=RULES['residual']['min_pairs'] and s3['n']>=RULES['residual']['min_pairs'],
        p4_within_board_loss=p4['ci95'] is not None and p4['median']<=-RULES['residual']['p4_within_board_loss_db']
            and p4['ci95'][1]<0)
    return dict(localized=all(criteria.values()),criteria=criteria,p4_pairs=p4['n'],s3_pairs=s3['n'])


def summarize(rows):
    """Enforce explicit independent evidence validity; pair before bootstrap."""
    groups={};contributions={}
    for board in ('echoear','p4xc'):
        by_arm={arm:[r for r in rows if r['board']==board and r['arm']==arm] for arm in ('A','B')}
        metrics={}
        for arm,records in by_arm.items():
            observed=[r for r in records if r.get('stt_observed') is True and type(r.get('hit')) is bool]
            included=[r for r in observed if r.get('stt_valid') is True and r.get('control_valid') is True]
            excluded=[r for r in observed if not (r.get('stt_valid') is True and r.get('control_valid') is True)]
            metrics[arm]=dict(originals=len(records),stt=binomial(sum(r['hit'] for r in included),len(included)),
                stt_observed=binomial(sum(r['hit'] for r in observed),len(observed)),
                stt_unavailable=len(records)-len(observed),stt_control_excluded=[dict(id=r.get('id'),hit=r['hit'],
                    reason=r.get('stt_exclusion_reason','control validity not established')) for r in excluded],
                stages={},transfer={},end_of_phrase={},
                missing_packets_word=dict(available=False,count=None,reason=RULES['packet_loss']))
            for stage in ('raw','aec','uplink'):
                valid=[r for r in records if valid_stage(r,stage)]
                metrics[arm]['stages'][stage]={part:median_ci([r['levels'][stage][part] for r in valid]) for part in ('phrase','word')}
                metrics[arm]['end_of_phrase'][stage]=median_ci([r['levels'][stage]['word']-r['levels'][stage]['phrase'] for r in valid])
            for name,left,right in (('aec_minus_raw','aec','raw'),('uplink_minus_aec','uplink','aec')):
                valid=[r for r in records if valid_stage(r,left) and valid_stage(r,right)]
                metrics[arm]['transfer'][name]={part:median_ci([r['levels'][left][part]-r['levels'][right][part]
                    for r in valid]) for part in ('phrase','word')}
        values={}
        for part in ('phrase','word'):
            values['acoustic/'+part]=metrics['A']['stages']['raw'][part]
            values['aec_without_echo/'+part]=metrics['A']['transfer']['aec_minus_raw'][part]
            paired={}
            for arm,records in by_arm.items():
                for r in records:
                    if valid_stage(r,'raw') and valid_stage(r,'aec'):
                        paired.setdefault(r['pair'],{})[arm]=r['levels']['aec'][part]-r['levels']['raw'][part]
            values['double_talk/'+part]=median_ci([p['B']-p['A'] for p in paired.values() if set(p)=={'A','B'}])
            for stage in ('aec','uplink'):
                paired={};corrected={}
                suffix=part if stage=='aec' else stage+'/'+part
                for arm,records in by_arm.items():
                    for r in records:
                        if not valid_stage(r,stage):continue
                        paired.setdefault(r['pair'],{})[arm]=r['levels'][stage][part]
                        if arm=='A':corrected.setdefault(r['pair'],{})[arm]=r['levels'][stage][part]
                        else:
                            estimate=r.get('residual_estimates',{}).get(stage,{}).get(part,{})
                            if estimate.get('valid') is True and np.isfinite(estimate.get('near_dbfs',float('nan'))):
                                corrected.setdefault(r['pair'],{})[arm]=estimate['near_dbfs']
                values['double_talk_output/'+suffix]=median_ci([p['B']-p['A'] for p in paired.values() if set(p)=={'A','B'}])
                values['double_talk_output_corrected/'+suffix]=median_ci([p['B']-p['A'] for p in corrected.values() if set(p)=={'A','B'}])
            for arm in ('A','B'):values['codec_delivery/'+arm+'/'+part]=metrics[arm]['transfer']['uplink_minus_aec'][part]
        for arm in ('A','B'):
            for stage in ('raw','aec','uplink'):values['end_of_phrase/'+arm+'/'+stage]=metrics[arm]['end_of_phrase'][stage]
        groups[board]=metrics;contributions[board]=values
    comparisons={key:attribution(contributions['p4xc'][key],contributions['echoear'][key]) for key in contributions['p4xc']}
    mixture=lambda key:key.startswith(('double_talk/','double_talk_output/')) or '/B/' in key
    for key,comparison in comparisons.items():
        stage,segment=key.split('/',1)
        comparison.update(label=RULES['stage_labels'].get(stage,stage)+'/'+segment,
            interpretation='mixture difference' if mixture(key) else 'stage contribution difference')
    crossings=[k for k,v in comparisons.items() if v['responsible']]
    deficits=[k for k in crossings if comparisons[k].get('deficit')]
    differences=[k for k in crossings if not comparisons[k].get('deficit')]
    claims={};localized=[]
    for stage in ('aec','uplink'):
        claims[stage]={}
        for part in ('phrase','word'):
            suffix=part if stage=='aec' else stage+'/'+part
            corrected='double_talk_output_corrected/'+suffix;original='double_talk_output/'+suffix
            claim=near_end_claim(contributions['p4xc'][corrected],contributions['echoear'][corrected],
                comparisons[corrected],comparisons[original])
            claims[stage][part]=claim
            if claim['localized']:localized.append(corrected)
    localized=[k for k in deficits if not mixture(k) and not k.startswith('double_talk_output_corrected/')]+localized
    mixture_crossings=[k for k in crossings if mixture(k)]
    unresolved='mixture difference observed; near-end localization unresolved'
    complete=all(groups[b][a]['originals']==20 and groups[b][a]['stt']['n']==20 and
        all(groups[b][a]['stages'][s]['word']['n']==20 for s in ('raw','aec','uplink'))
        for b in groups for a in ('A','B'))
    return dict(qualification=False,complete=complete,boards=groups,contributions=contributions,
        p4_vs_s3=comparisons,responsible=crossings,responsible_labels=[comparisons[k]['label'] for k in crossings],
        responsible_meaning='metric crossed the absolute-difference rule; not a causal near-end claim',
        deficits=deficits,differences=differences,mixture_differences=mixture_crossings,
        localized_deficits=localized,
        localization='; '.join(comparisons[k]['label'] for k in localized) if localized else unresolved if mixture_crossings else 'not localized',
        double_talk_near_end=claims,double_talk_near_end_localized=any(c['localized'] for s in claims.values() for c in s.values()),
        double_talk_localization='double-talk near-end localized under declared assumptions' if any(c['localized'] for s in claims.values() for c in s.values())
            else unresolved if mixture_crossings else 'not localized',
        stage_labels=RULES['stage_labels'],double_talk_output_scope=RULES['double_talk_output'],assumptions=RULES['assumptions'],
        caution=RULES['mixture'],delivery_scope=RULES['delivery'])

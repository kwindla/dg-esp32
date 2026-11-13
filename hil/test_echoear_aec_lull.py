"""Prospective v8.4 witness tests; no evidence/bench access."""
import numpy as np
import pytest

from echoear_aec_accept_analysis import classify_opportunity, summarize
from echoear_aec_lull import (RULE, measure_lulls, pre_near_source_witness,
                             stratum_summary, lull_witness, POLICY, declared_rule, cancellation_witness)


def synthetic(seed=9184, scale=.1):
    rng = np.random.default_rng(seed)
    source = rng.normal(0, 1000, 16000)
    reference = source*scale
    onset = 11000
    reference[10880:11200] = 0
    blocks = np.array([[i*320, (i+1)*320, i*20000, (i+1)*20000, 0] for i in range(50)])
    anchors = [dict(coordinate=i*20000, lower_s=i*.02, upper_s=i*.02) for i in range(51)]
    recorded = [dict(kind='tts-transport-write', pc_id='peer', context_id='old',
                     monotonic_ns=100000000+i*1000000, samples=640,
                     drop=False, non_silent=True) for i in range(24)]
    return dict(reference=reference, quiet=np.zeros(3200), blocks=blocks,
                anchors=anchors, onset=onset, source=source, recorded=recorded,
                pc_id='peer', context_id='old')


def measured(x):
    return measure_lulls(**{k:x[k] for k in ('reference','quiet','blocks','onset')})


def classify(**changes):
    values = dict(clean=True, delivered=True, near_valid=True, gate=False,
                  mic3='uncertain', future=True, lull_policy=RULE,
                  lull=dict(passed=True), integrity_passed=True)
    values.update(changes)
    return classify_opportunity(**values)


def test_legacy_classifier_and_strict_path_unchanged():
    assert classify(lull_policy=None) == 'unscorable-mic3'
    for lull in (None, dict(passed=False), dict(passed=True)):
        assert classify(mic3='active', lull=lull, integrity_passed=False) == 'eligible'
        assert classify(mic3='active', future=False, lull=lull) == 'unscorable-future'
    assert classify() == 'ongoing-reply-lull'
    assert classify(lull=dict(passed=False)) == 'unscorable-mic3'
    with pytest.raises(ValueError):classify(lull_policy='v84-typo')



def test_brief_lull_is_measured_not_a_strict_active_claim():
    x=synthetic();r=measured(x)
    assert r['passed'] and r['runs'][0]['duration_ms']==20
    assert r['uses_post_earliest_mic3']
    assert pre_near_source_witness(**x)['passed']


@pytest.mark.parametrize('frames,passed', [(1,True),(5,True),(6,False),(12,False)])
def test_total_run_not_age_or_future_offset(frames,passed):
    x=synthetic();x['reference'][10880:(34+frames)*320]=0
    assert measured(x)['passed'] is passed


def test_two_lulls_every_run_must_pass():
    x=synthetic();x['onset']=10880
    x['reference'][10560:10880]=0;x['reference'][10880:11200]=x['source'][10880:11200]*.1
    x['reference'][11200:13120]=0
    assert not measured(x)['passed']
    assert measured(x)['span_frames']>5


@pytest.mark.parametrize('broken', ['natural-end','unwritten','late-write','next-context','other-peer',
                                    'drop','missing-write','missing-anchor','gap','ambiguous','drift'])
def test_pre_near_evidence_fails_closed(broken):
    x=synthetic()
    if broken=='natural-end':x['source'][10680:]=0
    if broken=='unwritten':x['recorded']=x['recorded'][:16]
    if broken=='late-write':
        for e in x['recorded'][16:]:e['monotonic_ns']=1000000000
    if broken=='next-context':
        for e in x['recorded'][16:]:e['context_id']='next'
    if broken=='other-peer':
        for e in x['recorded'][16:]:e['pc_id']='other'
    if broken=='drop':x['recorded'][16]['drop']=True
    if broken=='missing-write':x['recorded'][16].pop('samples')
    if broken=='missing-anchor':x['anchors']=[]
    if broken=='gap':x['blocks'][25,4]=1
    if broken=='ambiguous':x['source'][:]=1
    if broken=='drift':x['reference'][7480:10680]=x['source'][7470:10670]*.1
    assert not pre_near_source_witness(**x)['passed'],broken


def test_future_generated_audio_and_post_near_events_do_not_change_pre_witness():
    x=synthetic();before=pre_near_source_witness(**x)
    x['source'][15360:]=np.nan
    x['recorded'].append(dict(kind='tts-transport-write',pc_id='peer',context_id='old',
                               monotonic_ns=1000000000,samples=640,drop=True))
    assert pre_near_source_witness(**x)==before


def test_rapid_cancellation_preserves_pre_near_witness_but_not_full_measurement():
    x=synthetic();before=pre_near_source_witness(**x)
    x['reference'][x['onset']-320:]=0
    assert pre_near_source_witness(**x)==before
    assert not measured(x)['passed']


@pytest.mark.parametrize('change,expected', [
    ({'clean':False},'excluded-noise-or-acquisition'),
    ({'delivered':False},'not-delivered-no-active-tts'),
    ({'near_valid':False},'unscorable-near-onset'),
    ({'gate':True},'in-window'),({'gate':None},'unscorable-gate'),
    ({'integrity_passed':False},'unscorable-mic3')])
def test_upstream_exclusions(change,expected):
    assert classify(**change)==expected


def test_strata_counts_misses_and_reports_empty_rates():
    rows=[]
    for opportunity,success in [('eligible',False),('ongoing-reply-lull',True)]:
        rows.append(dict(id=opportunity,kind='stop',volume_db=-6,opportunity=opportunity,
                         recognized=success,interruption_after_stimulus=success,credited_barge_in=success))
    old=summarize(rows)['-6:stop'];new=summarize(rows,lull_policy=RULE)['-6:stop']
    assert old['eligible']==1 and new['eligible']==2 and new['credited']==1
    assert 'strata' not in old
    assert new['strata']['strict']['recognition_rate']==0
    assert new['strata']['ongoing-reply-lull']['interruption_rate']==1
    assert new['strata']['lull_minus_strict_success_rate']==1
    assert stratum_summary([])['strict']['success_rate'] is None


@pytest.mark.parametrize('quiet,amplitude,passed',[(0,1.99,True),(0,2,True),(0,2.01,False),
                                                 (2,3.98,True),(2,4,False)])
def test_unchanged_strict_power_threshold(quiet,amplitude,passed):
    x=synthetic();x['quiet'][:]=quiet;x['reference'][10880:11200]=amplitude
    assert measured(x)['passed'] is passed


@pytest.mark.parametrize('seed,scale',[(3101,1),(3102,.5),(3103,.1),(3104,.05)])
def test_unseen_synthetic_content_and_levels(seed,scale):
    x=synthetic(seed,scale)
    assert pre_near_source_witness(**x)['passed'] and measured(x)['passed']
    x['reference'][:]=0
    assert not pre_near_source_witness(**x)['passed'] and not measured(x)['passed']


def test_unknown_or_unclosed_mic3_not_short_lull():
    x=synthetic();x['blocks'][34,4]=1
    assert not measured(x)['passed']
    x=synthetic();x['reference'][10880:]=0
    assert not measured(x)['passed']


def test_prospective_lull_counts_toward_ten_but_draft_never_qualifies():
    from test_aec_campaign import plan, passing_holds
    from echoear_aec_campaign_analysis import campaign_verdict
    p=plan();p.pop('ongoing_reply_lull',None);p['qualification']=True;holds=passing_holds(p)
    cells=[r for r in holds[0]['cases'] if r['kind']=='stop' and r['volume_db']==-6 and r.get('voice_class')=='firm']
    cells[0].update(opportunity='ongoing-reply-lull',recognized=True,interruption_after_stimulus=True)
    assert campaign_verdict(p,holds)['barge_in']['-6:firm:stop']['eligible']==9
    result=campaign_verdict(p,holds,lull_policy=RULE)
    group=result['barge_in']['-6:firm:stop']
    assert group['eligible']==10 and group['strata']['strict']['eligible']==9
    assert group['strata']['ongoing-reply-lull']['eligible']==1
    assert result['hypothetical_passed'] and not result['qualification']
    assert not result['acceptance_credit'] and not result['passed']
    cells[0]['barge_success']=False
    cells[1]['barge_success']=False
    assert not campaign_verdict(p,holds,lull_policy=RULE)['hypothetical_passed']
    # Retained holds marked report-only never gain acceptance denominator rows.
    holds[0]['acceptance_credit']=False
    assert campaign_verdict(p,holds,lull_policy=RULE)['barge_in']['-6:firm:stop']['eligible']==0


def test_lull_does_not_consume_spare_only_with_explicit_draft_option():
    from test_aec_campaign import plan, cell
    from echoear_aec_campaign import Schedule
    for policy in (None,RULE):
        q=Schedule([cell('cell')],[cell('spare')],plan()['noise_gate'],lull_policy=policy)
        q.finish(*q.next(),True,{'cell':'ongoing-reply-lull'})
        assert q.journal[0]['replacements']==[]
        assert q.unresolved==(['cell'] if policy is None else [])
        Schedule([cell('cell')],[cell('spare')],plan()['noise_gate'],q.state(),lull_policy=policy) if policy else None


def test_replay_rejects_output_anywhere_inside_evidence(tmp_path):
    from echoear_aec_lull_replay import replay
    with pytest.raises(ValueError,match='outside evidence'):
        replay(tmp_path,tmp_path/'campaign-04'/'accept-analysis.json')


def test_outcomes_are_not_opportunity_inputs():
    for recognized in (False,True):
        for interrupted in (False,True):
            # Analysis adds these only after the evidence-based class decision.
            row=dict(opportunity=classify(),recognized=recognized,
                     interruption_after_stimulus=interrupted)
            assert row['opportunity']=='ongoing-reply-lull'
            assert stratum_summary([row])['ongoing-reply-lull']['eligible']==1


def test_written_active_source_threshold_is_not_mic3_threshold():
    x=synthetic();x['source'][10680:]=32
    assert not pre_near_source_witness(**x)['passed']  # exactly 1024 LSB²
    x['source'][10680:]=32.01
    assert pre_near_source_witness(**x)['passed']


def test_equal_earliest_host_write_not_certainly_before_near():
    x=synthetic()
    for e in x['recorded'][16:]:e['monotonic_ns']=660000000
    assert not pre_near_source_witness(**x)['passed']


def test_reported_transport_write_gap_is_unknown_not_continuation():
    x=synthetic()
    x['recorded'].append(dict(kind='gap',stream='tts-written',pc_id='peer',drop=True,monotonic_ns=400000000))
    assert not pre_near_source_witness(**x)['passed']



def test_span_includes_active_middle_between_two_runs():
    x=synthetic();x['onset']=10880
    x['reference'][10560:10880]=0
    x['reference'][10880:11200]=x['source'][10880:11200]*.1
    x['reference'][11200:11520]=0
    result=measured(x)
    assert result['passed'] and result['span_frames']==3 and result['span_ms']==60


def test_closing_frame_missing_from_capture_is_unscorable():
    x=synthetic();x['reference']=x['reference'][:11200]
    assert not measured(x)['passed']
    assert classify(lull=lull_witness(**x))=='unscorable-mic3'


@pytest.mark.parametrize('frame',[33,34,35])
def test_gap_anywhere_in_span_or_closures_fails(frame):
    x=synthetic();x['blocks'][frame,4]=1
    assert classify(lull=lull_witness(**x))=='unscorable-mic3'


def test_cancel_before_closure_is_censored_and_after_closure_unchanged():
    x=synthetic()
    original=lull_witness(**x)
    assert classify(lull=original)=='ongoing-reply-lull'
    later=dict(valid=True,cancel_sample=x['onset']+3200,witnesses=[])
    assert classify(lull=lull_witness(**x,cancel=later))=='ongoing-reply-lull'
    # Previous counterexample, now explicitly unscorable rather than selected
    # into no-opportunity or eligible based on response speed.
    cancel=x['onset']-320
    x['reference'][cancel:]=0
    result=lull_witness(**x,cancel=dict(valid=True,cancel_sample=cancel,witnesses=[]))
    assert result['cancelled'] and not result['passed']
    assert classify(lull=result)=='unscorable-lull-cancelled'


def test_guard_equal_closure_passes_and_unknown_timing_fails_closed():
    x=synthetic();h=measured(x)['closing_frame_end_sample']
    assert classify(lull=lull_witness(**x,cancel=dict(valid=True,cancel_sample=h)))=='ongoing-reply-lull'
    assert classify(lull=lull_witness(**x,cancel=dict(valid=True,cancel_sample=h-1)))=='unscorable-lull-cancelled'
    assert classify(lull=lull_witness(**x,cancel=dict(valid=False)))=='unscorable-mic3'


def test_silent_and_strict_classes_are_unchanged_even_with_cancellation():
    for mic3,expected in [('silent','no-opportunity'),('active','eligible')]:
        assert classify(mic3=mic3,lull=dict(passed=False,cancelled=True))==expected
    assert classify(future=False)=='unscorable-future'


def test_last_written_sample_must_extend_beyond_closure():
    x=synthetic();h=measured(x)['closing_frame_end_sample']
    # Include the active source frame crossing L, but terminate at its end.
    x['recorded']=x['recorded'][:h//640]
    assert not lull_witness(**x)['passed']
    assert classify(lull=lull_witness(**x))=='unscorable-mic3'


def test_written_continuation_has_an_upper_start_horizon():
    x=synthetic();x['source'][10680:]=0
    x['source'][13760:]=1000  # onset+20ms+120ms is13240, so this is too late.
    assert not pre_near_source_witness(**x)['passed']


def test_lull_witness_never_reads_reference_beyond_reported_horizon():
    class ReadTracker:
        def __init__(self,values):self.values=values;self.ends=[]
        def __len__(self):return len(self.values)
        def __getitem__(self,key):
            assert isinstance(key,slice) and key.stop is not None
            self.ends.append(key.stop)
            return self.values[key]
    # Also exercise an early closing frame followed by the final onset-candidate
    # frame: all three C frames are part of the unchanged initial classification.
    for onset in (11000,11199):
        x=synthetic();x['onset']=onset
        tracker=ReadTracker(x['reference']);x['reference']=tracker
        result=lull_witness(**x)
        assert max(tracker.ends)<=result['measurement']['horizon_end_sample']
        assert result['measurement']['horizon_end_sample']<=onset+320+1920


def test_declaration_pins_rule_and_zero_cancellation_expectation():
    assert declared_rule({}) is None
    assert declared_rule(dict(ongoing_reply_lull=dict(POLICY)))==RULE
    for key in ('span_max_frames','horizon_after_latest_ms','alignment_origin_tolerance_samples','cancelled_expected'):
        p=dict(POLICY);p[key]+=1
        with pytest.raises(ValueError):declared_rule(dict(ongoing_reply_lull=p))


def test_declaration_enables_strata_but_one_cancelled_row_fails_zero_gate():
    from test_aec_campaign import plan,passing_holds
    from echoear_aec_campaign_analysis import campaign_verdict
    p=plan();p.update(qualification=True,ongoing_reply_lull=dict(POLICY));holds=passing_holds(p)
    r=campaign_verdict(p,holds)
    assert r['qualification'] and r['cancelled_lull_check']['passed']
    holds[0]['cases'].append(dict(id='cancelled',cell_id='cancelled',kind='stop',volume_db=-6,
        clean=True,expected_class='eligible',opportunity='unscorable-lull-cancelled'))
    r=campaign_verdict(p,holds)
    assert not r['qualification'] and r['cancelled_lull_check']['observed']==1
    assert r['barge_in']['-6:firm:stop']['strata']['cancelled_lull_check']['observed']==1


def test_cancelled_class_consumes_a_spare():
    from test_aec_campaign import plan,cell
    from echoear_aec_campaign import Schedule
    q=Schedule([cell('cell')],[cell('spare')],plan()['noise_gate'],lull_policy=RULE)
    q.finish(*q.next(),True,{'cell':'unscorable-lull-cancelled'})
    assert q.journal[0]['replacements'][0]['spare']=='spare'


def test_earliest_callback_or_copy_bracket_and_peer_scope():
    x=synthetic();events=[dict(event='interrupt-copy',pc_id='peer',t=.75),
                          dict(event='interrupt-copy',pc_id='other',t=.1)]
    r=cancellation_witness(x['blocks'],x['anchors'],'AUDIO_CANCEL callback_us=800000',events,'peer',.1)
    assert r['valid'] and r['cancel_sample']==11840
    # Device callback wins when its lower bracket is earlier than the copy.
    r=cancellation_witness(x['blocks'],x['anchors'],'AUDIO_CANCEL callback_us=700000',events,'peer',.1)
    assert r['cancel_sample']<=11200 and r['cancel_sample']<11840
    assert not cancellation_witness(x['blocks'],[], '',events,'peer',.1)['valid']


def test_post_horizon_cancel_and_outcomes_do_not_select_class():
    x=synthetic()
    for recognized in (False,True):
        for interrupted in (False,True):
            for sample in (None,x['onset']+3200,x['onset']+6400):
                proof=lull_witness(**x,cancel=dict(valid=True,cancel_sample=sample))
                row=dict(opportunity=classify(lull=proof),recognized=recognized,
                         interruption_after_stimulus=interrupted)
                assert row['opportunity']=='ongoing-reply-lull'
                assert stratum_summary([row])['ongoing-reply-lull']['eligible']==1



def test_two_short_runs_do_not_evade_outer_span_bound():
    x=synthetic();x['reference'][31*320:34*320]=0
    x['reference'][34*320:35*320]=x['source'][34*320:35*320]*.1
    x['reference'][35*320:37*320]=0
    r=measured(x)
    assert not r['passed'] and r['span_frames']==6


def test_capture_end_stays_unknown_even_with_later_cancellation():
    x=synthetic();x['reference']=x['reference'][:11200]
    proof=lull_witness(**x,cancel=dict(valid=True,cancel_sample=11200))
    assert proof['measurement']['capture_end_missing']
    assert classify(lull=proof)=='unscorable-mic3'


def test_early_cancel_does_not_consult_post_cancel_source_events():
    class ForbiddenEvents:
        def __iter__(self):raise AssertionError('Censored opportunity must not consult source write outcomes')
    x=synthetic();x['recorded']=ForbiddenEvents()
    proof=lull_witness(**x,cancel=dict(valid=True,cancel_sample=x['onset']-320))
    assert classify(lull=proof)=='unscorable-lull-cancelled'


def test_flag_cannot_be_added_to_a_different_reviewed_template(tmp_path,monkeypatch):
    import json
    import echoear_aec_campaign as runner
    from test_aec_campaign import plan
    p=plan();p.pop('ongoing_reply_lull',None)
    path=tmp_path/'hil/plans/echoear-aec-accept-v1.json'
    path.parent.mkdir(parents=True);path.write_text(json.dumps(p))
    p.update(status='declared',qualification=True,campaign_authorized=True,
             reviewed_spec_sha256=runner.sha(path),ongoing_reply_lull=dict(POLICY))
    monkeypatch.setattr(runner,'ROOT',tmp_path)
    monkeypatch.setattr(runner,'validate_spec',lambda p:True)
    monkeypatch.setattr(runner,'validate_review_preflight',lambda *a:None)
    with pytest.raises(ValueError,match='Reviewed contract changed: ongoing_reply_lull'):
        runner.validate_campaign(p)



def test_resume_cannot_change_the_lull_declaration(tmp_path):
    import json
    from echoear_aec_campaign_resume import run_chain
    from echoear_aec_bench import sha
    path=tmp_path/'declaration.json';path.write_text('{}')
    ref=dict(path=str(tmp_path),files_sha256={'declaration.json':sha(path)},declaration_sha256=sha(path))
    assert run_chain(dict(resume_from=ref))==[tmp_path]
    with pytest.raises(ValueError,match='Resume contract changed: ongoing_reply_lull'):
        run_chain(dict(resume_from=ref,ongoing_reply_lull=dict(POLICY)))



def test_cancel_bracket_cannot_skip_an_earlier_gapped_block():
    x=synthetic();x['blocks'][33,4]=1
    # .68 touches both the unknown frame33 and known frame34. Selecting only
    # frame34 would give a falsely late lower cancellation bound.
    events=[dict(event='interrupt-copy',pc_id='peer',t=.68)]
    r=cancellation_witness(x['blocks'],x['anchors'],'',events,'peer',.1)
    assert not r['valid']

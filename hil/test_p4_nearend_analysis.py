"""Synthetic host coverage of the predeclared 15d measurements and statistics."""
import copy
import numpy as np
import pytest
from scipy import signal
import p4_nearend_analysis as a


def fixture_pcm():
    rng=np.random.default_rng(41);x=np.zeros(21759)
    x[1600:10560]=rng.normal(0,2000,8960)
    x[12800:19840]=rng.normal(0,1700,7040)
    return x


def test_segments_are_source_defined_with_exact_boundary_and_partial_block():
    x=fixture_pcm();x[12160:12800]=2000
    masks=a.segments(x)
    assert masks['phrase'].max()==12479 and masks['word'].min()==12480
    assert masks['word'].max()<len(x)//320*320
    assert not np.intersect1d(*masks.values()).size
    quieted=x.copy();quieted[:12480]*=.5
    assert np.array_equal(a.segments(x)['word'],a.segments(quieted)['word'])


@pytest.mark.parametrize('x',[[],[0],np.zeros(22000),np.full(22000,np.nan),np.ones((20000,2)),np.r_[np.ones(12000),np.zeros(10000)]])
def test_bad_or_missing_source_segments_fail(x):
    with pytest.raises(ValueError):a.segments(x)


def test_activity_threshold_is_strict_and_source_only():
    x=np.zeros(22000);x[:320]=1000;x[12800:13120]=1000
    x[320:640]=1000*np.sqrt(.001);x[640:960]=1000*np.sqrt(.001)*1.00001
    masks=a.segments(x)
    assert 320 not in masks['phrase'] and 640 in masks['phrase']


def test_stage_alignment_and_known_delay_preserve_word_local_loss():
    x=fixture_pcm();onset=4000;processed=onset+1024
    pcm=np.zeros((40000,5));pcm[onset:onset+len(x),0]=x
    y=x*.5;y[12480:]*=.5
    pcm[processed:processed+len(y),4]=y
    uplink=np.r_[np.zeros(7000),y*1.25,np.zeros(9000)]
    row=a.stage_levels(x,pcm,uplink)
    assert row['alignment']['raw']['offset_samples']==onset
    assert row['alignment']['aec']['offset_samples']==processed
    assert row['alignment']['aec']['measured_pass'] is False
    assert row['alignment']['uplink']['offset_samples']==7000
    levels=row['levels']
    assert levels['aec']['phrase']-levels['raw']['phrase']==pytest.approx(-6.0206,abs=.02)
    assert levels['aec']['word']-levels['raw']['word']==pytest.approx(-12.0412,abs=.03)
    assert levels['uplink']['word']-levels['aec']['word']==pytest.approx(1.9382,abs=.02)


@pytest.mark.parametrize('issue',['silence','short','edge','duplicate','nan'])
def test_alignment_rejects_untrustworthy_windows(issue):
    x=fixture_pcm();y=np.r_[np.zeros(2000),x,np.zeros(2000)]
    if issue=='silence':y[:]=0
    if issue=='short':y=x[:-1]
    if issue=='edge':y=np.r_[x,np.zeros(4000)]
    if issue=='duplicate':y=np.r_[np.zeros(2000),x,np.zeros(2000),x,np.zeros(2000)]
    if issue=='nan':y[0]=np.nan
    with pytest.raises(ValueError):a.locate(x,y)


def test_uplink_alignment_failure_retains_raw_and_aec():
    x=fixture_pcm();pcm=np.zeros((40000,5));pcm[2000:2000+len(x),0]=x;pcm[3024:3024+len(x),4]=x
    row=a.stage_levels(x,pcm,np.zeros(45000))
    assert set(row['levels'])=={'raw','aec'} and 'uplink' in row['unavailable']


def test_capture_must_include_delayed_final_word():
    x=fixture_pcm();pcm=np.zeros((len(x)+2100,5));pcm[2000:2000+len(x),0]=x
    row=a.stage_levels(x,pcm)
    assert row['stage_validity']['raw'] and not row['stage_validity']['aec']
    assert 'final word' in row['unavailable']['aec']


@pytest.mark.parametrize('indices',[np.array([],dtype=int),np.array([-1]),np.array([10])])
def test_incomplete_level_windows_fail(indices):
    with pytest.raises(ValueError):a.level(np.ones(10),indices)


def test_zero_level_is_unavailable_not_minus_infinity():
    with pytest.raises(ValueError):a.level(np.zeros(10),np.array([0,1]))


def test_bootstrap_is_deterministic_median_and_95_percent():
    x=list(range(1,21));r=a.median_ci(x)
    assert r==a.median_ci(x) and r['median']==10.5 and r['n']==20
    assert r['ci95'][0]<r['median']<r['ci95'][1]
    assert a.median_ci([3]*20)['ci95']==[3,3]
    assert a.median_ci([])==dict(n=0,median=None,ci95=None)


@pytest.mark.parametrize('values',[[float('nan')],[float('inf')],[[1]]])
def test_bootstrap_rejects_nonfinite_or_nonvector(values):
    with pytest.raises(ValueError):a.median_ci(values)


@pytest.mark.parametrize('hits,total,lo,hi',[(0,20,0,.1684335),(20,20,.8315665,1),(1,1,.025,1),(0,1,0,.975)])
def test_exact_clopper_pearson_boundaries(hits,total,lo,hi):
    r=a.binomial(hits,total)
    assert r['ci95']==pytest.approx([lo,hi],abs=1e-7)


@pytest.mark.parametrize('hits,total',[(-1,20),(21,20),(1.0,20),(True,1),(1,-1)])
def test_invalid_binomial_counts(hits,total):
    with pytest.raises(ValueError):a.binomial(hits,total)


def test_zero_denominator_is_unavailable():
    assert a.binomial(0,0)['rate'] is None


@pytest.mark.parametrize('delta,lo,hi,responsible,deficit',[
    (-2,-2.5,-1.5,True,True),(-1.9999,-2.5,-1.5,False,False),
    (2,1.5,2.5,True,False),(-3,-4,0,False,False),(-3,-4,-.5,False,False),(-3,-4,-.5001,True,True)])
def test_attribution_two_db_and_strict_nonoverlap(delta,lo,hi,responsible,deficit):
    r=a.attribution(dict(median=delta,ci95=[lo,hi]),dict(median=0,ci95=[-.5,.5]))
    assert r['responsible']==responsible and r['deficit']==deficit


def test_unavailable_attribution_never_selects_cause():
    assert not a.attribution(a.median_ci([]),a.median_ci([0]))['responsible']


@pytest.mark.parametrize('texts,hit',[(['What about Italy?'],True),(['ITALY'],True),(['illness','getting'],False),
    (['italys'],False),([],False),(['Italy.','Italy.'],True)])
def test_stt_whole_word_rule(texts,hit):
    assert a.italy_hit(texts)==hit


def rows():
    records=[]
    for board in ('echoear','p4xc'):
        for pair in range(20):
            for arm in ('A','B'):
                raw=-30-(3 if board=='p4xc' else 0)
                attenuation=-1-(3 if board=='p4xc' and arm=='B' else 0)
                levels={s:dict(phrase=v,word=v-2) for s,v in [('raw',raw),('aec',raw+attenuation),('uplink',raw+attenuation+1)]}
                records.append(dict(board=board,pair=pair,arm=arm,hit=True,levels=levels,control_valid=True,
                    stage_validity={s:True for s in ('raw','aec','uplink')},stt_observed=True,stt_valid=True))
    return records


def test_all_derived_contributions_and_paired_doubletalk():
    report=a.summarize(rows());p4=report['contributions']['p4xc']
    assert report['complete']
    assert p4['acoustic/word']['median']==-35
    assert p4['aec_without_echo/word']['median']==-1
    assert p4['double_talk/word']['median']==-3
    assert p4['double_talk_output/word']['median']==-3
    assert p4['codec_delivery/B/word']['median']==1
    assert p4['end_of_phrase/B/uplink']['median']==-2
    assert report['p4_vs_s3']['acoustic/word']['responsible']
    assert report['p4_vs_s3']['double_talk/word']['responsible']
    assert report['p4_vs_s3']['double_talk_output/word']['responsible']
    assert 'acoustic, including placement/word' in report['responsible_labels']
    assert 'acoustic, including placement/word' in report['localization']
    assert report['p4_vs_s3']['double_talk/word']['label']=='double_talk (includes echo-removal confound)/word'
    assert not report['p4_vs_s3']['aec_without_echo/word']['responsible']
    assert report['boards']['p4xc']['B']['missing_packets_word']['count'] is None


def test_missing_stages_and_transcripts_never_become_zero_or_miss():
    records=rows();records[-1]['levels']={};records[-1]['hit']=None
    r=a.summarize(records)
    assert not r['complete'] and r['contributions']['p4xc']['double_talk/word']['n']==19
    assert r['contributions']['p4xc']['double_talk_output/word']['n']==19
    assert r['boards']['p4xc']['B']['stt']['n']==19 and r['boards']['p4xc']['B']['stt_unavailable']==1


def test_unpaired_blocks_cannot_create_doubletalk_estimate():
    records=[r for r in rows() if r['arm']=='B' or r['pair']<10]
    assert a.summarize(records)['contributions']['p4xc']['double_talk/phrase']['n']==10
    assert a.summarize(records)['contributions']['p4xc']['double_talk_output/phrase']['n']==10


def test_no_difference_reports_not_localized():
    records=rows()
    for r in records:r['levels']={s:dict(phrase=-30,word=-30) for s in ('raw','aec','uplink')}
    assert a.summarize(records)['localization']=='not localized'
    assert a.summarize([])['localization']=='not localized'


def test_extra_echo_removal_fakes_old_metric_without_output_loss():
    records=rows()
    for r in records:
        raw=-24 if r['board']=='p4xc' and r['arm']=='B' else -30
        r['levels']={s:dict(phrase=v,word=v-2) for s,v in [('raw',raw),('aec',-31),('uplink',-30)]}
    report=a.summarize(records)
    for segment in ('phrase','word'):
        old=report['p4_vs_s3']['double_talk/'+segment]
        new=report['p4_vs_s3']['double_talk_output/'+segment]
        assert old['responsible'] and old['deficit'] and old['p4_minus_s3_db']==-6
        assert 'includes echo-removal confound' in old['label']
        for board in ('echoear','p4xc'):
            assert report['contributions'][board]['double_talk_output/'+segment]==dict(n=20,median=0.,ci95=[0.,0.])
        assert new['p4_minus_s3_db']==0 and not new['responsible']
    assert 'mixture' in report['double_talk_output_scope']
    assert report['localization']=='mixture difference observed; near-end localization unresolved'


@pytest.mark.parametrize('delta,responsible,deficit',[
    (-2,True,True),(-1.9999,False,False),(0,False,False),(1.9999,False,False),(2,True,False)])
def test_doubletalk_output_attribution_boundaries_and_sign(delta,responsible,deficit):
    records=rows()
    for r in records:
        offset=delta if r['board']=='p4xc' and r['arm']=='B' else 0
        r['levels']={s:dict(phrase=-30,word=-30+(offset if s=='aec' else 0)) for s in ('raw','aec','uplink')}
    report=a.summarize(records)
    comparison=report['p4_vs_s3']['double_talk_output/word']
    assert comparison['responsible']==responsible and comparison['deficit']==deficit
    assert comparison['p4_minus_s3_db']==pytest.approx(delta)
    assert not report['p4_vs_s3']['double_talk_output/phrase']['responsible']


@pytest.mark.parametrize('offsets',[[-4]*10+[0]*10,[-10]*10+[4]*10])
def test_doubletalk_output_touching_or_overlapping_intervals_cannot_attribute(offsets):
    records=rows()
    for r in records:
        offset=offsets[r['pair']] if r['board']=='p4xc' and r['arm']=='B' else 0
        r['levels']={s:dict(phrase=-30,word=-30+(offset if s=='aec' else 0)) for s in ('raw','aec','uplink')}
    report=a.summarize(records);comparison=report['p4_vs_s3']['double_talk_output/word']
    assert abs(comparison['p4_minus_s3_db'])>=2
    assert not comparison['intervals_nonoverlap'] and not comparison['responsible']


def test_doubletalk_output_does_not_require_raw_levels():
    records=rows()
    for r in records:del r['levels']['raw']
    report=a.summarize(records)
    assert report['contributions']['p4xc']['double_talk_output/word']['n']==20
    assert report['contributions']['p4xc']['double_talk_output/word']['median']==-3
    assert report['contributions']['p4xc']['double_talk/word']['n']==0


def test_doubletalk_output_requires_both_arms_of_the_same_block():
    records=rows()
    for r in records:
        if r['arm']=='A':r['pair']+=20
    report=a.summarize(records)
    for board in ('echoear','p4xc'):
        metric=report['contributions'][board]['double_talk_output/word']
        assert metric==dict(n=0,median=None,ci95=None)
    assert report['p4_vs_s3']['double_talk_output/word']['available'] is False


def test_doubletalk_output_uses_median_of_paired_differences_independent_of_row_order():
    records=[r for r in rows() if r['pair']<3]
    for r in records:
        value=([-50,-40,-39] if r['arm']=='A' else [-40,-40,-28])[r['pair']]
        r['levels']['aec']=dict(phrase=value,word=value)
    report=a.summarize(list(reversed(records)))
    assert report['contributions']['p4xc']['double_talk_output/word']['median']==10
    assert report['contributions']['p4xc']['double_talk_output/word']['n']==3


@pytest.mark.parametrize('delta',[-3,3])
def test_acoustic_attribution_labels_include_placement_for_either_sign(delta):
    records=rows()
    for r in records:
        offset=delta if r['board']=='p4xc' else 0
        r['levels']={s:dict(phrase=-30+offset,word=-30+offset) for s in ('raw','aec','uplink')}
    report=a.summarize(records)
    for segment in ('phrase','word'):
        key='acoustic/'+segment;label='acoustic, including placement/'+segment
        assert report['p4_vs_s3'][key]['responsible']
        assert report['p4_vs_s3'][key]['label']==label
        assert key in report['responsible'] and label in report['responsible_labels']
        if delta<0:assert key in report['deficits'] and label in report['localization']
        else:assert key in report['differences'] and key not in report['deficits'] and report['localization']=='not localized'


@pytest.mark.parametrize('segment,residual,valid,near',[(2.,1.,True,1.),(1.9999,1.,False,None),
    (1.,1.,False,None),(0.,0.,False,None),(1.,0.,True,1.),(3.,1.,True,2.)])
def test_residual_power_gate_boundary_and_zero(segment,residual,valid,near):
    result=a.corrected_power(segment,residual)
    assert result['valid'] is valid and result['near_power']==near
    if valid:assert result['near_dbfs']==pytest.approx(10*np.log10(near/32768**2))
    else:assert result['near_dbfs'] is None and result['reason']


@pytest.mark.parametrize('segment,residual',[(float('nan'),1),(1,float('inf')),(-1,0),(1,-1)])
def test_invalid_residual_power_rejected(segment,residual):
    with pytest.raises(ValueError):a.corrected_power(segment,residual)


def corrected_rows():
    records=rows()
    for r in records:
        value=-36 if r['board']=='p4xc' and r['arm']=='B' else -30
        r['levels']={stage:dict(phrase=value,word=value) for stage in ('raw','aec','uplink')}
        if r['arm']=='B':r['residual_estimates']={stage:{segment:dict(valid=True,near_dbfs=value)
            for segment in ('phrase','word')} for stage in ('aec','uplink')}
    return records


def test_corrected_doubletalk_claim_requires_paired_corrected_and_mixture_deficits():
    report=a.summarize(corrected_rows())
    assert report['double_talk_near_end_localized']
    for stage in ('aec','uplink'):
        for segment in ('phrase','word'):
            assert report['double_talk_near_end'][stage][segment]['localized']
            assert all(report['double_talk_near_end'][stage][segment]['criteria'].values())
            suffix=segment if stage=='aec' else stage+'/'+segment
            assert 'double_talk_output_corrected/'+suffix in report['localized_deficits']
            assert 'double_talk_output/'+suffix not in report['localized_deficits']


@pytest.mark.parametrize('pairs,localized',[(14,False),(15,True),(20,True)])
@pytest.mark.parametrize('board',['echoear','p4xc'])
def test_corrected_doubletalk_requires_fifteen_pairs_on_each_board(pairs,localized,board):
    records=corrected_rows()
    for r in records:
        if r['board']==board and r['arm']=='B' and r['pair']>=pairs:r['residual_estimates']={}
    report=a.summarize(records)
    assert report['double_talk_near_end_localized'] is localized
    if not localized:assert report['localization']=='mixture difference observed; near-end localization unresolved'


def test_corrected_deficit_without_same_direction_mixture_does_not_localize():
    records=corrected_rows()
    for r in records:r['levels']={stage:dict(phrase=-30,word=-30) for stage in ('raw','aec','uplink')}
    report=a.summarize(records)
    assert report['p4_vs_s3']['double_talk_output_corrected/word']['deficit']
    assert not report['double_talk_near_end_localized']


def test_corrected_values_from_rejected_stages_never_count():
    records=corrected_rows();records[-1]['stage_validity']['aec']=False
    report=a.summarize(records)
    assert report['contributions']['p4xc']['double_talk_output_corrected/word']['n']==19
    assert report['boards']['p4xc']['B']['stages']['aec']['word']['n']==19
    assert not report['complete']


def test_summarizer_requires_explicit_validity_even_if_invalid_values_remain():
    records=rows();records[-1]['stage_validity']['uplink']=False
    report=a.summarize(records)
    assert report['boards']['p4xc']['B']['stages']['uplink']['word']['n']==19
    assert not report['complete'] and report['boards']['p4xc']['B']['stt']['n']==20
    del records[-1]['stage_validity']
    assert a.summarize(records)['boards']['p4xc']['B']['stages']['raw']['word']['n']==19


@pytest.mark.parametrize('p4median,p4ci,expected',[(0,[0,0],False),(-1.9999,[-2.2,-1.5],False),
    (-2,[-2.5,-1.5],True),(-2,[-3,0],False)])
def test_within_board_guard_blocks_s3_residual_gain_without_p4_loss(p4median,p4ci,expected):
    p4=dict(n=20,median=p4median,ci95=p4ci);s3=dict(n=20,median=4,ci95=[3,5])
    claim=a.near_end_claim(p4,s3,dict(deficit=True),dict(deficit=True))
    assert claim['localized'] is expected


def test_exact_silence_roundoff_cannot_win_long_capture_alignment():
    source=fixture_pcm()*.01
    target=np.r_[np.zeros(48000),source,np.zeros(10241)]
    assert a.locate(source,target)['offset_samples']==48000

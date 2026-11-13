import json
import wave
from pathlib import Path
import numpy as np
import pytest
from echoear_aec_fixture_scale import attenuate,pcm,verify_scaled
from echoear_aec_stimulus_levels import window_levels,stimulus_levels
from test_aec_campaign import plan,passing_holds,cell
from echoear_aec_campaign_analysis import campaign_verdict
from echoear_aec_campaign import Schedule,validate_spec


def wav(path,x):
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes(np.asarray(x,dtype='<i2').tobytes())


@pytest.mark.parametrize('gain',[-10,-6])
def test_scaling_is_exact_and_preserves_pcm_shape_without_clipping(tmp_path,gain):
    source=tmp_path/'source.wav';dest=tmp_path/'normal.wav'
    samples=np.array([-32768,-1001,-1,0,1,1001,32767],dtype='<i2');wav(source,samples)
    row=attenuate(source,dest,gain)
    assert verify_scaled(row) and row['output_clipped_samples']==0
    params,y=pcm(dest)
    np.testing.assert_array_equal(y,np.rint(samples.astype(float)*10**(gain/20)))
    assert params.framerate==48000 and params.nframes==len(samples)
    with pytest.raises(FileExistsError):attenuate(source,dest,gain)
    wav(dest,[1]*len(samples))
    with pytest.raises(ValueError,match='hash mismatch'):verify_scaled(row)


def test_only_attenuation_is_allowed(tmp_path):
    with pytest.raises(ValueError,match='Scale-down'):attenuate(tmp_path/'unused',tmp_path/'unused2',1)


def test_all_normal_fixtures_and_cells_are_bound():
    p=plan();assert validate_spec(p)
    fixtures=p['fixture_policy']['final_calibrated_paths_and_hashes']
    normal={k:v for k,v in fixtures.items() if k.endswith('-normal')}
    assert len(normal)==11
    for k,v in normal.items():
        assert v['gain_db']==(-10 if k=='stop-normal' else -6)
        assert verify_scaled(v)
    for key,n in [('cases',20),('spares',13)]:
        rows=[c for c in p[key] if c.get('voice_class')=='normal'];assert len(rows)==n
        for c in rows:
            old=next(o for o in p[key] if o['id']==c['id'].replace('qualification-normal','qualification-01'))
            for field in ['kind','volume_db','stop_offset_s','expected_class','expected_text','real_bot_reply','guard_ms']:
                assert c[field]==old[field]
            assert c['fixture_sha256']==normal[c['fixture_id']]['sha256']
    p['cases'].remove(next(c for c in p['cases'] if c.get('voice_class')=='normal'))
    with pytest.raises(ValueError,match='denominator'):validate_spec(p)


def test_firm_and_normal_cannot_pool_successes_or_eligible_counts():
    p=plan();p['qualification']=True;holds=passing_holds(p)
    result=campaign_verdict(p,holds);assert result['qualification']
    assert len(result['barge_in'])==6
    normal=[c for c in holds[0]['cases'] if c.get('voice_class')=='normal' and c['kind']=='stop']
    normal[0]['barge_success']=False;assert campaign_verdict(p,holds)['qualification']
    normal[1]['barge_success']=False;result=campaign_verdict(p,holds)
    assert result['barge_in']['-6:firm:stop']['successes']==10
    assert result['barge_in']['-6:normal:stop']['successes']==8 and not result['qualification']
    normal[1]['barge_success']=True;normal[0]['opportunity']='no-opportunity'
    assert not campaign_verdict(p,holds)['qualification']


def test_normal_spares_cannot_replace_firm_or_vice_versa():
    p=plan();a=cell('firm');b=dict(cell('normal'),voice_class='normal')
    q=Schedule([a,b],[dict(cell('normal-spare'),voice_class='normal')],p['noise_gate'])
    q.finish(*q.next(),True,{'firm':'no-opportunity'})
    assert q.unresolved==['firm'] and q.journal[-1]['replacements']==[]
    q.finish(*q.next(),True,{'normal':'no-opportunity'})
    assert q.journal[-1]['replacements'][0]['spare']=='normal-spare'


def test_level_diagnostic_uses_band_and_afe_delay_and_missing_is_not_zero():
    rate=16000;t=np.arange(64000)/rate;raw=32768*10**(-47/20)*np.sqrt(2)*np.sin(2*np.pi*1000*t)
    afe=np.r_[np.zeros(1024),raw[:-1024]*.5]
    quiet=np.random.default_rng(2).normal(size=32000)*.3
    result=window_levels(raw,afe,quiet,quiet,8000,40000)
    assert result['raw_mic1']['active_rms_dbfs']==pytest.approx(-47,abs=.05)
    assert result['afe']['active_rms_dbfs']==pytest.approx(-53.02,abs=.05)
    assert result['gate'] is False and 'echo' in result['attribution']
    assert not window_levels(raw,afe,quiet,quiet,8000,64000)['scorable']
    assert not stimulus_levels(dict(stimulus_skipped=True),{},raw,afe,quiet,quiet)['scorable']
    assert not stimulus_levels({}, {}, raw,afe,quiet,quiet)['scorable']

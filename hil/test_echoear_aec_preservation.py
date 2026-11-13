import numpy as np
from echoear_aec_preservation import compare,propose_tolerances,BANDS


def signal():
    t=np.arange(16000)/16000
    return sum(np.sin(2*np.pi*f*t) for f in (400,800,1600,3200,5500))*1000*(.6+.4*np.sin(2*np.pi*3*t))


def test_matched_level_keeps_attenuation_visible():
    x=signal();m=compare(x,x*.1)
    assert m['scorable'] and m['gate'] is False
    assert np.allclose(m['relative_band_delta_db'],0,atol=1e-8)
    assert abs(m['level_delta_db']+20)<1e-9
    assert m['envelope']['rmse_db']<1e-9
    assert abs(m['envelope']['correlation']-1)<1e-9


def test_spectral_and_envelope_damage_are_independent():
    x=signal();t=np.arange(len(x))/16000
    damaged=x+10000*np.sin(2*np.pi*800*t)
    assert compare(x,damaged)['relative_band_delta_db'][1]>3
    dropout=x.copy();dropout[4000:8000]=0
    assert compare(x,dropout)['envelope']['rmse_db']>5
    assert not compare(x,x[:-1])['scorable']
    assert not compare(x,x*0)['scorable']


def test_tolerance_proposal_uses_only_clean_firm_and_never_gates():
    good=dict(compare(signal(),signal()),voice_class='firm',clean=True)
    bad=dict(compare(signal(),signal()*2),voice_class='normal',clean=True)
    bad['relative_band_delta_db']=[100]*5
    dirty=dict(bad,voice_class='firm',clean=False)
    m=propose_tolerances([good,bad,dirty])
    assert m['n']==1 and m['gate'] is False and m['requires_user_decision']
    assert max(m['proposed_band_abs_limit_db'])==1

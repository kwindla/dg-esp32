from copy import deepcopy
import hashlib,json
import numpy as np
import pytest
from echoear_hot_stt import pair_gate,word_errors,align_prompt
from echoear_analog_probe import compare,prepare
from mic_protection_characterization import characterize
from step7_support import write_pcm,pcm


def test_already_open_silent_session_needs_state_not_a_transition(monkeypatch):
    import re
    import echoear_hot_stt as hot
    monkeypatch.setattr(hot.time,'sleep',lambda _:None)
    class Board:
        cursor=1
        def __init__(self):self.commands=[]
        def command(self,value):self.commands.append(value)
        def wait_for(self,pattern,timeout,since):
            assert re.search(pattern,'MIC_GATE t=18912275 closed=0 pre=-120.0 post=-120.0')
            assert not re.search(pattern,'MIC_GATE t=19000000 closed=1 pre=-20 post=-120')
    board=Board();hot.enable_scored_capture(board)
    assert board.commands==['audio 1','quiet 1']


def test_stt_pair_requires_engagement_and_no_wer_regression():
    rows=[dict(target_dbfs=t,acquisition_passed=True,firmware_limited_delta=n,
        headroom=dict(limited_samples=n,native_peak=peak),word_errors=dict(accuracy=1,wer=0))
        for t,n,peak in [(-39,0,2500),(-27,50,10000)]]
    assert pair_gate(rows)['passed']
    for case in ('inactive','baseline_limited','stt','acquisition','missing','order'):
        r=deepcopy(rows)
        if case=='inactive':r[1]['firmware_limited_delta']=0
        if case=='baseline_limited':r[0]['headroom']['limited_samples']=1
        if case=='stt':r[1]['word_errors']=dict(accuracy=.95,wer=.05)
        if case=='acquisition':r[0]['acquisition_passed']=False
        if case=='missing':r.pop()
        if case=='order':r.reverse()
        assert not pair_gate(r)['passed'],case


def test_true_wer_retains_insertions_over_one_and_number_normalization():
    assert word_errors('one','one two three')['wer']==2
    assert word_errors('It is 12:30','It is twelve thirty')['wer']==0


def test_received_pcm_alignment_does_not_replace_or_synthesize_input():
    rng=np.random.default_rng(1);source=rng.normal(size=32000)*1000
    observed=np.r_[np.zeros(5000),source*.7,np.zeros(4000)]
    segment,match=align_prompt(source,observed)
    assert match['offset_samples']==5000
    np.testing.assert_array_equal(segment,observed[3400:38600])
    with pytest.raises(ValueError):align_prompt(source,np.zeros(1000))


def tone_measure(fundamental,h2=-50,h3=-50):
    return dict(fundamental_dbfs=fundamental,harmonics_dbc=dict(H2=h2,H3=h3))


def test_analog_comparison_cancels_linear_response_and_flags_extra_compression():
    rows=[dict(relative_source_db=db,board=tone_measure(-30+db),jabra=tone_measure(-20+db)) for db in (-12,-6,0)]
    assert not compare(rows)['investigation_required']
    rows[-1]['board']['fundamental_dbfs']-=2
    assert compare(rows)['investigation_required']
    rows[-1]['board']=tone_measure(-30,h3=-30)
    assert compare(rows)['investigation_required']
    rows[-1]['jabra']=tone_measure(-20,h3=-30)
    assert not compare(rows)['investigation_required'] # Common source distortion is not isolated ADC overload.


def test_single_capture_bank_preserves_complete_loudest_fixture(tmp_path):
    bank=tmp_path/'bank';bank.mkdir();values={}
    for i,level in ((1,-25),(2,-18)):
        path=bank/f'tts_{i}.wav';write_pcm(path,np.sin(np.arange(32000))*10000)
        values[path.name]=dict(active_rms_dbfs=level,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    (bank/'manifest.json').write_text(json.dumps(values))
    out=tmp_path/'probe';result=prepare(out,bank)['tts_1.wav']
    assert len(result['probe']['tone_windows'])==3
    at=round(result['probe']['speech_start']*16000)
    np.testing.assert_array_equal(pcm(out/'tts_1.wav')[at:],pcm(bank/'tts_2.wav'))
    assert result['seconds']<28
    with pytest.raises(FileExistsError):prepare(out,bank)


def test_limiter_distortion_is_not_misrepresented_as_transparent():
    low=characterize(300,0);hot=characterize(300,6);alias=characterize(2500,6)
    assert low['thd_percent']<.001
    assert 9<hot['thd_percent']<11
    assert alias['nonharmonic_db']>-45
    assert hot['output_peak']<16384

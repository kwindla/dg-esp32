"""Offline regressions for Fable's 8f3 should-fix items."""
from pathlib import Path
import numpy as np
import pytest
from playback_quality import decoded_match
from step7_support import pcm
from step8e2b_analysis import device_send_cadence
from uplink_continuity import audio_idle_windows,link_tail


@pytest.mark.parametrize('before_ms,after_ms,seq_step,idle,excess_ms',[
    (1300,30,55,False,1300),
    (100,100,1,True,None),
    (101,30,1,False,101),
    (30,101,1,False,101),
    (30,30,2,False,20),
    (1300,1400,1,False,2700),
])
def test_idle_requires_containment_and_no_sequence_loss(before_ms,after_ms,seq_step,idle,excess_ms):
    states=[dict(enabled=0,host_s=10,device_us=10000000),
            dict(enabled=1,host_s=20,device_us=20000000)]
    packets=[dict(pc_id='one',ssrc=2,seq=65535,timestamp=0,observed_ms=10000-before_ms),
             dict(pc_id='one',ssrc=2,seq=(65535+seq_step)%65536,timestamp=960,observed_ms=20000+after_ms)]
    gate=link_tail(packets,packets[0]['observed_ms']/1000,packets[1]['observed_ms']/1000,
                   startup_seconds=0,excluded_windows=audio_idle_windows(states))
    gap=gate['gaps'][0]
    assert (gap['category']=='idle') is idle
    if not idle:assert gap['gap_ms']==pytest.approx(excess_ms)
    assert gate['whole_capture_maximum_ms']==10000+before_ms+after_ms
    sends=[dict(kind=12,reserved=1,ssrc=2,seq=r['seq'],timestamp=r['timestamp'],device_us=r['observed_ms']*1000) for r in packets]
    cadence=device_send_cadence(sends,sends[0]['device_us'],sends[1]['device_us'],
                                audio_idle_windows(states,clock='device_us'))
    assert cadence['idle']['count']==int(idle)
    assert cadence['raw_gaps_ms']['maximum']==10000+before_ms+after_ms
    if not idle:
        assert cadence['gaps_ms']['maximum']==pytest.approx(excess_ms)
        assert cadence['hard_ceiling_passed'] is (excess_ms<=60)


@pytest.mark.parametrize('pause_ms',[400,1000])
def test_real_reference_quiet_pause_stays_passing(pause_ms):
    source=pcm(Path(__file__).parent/'fixtures/step8f3-tts-reference.wav')
    cut=16000;n=pause_ms*16
    pause=np.random.default_rng(8).normal(0,1,n)
    reference=np.r_[source[:cut],pause,source[cut:]]
    decoded=np.r_[np.zeros(500),source[:cut],np.zeros(n),source[cut:],np.zeros(500)]
    gate=decoded_match(decoded,reference)
    assert gate['passed'] and gate['quiet_block_failures']==0
    assert gate['quiet_blocks_excluded']>0
    assert all(b['passed'] for b in gate['quiet_blocks'])


@pytest.mark.parametrize('speech_ms',[250,375])
def test_speech_copied_into_natural_reference_pause_fails(speech_ms):
    source=pcm(Path(__file__).parent/'fixtures/step8f3-tts-reference.wav')
    decoded=source.copy();start=16*1600;n=speech_ms*16
    decoded[start:start+n]=source[3200:3200+n]
    gate=decoded_match(np.r_[np.zeros(500),decoded,np.zeros(500)],source)
    assert not gate['passed']
    assert gate['quiet_block_failures']>=2
    failed=[b for b in gate['blocks'] if b.get('quiet_violation')]
    assert len(failed)==gate['quiet_block_failures']
    assert all(b['correlation']==0 for b in failed)


@pytest.mark.parametrize('tail',[0,180])
def test_twenty_minute_three_fault_soak_does_not_claim_growth(tail):
    from agent_conversation_test import soak_schedule
    assert soak_schedule(20,False,tail,fault_free=False)==(
        (120,'reconnect'),(300,'server-restart'),(480,'reconnect'))


def test_growth_modes_still_require_feasible_fit():
    from agent_conversation_test import soak_schedule
    from soak_memory import QUALIFICATION_MEMORY_TAIL_SECONDS
    assert soak_schedule(20,True)==soak_schedule(20,True,QUALIFICATION_MEMORY_TAIL_SECONDS)
    assert soak_schedule(20,False,0,fault_free=True)==()
    with pytest.raises(AssertionError,match='Pooled heap growth'):
        soak_schedule(20,True,0)
    with pytest.raises(AssertionError,match='Negative memory tail'):
        soak_schedule(20,False,-1,fault_free=False)

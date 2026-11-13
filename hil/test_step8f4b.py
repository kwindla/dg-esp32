"""Offline regressions for contained loss and tolerated quiet-tail truncation."""
from pathlib import Path
import numpy as np
import pytest
from playback_quality import decoded_match
from step7_support import pcm
from step8e2b_analysis import device_send_cadence
from uplink_continuity import link_tail


@pytest.mark.parametrize('seq_step,timestamp_step,expected_ms',[
    (2,1920,40),          # One lost packet at a pause boundary.
    (55,65280,1360),      # Timestamp estimate is larger.
    (55,960,1080),        # Sequence estimate is larger.
])
def test_contained_loss_uses_packet_estimate_on_both_clocks(seq_step,timestamp_step,expected_ms):
    packets=[dict(pc_id='one',ssrc=2,seq=65535,timestamp=2**32-960,observed_ms=9970),
             dict(pc_id='one',ssrc=2,seq=(65535+seq_step)%65536,
                  timestamp=(2**32-960+timestamp_step)%2**32,observed_ms=20030)]
    link=link_tail(packets,9.97,20.03,startup_seconds=0,
        excluded_windows=[dict(start_s=10,end_s=20,category='idle')])
    gap=link['gaps'][0]
    assert gap['category']=='eligible'
    assert gap['gap_ms']==pytest.approx(expected_ms)
    assert gap['raw_gap_ms']==10060
    assert gap['timestamp_step']==timestamp_step
    assert link['passed'] is (expected_ms<=60)
    sends=[dict(kind=12,reserved=1,ssrc=r['ssrc'],seq=r['seq'],timestamp=r['timestamp'],
                device_us=r['observed_ms']*1000) for r in packets]
    cadence=device_send_cadence(sends,9970000,20030000,
        [dict(start_us=10000000,end_us=20000000,category='idle')])
    assert cadence['idle']['count']==0
    assert cadence['gaps_ms']['maximum']==pytest.approx(expected_ms)
    assert cadence['raw_gaps_ms']['maximum']==10060
    assert cadence['hard_ceiling_passed'] is (expected_ms<=60)


@pytest.mark.parametrize('idle_first',[True,False])
def test_fault_overlapping_noncontained_idle_retains_raw_gap(idle_first):
    windows=[dict(start_s=10,end_s=20,category='idle'),
             dict(start_s=9,end_s=11,category='fault')]
    if not idle_first:windows.reverse()
    packets=[dict(pc_id='one',ssrc=2,seq=1,timestamp=0,observed_ms=8700),
             dict(pc_id='one',ssrc=2,seq=2,timestamp=960,observed_ms=20030)]
    link=link_tail(packets,8.7,20.03,startup_seconds=0,excluded_windows=windows)
    assert link['gaps'][0]['category']=='fault'
    assert link['gaps'][0]['gap_ms']==link['gaps'][0]['raw_gap_ms']==11330
    sends=[dict(kind=12,reserved=1,ssrc=r['ssrc'],seq=r['seq'],timestamp=r['timestamp'],
                device_us=r['observed_ms']*1000) for r in packets]
    cadence=device_send_cadence(sends,8700000,20030000,
        [dict(start_us=w['start_s']*1e6,end_us=w['end_s']*1e6,category=w['category']) for w in windows])
    assert cadence['excluded'][0]['categories']==['fault']
    assert cadence['excluded'][0]['gap_ms']==cadence['excluded'][0]['raw_gap_ms']==11330


@pytest.mark.parametrize('missing_samples',[1,160,320])
def test_truncated_trailing_quiet_block_judges_samples_present(missing_samples):
    source=pcm(Path(__file__).parent/'fixtures/step8f3-tts-reference.wav')
    reference=np.r_[source,np.zeros(4800)]
    gate=decoded_match(reference[:-missing_samples],reference)
    assert gate['passed'] and gate['quiet_block_failures']==0
    tail=gate['quiet_blocks'][-1]
    assert tail['decoded_samples']==tail['samples']-missing_samples
    assert tail['passed'] and tail['status']=='passed'
    assert gate['correlation']==pytest.approx(1)
    assert not decoded_match(reference[:-321],reference)['passed']


def test_empty_trailing_quiet_block_is_unavailable_and_excluded():
    source=pcm(Path(__file__).parent/'fixtures/step8f3-tts-reference.wav')
    padding=1600-len(source)%1600
    reference=np.r_[source,np.zeros(padding+160)]
    gate=decoded_match(reference[:-160],reference)
    assert gate['passed'] and gate['quiet_block_failures']==0
    tail=gate['quiet_blocks'][-1]
    assert tail['status']=='unavailable' and tail['passed'] is None
    assert tail['decoded_samples']==0 and tail['decoded_power'] is None
    assert all(b['source_start']!=tail['source_start'] for b in gate['blocks'])


def test_loud_signal_in_short_trailing_quiet_block_is_a_violation():
    source=pcm(Path(__file__).parent/'fixtures/step8f3-tts-reference.wav')
    reference=np.r_[source,np.zeros(4800)]
    decoded=reference[:-160].copy()
    start=len(reference)//1600*1600
    decoded[start:]=10000*np.sin(np.arange(len(decoded)-start)*2*np.pi*1000/16000)
    gate=decoded_match(decoded,reference)
    assert gate['correlation']<1 and gate['quiet_block_failures']>=1
    assert gate['quiet_blocks'][-1]['status']=='failed'


@pytest.mark.parametrize('at',[16000,25600])
def test_drift_insert_before_natural_pause_keeps_quiet_blocks_valid(at):
    source=pcm(Path(__file__).parent/'fixtures/step8f3-tts-reference.wav')
    # Firmware Insert repeats the previous active 20 ms frame at 3/4 amplitude.
    decoded=np.r_[np.zeros(500),source[:at],source[at-320:at]*.75,source[at:],np.zeros(500)]
    gate=decoded_match(decoded,source)
    assert gate['passed'] and gate['quiet_block_failures']==0

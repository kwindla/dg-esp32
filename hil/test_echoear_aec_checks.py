import copy
import unittest
import numpy as np
from echoear_aec_checks import erle,echo_campaign,barge_campaign

class AecChecksTests(unittest.TestCase):
    def test_measured_delay_and_reference_activity_are_required(self):
        x=np.sin(np.arange(16000)*.2)*1000
        self.assertFalse(erle(x,x/100,x)['available'])
        result=erle(x,x/10,x,delay_samples=0)
        self.assertAlmostEqual(result['median_db'],20,places=6)
        self.assertFalse(erle(x,x/100,np.zeros_like(x),delay_samples=0)['available'])
        self.assertFalse(erle(x,x[:-1],x,delay_samples=0)['available'])

    def test_saturation_cannot_be_hidden_by_good_erle(self):
        x=np.ones(16000)*32767
        result=erle(x,x/100,x,delay_samples=0)
        self.assertTrue(result['available']);self.assertFalse(result['passed'])

    def test_echo_requires_ten_unique_full_duplex_replies_and_no_false_turn(self):
        rows=[dict(id=i,aec_enabled=True,bot_full_duplex=True,firmware_full_duplex=True,ns_enabled=False,agc_enabled=False,gain_matched=True,false_user_turns=0,
                   erle=dict(available=True,median_db=21,saturated_samples=0),
                   **{name:{'passed':True} for name in ('transport','clips','uplink','admission')}) for i in range(10)]
        self.assertTrue(echo_campaign(rows,list(range(10)))['passed'])
        for field,value in (('bot_full_duplex',False),('firmware_full_duplex',False),('ns_enabled',True),('false_user_turns',1),('id',1),('transport',{'passed':False})):
            altered=copy.deepcopy(rows);altered[0][field]=value
            self.assertFalse(echo_campaign(altered,list(range(10)))['passed'])

    def test_barge_requires_acoustic_stop_in_every_trial_and_four_exact_transcripts(self):
        rows=[dict(id=i,aec_enabled=True,bot_full_duplex=True,firmware_full_duplex=True,ns_enabled=False,agc_enabled=False,gain_matched=True,expected='Stop and listen',transcript='Stop, and listen!',
                   jabra_user_onset_s=10,jabra_bot_end_s=11,
                   **{name:{'passed':True} for name in ('transport','clips','uplink','admission')}) for i in range(5)]
        rows[0]['transcript']='wrong'
        self.assertTrue(barge_campaign(rows,list(range(5)))['passed'])
        rows[1]['jabra_bot_end_s']=11.001
        self.assertFalse(barge_campaign(rows,list(range(5)))['passed'])
        rows[1]['jabra_bot_end_s']=11;rows[1]['transcript']='wrong'
        self.assertFalse(barge_campaign(rows,list(range(5)))['passed'])
        rows[1]['transcript']='Stop and listen';rows[1]['jabra_bot_end_s']=None
        self.assertFalse(barge_campaign(rows,list(range(5)))['passed'])

if __name__=='__main__':unittest.main()


def test_live_admission_requires_counters_and_complete_dispatch():
    from echoear_aec_checks import live_admission
    a = dict(generation=1, pc_id='p', drop_reasons={'admission_budget': 0},
             data_admission={'received': 4, 'dispatched': 4})
    b = copy.deepcopy(a)
    assert live_admission(a,b,recorder_messages=0)['passed']
    b['data_admission']['received'] = 5
    assert not live_admission(a,b,recorder_messages=0)['passed']
    b['data_admission']['dispatched'] = 5
    b['drop_reasons']['admission_budget'] = 1
    assert not live_admission(a,b,recorder_messages=0)['passed']
    b = copy.deepcopy(a); b['generation'] = 2
    assert not live_admission(a,b,recorder_messages=0)['passed']
    assert not live_admission({}, {})['passed']


def test_live_admission_requires_progress_for_recorder_messages():
    from echoear_aec_checks import live_admission
    a = dict(generation=1, pc_id='p', drop_reasons={'admission_budget': 0},
             data_admission={'received': 6, 'dispatched': 6})
    b = copy.deepcopy(a)
    assert not live_admission(a, b)['passed']
    assert not live_admission(a, b, recorder_messages=7)['passed']
    for count in (6, 7, 8):
        b['data_admission'] = dict(received=6+count, dispatched=6+count)
        assert live_admission(a, b, recorder_messages=7)['passed'] == (count >= 7)
    for invalid in (None, True, -1, 7.0):
        assert not live_admission(a, b, recorder_messages=invalid)['passed']


def test_step6_retained_reply2_stall():
    """Use original evidence, never commit captures/fixtures into the repository."""
    import json
    from pathlib import Path
    import pytest
    from echoear_aec_checks import live_admission, outgoing_rtvi_count
    root = Path('/home/khkramer/src/evidence/echoear/aec-demo/step-6/live-2')
    if not root.exists(): pytest.skip('Retained step-6 bench evidence not installed')
    folder = root/'cap-02-a1'
    before = json.loads((folder/'pre-window-after.json').read_text())
    after = json.loads((folder/'post-window-after.json').read_text())
    requests = [json.loads(x) for x in (root/'evaluation/events.jsonl').read_text().splitlines()]
    start = next(e['t'] for e in requests if e.get('event') == 'script-request' and e.get('case_id') == 'cap-02-a1')
    events = [json.loads(x) for p in (root/'sessions').glob('*/events.jsonl') for x in p.read_text().splitlines()]
    count = outgoing_rtvi_count(events, int(start*1e9), int((start+16)*1e9))
    check = live_admission(before, after, recorder_messages=count)
    assert count == 7 and check['received_delta'] == check['dispatched_delta'] == 0
    assert check['admission_budget_delta'] == 0 and not check['passed']

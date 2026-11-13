import copy
import os
from unittest.mock import patch
import unittest
from standalone_proof import quiet_gate,turn_complete,configure_environment,ROOT

class StandaloneProofTests(unittest.TestCase):
    def test_echoear_fixture_origin_selected_before_import(self):
        with patch.dict(os.environ, {'STEP9A_FIXTURES':'legacy'}):
            configure_environment()
            self.assertEqual(os.environ['STEP9A_FIXTURES'],str(ROOT/'hil/fixtures/echoear360'))
            self.assertEqual(os.environ['DG_BOARD'],'echoear')

    def test_reply_must_follow_new_user_turn(self):
        old=['Transcript: user: old','Transcript: assistant: old']
        self.assertFalse(turn_complete(old,old)['passed'])
        self.assertFalse(turn_complete(old,old+['Transcript: assistant: late','Transcript: user: new'])['passed'])
        self.assertTrue(turn_complete(old,old+['Transcript: user: new','Transcript: assistant: reply'])['passed'])

    def test_wifi_gate_records_but_does_not_gate_wired(self):
        before=dict(monotonic_s=0,interfaces={n:dict(rx_bytes=0,tx_bytes=0) for n in ('enp7s0','wlp8s0')})
        after=copy.deepcopy(before);after['monotonic_s']=30
        after['interfaces']['enp7s0']['rx_bytes']=1_000_000
        after['interfaces']['wlp8s0']['rx_bytes']=1_000_001
        self.assertTrue(quiet_gate(before,after)['passed'])
        after['interfaces']['wlp8s0']['rx_bytes']=2_000_001
        self.assertFalse(quiet_gate(before,after)['passed'])
        del after['interfaces']['wlp8s0']
        self.assertFalse(quiet_gate(before,after)['passed'])

if __name__=='__main__':unittest.main()

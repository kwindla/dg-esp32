import unittest
from unittest.mock import Mock,patch
from echoear_touch_proof import phase_sequence,new_session,inject_during_connecting
class TouchProof(unittest.TestCase):
    def test_connecting_injection_is_consumed_in_same_phase_without_request(self):
        before=dict(phase=0,generation=3,requests=1,request_rejected=0,driver=dict(injected=2,injected_ignored=1))
        after=dict(before,driver=dict(injected=3,injected_ignored=2))
        touch=Mock(side_effect=[before,after]);inject=Mock()
        def wait(fn,seconds):
            self.assertEqual(seconds,5)
            return fn()
        result=inject_during_connecting(touch,inject,wait)
        self.assertTrue(result['passed']);inject.assert_called_once()
    def test_ready_consumption_cannot_pass_as_connecting_injection(self):
        before=dict(phase=0,generation=3,requests=1,request_rejected=0,driver=dict(injected=2,injected_ignored=1))
        for changes in (dict(phase=1),dict(generation=5),dict(requests=2),dict(request_rejected=1)):
            after=dict(before,driver=dict(injected=3,injected_ignored=2));after.update(changes)
            result=inject_during_connecting(Mock(side_effect=[before,after]),Mock(),lambda fn,seconds:fn())
            self.assertFalse(result['passed'])
    def test_only_ordered_completed_screens_prove_transition(self):
        rows=[dict(screen=s,completed_us=t,rendered=n) for n,(s,t) in enumerate(((0,10),(1,20),(2,30)),1)]
        self.assertTrue(phase_sequence(rows)['passed'])
        self.assertFalse(phase_sequence(rows[1:])['passed'])
        self.assertFalse(phase_sequence(rows[::-1])['passed'])
        rows[2]['rendered']=rows[1]['rendered'];self.assertFalse(phase_sequence(rows)['passed'])
    def test_new_pipeline_is_correlated_to_new_pc_and_arm_time(self):
        events=[dict(pc_id='old',event='session-start',t=2),dict(pc_id='new',event='session-start',t=3)]
        self.assertEqual(new_session(events,'old',2),'new')
        self.assertIsNone(new_session(events,'old',4))
        events.append(dict(pc_id='extra',event='session-start',t=3));self.assertIsNone(new_session(events,'old',2))
if __name__=='__main__':unittest.main()

import copy
import unittest
from unittest.mock import patch
from step9a_echoear_flood import verdict,prepare_flood_board

class FloodVerdict(unittest.TestCase):
    def test_cold_and_restored_demo_initialize_before_display_mode_commands(self):
        for initialized in (False,True):
            class Board:
                def __init__(self):self.initialized=initialized;self.commands=[]
                def command(self,command):
                    if command.startswith('disp ') and not self.initialized:
                        raise RuntimeError('disp init first')
                    self.commands.append(command)
            board=Board()
            def provision(*args):
                board.initialized=True
                return dict(budget=dict(budget=491520))
            with patch('step9a_echoear_flood.prepare_board',side_effect=provision) as helper:
                result=prepare_flood_board(board,'http://fixture:7861','own-budget.json',{'budget':491520})
            self.assertEqual(helper.call_count,1)
            self.assertEqual(result['budget']['budget'],491520)
            self.assertIn('disp bench cat 30',board.commands)
    def evidence(self):
        def sample(n):
            return dict(heap=dict(internal=dict(minimum_free=90000,largest=40000,sampled_largest_minimum=40000,window_session_minimum_free=100000)),
                        failed_allocations=dict(internal=0,total=0),display=dict(dma_errors=0,dead=False,failed=False),
                        data_admission=dict(received=n*800,dispatched=n*790,sdk_backpressure=0),drop_reasons=dict(admission_budget=n*10))
        return dict(rate='25',wire_show=dict(accepted=True),wire_show_rendered=True,samples=[sample(0),sample(1)],clocks=[dict(device_us=1,display=dict(rendered=1)),dict(device_us=30000001,display=dict(rendered=850))],
                    completed=dict(pending_bytes=0),sending_complete=dict(count=750,elapsed_s=30),monitor=dict(passed=True,app_control_min_free=4096))
    def test_accept_and_fail_each_required_evidence(self):
        good=self.evidence();self.assertTrue(verdict(good)['passed'])
        for key,value in [('dma_errors',1),('dead',True),('failed',True)]:
            bad=copy.deepcopy(good);bad['samples'][-1]['display'][key]=value
            self.assertFalse(verdict(bad)['passed'])
        bad=copy.deepcopy(good);bad['samples'][-1]['failed_allocations']['total']=1
        self.assertFalse(verdict(bad)['passed'])
        bad=copy.deepcopy(good);bad['clocks'][-1]['display']['rendered']=1
        self.assertFalse(verdict(bad)['passed'])
        bad=copy.deepcopy(good);bad['completed']['pending_bytes']=1
        self.assertFalse(verdict(bad)['passed'])
    def test_saturation_requires_actual_counted_shedding(self):
        r=self.evidence();r['rate']='saturating';self.assertTrue(verdict(r)['passed'])
        r['samples'][-1]['drop_reasons']['admission_budget']=0
        self.assertFalse(verdict(r)['passed'])
    def test_delayed_chat_needs_sustained_media_and_immediate_budget_activation(self):
        r=self.evidence();r['prechat_hold_ms']=3000
        self.assertFalse(verdict(r)['passed'])
        r['samples'][0].update(rtp_before_chat_packets=150,first_rtp_before_chat_us=1000,
            connect_timings={'channel_open_us':3001000},peer_profile={'prechat_receive_budget_activations':1})
        self.assertTrue(verdict(r)['passed'])
        r['samples'][0]['peer_profile']['prechat_receive_budget_activations']=0
        self.assertFalse(verdict(r)['passed'])

if __name__=='__main__':unittest.main()

import copy
import unittest
from usb_absence import network_gate, score
from playback_quality import TRANSPORT_COUNTERS


def evidence():
    before = dict(playout_schema=2, pc_id='same', generation=1, sent=1, received=1,
                  mic_in_turn_clips=0, mic_gate_open_clips=0, mic_gate_closed_clips=0,
                  console_output={}, display={'rendered': 10},
                  tasks=[dict(name=name, id=i, runtime_us=10) for i,name in enumerate(
                      ('audio-capture', 'audio-playback', 'peer-loop', 'app-control'))])
    before['tasks'].append(dict(name='sys_evt', id=50, runtime_us=10, stack_free_bytes=1600))
    before.update({k:0 for k in TRANSPORT_COUNTERS})
    after = copy.deepcopy(before)
    after.update(sent=100, received=100, display={'rendered': 200})
    for task in after['tasks']:
        task['runtime_us'] += 100
    events = [dict(t=t, turn=i, pc_id='same', event=event) for i,t in enumerate((10,30,50), 1)
              for event in ('vad-stop', 'usb-fixture-reply', 'bot-audio-end')]
    return before, after, events


class AbsenceTests(unittest.TestCase):
    def test_complete_same_session_three_windows_pass(self):
        self.assertTrue(score(*evidence(), 0, 60)['passed'])

    def test_losses_resets_missing_turn_and_stalled_workers_fail(self):
        for defect in ('lost', 'pc_id', 'no_turn', 'stalled_worker', 'no_display', 'no_output_telemetry', 'no_fixture_reply', 'small_system_stack'):
            with self.subTest(defect=defect):
                before, after, events = evidence()
                if defect == 'lost':
                    after['lost'] = 1
                elif defect == 'pc_id':
                    after['pc_id'] = 'reconnected'
                elif defect == 'no_turn':
                    events = [e for e in events if e['t'] != 30]
                elif defect == 'stalled_worker':
                    after['tasks'][0]['runtime_us'] = 10
                elif defect == 'no_display':
                    after['display']['rendered'] = 10
                elif defect == 'small_system_stack':
                    after['tasks'][-1]['stack_free_bytes'] = 60
                elif defect == 'no_fixture_reply':
                    events = [e for e in events if e['event'] != 'usb-fixture-reply']
                else:
                    del after['console_output']
                self.assertFalse(score(before, after, events, 0, 60)['passed'])

    def test_missing_busy_or_reset_nic_cannot_pass(self):
        before = dict(monotonic_s=0, interfaces={i:dict(rx_bytes=100,tx_bytes=100)
                      for i in ('enp7s0','wlp8s0')})
        after = copy.deepcopy(before)
        after['monotonic_s'] = 30
        self.assertTrue(network_gate(before, after)['passed'])
        for value in (-1, 2_000_101):
            after['interfaces']['wlp8s0']['rx_bytes'] = value
            self.assertFalse(network_gate(before, after)['passed'])
        del after['interfaces']['wlp8s0']
        self.assertFalse(network_gate(before, after)['passed'])

class ToolEnvironmentTests(unittest.TestCase):
    def test_separate_idf_interpreter_preflight_never_passes_a_port(self):
        import step9a_echoear
        from unittest.mock import Mock, patch
        with patch.dict('os.environ', {'ESPTOOL_PYTHON': '/idf/python'}), \
             patch.object(step9a_echoear.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertEqual(step9a_echoear.check_esptool(), '/idf/python')
            self.assertEqual(run.call_args.args[0], ['/idf/python', '-c', 'import esptool'])

    def test_missing_esptool_fails_before_hardware(self):
        import step9a_echoear
        from unittest.mock import Mock, patch
        with patch.object(step9a_echoear.subprocess, 'run', return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'ESPTOOL_PYTHON'):
                step9a_echoear.check_esptool()


if __name__ == '__main__':
    unittest.main()

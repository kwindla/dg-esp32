import unittest
from demo_policy import capability_aec, turn_policy


class DemoPolicyTests(unittest.TestCase):
    def test_bot_never_mutes_the_user(self):
        # Decision 141: the device alone gates the mic; one policy for every image.
        for profile in ('demo', 'dev'):
            self.assertEqual(turn_policy(profile=profile).user_mute_strategies, [])

    def test_override_argument_is_gone(self):
        with self.assertRaises(TypeError):
            turn_policy('1')


class CapabilityTests(unittest.TestCase):
    def test_valid_capability(self):
        for aec in (False, True):
            body = {'audio': {'v': 1, 'aec': aec, 'ref_gain_db': 15, 'guard_ms': 400}}
            self.assertIs(capability_aec(body), aec)

    def test_unknown_legacy_and_malformed_report_no_aec(self):
        good = {'v': 1, 'aec': True, 'ref_gain_db': 15, 'guard_ms': 400}
        bodies = [None, {}, [], 'aec', True, {'aec': True}, {'audio': None},
                  {'audio': []}, {'audio': True}, {'audio': {}},
                  {'body': {'audio': good}}]
        for key, values in {'v': [None, True, 1.0, '1', 2],
                            'aec': [None, 1, 0, 'true', 'false'],
                            'ref_gain_db': [None, True, -3, 14, 36, '15'],
                            'guard_ms': [None, True, -1, 1001, '400', 400.0]}.items():
            bodies.extend({'audio': dict(good, **{key: value})} for value in values)
            bodies.append({'audio': {k: v for k, v in good.items() if k != key}})
        for body in bodies:
            with self.subTest(body=body):
                self.assertFalse(capability_aec(body))


if __name__ == '__main__':
    unittest.main()

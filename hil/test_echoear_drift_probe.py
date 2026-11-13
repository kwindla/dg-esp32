import unittest
from echoear_drift_probe import drift_events

class DriftProbeTests(unittest.TestCase):
    def test_decision_telemetry_is_typed_and_preserved(self):
        rows=drift_events(['noise','PLAYOUT_DRIFT t=123 render=100 gen=3 playout=1 edit=drop ready=8 target=5 sum=351 n=50 persist_us=1000000'])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['ready'],8)
        self.assertEqual(rows[0]['target'],5)
        self.assertEqual(rows[0]['edit'],'drop')
        self.assertEqual(rows[0]['persist_us'],1000000)

if __name__=='__main__':unittest.main()

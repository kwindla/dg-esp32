import base64
import unittest
import numpy as np
from echoear_aec_capture import decode_capture


class AecCaptureTests(unittest.TestCase):
    def lines(self):
        data = np.array([[1,-2,3],[-32768,32767,0]],dtype='<i2')
        return ['AEC_DUMP BEGIN frames=2 channels=3 rate=16000 order=mic,ref,processed adapter_delay=512 measured_delay=unknown truncated=0 ns=0 agc=0',
                'AEC_PCM 0 '+base64.b64encode(data.tobytes()).decode(), 'AEC_DUMP END frames=2']

    def test_preserves_signed_channels_and_does_not_invent_measured_delay(self):
        capture = decode_capture(self.lines())
        self.assertEqual(capture['pcm'].tolist(), [[1,-2,3],[-32768,32767,0]])
        self.assertEqual(capture['adapter_delay_samples'],512)
        self.assertIsNone(capture['measured_delay_samples'])

    def test_missing_duplicate_out_of_order_and_truncated_records_fail(self):
        lines = self.lines()
        for bad in (lines[:2], lines[:1]+lines[2:], lines[:2]+lines[1:],
                    [lines[0],lines[1].replace('PCM 0 ','PCM 1 '),lines[2]],
                    [lines[0].replace('truncated=0','truncated=1')]+lines[1:]):
            with self.assertRaises(ValueError):decode_capture(bad)

    def test_version_two_preserves_all_slots_in_explicit_mic_order(self):
        data = np.array([[1,2,-3,-4,5],[-32768,32767,-1,0,42]],dtype='<i2')
        header = 'AEC_DUMP BEGIN schema=2 frames=2 channels=5 rate=16000 order=mic1,mic2,mic3,mic4,processed adapter_delay=512 measured_delay=unknown truncated=0 ns=0 agc=0'
        lines = [header,'AEC_PCM 0 '+base64.b64encode(data.tobytes()).decode(),'AEC_DUMP END frames=2']
        capture = decode_capture(lines)
        self.assertEqual(capture['schema'],2)
        self.assertEqual(capture['channels'],['mic1','mic2','mic3','mic4','processed'])
        np.testing.assert_array_equal(capture['pcm'],data)
        for wrong in (header.replace('schema=2 ',''),header.replace('mic2,mic3','mic3,mic2'),header.replace('channels=5','channels=3')):
            with self.assertRaises(ValueError):decode_capture([wrong]+lines[1:])

    def test_timing_bounds_and_missing_rows(self):
        lines = self.lines()
        capture = decode_capture(lines[:1]+['AEC_TIME start=0 end=2 lower_us=100 upper_us=300 gap=0']+lines[1:])
        self.assertEqual(capture['timing'].tolist(), [[0,2,100,300,0]])
        self.assertEqual(decode_capture(lines)['timing'].shape, (0,5))
        with self.assertRaisesRegex(ValueError, 'Missing capture timing'):
            decode_capture(lines[:-1]+[lines[-1]+' timing_blocks=1'])
        for row in ('AEC_TIME start=1 end=2 lower_us=100 upper_us=300 gap=0',
                    'AEC_TIME start=0 end=1 lower_us=100 upper_us=300 gap=0',
                    'AEC_TIME start=0 end=2 lower_us=300 upper_us=100 gap=0',
                    'AEC_TIME garbage'):
            with self.assertRaises(ValueError):
                decode_capture(lines[:1]+[row]+lines[1:])

    def test_thirty_second_capture_round_trips_all_1500_timing_rows(self):
        frames = 30 * 16000
        pcm = np.arange(frames * 5, dtype=np.int16).reshape(frames, 5)
        lines = [f'AEC_DUMP BEGIN schema=2 frames={frames} channels=5 rate=16000 order=mic1,mic2,mic3,mic4,processed adapter_delay=512 measured_delay=unknown truncated=0 ns=0 agc=0']
        rows = [[at, at+320, at*1000000//16000-75000, (at+320)*1000000//16000, 0]
                for at in range(0, frames, 320)]
        lines += ['AEC_TIME start=%d end=%d lower_us=%d upper_us=%d gap=%d' % tuple(row) for row in rows]
        data = pcm.tobytes()
        lines += [f'AEC_PCM {at//2} '+base64.b64encode(data[at:at+384]).decode()
                  for at in range(0, len(data), 384)]
        lines += [f'AEC_DUMP END frames={frames} timing_blocks={len(rows)}']
        capture = decode_capture(lines)
        self.assertEqual(len(capture['timing']), 1500)
        np.testing.assert_array_equal(capture['timing'], rows)
        np.testing.assert_array_equal(capture['pcm'], pcm)
        with self.assertRaisesRegex(ValueError, 'Invalid capture timing extent'):
            decode_capture(lines[:750] + lines[751:])


if __name__ == '__main__':
    unittest.main()

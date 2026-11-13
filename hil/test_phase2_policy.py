"""Regression tests for the brief's distribution and outlier rules."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from phase2_campaign import cohort, compare, distribution
from phase2_cpu import intervals
from phase2_endurance import uplink_summary, snapshot


def population():
    return dict(media_sessions=9,boot_errors=[],metrics={
        'encode_p95_us':distribution([20000]*9),
        'missing':distribution([0]*9),
        'reply_correlation':distribution([.97]*9)},
        events=dict(missing=0,i2s_underflows=0,rebuffers=0),
        samples=[],reruns=[],audio_counters=[[0,0,0]],
        heap_phases=dict(ready=dict(general_internal=distribution([120000]*9))))


class DistributionPolicy(unittest.TestCase):
    def test_incomplete_heap_census_retries_without_losing_media_perf(self):
        row=dict(heap=dict(main_largest=32000,psram=6000000),
                 regions=[dict(kind=k) for k in ['main','rom','sram2','reserve','rtc','external']],
                 audio=[[0,0,0]],tasks=[],perf=dict(received=1234))
        complete=copy.deepcopy(row);complete['heap']['general_internal']=128000
        with mock.patch('phase2_endurance.snapshot_once',side_effect=[row,complete]) as collect, \
             mock.patch('phase2_endurance.time.sleep'):
            result=snapshot(None)
        self.assertEqual(collect.call_count,2)
        self.assertTrue(result['census_complete'])
        self.assertEqual(result['incomplete_census_attempts'][0]['perf']['received'],1234)

    def test_soak_only_retains_missing_census_after_three_attempts(self):
        row=dict(heap={},regions=[],audio=[],tasks=[],perf=dict(received=1234))
        with mock.patch('phase2_endurance.snapshot_once',side_effect=lambda _:copy.deepcopy(row)), \
             mock.patch('phase2_endurance.time.sleep'):
            with self.assertRaises(AssertionError):snapshot(None,active=True)
            result=snapshot(None,active=True,allow_incomplete=True)
        self.assertFalse(result['census_complete'])
        self.assertEqual(result['perf']['received'],1234)
        self.assertNotIn('general_internal',result['heap'])
        self.assertEqual(len(result['incomplete_census_attempts']),2)
        json.dumps(result)  # Retained attempt history must not contain cycles.

    def test_soak_sequence_wrap_reordering_and_duplicates(self):
        packets=[dict(seq=(65530+i)%65536,timestamp=i*960,arrival_ms=i*20) for i in range(90000)]
        packets[20],packets[21]=packets[21],packets[20]
        packets.insert(25,dict(packets[24]))
        result=uplink_summary(packets)
        self.assertEqual(result['unique_timestamps'],90000)
        self.assertEqual(result['wire_missing_lower_bound'],0)
        del packets[100]
        self.assertEqual(uplink_summary(packets)['wire_missing_lower_bound'],1)

    def test_nine_sample_p90_is_second_worst(self):
        self.assertEqual(distribution(range(1,10))['p90'],8)
        self.assertEqual(distribution(range(1,10))['p50'],5)

    def test_two_more_loss_sessions_fail_even_with_zero_median(self):
        a=population();b=copy.deepcopy(a)
        b['events']['missing']=2;b['heap_phases']['ready']['general_internal']=distribution([130000]*9)
        self.assertFalse(compare(a,b)['checks']['missing_event_count'])
        b['events']['missing']=1
        self.assertTrue(compare(a,b)['checks']['missing_event_count'])

    def test_small_timing_floor_and_gain_requirement(self):
        a=population();b=copy.deepcopy(a)
        a['metrics']['encode_p95_us']=distribution([1000]*9)
        b['metrics']['encode_p95_us']=distribution([2000]*9)
        self.assertTrue(compare(a,b)['checks']['encode_p95_us'])
        self.assertFalse(compare(a,b)['checks']['ready_gain'])
        b['metrics']['encode_p95_us']=distribution([2001]*9)
        self.assertFalse(compare(a,b)['checks']['encode_p95_us'])

    def test_nonreproducing_failure_remains_in_statistics(self):
        a=population();b=copy.deepcopy(a)
        b['samples']=[dict(boot='boot1',index=2,acceptance_error='Fixture correlation/duration failed')]
        b['reruns']=[dict(boot='boot1',index=2)]
        b['metrics']['reply_correlation']=distribution([.6]+[.97]*8)
        before=copy.deepcopy(b['metrics'])
        result=compare(a,b)
        self.assertTrue(result['checks']['reproduced_media_failures'])
        self.assertEqual(b['metrics'],before)
        self.assertEqual(len(result['nonreproducing_failures']),1)
        b['reruns'][0]['acceptance_error']='Fixture correlation/duration failed'
        self.assertFalse(compare(a,b)['checks']['reproduced_media_failures'])

    def test_missing_media_sample_never_passes(self):
        a=population();b=copy.deepcopy(a);b['media_sessions']=8
        self.assertFalse(compare(a,b)['checks']['complete'])

    def test_rerun_exception_does_not_erase_formal_loss_count(self):
        a=population();b=copy.deepcopy(a)
        b['events']['missing']=2
        b['samples']=[dict(boot='boot1',index=i,rtp=dict(missing=1)) for i in (1,2)]
        b['reruns']=[dict(boot='boot1',index=i,rtp=dict(missing=0)) for i in (1,2)]
        result=compare(a,b)
        self.assertTrue(result['checks']['missing_event_count'])
        self.assertEqual(result['B_events']['missing'],2)
        self.assertEqual(len(result['nonreproducing_event_exceptions']['missing']),2)

    def test_performance_exception_requires_encoder_improvement(self):
        a=population();b=copy.deepcopy(a)
        self.assertFalse(compare(a,b,performance=True)['checks']['ready_gain'])
        b['metrics']['encode_p95_us']=distribution([19000]*9)
        self.assertTrue(compare(a,b,performance=True)['checks']['ready_gain'])

    def test_different_rerun_failure_does_not_reproduce_original(self):
        a=population();b=copy.deepcopy(a)
        b['samples']=[dict(boot='boot1',index=1,closed=dict(queue_dropped=1),
                           acceptance_error='Local queue overflow or invalid frames')]
        b['reruns']=[dict(boot='boot1',index=1,closed=dict(queue_dropped=0),rtp=dict(missing=1),
                          acceptance_error='Server RTP loss/timing')]
        self.assertTrue(compare(a,b)['checks']['reproduced_media_failures'])
        # A queue error masked by the earlier RTP assertion still reproduces.
        b['reruns'][0]['closed']['queue_dropped']=1
        self.assertFalse(compare(a,b)['checks']['reproduced_media_failures'])

    def test_hardware_counter_deltas_include_interleaved_rerun(self):
        def sample(index,total):
            return dict(index=index,closed=dict(i2s_underflows=total),
                        rtp=dict(missing=0,max_gap_ms=20,p95_gap_ms=20),
                        greeting=dict(correlation=.97),reply=dict(correlation=.97))
        original=dict(sessions=[sample(1,1),sample(2,1),sample(3,2)],reruns=[sample(1,1)])
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);boot=directory/'boot1';boot.mkdir()
            path=boot/'result.json';path.write_text(json.dumps(original))
            with mock.patch('phase2_campaign.summarize',return_value=dict(heap_phases={})):
                result=cohort(directory)
            self.assertEqual([s['closed']['i2s_underflows'] for s in result['samples']],[1,0,1])
            self.assertEqual(result['reruns'][0]['closed']['i2s_underflows'],0)
            self.assertEqual(result['events']['i2s_underflows'],2)
            self.assertEqual(json.loads(path.read_text()),original)

    def test_cpu_uses_live_task_ids_and_excludes_speech(self):
        text='''HEAP phase=ready t=1000000
I (1100) STACK task=audio-tx free_bytes=100 priority=11 runtime_us=10000 core=1 task_id=18
HEAP phase=stats t=1100000
I (1200) STACK task=audio-playback free_bytes=100 priority=13 runtime_us=20000 core=1 task_id=16
HEAP phase=streaming t=5000000
I (10100) STACK task=audio-tx free_bytes=90 priority=11 runtime_us=2710000 core=1 task_id=18
I (10200) STACK task=audio-playback free_bytes=90 priority=13 runtime_us=2270000 core=1 task_id=16
HEAP phase=after-disconnect t=11000000
HEAP phase=ready t=12000000
I (12100) STACK task=audio-tx free_bytes=100 priority=11 runtime_us=0 core=1 task_id=26
HEAP phase=streaming t=13000000
HEAP phase=stats t=14000000
I (14100) STACK task=audio-tx free_bytes=90 priority=11 runtime_us=1000000 core=1 task_id=26
'''
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'serial.log';path.write_text(text)
            path.with_name('result.json').write_text(json.dumps(dict(sessions=[
                dict(heap_rows=[dict(phase='ready',t=1000000)])])))
            measured=intervals(path)
        self.assertEqual(len(measured),1)
        cpu={t['name']:t['cpu_percent'] for t in measured[0]['tasks']}
        self.assertEqual(cpu,{'audio-tx':30,'audio-playback':25})

if __name__=='__main__':unittest.main()

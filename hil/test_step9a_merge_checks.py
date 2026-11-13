import unittest
from step9a_merge_checks import memory_gate,audio_gate,reply_fps,census_gate,rendered_show
from step9a_r2_checks import audit
class MergeChecks(unittest.TestCase):
    def test_memory_missing_floor_and_failed_allocation(self):
        s={'heap':{'internal':{'minimum_free':81920,'largest':32768}},'failed_allocations':{'internal':0,'total':0}}
        self.assertTrue(memory_gate([s])['passed']);self.assertFalse(memory_gate([])['passed'])
        s['heap']['internal']['minimum_free']-=1;self.assertFalse(memory_gate([s])['passed'])
        s['heap']['internal']['minimum_free']+=1;s['failed_allocations']['internal']=1
        self.assertFalse(memory_gate([s])['passed'])
    def test_any_playback_write_error_fails_even_if_transport_clean(self):
        s={'audio':dict(drops=0,read_errors=0,write_errors=0,clips=3),'i2s_underflows':0,'decode_errors':0,'pcm_queue_drops':0,'drop_reasons':{'uplink_gap':0}}
        t={'audio':dict(s['audio']),'i2s_underflows':0,'decode_errors':0,'pcm_queue_drops':0,'drop_reasons':{'uplink_gap':0}}
        self.assertTrue(audio_gate(s,t)['passed']);t['audio']['write_errors']=1
        self.assertFalse(audio_gate(s,t)['passed']);self.assertFalse(audio_gate({}, {})['passed'])
    def test_liveness_is_not_fps(self):
        a={'device_us':1000000,'display':{'rendered':1}}
        b={'device_us':3000000,'display':{'rendered':3}}
        self.assertFalse(reply_fps(a,b)['passed']);b['display']['rendered']=49
        self.assertTrue(reply_fps(a,b)['passed'])
    def test_stack_floor(self):
        self.assertEqual(audit(b'I (10) app: STACK task=app-control free_bytes=2048 priority=4\n')['app_control_min_free'],2048)
        with self.assertRaises(RuntimeError):audit(b'STACK task=app-control free_bytes=2047\n')
    def test_census_requires_actual_heap_and_whole_cache_lines(self):
        good='lcd-census: staging bytes=20480 psram_chip=16777216 psram_heap_total=14554892 cache_line=32 half_bytes=10240'
        self.assertEqual(census_gate([good])[0]['psram_heap_total'],14554892)
        with self.assertRaises(AssertionError):census_gate([good.replace('14554892','13999999')])
        with self.assertRaises(AssertionError):census_gate([])

    def test_later_cat_revision_requires_actual_render_and_unchanged_probe_layer(self):
        body=dict(hash='a'*64,layer=7,x=16,y=0)
        completed=dict(state=7,error='none',revision=10)
        status=dict(revisions=[12,12,11])
        layers=dict(layers=[[7,'a'*64,16,0,24,24,True]])
        self.assertTrue(rendered_show(completed,status,layers,body))
        status['revisions'][2]=9;self.assertFalse(rendered_show(completed,status,layers,body))
        status['revisions'][2]=11;layers['layers'][0][2]=8
        self.assertFalse(rendered_show(completed,status,layers,body))
        layers['layers'][0][2]=16;completed['revision']=0
        self.assertFalse(rendered_show(completed,status,layers,body))

    def test_whole_turn_loss_cannot_hide_outside_reply_window(self):
        from playback_quality import TRANSPORT_COUNTERS,transport_gate
        before=dict.fromkeys(TRANSPORT_COUNTERS,0);before.update(playout_schema=2,pc_id='one',generation=1)
        after=dict(before);after['lost']=9;after['concealed']=9
        self.assertFalse(transport_gate(before,after)['passed'])
        self.assertTrue(transport_gate(before,before)['passed'])

    def test_campaign_recomputes_raw_loss_despite_old_pass_flags(self):
        import json
        from step9a_merge_checks import merge_gate
        from playback_quality import TRANSPORT_COUNTERS
        before=dict.fromkeys(TRANSPORT_COUNTERS,0);before.update(playout_schema=2,pc_id='one',generation=1)
        after=dict(before);after['lost']=1
        row=dict(passed=True,pc_id='one',display_merge={'passed':True},before_perf=before,after_perf=after)
        result=dict(runs=[row,row,row],display_monitor={'passed':True,'app_control_min_free':9000},link_tail={'passed':True})
        sample={'heap':{'internal':{'minimum_free':90000,'largest':49000}},'failed_allocations':{'internal':0,'total':0}}
        gate=merge_gate(result,'PERF '+json.dumps(sample))
        self.assertTrue(gate['checks']['three_runs']);self.assertFalse(gate['checks']['whole_turn_transport'])
        self.assertFalse(gate['passed'])
        sample['failed_allocations']['total']=1
        self.assertFalse(memory_gate([sample])['passed'])

    def test_full_reply_lower_bound_cannot_hide_long_stalls(self):
        from step9a_merge_checks import full_reply_fps
        def sample(t,frames):return dict(requested_at=t,completed_at=t+.02,perf=dict(device_us=int(t*1e6),display=dict(rendered=frames)))
        samples=[sample(1.1,0),sample(2.2,30),sample(10.8,60)]
        self.assertFalse(full_reply_fps(samples,1,11,11000000)['passed'])
        samples[-1]['perf']['display']['rendered']=270
        self.assertTrue(full_reply_fps(samples,1,11,11000000)['passed'])
        self.assertFalse(full_reply_fps(samples,1,4,4000000)['passed'])
        samples.append(sample(12,9999))
        self.assertEqual(full_reply_fps(samples,1,11,11000000)['frames_lower_bound'],270)


    def test_full_perf_cannot_overlap_prompt_reply_acquisition(self):
        from step9a_merge_checks import perf_window_gate
        before=dict(start=1,end=2);after=dict(start=12,end=13)
        self.assertTrue(perf_window_gate([before,after],3,11)['passed'])
        for during in [dict(start=2.9,end=3.1),dict(start=5,end=5.2),dict(start=10.9,end=11.1)]:
            self.assertFalse(perf_window_gate([before,during,after],3,11)['passed'])
        self.assertFalse(perf_window_gate([],3,11)['passed'])


def test_gdma_readiness_requires_every_register_and_all_five_pairs():
    import json
    import pytest
    from step9a_merge_checks import ready_gdma_record
    record=dict(device_us=123,pairs=[dict(pair=i,tx_peri_sel=63,rx_peri_sel=63,tx_priority=0,rx_priority=0) for i in range(5)])
    def line(r):return 'DISPLAY_R4_GDMA_READY '+json.dumps(r)
    assert ready_gdma_record(line(record))==record
    for count in range(5):
        with pytest.raises(AssertionError):ready_gdma_record(line(dict(record,pairs=record['pairs'][:count])))
    for key in record['pairs'][0]:
        broken=json.loads(json.dumps(record));del broken['pairs'][0][key]
        with pytest.raises(AssertionError):ready_gdma_record(line(broken))
    with pytest.raises(ValueError):ready_gdma_record(line(record)[:-2]+'I (31754) esp-peer: PHASE open')

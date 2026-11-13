import copy
import unittest
import numpy as np
from echoear_aec_scoring import (
    reply_erle, saturation, known_match, quiet_stop, native_stop,
    future_speech, final_transcript, session_integrity, rtp_coverage, adjudicate_transcripts,
)
from playback_quality import TRANSPORT_COUNTERS


class ScoredAecTests(unittest.TestCase):
    def test_quiet_calibrated_reference_and_untrimmed_saturation(self):
        rng = np.random.default_rng(8)
        pcm = np.zeros((20000,5))
        pcm[:,0] = rng.normal(0,300,20000)
        pcm[:,2] = rng.normal(0,10,20000) # ~-70dBFS electrical divider
        pcm[1024:,4] = pcm[:-1024,0]/20
        args = dict(quiet_reference=np.ones(8000), delay_samples=1024,
                    reply_start=0, reply_end=16000)
        score = reply_erle(pcm,**args)
        self.assertTrue(score['passed'])
        self.assertAlmostEqual(score['median_db'],26.0205999,places=5)
        for index in (0,19999):
            altered=pcm.copy(); altered[index,1]=32767
            self.assertFalse(reply_erle(altered,**args)['passed'])
        self.assertFalse(reply_erle(pcm,**dict(args,reply_end=20000))['available'])
        self.assertFalse(reply_erle(pcm,**dict(args,quiet_reference=np.ones(8000)*20))['available'])
        pcm[:,4]=0
        self.assertFalse(reply_erle(pcm,**dict(args,delay_samples=None))['available'])
        self.assertFalse(saturation(np.zeros((0,5)))['passed'])

    def test_known_source_rejects_missing_and_duplicate_witness(self):
        word=np.random.default_rng(1).normal(0,1000,4000)
        captured=np.r_[np.zeros(3000),word,np.zeros(6000)]
        self.assertTrue(known_match(word,captured)['available'])
        self.assertFalse(known_match(word,np.zeros(13000))['available'])
        self.assertFalse(known_match(word,np.r_[word,np.zeros(3000),word])['available'])

    def test_stop_bound_cannot_subtract_capture_backlog_or_hide_resumption(self):
        x=np.zeros(64000)
        x[:8000]=1000
        receipts=np.column_stack((np.arange(320,len(x)+1,320),10+np.arange(1,len(x)//320+1)*.02))
        args=dict(request_s=10.25,word_end=8000,marker_start=50000,quiet_rms=1)
        self.assertTrue(quiet_stop(x,receipts,**args)['passed'])
        delayed=receipts.copy();delayed[:,1]+=1
        self.assertFalse(quiet_stop(x,delayed,**args)['passed'])
        altered=x.copy();altered[40000:44000]=500
        self.assertFalse(quiet_stop(altered,receipts,**args)['passed'])
        self.assertFalse(quiet_stop(x,receipts[:-1],**args)['available'])
        self.assertFalse(quiet_stop(x,receipts,**dict(args,word_end=51000))['available'])

    def test_native_dac_cannot_hide_behind_jabra_self_suppression(self):
        pcm=np.zeros((64000,5));pcm[:11000,2]=30
        args=dict(word_match=dict(available=True,start=10000,end=12000),
                  marker_match=dict(available=True,start=50000),
                  bot_match=dict(available=True,end=5000),quiet_reference=np.ones(8000))
        self.assertTrue(native_stop(pcm,**args)['passed'])
        pcm[15000:25000,2]=30
        self.assertFalse(native_stop(pcm,**args)['passed'])
        args['word_match']['available']=False
        self.assertFalse(native_stop(pcm,**args)['passed'])

    def test_natural_pause_completion_stall_and_transcript_selection_fail(self):
        source=np.ones(60000)*1000
        args=dict(source_offset=8000,generated_before_trigger=60000)
        self.assertTrue(future_speech(source,**args)['passed'])
        for count in (10000,30000):
            self.assertFalse(future_speech(source,**dict(args,generated_before_trigger=count))['passed'])
        source[16000:20000]=0
        self.assertFalse(future_speech(source,**args)['passed'])
        self.assertFalse(future_speech(source[:16000],**args)['passed'])
        good=dict(event='user-turn-stopped',text='Stop!')
        provider=dict(event='frame',frame='TranscriptionFrame',text='Stop.')
        self.assertTrue(final_transcript([good,provider])['correct'])
        self.assertFalse(final_transcript([good,provider,dict(provider,text='keep talking')])['correct'])
        self.assertFalse(final_transcript([good])['correct'])
        self.assertFalse(final_transcript([good,dict(good,text='wrong')])['correct'])
        self.assertFalse(final_transcript([])['available'])

    def test_whole_session_strict_counters_heap_tasks_and_rtp(self):
        p=dict(pc_id='peer',generation=1,playout_schema=2,device_us=100,
               **{key:0 for key in TRANSPORT_COUNTERS},
               mic_in_turn_clips=0,mic_gate_open_clips=0,mic_gate_closed_clips=0,
               heap={'internal':{'minimum_free':90000,'largest':40000}},
               failed_allocations={'internal':0,'total':0},
               aec=dict(enabled=True,ns_enabled=False,agc_enabled=False,errors=0,reference_clips=0,output_clips=0),
               audio=dict(drops=0,read_errors=0,write_errors=0,write_timeouts=0,clips=0),output_volume_relative_db=0,
               display=dict(lcd_initialized=True,animation=True,tx_pri=1,rendered=1),
               tasks=[dict(name=name,stack_free_bytes=8000) for name in
                      ('peer-loop','audio-tx','audio-capture','audio-playback','app-control')])
        packets=[dict(pc_id='peer',ssrc=1,observed_ms=1000+i*20,seq=i,timestamp=i*960) for i in range(201)]
        args=dict(start_s=1.1,end_s=4.9)
        p2=copy.deepcopy(p);p2['device_us']=1000100;p2['display']['rendered']=31
        self.assertTrue(session_integrity([p,p2],packets,**args)['passed'])
        for key in TRANSPORT_COUNTERS:
            changed=copy.deepcopy(p2);changed[key]=1
            self.assertFalse(session_integrity([p,changed],packets,**args)['passed'],key)
        changed=copy.deepcopy(p);changed['heap']['internal']['minimum_free']=81919
        self.assertFalse(session_integrity([p,changed],packets,**args)['passed'])
        changed=copy.deepcopy(p);changed['tasks']=changed['tasks'][:-1]
        self.assertFalse(session_integrity([p,changed],packets,**args)['passed'])
        changed=copy.deepcopy(p);changed['pc_id']='new'
        self.assertFalse(session_integrity([p,changed],packets,**args)['passed'])
        self.assertFalse(session_integrity([p,p],packets[10:],**args)['passed'])
        self.assertFalse(session_integrity([p,p],packets[:50]+packets[52:],**args)['passed'])
        altered=copy.deepcopy(p2);altered['audio']['write_timeouts']=1
        self.assertFalse(session_integrity([p,altered],packets,**args)['passed'])
        altered=copy.deepcopy(p2);altered['output_volume_relative_db']=-6
        self.assertFalse(session_integrity([p,altered],packets,**args)['passed'])
        self.assertTrue(rtp_coverage(packets,packets,1.1,4.9)['passed'])
        self.assertFalse(rtp_coverage(packets,packets[:50]+packets[52:],1.1,4.9)['passed'])

    def test_late_conflicting_provider_final_cannot_preserve_earlier_correct_verdict(self):
        rows=[dict(id='barge-01',kind='barge',start_s=10,end_s=15)]
        good=[dict(event='user-turn-stopped',text='Stop.',case_id='barge-01',t=13),
              dict(event='frame',frame='TranscriptionFrame',text='Stop.',case_id='barge-01',t=12.9)]
        self.assertEqual(adjudicate_transcripts(rows,good)['correct'],1)
        for case_id in ('barge-01',None):
            late=dict(event='frame',frame='TranscriptionFrame',text='keep talking',case_id=case_id,t=17)
            scored=adjudicate_transcripts(rows,good+[late])
            self.assertEqual(scored['unassigned'],[late])
            if case_id=='barge-01':self.assertEqual(scored['correct'],0)


if __name__=='__main__':unittest.main()

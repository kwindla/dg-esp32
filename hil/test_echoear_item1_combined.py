import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from echoear_item1_combined import (CANDIDATE, DOWNLINK_LOSS_COUNTERS, ELF, ConsoleLedger, completed_boot_screens, fresh_bot,
    rf_pre_gate, sample_gate, session_transport_gate, validate_image, zero_session_gate)
from playback_quality import transport_gate
from playback_quality import TRANSPORT_COUNTERS


def sample():
    return dict(playout_schema=2,pc_id='peer-one',generation=1,
        **{key:0 for key in (*TRANSPORT_COUNTERS,'mic_in_turn_clips','mic_gate_open_clips','mic_gate_closed_clips')},
        heap=dict(internal=dict(minimum_free=88364,free=100000,largest=49152)),
        failed_allocations=dict(internal=0,total=0),
        display=dict(lcd_initialized=True,engine_snapshot=True,dead=False,failed=False,
            dma_errors=0,lock_timeouts=0,lock_failures=0,send_failures=0,late_callbacks=0),
        tasks=[dict(name='audio-tx',stack_bytes=28672,stack_free_bytes=6600)])


class Combined(unittest.TestCase):
    def test_boot_requires_connecting_then_cat_without_disconnected(self):
        # Exact completed frame records from the one retained f8b8573 acquisition.
        rows=[dict(n=1,screen=1,completed_us=10362214,revision=2,rendered=1),
              dict(n=2,screen=2,completed_us=11450175,revision=4,rendered=3)]
        def lines(records):return [(i,float(i),'DISPLAY_DEMO_RENDERED '+json.dumps(row)) for i,row in enumerate(records)]
        from bot_restart import completed_screens
        self.assertFalse(completed_screens(lines(rows))['passed'])  # Teardown still needs Disconnected.
        self.assertTrue(completed_boot_screens(lines(rows))['passed'])
        self.assertFalse(completed_boot_screens(lines(rows[::-1]))['passed'])
        self.assertFalse(completed_boot_screens(lines(rows[1:]))['passed'])
        self.assertFalse(completed_boot_screens(lines(rows[:1]))['passed'])
        for key,value in (('completed_us',10362214),('completed_us',None),('rendered',1)):
            bad=copy.deepcopy(rows);bad[-1][key]=value
            self.assertFalse(completed_boot_screens(lines(bad))['passed'])

    def test_zero_cumulative_counters_include_startup_edits_and_split_clips(self):
        self.assertTrue(zero_session_gate(sample())['passed'])
        for key in (*TRANSPORT_COUNTERS,'mic_in_turn_clips','mic_gate_open_clips','mic_gate_closed_clips'):
            bad=(2,None,False,-1) if key in DOWNLINK_LOSS_COUNTERS else (1,None,False,-1)
            for value in bad:
                row=sample();row[key]=value
                with self.subTest(key=key,value=value):self.assertFalse(zero_session_gate(row)['passed'])
            row=sample();del row[key];self.assertFalse(zero_session_gate(row)['passed'])

    def test_decision_134_d5_is_one_distinct_downlink_frame_per_session(self):
        self.assertEqual(DOWNLINK_LOSS_COUNTERS,('lost','concealed','plc_frames','playout_dropped'))
        def shape(lost,concealed,plc,dropped,**rejected):
            row=sample();row.update(lost=lost,concealed=concealed,plc_frames=plc,playout_dropped=dropped)
            row['playout']=dict(rejected=dict(dict.fromkeys(('capacity','conflict','duplicate','late','window'),0),**rejected))
            return row
        cases=[
            ('zero',shape(0,0,0,0),True),
            ('one missing slot concealed by PLC',shape(1,1,1,0),True),
            ('one late rejection only',shape(0,0,0,1,late=1),True),
            ('one capacity rejection only',shape(0,0,0,1,capacity=1),True),
            # combined-4 samples 15-16: one late rejection beside one PLC slot. Not provably one frame -> FAIL.
            ('combined-4 shape 1/1/1/1 late=1',shape(1,1,1,1,late=1),False),
            ('missing slot plus duplicate rejection',shape(1,1,1,1,duplicate=1),False),
            ('two lost frames',shape(2,2,2,0),False),
            ('two lost frames and two drops',shape(2,2,2,2,late=2),False),
            ('two rejections',shape(0,0,0,2,late=1,duplicate=1),False),
            ('concealed and plc_frames disagree',shape(1,1,0,0),False),
            ('PLC without a missing slot',shape(0,1,1,0),False)]
        for name,row,want in cases:
            with self.subTest(name):self.assertEqual(zero_session_gate(row)['passed'],want)
        self.assertTrue(sample_gate([shape(0,0,0,0),shape(1,1,1,0),shape(1,1,1,0)])['passed'])
        self.assertFalse(sample_gate([shape(1,1,1,0),shape(1,1,1,1,late=1)])['passed'])
        # Item1 only: every other transport counter and clip counter stays zero even beside one loss.
        for key in (*(k for k in TRANSPORT_COUNTERS if k not in DOWNLINK_LOSS_COUNTERS),'mic_in_turn_clips','mic_gate_open_clips','mic_gate_closed_clips'):
            row=shape(1,1,1,0);row[key]=1
            with self.subTest(other=key):self.assertFalse(zero_session_gate(row)['passed'])

    def test_decision_134_d5_session_deltas_for_cycles_and_restart(self):
        def perf(**counters):
            row=sample();row.update(counters);return row
        before=perf()
        self.assertTrue(session_transport_gate(transport_gate(before,perf())))
        one=perf(lost=1,concealed=1,plc_frames=1)
        self.assertFalse(transport_gate(before,one)['passed'])          # item2 strict rule is unchanged
        self.assertTrue(session_transport_gate(transport_gate(before,one)))
        self.assertTrue(session_transport_gate(transport_gate(before,perf(playout_dropped=1))))
        for bad in (dict(lost=1,concealed=1,plc_frames=1,playout_dropped=1),dict(lost=2,concealed=2,plc_frames=2),
                    dict(playout_dropped=2),dict(lost=1,concealed=1,plc_frames=0),dict(drift_inserts=1),dict(i2s_underflows=1)):
            with self.subTest(bad=bad):self.assertFalse(session_transport_gate(transport_gate(before,perf(**bad))))
        moved=perf();moved['pc_id']='peer-two'
        self.assertFalse(session_transport_gate(transport_gate(before,moved)))
        missing=perf();del missing['lost']
        self.assertFalse(session_transport_gate(transport_gate(before,missing)))

    def test_rf_pre_gate_is_recorded_from_two_snapshots(self):
        snaps=iter([dict(monotonic_s=1.0),dict(monotonic_s=31.0)]);slept=[]
        def gate(a,b):return dict(passed=True,before=a,after=b)
        result=rf_pre_gate(lambda:next(snaps),gate,sleep=slept.append)
        self.assertEqual(slept,[30]);self.assertEqual(result['pre_gate_seconds'],30)
        self.assertEqual((result['before']['monotonic_s'],result['after']['monotonic_s']),(1.0,31.0))

    def test_memory_floor_cannot_be_erased_by_current_free_recovery(self):
        self.assertTrue(sample_gate([sample()])['passed'])
        for bucket,key,value in (('heap','minimum_free',81919),('heap','largest',32767),('failed_allocations','internal',1),('failed_allocations','total',1)):
            row=sample()
            if bucket=='heap':row['heap']['internal'][key]=value
            else:row[bucket][key]=value
            self.assertFalse(sample_gate([row,sample()])['passed'])
        self.assertFalse(sample_gate([])['passed'])

    def test_every_display_and_tx_stack_sample_is_required(self):
        for key,value in (('dma_errors',1),('lock_timeouts',1),('lock_failures',1),('engine_snapshot',False),('lcd_initialized',False),('failed',True),('dead',True)):
            row=sample();row['display'][key]=value;self.assertFalse(sample_gate([sample(),row])['passed'])
        for key,value in (('stack_bytes',32768),('stack_free_bytes',4095)):
            row=sample();row['tasks'][0][key]=value;self.assertFalse(sample_gate([row])['passed'])
        row=sample();row['tasks']=[];self.assertFalse(sample_gate([row])['passed'])
        self.assertFalse(sample_gate([sample(),row])['passed'])
        row=sample();row['tasks']*=2;self.assertFalse(sample_gate([row])['passed'])

    def test_actual_console_send_is_prevented_during_autonomous_phase(self):
        send=Mock();ledger=ConsoleLedger(send);ledger('setup');send.assert_called_once_with('setup')
        ledger.phase='autonomous'
        with self.assertRaises(AssertionError):ledger('stats')
        send.assert_called_once_with('setup');self.assertEqual(ledger.commands[-1]['phase'],'autonomous')
        ledger.phase='cleanup';ledger('demo hold');self.assertEqual(send.call_count,2)

    def test_one_fresh_peer_pipeline_start_and_ready_are_all_required(self):
        old=dict(peers=['a'],pipelines=['Pipeline#0'],starts=1,ready=1)
        new=dict(peers=['a','b'],pipelines=['Pipeline#0','Pipeline#1'],starts=2,ready=2)
        self.assertTrue(fresh_bot(old,new)['passed'])
        for key,value in (('peers',['a']),('peers',['a','b','c']),('pipelines',['Pipeline#0']),('starts',3),('ready',1)):
            bad=copy.deepcopy(new);bad[key]=value;self.assertFalse(fresh_bot(old,bad)['passed'])

    def test_manifest_identity_rejected_before_using_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory);(image/'manifest.json').write_text('{"commit":"wrong","elf_sha256":"wrong","autostart":true}')
            with self.assertRaisesRegex(RuntimeError,'Exact announced'):validate_image(image)

    def test_publisher_file_manifest_and_legacy_hash_manifest_are_verified(self):
        with tempfile.TemporaryDirectory() as directory, patch('demo_session.firmware_artifacts',return_value=ELF):
            image=Path(directory)
            (image/'dg-esp32.bin').write_bytes(b'frozen test payload')
            (image/'project_description.json').write_text(json.dumps(dict(project_version=CANDIDATE[:7])))
            (image/'flasher_args.json').write_text(json.dumps(dict(flash_files={'0x10000':'dg-esp32.bin'})))
            hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in image.iterdir()}
            base=dict(commit=CANDIDATE,elf_sha256=ELF,profile='demo')
            for fields in (dict(files={k:dict(sha256=v) for k,v in hashes.items()}),dict(sha256=hashes,autostart=True)):
                manifest=dict(base,**fields);(image/'manifest.json').write_text(json.dumps(manifest))
                self.assertEqual(validate_image(image)[0],manifest)
                bad=dict(manifest,autostart=False);(image/'manifest.json').write_text(json.dumps(bad))
                with self.assertRaisesRegex(RuntimeError,'Exact announced'):validate_image(image)
            (image/'manifest.json').write_text(json.dumps(dict(base,files={k:dict(sha256=v) for k,v in hashes.items()})))
            (image/'dg-esp32.bin').write_bytes(b'tampered')
            with self.assertRaisesRegex(RuntimeError,'Frozen artifact changed'):validate_image(image)


if __name__=='__main__':unittest.main()


class Decision141Tests(unittest.TestCase):
    def test_removed_duplex_override_is_refused_before_any_validation_or_lock(self):
        import echoear_item1_combined as item1
        with patch.dict('os.environ',{'ECHOEAR_AEC_FULL_DUPLEX':'0'}), \
             patch.object(item1,'validate_image',side_effect=AssertionError('refusal must come first')), \
             self.assertRaises(SystemExit) as raised:
            item1.main(['--image','x','--out','y','--binding','z'])
        self.assertEqual(raised.exception.code,2)

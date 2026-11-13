"""Synthetic step-1b evidence tests. No serial, ALSA, sockets or real bench lock."""
import copy
import argparse
import json
import struct
import tempfile
import unittest
import wave
import zlib
from pathlib import Path
from unittest.mock import Mock, patch
import measure_b as m
import run_audio_b as run
import build_evidence


def wire(row):
    payload=json.dumps(row,separators=(',',':')).encode()
    return b'B '+payload+f' {zlib.crc32(payload):08x}'.encode()


def slot_rows():
    return [dict(kind='slot',channel=i,samples=320,rms_dbfs=-40,peak=100,clips=0,gain_reg=26) for i in range(1,5)]+[
        dict(kind='slots_end',frames=320,overflows=0)]


def afe_rows(seconds=600):
    # Large synthetic chunk count is intentional: validates a real ten-minute
    # sample denominator, rather than trusting the device's duration label.
    count=seconds*16000//512
    rows=[dict(kind='aec_begin',chunk=512,rate=16000,channels=2,mic=1,ref=3,core=1,seconds=seconds,stimulus=1,ref_gain_db=15,ref_gain_reg=0x15,dma_descriptors=4,dma_frames=240,stack_bytes=16384,stack_memory='PSRAM')]
    rows += [dict(kind='aec_chunk',seq=i,cycles=1000,us=3,core=1,bytes=1024,mic_power=10000,ref_power=500,out_power=100) for i in range(count)]
    rows += [dict(kind='aec_end',chunks=count,samples=count*512,output_samples=count*512,cycles=count*1000,
                  process_us=count*3,overflows=0,elapsed_us=seconds*1000000,erle_db=20)]
    rows += [dict(kind='heap',phase=phase,internal_free=200000-(100 if phase=='after_create' else 0),
                 psram_free=1000000-(50000 if phase=='after_create' else 0),internal_min=100000,internal_largest=50000)
             for phase in ('before_worker','after_worker_create','before_create','after_create','after_buffers','end','after_destroy','after_worker_delete')]
    return rows


def panel_rows(clock=40,interval=19000):
    rows=[dict(kind='panel_begin',clock_mhz=clock,width=800,height=800,h_total=880,v_total=840,psram=True)]
    for kind in ('panel_vsync','panel_frame'):
        rows += [dict(kind=kind,seq=i,us32=((1<<32)-100+interval*i)%(1<<32)) for i in range(5)]
    return rows+[dict(kind='panel_end',vsync=5,frame_complete=5,overflow=False)]


class RecordsTests(unittest.TestCase):
    def test_crc_round_trip_and_unrelated_console_lines(self):
        row=dict(kind='slot',rms_dbfs=-42.25)
        self.assertEqual(m.records([b'OK',wire(row),b'LOOP END reads=1 errors=0']),[row])

    def test_corrupt_crc_payload_or_missing_kind_rejected(self):
        for row in (wire(dict(kind='slot'))[:-1]+b'0',b'B {broken} 12345678',wire(dict(value=1))):
            with self.subTest(row=row), self.assertRaises(ValueError): m.records([row])

    def test_firmware_error_is_fatal_even_with_ok(self):
        with self.assertRaisesRegex(ValueError,'ERR B'): m.records([b'ERR B arguments',b'OK'])

    def test_sequence_rejects_loss_duplicates_and_reordering(self):
        rows=[dict(kind='event',seq=i) for i in range(3)]
        for corrupt in (rows[:-1],[rows[0],rows[0],rows[2]],list(reversed(rows))):
            with self.assertRaises(ValueError): m.sequence(corrupt,'event',3)

    def test_distributions_reject_missing_or_invalid_values(self):
        for values in ([],[float('nan')],[-1]):
            with self.assertRaises(ValueError): m.distribution(values)
        self.assertEqual(m.distribution([4,1,2,3]),dict(count=4,p50=2.5,p99=4,max=4))


class AudioTests(unittest.TestCase):
    def test_opus_only_campaign_uploads_speech_before_benchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=argparse.Namespace(out=Path(tmp),build=Path('/never-read'),stage='opus',aec_seconds=600,
                                    ref_gain=15,speech_wav=Path('/synthetic/speech.wav'),acoustic=False)
            board=Mock();board.__enter__=Mock(return_value=board);board.__exit__=Mock(return_value=False)
            events=[]
            def command(text,timeout):
                events.append(text)
                return [wire(dict(kind='peer'))] if text=='peer' else [wire(dict(kind='opus_unavailable',reason='synthetic'))]
            board.command.side_effect=command
            with patch.object(run,'arguments',return_value=args),patch.object(run,'collect',return_value={}),patch.object(run,'require_bench'),patch.object(run,'boot_check'),patch.object(run,'Board',return_value=board),patch.object(run,'upload',side_effect=lambda b,p:events.append('upload') or dict(samples=320)):
                run.main()
            self.assertEqual(events,['peer','upload','opus 300'])
            self.assertEqual(json.loads((Path(tmp)/'result.json').read_text())['playback']['samples'],320)

    def test_aec_campaign_sets_and_validates_selected_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=argparse.Namespace(out=Path(tmp),build=Path('/never-read'),stage='aec',aec_seconds=32,
                                    ref_gain=21,speech_wav=None,acoustic=False)
            board=Mock();board.__enter__=Mock(return_value=board);board.__exit__=Mock(return_value=False)
            rows=afe_rows(32);rows[0].update(ref_gain_db=21,ref_gain_reg=0x17)
            def command(text,timeout):
                if text=='peer':return [wire(dict(kind='peer'))]
                if text=='vol -6':return [b'VOL db=-6.0 rc=0']
                if text.startswith('aec '):return [wire(row) for row in rows]
                return [b'OK']
            board.command.side_effect=command
            with patch.object(run,'arguments',return_value=args),patch.object(run,'collect',return_value={}),patch.object(run,'require_bench'),patch.object(run,'boot_check'),patch.object(run,'Board',return_value=board):
                run.main()
            commands=[call.args[0] for call in board.command.call_args_list]
            self.assertLess(commands.index('refgain 21'),commands.index('aec 32 1'))
            self.assertEqual(json.loads((Path(tmp)/'result.json').read_text())['aec']['config']['ref_gain_db'],21)

    def test_aec_default_and_sweep_selected_gain_argument(self):
        args=['--out','/unused/out','--build','/unused/build']
        self.assertEqual(run.arguments(args).ref_gain,15)
        self.assertEqual(run.arguments(args+['--ref-gain','21']).ref_gain,21)
        with patch('sys.stderr'),self.assertRaises(SystemExit): run.arguments(args+['--ref-gain','14'])

    def test_aec_operating_point_and_separate_worker_accounting(self):
        rows=afe_rows(32)
        before=next(row for row in rows if row.get('phase')=='before_worker')
        after=next(row for row in rows if row.get('phase')=='after_worker_create')
        after['psram_free']=before['psram_free']-16384
        after['internal_free']=before['internal_free']-500
        result=m.aec(rows)
        self.assertEqual(result['worker_psram_delta'],16384)
        self.assertEqual(result['worker_internal_delta'],500)
        self.assertEqual(result['afe_psram_delta'],50000)
        self.assertEqual(result['afe_internal_delta'],100)
        for key,value in [('ref_gain_db',0),('ref_gain_reg',0x10),('dma_descriptors',6),('dma_frames',320),('stack_memory','INTERNAL'),('stack_bytes',8192)]:
            changed=copy.deepcopy(rows);changed[0][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'operating point'): m.aec(changed)
        rows[0].update(ref_gain_db=21,ref_gain_reg=0x17)
        self.assertEqual(m.aec(rows,expected_gain=21)['config']['ref_gain_db'],21)

    def test_gain_return_code_and_witness_required(self):
        for rows in ([b'OK'],[b'GAIN mic=1 db=0 rc=-1'],[b'GAIN mic=2 db=0 rc=0'],[b'GAIN mic=1 db=0 rc=1'],[b'GAIN mic=1 db=0 rc=0']*2):
            with self.subTest(rows=rows),self.assertRaisesRegex(ValueError,'GAIN'):
                run.checked(Mock(command=Mock(return_value=rows)),'gain 1 0')
        run.checked(Mock(command=Mock(return_value=[b'GAIN mic=1 db=0 rc=0',b'OK'])),'gain 1 0')

    def test_wave_peak_limit_rejected_before_any_upload_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'speech.wav'
            for amplitude,accepted in [(8230,True),(8231,False),(-32768,False),(32767,False)]:
                with wave.open(str(path),'wb') as stream:
                    stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
                    stream.writeframes(struct.pack('<320h',*([amplitude]*320)))
                board=Mock();board.command.side_effect=lambda text,timeout:[wire(dict(kind='pcm_ready',samples=320))] if text=='pcm_end' else [b'OK']
                if accepted:
                    result=run.upload(board,path)
                    self.assertLessEqual(result['peak_dbfs'],-12);self.assertEqual(result['peak'],8230)
                    self.assertAlmostEqual(result['peak_dbfs'],result['rms_dbfs'])
                else:
                    with self.subTest(amplitude=amplitude),self.assertRaisesRegex(ValueError,'exceeds -12 dBFS'):run.upload(board,path)
                    board.command.assert_not_called()

    def test_opus_requires_plc_and_worker_high_water_witness(self):
        rows=[dict(kind='opus_begin',codec_version='2.5.0',pcm_bytes=640,rate=16000,channels=1,frame_ms=20,bitrate=24000,complexity=0,fec=False,dtx=False,stack_bytes=32768,stack_memory='INTERNAL',core=0,input='uploaded_speech'),
              dict(kind='opus_frame',seq=0,encode_cycles=100,decode_cycles=40,encoded_bytes=60,decoded_samples=320,core=0,plc_cycles=50,plc_samples=320),
              dict(kind='opus_end',frames=1,plc_frames=1,stack_high_water_bytes=12000)]
        self.assertEqual(m.opus(rows)['plc_cycles']['p50'],50)
        for index,key,value in [(0,'stack_bytes',16384),(0,'stack_memory','PSRAM'),(0,'input','unknown'),(1,'core',1),(1,'plc_samples',0),(2,'plc_frames',0),(2,'stack_high_water_bytes',0)]:
            bad=copy.deepcopy(rows);bad[index][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):m.opus(bad)

    def test_slot_count_and_overflow_checked(self):
        self.assertEqual(len(m.slots(slot_rows())['channels']),4)
        for rows in (slot_rows()[1:],slot_rows()+[slot_rows()[0]]):
            with self.assertRaises(ValueError): m.slots(rows)
        rows=slot_rows();rows[-1]['overflows']=1
        with self.assertRaisesRegex(ValueError,'Incomplete'): m.slots(rows)

    def test_gain_mapping_and_insufficient_signal(self):
        base=m.slots(slot_rows());changed=copy.deepcopy(base)
        changed['channels']['2']['rms_dbfs']=-65
        self.assertTrue(m.gain_identity(base,changed,2)['matches_expected'])
        self.assertFalse(m.gain_identity(base,changed,1)['matches_expected'])
        self.assertIsNone(m.gain_identity(base,base,1)['observed_slot'])

    def test_ten_minute_afe_counts_caps_and_cycle_totals(self):
        result=m.aec(afe_rows())
        self.assertTrue(result['ten_minute_soak']);self.assertTrue(result['heap_floor_met'])
        self.assertEqual(result['afe_internal_delta'],100);self.assertEqual(result['afe_psram_delta'],50000)
        self.assertEqual(result['cycles']['count'],18750)

    def test_short_duration_is_not_ten_minute_qualification(self):
        self.assertFalse(m.aec(afe_rows(32))['ten_minute_soak'])

    def test_afe_missing_row_format_output_core_and_overflow_fail_closed(self):
        cases=[]
        rows=afe_rows(32);rows.pop(2);cases.append(rows)
        for key,value in (('overflows',1),('output_samples',0),('cycles',0)):
            rows=afe_rows(32);next(row for row in rows if row['kind']=='aec_end')[key]=value;cases.append(rows)
        rows=afe_rows(32);rows[1]['core']=0;cases.append(rows)
        rows=afe_rows(32);rows[0]['ref']=4;cases.append(rows)
        rows=afe_rows(32);rows.pop();cases.append(rows)
        for rows in cases:
            with self.assertRaises(ValueError): m.aec(rows)

    def test_opus_unavailable_is_not_a_pass(self):
        result=m.opus([dict(kind='opus_unavailable',reason='vendor guard')])
        self.assertFalse(result['available']);self.assertEqual(result['reason'],'vendor guard')

    def test_opus_production_frame_and_settings(self):
        rows=[dict(kind='opus_begin',codec_version='2.5.0',pcm_bytes=640,rate=16000,channels=1,frame_ms=20,bitrate=24000,complexity=0,fec=False,dtx=False,stack_bytes=32768,stack_memory='INTERNAL',core=0,input='synthetic_sine'),
              dict(kind='opus_frame',seq=0,encode_cycles=100,decode_cycles=40,encoded_bytes=60,decoded_samples=320,core=0,plc_cycles=50,plc_samples=320),
              dict(kind='opus_end',frames=1,plc_frames=1,stack_high_water_bytes=12000)]
        self.assertTrue(m.opus(rows)['available'])
        rows[0]['frame_ms']=40
        with self.assertRaises(ValueError): m.opus(rows)

    def test_opus_wrong_version_or_pcm_frame_contract_rejected(self):
        rows=[dict(kind='opus_begin',codec_version='2.5.0',pcm_bytes=640,rate=16000,channels=1,frame_ms=20,bitrate=24000,complexity=0,fec=False,dtx=False,stack_bytes=32768,stack_memory='INTERNAL',core=0,input='synthetic_sine'),
              dict(kind='opus_frame',seq=0,encode_cycles=100,decode_cycles=40,encoded_bytes=60,decoded_samples=320,core=0,plc_cycles=50,plc_samples=320),
              dict(kind='opus_end',frames=1,plc_frames=1,stack_high_water_bytes=12000)]
        for field,value in (('codec_version','2.6.2'),('pcm_bytes',1280)):
            corrupted=copy.deepcopy(rows);corrupted[0][field]=value
            with self.assertRaisesRegex(ValueError,'ABI mismatch'): m.opus(corrupted)
        rows[0]['frame_ms']=20;rows[1]['decoded_samples']=640
        with self.assertRaises(ValueError): m.opus(rows)

    def test_wave_upload_crc_sequence_and_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'speech.wav'
            with wave.open(str(path),'wb') as stream:
                stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(bytes(1280))
            board=Mock();board.command.side_effect=lambda text,timeout:[wire(dict(kind='pcm_ready',samples=640))] if text=='pcm_end' else [b'OK']
            self.assertEqual(run.upload(board,path)['samples'],640)
            commands=[call.args[0] for call in board.command.call_args_list]
            self.assertEqual(commands[0],'pcm_begin 640');self.assertEqual(commands[-1],'pcm_end')
            self.assertEqual([int(text.split()[1]) for text in commands[1:-1]],[0,1,2])
            with wave.open(str(path),'wb') as stream:
                stream.setnchannels(2);stream.setsampwidth(2);stream.setframerate(48000);stream.writeframes(bytes(1280))
            board.reset_mock()
            with self.assertRaisesRegex(ValueError,'16 kHz'): run.upload(board,path)
            board.command.assert_not_called()

    def test_default_slot_campaign_does_not_open_host_audio(self):
        gains={1:30,2:30}
        def command(text,timeout):
            if text.startswith('gain '):
                _,mic,db=text.split();gains[int(mic)]=int(db)
                return [f'GAIN mic={mic} db={db} rc=0'.encode(),b'OK']
            if text.startswith('vol '): return [b'VOL db=-6.0 rc=0',b'OK']
            if text.startswith('slots '):
                rows=slot_rows()
                for row in rows[:-1]:
                    if row['channel'] in gains and gains[row['channel']]==0: row['rms_dbfs']-=25
                return [wire(row) for row in rows]
            return [b'OK']
        with patch.object(run,'acoustic_start',side_effect=AssertionError('must be explicit')):
            result=run.slot_campaign(Mock(command=command),Path('/unused'),acoustic=False)
        self.assertFalse(result['acoustic_requested'])
        self.assertEqual([item['observed_slot'] for item in result['gain_discrimination']],['1','2'])
        self.assertTrue(all(item['matches_expected'] for item in result['gain_discrimination']))
        self.assertEqual(gains,{1:30,2:30})


class TouchPanelTests(unittest.TestCase):
    def test_touch_absence_is_recorded_and_alternate_address_accepted(self):
        result=m.touch([dict(kind='touch_error',reason='GT911 unavailable',tried=[93,20],rc=-1)],[])
        self.assertFalse(result['available'])
        rows=self.touch_rows();rows[0]['address']=0x14
        self.assertTrue(m.touch(rows,[b'TOUCH 1 2'])['available'])

    def test_touch_under_load_requires_successful_loop_witness(self):
        for witness in ([],[b'LOOP END reads=0 errors=0'],[b'LOOP END reads=6 errors=1'],[b'LOOP END reads=6 errors=0']*2):
            with self.subTest(witness=witness),self.assertRaises(ValueError):
                m.touch(self.touch_rows(True),[b'TOUCH 1 2']+witness,require_loop=True)
        result=m.touch(self.touch_rows(True),[b'TOUCH 1 2',b'LOOP END reads=60 errors=0'],require_loop=True)
        self.assertEqual(result['audio_loop'],dict(reads=60,errors=0))

    def test_touch_absence_still_collects_loop_end(self):
        board=Mock();board.command.side_effect=[[wire(dict(kind='touch_error',reason='GT911 unavailable',tried=[93,20],rc=-1))],[b'OK']]
        board.until.return_value=[b'LOOP END reads=60 errors=0']
        result=run.touch_campaign(board,1)
        self.assertFalse(result['available']);self.assertEqual(result['audio_loop']['errors'],0)
        board.until.assert_called_once()
        board.until.return_value=[b'LOOP END reads=60 errors=2']
        board.command.side_effect=[[wire(dict(kind='touch_error',reason='GT911 unavailable',tried=[93,20],rc=-1))],[b'OK']]
        with self.assertRaisesRegex(ValueError,'loop'):run.touch_campaign(board,1)

    def test_selected_clock_includes_measured_pll_quantization(self):
        run=m.panel(panel_rows(50,15400)) # PLL yields about 48 MHz, not request 50.
        selected=m.select_panel([run],[50])
        self.assertEqual(selected['lowest_tested_timing_clock_mhz'],50)
        self.assertAlmostEqual(selected['lowest_tested_timing_effective_pixel_clock_mhz'],48,delta=.1)
        self.assertEqual(selected['lowest_tested_visually_stable_effective_pixel_clock_mhz'],run['effective_pixel_clock_mhz'])

    def touch_rows(self,codec=False):
        return [dict(kind='touch_begin',address=93,hz=50,x_max=800,y_max=800,product_id_hex='39313100',codec_traffic=codec),
                dict(kind='touch_poll',seq=0,us=100,rc=0,started_us=0),
                dict(kind='touch_poll',seq=1,us=150,rc=0,started_us=20000),
                dict(kind='touch_end',polls=2,events=1,errors=0,codec_ops=2 if codec else 0,codec_errors=0)]

    def test_touch_coords_poll_and_codec_contention(self):
        result=m.touch(self.touch_rows(True),[b'TOUCH 12 799'])
        self.assertEqual(result['observed_range'],dict(x=[12,12],y=[799,799]))
        self.assertEqual(result['poll_interval_us']['p50'],20000)
        self.assertEqual(result['out_of_range'],0)
        self.assertEqual(m.touch(self.touch_rows(),[b'TOUCH 800 801'])['out_of_range'],1)

    def test_touch_accepts_and_records_gt911_and_gt9271_product_ids(self):
        for product,controller in [('39313100','GT911'),('39323731','GT9271')]:
            with self.subTest(product=product):
                rows=self.touch_rows();rows[0]['product_id_hex']=product
                result=m.touch(rows,[b'TOUCH 12 799'])
                self.assertTrue(result['available']);self.assertEqual(result['controller'],controller)
                self.assertEqual(result['config']['product_id_hex'],product)
                self.assertEqual(result['observed_range'],dict(x=[12,12],y=[799,799]))
        for product in ('393131ff','39323732','00000000','393237'):
            rows=self.touch_rows();rows[0]['product_id_hex']=product
            with self.subTest(product=product),self.assertRaisesRegex(ValueError,'configuration mismatch'):
                m.touch(rows,[b'TOUCH 12 799'])

    def test_touch_no_events_is_unmeasured_coordinates(self):
        rows=self.touch_rows();rows[-1]['events']=0
        result=m.touch(rows,[])
        self.assertIsNone(result['observed_range']);self.assertIn('unmeasured',result['coordinate_validation'])

    def test_touch_loss_or_i2c_error_or_absent_codec_traffic_fails(self):
        for change in ('loss','error','no_codec'):
            rows=self.touch_rows(True)
            if change=='loss': rows.pop(1)
            elif change=='error': rows[1]['rc']=-1
            else: rows[-1]['codec_ops']=0
            with self.assertRaises(ValueError): m.touch(rows,[b'TOUCH 1 2'])

    def test_panel_period_wrap_and_timing_threshold(self):
        result=m.panel(panel_rows())
        self.assertEqual(result['vsync_period_us']['p50'],19000)
        self.assertTrue(result['timing_meets_50_hz']);self.assertEqual(result['visual_stability'],'unmeasured')
        self.assertFalse(m.panel(panel_rows(interval=21000))['timing_meets_50_hz'])

    def test_lowest_panel_clock_requires_explicit_visual_witness(self):
        runs=[m.panel(panel_rows(40)),m.panel(panel_rows(38)),m.panel(panel_rows(36,21000))]
        selected=m.select_panel(runs)
        self.assertEqual(selected['lowest_tested_timing_clock_mhz'],38)
        self.assertIsNone(selected['lowest_tested_visually_stable_clock_mhz'])
        self.assertEqual(m.select_panel(runs,[40])['lowest_tested_visually_stable_clock_mhz'],40)
        runs[1]['driver_underruns_observed']=1
        self.assertEqual(m.select_panel(runs)['lowest_tested_timing_clock_mhz'],40)

    def test_panel_missing_callback_or_overflow_is_fatal(self):
        rows=panel_rows();rows.pop(1)
        with self.assertRaises(ValueError): m.panel(rows)
        rows=panel_rows();rows[-1]['overflow']=True
        with self.assertRaises(ValueError): m.panel(rows)


class BuildEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.source=Path(self.temp.name)/'project';self.build=self.source/'build';self.build.mkdir(parents=True)
        self.project=dict(target='esp32p4',project_name='probe',project_path=str(self.source),config_file=str(self.build/'sdkconfig'))
        self.metadata=self.build/'project_description.json';self.metadata.write_text(json.dumps(self.project))
        self.lock=self.source/'dependencies.lock'
        self.lock.write_text('dependencies:\n  espressif/esp-sr:\n    version: 2.4.7\n  espressif/esp_peer:\n    version: 1.5.7\n  espressif/esp_audio_codec:\n    version: 2.5.0\n')
        self.map=self.build/'probe.map';sections=[]
        bindings=[
            ('espressif__esp-sr/lib/esp32p4_less_v3_idf6','libesp_audio_processor.a','aec_process'),
            ('espressif__esp-sr/lib/esp32p4_less_v3_idf6','libesp_audio_front_end.a','afe_aec_create'),
            ('espressif__esp_peer/libs/esp32p4','libpeer_default.a','esp_peer_get_default_impl'),
        ]
        bindings += [('espressif__esp_audio_codec/lib/esp32p4','libesp_audio_codec.a',symbol) for symbol in
                     ('esp_opus_enc_open','esp_opus_enc_process','esp_opus_enc_close','esp_opus_dec_open',
                      'esp_opus_dec_decode','esp_opus_dec_reset','esp_opus_dec_close')]
        for component,name,symbol in bindings:
            path=self.source/'managed_components'/component/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'archive')
            sections.append(f' .text.{symbol}\n  0x48000000 0x10 {path}(engine.obj)\n')
        self.map.write_text(''.join(sections))
        for suffix in ('bin','elf'): (self.build/f'probe.{suffix}').write_bytes(b'image')

    def test_actual_archive_symbol_binding_and_hashes(self):
        result=build_evidence.collect(self.build)
        self.assertEqual(result['esp_sr'],'2.4.7')
        self.assertEqual(result['afe_archive']['bytes'],7)
        self.assertEqual(len(result['afe_archive']['sha256']),64)
        self.assertFalse((self.build/'sdkconfig').exists()) # metadata only; no config read

    def test_archive_name_without_symbol_binding_is_insufficient(self):
        self.map.write_text(self.map.read_text().replace('.text.aec_process','.text.unrelated'))
        with self.assertRaisesRegex(ValueError,'binding absent'): build_evidence.collect(self.build)

    def test_wrong_target_or_root_config_metadata_rejected(self):
        for key,value in (('target','esp32s3'),('config_file',str(self.source/'sdkconfig'))):
            metadata=dict(self.project);metadata[key]=value;self.metadata.write_text(json.dumps(metadata))
            with self.assertRaises(ValueError): build_evidence.collect(self.build)

    def test_wrong_component_version_rejected(self):
        self.lock.write_text(self.lock.read_text().replace('2.4.7','2.4.6'))
        with self.assertRaisesRegex(ValueError,'pin mismatch'): build_evidence.collect(self.build)

    def test_p4_codec_exception_does_not_accept_s3_pin(self):
        self.lock.write_text(self.lock.read_text().replace('2.5.0','2.6.2'))
        with self.assertRaisesRegex(ValueError,'P4 codec exception'): build_evidence.collect(self.build)

    def test_codec_archive_without_opus_implementation_rejected(self):
        self.map.write_text(self.map.read_text().replace('.text.esp_opus_dec_decode','.text.unrelated'))
        with self.assertRaisesRegex(ValueError,'binding absent'): build_evidence.collect(self.build)


if __name__=='__main__': unittest.main()

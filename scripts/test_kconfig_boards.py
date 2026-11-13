"""Resolve the real board/AEC Kconfig with IDF confgen; no firmware or ports.

Only target/heap/task infrastructure is stubbed. Inputs contain no credentials;
generated configs stay in TMPDIR. Values of these synthetic configs are checked with the
key-diff parser; only test results are emitted, never generated config contents.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('keydiff',ROOT/'scripts/sdkconfig_keydiff.py')
keydiff=importlib.util.module_from_spec(spec);spec.loader.exec_module(keydiff)
IDF_PYTHON=Path(os.environ.get('IDF_PYTHON_ENV_PATH',Path.home()/'.espressif/python_env/idf6.1_py3.12_env'))/'bin/python'


class BoardKconfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='board-confgen-')
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.counter=0

    def resolve(self,target,extra=()):
        self.counter+=1
        directory=self.directory/str(self.counter);directory.mkdir()
        kconfig=directory/'Kconfig'
        kconfig.write_text('''config IDF_TARGET
    string "Target"
config IDF_TARGET_ESP32S3
    bool
    default y if IDF_TARGET = "esp32s3"
config IDF_TARGET_ESP32P4
    bool
    default y if IDF_TARGET = "esp32p4"
config HEAP_ABORT_WHEN_ALLOCATION_FAILS
    bool "Fail stop"
config FREERTOS_TASK_CREATE_ALLOW_EXT_MEM
    bool
    default y
config HEAP_TRACING_STANDALONE
    bool "standalone trace"
config ESP_SYSTEM_USE_FRAME_POINTER
    bool "frame pointers"
'''+f'source "{ROOT}/main/Kconfig.projbuild"\n'+
            f'source "{ROOT}/components/pipecat_esp_peer/Kconfig"\n'+
            f'source "{ROOT}/components/dg_audio/Kconfig"\n')
        defaults=directory/'defaults'
        defaults.write_text(f'CONFIG_IDF_TARGET="{target}"\n'+'\n'.join('CONFIG_'+value for value in extra)+'\n')
        config=directory/'sdkconfig'
        subprocess.run([str(IDF_PYTHON),'-m','kconfgen','--kconfig',str(kconfig),
                        '--config',str(config),'--defaults',str(defaults),'--output','config',str(config),
                        '--output','header',str(directory/'sdkconfig.h')],
                       capture_output=True,text=True,check=True,timeout=20)
        return directory,keydiff.settings(config)

    def assert_header(self,directory,enabled,gain=0,guard=0,rearm=0):
        source=directory/'check.cpp'
        source.write_text('#include "dg_audio/aec_config.h"\n'+
            f'static_assert(dg_audio::aec_config::enabled=={str(enabled).lower()});\n'+
            f'static_assert(dg_audio::aec_config::ref_gain_db=={gain});\n'+
            f'static_assert(dg_audio::aec_config::guard_ms=={guard});\n'+
            f'static_assert(dg_audio::aec_config::rearm_ms=={rearm});\n')
        subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror','-I',str(directory),
                        '-I',str(ROOT/'components/dg_audio/include'),'-c',str(source),'-o',str(directory/'check.o')],
                       capture_output=True,text=True,check=True,timeout=20)

    def assert_flag(self,values,key,enabled):
        self.assertEqual(values.get('CONFIG_'+key,'n'),'y' if enabled else 'n',key)

    def test_p4_audio_defaults_have_one_owner_and_preserve_aec_frame40_priority(self):
        # The one mic override comes from the public P4 overlay. Seed/stack
        # defaults come from peer Kconfig, not duplicated overlay constants.
        overlay=keydiff.settings(ROOT/'boards/p4xc/sdkconfig.defaults')
        self.assertNotIn('CONFIG_DG_PLAYBACK_SEED_DBFS_X10',overlay)
        self.assertNotIn('CONFIG_DG_AUDIO_TX_STACK_BYTES',overlay)
        mic='DG_MIC_GAIN_PERCENT='+overlay['CONFIG_DG_MIC_GAIN_PERCENT']
        _,normal=self.resolve('esp32p4',[mic])
        self.assertEqual(normal['CONFIG_DG_MIC_GAIN_PERCENT'],overlay['CONFIG_DG_MIC_GAIN_PERCENT'])
        self.assertTrue(-600<=int(normal['CONFIG_DG_PLAYBACK_SEED_DBFS_X10'])<=-100)
        self.assertEqual(normal['CONFIG_DG_AUDIO_TX_STACK_BYTES'],'26624')
        _,s3_aec=self.resolve('esp32s3',['DG_ECHOEAR_AEC=y'])
        _,p4_aec=self.resolve('esp32p4',[mic,'DG_ECHOEAR_AEC=y'])
        self.assertEqual(p4_aec['CONFIG_DG_AUDIO_TX_STACK_BYTES'],s3_aec['CONFIG_DG_AUDIO_TX_STACK_BYTES'])
        for enabled in (False,True):
            _,frame40=self.resolve('esp32p4',[mic,'DG_OPUS_UPLINK_FRAME40=y']+(['DG_ECHOEAR_AEC=y'] if enabled else []))
            self.assertGreaterEqual(int(frame40['CONFIG_DG_AUDIO_TX_STACK_BYTES']),32768)

    def test_encoder_stack_resolution_all_board_aec_frame40_defaults(self):
        for target,board in (('esp32p4','DG_BOARD_P4XC'),('esp32s3','DG_BOARD_ECHOEAR'),
                             ('esp32s3','DG_BOARD_WAVESHARE_146')):
            for aec in (False,True):
                for frame40 in (False,True):
                    with self.subTest(board=board,aec=aec,frame40=frame40):
                        extra=[board+'=y','DG_ECHOEAR_AEC='+('y' if aec else 'n'),
                               'DG_OPUS_UPLINK_FRAME40='+('y' if frame40 else 'n')]
                        _,values=self.resolve(target,extra)
                        enabled=aec and board!='DG_BOARD_WAVESHARE_146'
                        expected=32768 if frame40 else 26624 if enabled or board=='DG_BOARD_P4XC' else \
                                 28672 if board=='DG_BOARD_ECHOEAR' else 32768
                        self.assertEqual(values['CONFIG_DG_AUDIO_TX_STACK_BYTES'],str(expected))

    def test_s3_legacy_aec_off_and_on(self):
        for enabled in (False,True):
            with self.subTest(enabled=enabled):
                directory,values=self.resolve('esp32s3',['DG_ECHOEAR_AEC=y','DG_ECHOEAR_AEC_REF_GAIN_DB=15'] if enabled else [])
                self.assert_flag(values,'DG_BOARD_ECHOEAR',True)
                self.assert_flag(values,'DG_ECHOEAR_AEC',enabled)
                self.assert_flag(values,'HEAP_ABORT_WHEN_ALLOCATION_FAILS',enabled)
                self.assert_flag(values,'DG_REUSE_TRANSPORT_WORKERS',enabled)
                self.assert_header(directory,enabled,15 if enabled else 0,400 if enabled else 0,2000 if enabled else 0)

    def test_heap_attribution_is_explicit_and_p4_only(self):
        for target in ('esp32p4','esp32s3'):
            for requested in (False,True):
                extra=['HEAP_TRACING_STANDALONE=y','ESP_SYSTEM_USE_FRAME_POINTER=y','DG_HEAP_ATTRIBUTION=y'] if requested else []
                _,values=self.resolve(target,extra)
                self.assert_flag(values,'DG_HEAP_ATTRIBUTION',target=='esp32p4' and requested)
        _,values=self.resolve('esp32p4',['DG_HEAP_ATTRIBUTION=y'])
        self.assert_flag(values,'DG_HEAP_ATTRIBUTION',False)

    def test_nondefault_legacy_values_on_both_targets(self):
        for target in ('esp32s3','esp32p4'):
            with self.subTest(target=target):
                directory,values=self.resolve(target,['DG_ECHOEAR_AEC=y','DG_ECHOEAR_AEC_REF_GAIN_DB=12',
                                                     'DG_ECHOEAR_AEC_GUARD_MS=123','DG_ECHOEAR_AEC_GUARD_REARM_MS=777'])
                for name,value in [('REF_GAIN_DB','12'),('GUARD_MS','123'),('GUARD_REARM_MS','777')]:
                    self.assertEqual(values['CONFIG_DG_ECHOEAR_AEC_'+name],value)
                self.assert_header(directory,True,12,123,777)

    def test_p4_aec_and_demo_combinations(self):
        for aec in (False,True):
            for demo in (False,True):
                with self.subTest(aec=aec,demo=demo):
                    extra=(['DG_ECHOEAR_AEC=y'] if aec else [])+(['DG_DEMO_AUTOSTART=y'] if demo else [])
                    directory,values=self.resolve('esp32p4',extra)
                    self.assert_flag(values,'DG_BOARD_P4XC',True)
                    self.assert_flag(values,'DG_BOARD_ECHOEAR',False)
                    self.assert_flag(values,'DG_BOARD_WAVESHARE_146',False)
                    self.assert_flag(values,'DG_ECHOEAR_AEC',aec)
                    self.assert_flag(values,'HEAP_ABORT_WHEN_ALLOCATION_FAILS',aec)
                    self.assert_flag(values,'DG_DEMO_AUTOSTART',demo)
                    self.assert_header(directory,aec,0,400 if aec else 0,2000 if aec else 0)

    def test_waveshare_cannot_select_aec_demo_or_calibration(self):
        for requested in ([],['DG_ECHOEAR_AEC=y','DG_DEMO_AUTOSTART=y','DG_ECHOEAR_AEC_REF_GAIN_DB=12']):
            directory,values=self.resolve('esp32s3',['DG_BOARD_WAVESHARE_146=y']+requested)
            self.assert_flag(values,'DG_BOARD_WAVESHARE_146',True)
            self.assert_flag(values,'DG_ECHOEAR_AEC',False)
            self.assert_flag(values,'DG_DEMO_AUTOSTART',False)
            self.assertNotIn('CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB',values)
            self.assert_header(directory,False)

    def test_other_targets_board_choices_are_unavailable(self):
        for target,requested,selected in [('esp32s3','DG_BOARD_P4XC','DG_BOARD_ECHOEAR'),
                                          ('esp32p4','DG_BOARD_ECHOEAR','DG_BOARD_P4XC'),
                                          ('esp32p4','DG_BOARD_WAVESHARE_146','DG_BOARD_P4XC')]:
            with self.subTest(target=target,requested=requested):
                _,values=self.resolve(target,[requested+'=y'])
                self.assert_flag(values,requested,False)
                self.assert_flag(values,selected,True)


if __name__=='__main__':unittest.main()

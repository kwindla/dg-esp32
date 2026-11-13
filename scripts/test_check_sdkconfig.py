"""Regression checks for local overrides, redaction and explicit disabled values."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('check_config', Path(__file__).with_name('check-sdkconfig.py'))
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

ROOT = Path(__file__).resolve().parents[1]
# Explicit exclusions from the unchanged legacy S3 defaults. Adding/removing
# any root key requires a deliberate common-vs-hardware classification here.
S3_ONLY_KEYS = {'CONFIG_'+name for name in '''
IDF_TARGET ESPTOOLPY_FLASHSIZE_16MB ESPTOOLPY_FLASHSIZE
SPIRAM SPIRAM_MODE_OCT SPIRAM_SPEED_80M SPIRAM_XIP_FROM_PSRAM
ESP_DEFAULT_CPU_FREQ_MHZ_240 ESP_CONSOLE_USB_SERIAL_JTAG
ESP32S3_INSTRUCTION_CACHE_32KB ESP_WIFI_STATIC_RX_BUFFER_NUM
ESP_WIFI_IRAM_OPT ESP_WIFI_RX_IRAM_OPT SPIRAM_TRY_ALLOCATE_WIFI_LWIP
ESPTOOLPY_FLASHMODE_DIO ESPTOOLPY_FLASHMODE_QIO
'''.split()}

P4_AUDIO_KEYS = {'CONFIG_DG_MIC_GAIN_PERCENT'}
P4_DISPLAY_KEYS = {'CONFIG_LCD_DSI_ISR_HANDLER_IN_IRAM','CONFIG_LCD_DSI_ISR_CACHE_SAFE',
                   'CONFIG_SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY'}

P4_CORE_KEYS = {'CONFIG_IDF_TARGET','CONFIG_SPIRAM'}
P4_CORE_PREFIXES = ('CONFIG_ESP32P4_','CONFIG_ESPTOOLPY_',
                    'CONFIG_SPIRAM_MODE_','CONFIG_SPIRAM_SPEED_','CONFIG_SPIRAM_XIP_',
                    'CONFIG_ESP_CONSOLE_','CONFIG_PM_','CONFIG_ESP_DEFAULT_CPU_FREQ_','CONFIG_CACHE_',
                    'CONFIG_ESP_WIFI_REMOTE_','CONFIG_SLAVE_IDF_TARGET_','CONFIG_ESP_HOSTED_','CONFIG_WIFI_RMT_')
# Wi-Fi/LWIP allocation belongs to the later hosted/remote dependency graph.
DEFERRED_WIFI_KEYS = {'CONFIG_SPIRAM_TRY_ALLOCATE_WIFI_LWIP'}

class BoardDefaultsTests(unittest.TestCase):
    def test_common_exactly_covers_every_non_s3_root_key_without_drift(self):
        root=checker.settings(ROOT/'sdkconfig.defaults')
        common=checker.settings(ROOT/'boards/common/sdkconfig.defaults')
        self.assertFalse(set(common)&S3_ONLY_KEYS)
        self.assertEqual(set(root),set(common)|S3_ONLY_KEYS)
        self.assertEqual(common,{key:value for key,value in root.items() if key not in S3_ONLY_KEYS})

    def test_common_excludes_target_hardware_console_cache_and_native_wifi(self):
        common=checker.settings(ROOT/'boards/common/sdkconfig.defaults')
        prefixes=('CONFIG_IDF_TARGET','CONFIG_ESPTOOLPY_FLASH','CONFIG_SPIRAM_MODE_',
                  'CONFIG_SPIRAM_SPEED_','CONFIG_ESP_DEFAULT_CPU_FREQ','CONFIG_ESP_CONSOLE_',
                  'CONFIG_ESP32S3_','CONFIG_CACHE_','CONFIG_ESP_WIFI_')
        self.assertFalse([key for key in common if key.startswith(prefixes)])

    def test_p4_core_overlay_and_two_mbaud_power_management_constraint(self):
        values=checker.settings(ROOT/'boards/p4xc/sdkconfig.defaults')
        self.assertFalse([key for key in values if key not in P4_CORE_KEYS and key not in P4_AUDIO_KEYS and key not in P4_DISPLAY_KEYS and not key.startswith(P4_CORE_PREFIXES)])
        self.assertFalse(set(values)&DEFERRED_WIFI_KEYS)
        self.assertEqual(set(values)&set(checker.settings(ROOT/'boards/common/sdkconfig.defaults')),
                         {'CONFIG_DG_MIC_GAIN_PERCENT'})
        self.assertNotIn('CONFIG_DG_PLAYBACK_SEED_DBFS_X10',values)
        self.assertNotIn('CONFIG_DG_AUDIO_TX_STACK_BYTES',values)
        gain=int(values['CONFIG_DG_MIC_GAIN_PERCENT'].split('=',1)[1])
        self.assertTrue(1<=gain<=1600)
        for key,value in {'ESP_CONSOLE_UART_CUSTOM':'y','ESP_CONSOLE_UART_BAUDRATE':'2000000',
                          'ESP_CONSOLE_UART_CUSTOM_NUM_0':'y',
                          'PM_ENABLE':'n','PM_SLEEP_CLK_ICG_ENABLE':'n','ESP_DEFAULT_CPU_FREQ_MHZ_360':'y'}.items():
            self.assertEqual(values['CONFIG_'+key],'CONFIG_'+key+'='+value)

    def test_deferred_wifi_keys_cannot_match_the_p4_core_allowlist(self):
        for key in DEFERRED_WIFI_KEYS:
            self.assertNotIn(key,P4_CORE_KEYS)
            self.assertFalse(key.startswith(P4_CORE_PREFIXES))

    def test_p4_display_preserves_spike_handler_placement_without_claiming_cache_safety(self):
        values=checker.settings(ROOT/'boards/p4xc/sdkconfig.defaults')
        self.assertEqual(values['CONFIG_SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY'],'CONFIG_SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY=y')
        self.assertEqual(values['CONFIG_LCD_DSI_ISR_HANDLER_IN_IRAM'],'CONFIG_LCD_DSI_ISR_HANDLER_IN_IRAM=y')
        self.assertEqual(values['CONFIG_LCD_DSI_ISR_CACHE_SAFE'],'CONFIG_LCD_DSI_ISR_CACHE_SAFE=n')

class ConfigTests(unittest.TestCase):
    def test_subproject_local_key_not_applicable_and_disabled_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root/'sub'; project.mkdir()
            (root/'sdkconfig.local').write_text('CONFIG_DG_PASSWORD="private-test-value"\n')
            (project/'sdkconfig.defaults').write_text('# CONFIG_FEATURE is not set\n')
            (project/'sdkconfig').write_text('# CONFIG_FEATURE is not set\n')
            old = checker.ROOT; checker.ROOT = root
            try:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertFalse(checker.check(project))
                self.assertIn('not applicable', output.getvalue())
                self.assertNotIn('private-test-value', output.getvalue())
                (project/'sdkconfig').write_text('CONFIG_FEATURE=y\n')
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(checker.check(project))
            finally: checker.ROOT = old

    def test_root_missing_local_key_still_fails_and_redacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'sdkconfig.local').write_text('CONFIG_DG_PASSWORD="private-test-value"\n')
            (root/'sdkconfig').write_text('')
            old = checker.ROOT; checker.ROOT = root
            try:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertTrue(checker.check(root))
                self.assertNotIn('private-test-value', output.getvalue())
                self.assertNotIn('not applicable (absent', output.getvalue())
            finally: checker.ROOT = old

if __name__ == '__main__': unittest.main()

import json
import unittest
from step9a_r2_checks import DisplayMonitor, audit


class Checks(unittest.TestCase):
    def test_serial_reset_registers_expected_boot_before_the_pulse(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from esp_serial import EspSerial
        boot='I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+'a'*64
        monitor=DisplayMonitor();monitor.line(boot)
        monitor.line('PERF {"display":{"rendered":100,"animation":true}}')
        def ready(*args,**kwargs):
            self.assertEqual(monitor.pending_requested_boots,1)
            monitor.line(boot);return boot
        board=SimpleNamespace(cursor=7,s=SimpleNamespace(dtr=True,rts=True),display_monitor=monitor,wait_for=ready)
        with patch('esp_serial.time.sleep'):EspSerial.reset(board)
        monitor.line('PERF {"display":{"rendered":10,"animation":true}}')
        self.assertTrue(monitor.result()['passed'])

    def test_missing_exact_boot_retires_ticket_and_later_reboot_stays_fatal(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from esp_serial import EspSerial
        import re
        boot='I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+'a'*64
        for missing in ('READY transport-console',boot[:-1],'garbage '+boot):
            monitor=DisplayMonitor();monitor.line(boot)
            monitor.line('PERF {"display":{"rendered":100,"animation":true}}')
            def wait(pattern,*args,**kwargs):
                self.assertIsNone(re.search(pattern,missing))
                raise TimeoutError('Missing exact boot')
            board=SimpleNamespace(cursor=7,s=SimpleNamespace(dtr=True,rts=True),display_monitor=monitor,wait_for=wait)
            with patch('esp_serial.time.sleep'),self.assertRaises(TimeoutError):EspSerial.reset(board)
            self.assertEqual(monitor.pending_requested_boots,0)
            monitor.line(boot)
            self.assertFalse(monitor.result()['boot_epochs'][-1]['requested'])
            self.assertTrue(any('unexpected_boot' in e for e in monitor.errors))
            with self.assertRaisesRegex(RuntimeError,'requested_boot_failed'):monitor.check()

    def test_reset_pulse_failure_retires_ticket(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from esp_serial import EspSerial
        class BrokenPort:
            def __setattr__(self,*args):raise OSError('Pulse failed')
        monitor=DisplayMonitor();board=SimpleNamespace(cursor=7,s=BrokenPort(),display_monitor=monitor,wait_for=Mock())
        with self.assertRaises(OSError):EspSerial.reset(board)
        self.assertEqual(monitor.pending_requested_boots,0)
        board.wait_for.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'Pulse failed'):monitor.check()
    def test_requested_boot_restarts_only_liveness_and_raw_audit_replays_ledger(self):
        boot='I (1338) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+'a'*64
        def perf(n):return 'PERF '+json.dumps(dict(display=dict(rendered=n,animation=True)))
        monitor=DisplayMonitor();monitor.line(boot);monitor.line(perf(2170))
        monitor.expect_boot_reset();monitor.line(boot);monitor.line(perf(731));monitor.line(perf(734))
        self.assertTrue(monitor.result()['passed']);self.assertEqual(monitor.snapshots,3)
        self.assertTrue(monitor.result()['boot_epochs'][1]['requested'])
        raw=('\n'.join((boot,perf(2170),boot,perf(731),perf(734)))+'\n').encode()
        self.assertTrue(audit(raw,expected_boot_indices=[1])['passed'])
        with self.assertRaisesRegex(RuntimeError,'unexpected_boot'):audit(raw)

    def test_reboot_during_original_remains_fatal_and_errors_survive_requested_reset(self):
        boot='I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+'a'*64
        m=DisplayMonitor();m.line(boot);m.line('PERF {"display":{"rendered":100,"animation":true}}')
        m.line(boot)
        with self.assertRaisesRegex(RuntimeError,'unexpected_boot'):m.check()
        m=DisplayMonitor();m.line(boot);m.feed(b'E (2) spi_master: DMA TX underflow\n')
        m.expect_boot_reset();m.line(boot)
        with self.assertRaisesRegex(RuntimeError,'spi_master'):m.check()
        self.assertEqual(len(m.result()['boot_epochs']),2)
        m=DisplayMonitor();m.line('PERF {"display":{"rendered":0,"animation":false}}');m.line(boot)
        with self.assertRaisesRegex(RuntimeError,'unexpected_boot'):m.check()

    def test_prose_or_truncated_boot_cannot_reset_liveness(self):
        for line in ('prose elf_sha256='+'a'*64,'I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+'a'*63):
            m=DisplayMonitor();m.line('PERF {"display":{"rendered":100,"animation":true}}')
            m.line(line);m.line('PERF {"display":{"rendered":10,"animation":true}}')
            with self.assertRaisesRegex(RuntimeError,'display_liveness'):m.check()
        with self.assertRaisesRegex(RuntimeError,'missing_requested_boot_records'):
            audit(b'PERF {"display":{"rendered":1,"animation":true}}\n',expected_boot_indices=[1])

    def test_every_byte_boundary_and_cleanup_error(self):
        data=b'OK #1 cleanup\nE spi_master: DMA TX underflow detected\n'
        for split in range(len(data)+1):
            m=DisplayMonitor();m.feed(data[:split]);m.feed(data[split:])
            with self.assertRaises(RuntimeError):m.check()

    def test_interleaved_perf_is_failure(self):
        with self.assertRaises(RuntimeError):
            audit(b'PERF {"display":{}E spi_master: DMA TX underflow detected\n}\n')

    def test_malformed_without_driver_text(self):
        with self.assertRaises(RuntimeError):audit(b'PERF {"display":\n')

    def test_counter_liveness_and_diagnostic(self):
        m=DisplayMonitor(diagnostic=True)
        for rendered in (5,6,6):
            m.line('PERF '+json.dumps({'display':{'rendered':rendered,'animation':True}}))
        self.assertFalse(m.result()['passed']);m.check()
        m.diagnostic=False
        with self.assertRaises(RuntimeError):m.check()

    def test_recovery_timeout_and_dead_stop(self):
        for key in ('display_recovery_attempts','display_recoveries','gate_timeouts','display_gate_timeouts','dead'):
            m=DisplayMonitor();m.line('DISPLAY '+json.dumps({key:1}))
            with self.assertRaises(RuntimeError):m.check()

    def test_lcd_tag_and_malformed_diagnostic(self):
        for data in (b'E (123) lcd_panel.io.spi: recycle failed', b'DISPLAY_FLASH_STATS {bad}\n'):
            with self.assertRaises(RuntimeError):audit(data)

    def test_durable_store_terminal_survives_cache_eviction(self):
        from types import SimpleNamespace
        from step9a_support import terminal
        event={'id':'persist-1','error':'none','persisted':True}
        board=SimpleNamespace(display_terminal_events=True,
            display_request_kinds={'persist-1':'persist'},display_request_cursors={'persist-1':7},
            wait_for=lambda regex,timeout,since:'I (123) display-store: DISPLAY_WORK_RESULT '+json.dumps(event))
        self.assertEqual(terminal(board,'persist-1')['state'],3)

    def test_counter_channel_fails_without_rom_text(self):
        for key in ('dma_errors','send_failures','late_callbacks'):
            with self.assertRaises(RuntimeError):audit(('PERF '+json.dumps({'display':{key:1,'rendered':7,'animation':False}})+'\n').encode())
        with self.assertRaises(RuntimeError):audit(b'DISPLAY {"dma":[0,1]}\n')

    def test_anchor_ignores_prose(self):
        self.assertTrue(audit(b'I (10) app: prose containing DISPLAY words\n')['passed'])

    def test_diagnostic_label_matches_monitor_mode(self):
        self.assertTrue(audit(b'DISPLAY {"diagnostic":true}\n',diagnostic=True)['passed'])
        with self.assertRaises(RuntimeError):audit(b'DISPLAY {"diagnostic":true}\n')

    def test_production_priority_requires_capture_success_and_priority_one(self):
        good={'lcd_initialized':True,'lcd_tx_captured':True,'priority_applies':1,'last_priority_written':1,'tx_pri':1,'rendered':7,'animation':False}
        def wire(h):return ('PERF '+json.dumps({'display':h,'crypto_dma':{'sha_psram_input_bytes':123456,'aes_psram_input_bytes':4096}})+'\n').encode()
        self.assertTrue(audit(wire(good))['passed'])  # Nonzero PSRAM bytes are reported, not forbidden.
        for key,value in [('lcd_tx_captured',False),('priority_applies',0),('last_priority_written',0),('last_priority_written',2),('priority_applies',True),('tx_pri',0),('tx_pri',-1),('tx_pri',True)]:
            with self.subTest(key=key,value=value):
                bad=dict(good);bad[key]=value
                with self.assertRaises(RuntimeError):audit(wire(bad))
        for key in ('lcd_tx_captured','priority_applies','last_priority_written','tx_pri'):
            bad=dict(good);del bad[key]
            with self.assertRaises(RuntimeError):audit(wire(bad))

    def test_priority_not_required_before_lcd_initialization(self):
        self.assertTrue(audit(b'PERF {"display":{"lcd_initialized":false,"rendered":0,"animation":false}}\n')['passed'])

    def test_numeric_diagnostic_records_have_strict_json_framing(self):
        with self.assertRaises(RuntimeError):audit(b'DISPLAY_R4_GDMA_READY {bad}\n')
        self.assertTrue(audit(b'DISPLAY_R4_GDMA_READY {"pairs":[]}\n')['passed'])

    def test_readiness_spliced_with_informational_peer_log_fails_closed(self):
        # Reproduce B5's fifth-pair truncation followed by an I-level peer log.
        # There is no E-level tag to rescue a monitor that stops parsing JSON.
        pairs=[dict(pair=i,tx_peri_sel=63,rx_peri_sel=63,tx_priority=0,rx_priority=0) for i in range(5)]
        complete='DISPLAY_R4_GDMA_READY '+json.dumps(dict(device_us=30425680,pairs=pairs),separators=(',',':'))
        at=complete.rindex('"rx_peri_sel"')+2
        spliced=(complete[:at]+'I (31754) esp-peer: PHASE open t=29432741\n').encode()
        for split in range(len(spliced)+1):
            m=DisplayMonitor();pending=b''
            for chunk in (spliced[:split],spliced[split:]):
                m.feed(chunk);pending+=chunk
                while b'\n' in pending:
                    line,pending=pending.split(b'\n',1);m.line(line.decode())
            self.assertEqual(m.result()['errors'][0]['malformed_stat'],'DISPLAY_R4_GDMA_READY')
            with self.assertRaises(RuntimeError):m.check()
        # The same two complete serialized records must remain acceptable.
        self.assertTrue(audit((complete+'\nI (31754) esp-peer: PHASE open t=29432741\n').encode())['passed'])

    def test_clean_advances(self):
        data=b''.join(('PERF '+json.dumps({'display':{'rendered':i,'animation':True}})+'\n').encode() for i in (2,3,7))
        self.assertTrue(audit(data)['passed'])

    def test_raw_failure_retains_component_and_line_at_every_boundary(self):
        line=b'E (2025) I2C_If: Fail to write to dev 30\n'
        for split in range(len(line)+1):
            m=DisplayMonitor();m.feed(line[:split]);m.feed(line[split:])
            error=m.result()['errors'][0]
            self.assertEqual(error['component'],'I2C_If')
            self.assertEqual(error['origin'],'non-display')
            self.assertEqual(error['matched_line'],line.decode().strip())
            with self.assertRaisesRegex(RuntimeError,'non-display stop'):m.check()
        self.assertTrue(audit(b'W (2025) I2C_If: verified REG44 startup retry\n')['passed'])

if __name__=='__main__':unittest.main()

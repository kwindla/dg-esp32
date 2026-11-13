"""Demo lifecycle checks with no serial, listening sockets or service calls."""
from contextlib import contextmanager
import io
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import demo_session as demo
from esp_serial import SerialDisconnected


class DemoTests(unittest.TestCase):
    def test_preflight_rejects_stale_build_metadata_or_mixed_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);descriptor=bytearray(256)
            descriptor[:4]=bytes.fromhex('3254cdab');descriptor[16:19]=b'old'
            elf=b'ELF'+descriptor[:144]+b'trailer'
            descriptor[144:176]=hashlib.sha256(elf).digest()
            (root/'dg-esp32.elf').write_bytes(elf)
            (root/'dg-esp32.bin').write_bytes(bytes(32)+descriptor)
            self.assertEqual(demo.firmware_artifacts(root,'old'),hashlib.sha256(elf).hexdigest())
            with self.assertRaisesRegex(RuntimeError,'Stale'):
                demo.firmware_artifacts(root,'new')
            (root/'dg-esp32.elf').write_bytes(elf+b'new link')
            with self.assertRaisesRegex(RuntimeError,'SHA256'):
                demo.firmware_artifacts(root,'old')

    def test_check_only_never_acquires_bench_or_starts_server(self):
        with patch.object(demo, 'preflight', return_value={'elf_sha256': 'test'}), \
             patch.object(demo, 'demo_server', side_effect=AssertionError('server access')):
            self.assertEqual(demo.main(['--check-only']), 0)

    def test_explicit_firmware_ref_is_forwarded_without_hardware_access(self):
        with patch.object(demo, 'preflight', return_value={}) as preflight:
            self.assertEqual(demo.main(['--check-only', '--firmware-ref', 'b8986a8']), 0)
            preflight.assert_called_once_with('b8986a8')

    def test_unplug_counters_reject_reset_loss_or_clip(self):
        from test_usb_absence import evidence
        before, after, _ = evidence()
        self.assertTrue(demo.unplug_counter_checks(before, after)['passed'])
        for key, value in (('pc_id', 'new-session'), ('lost', 1), ('mic_in_turn_clips', 1)):
            damaged = dict(after, **{key: value})
            self.assertFalse(demo.unplug_counter_checks(before, damaged)['passed'])

    def test_implicit_observe_mode_rejects_unplug_stats_before_hardware(self):
        for mode in ('--observe','--provision','--provision-only'):
            with self.subTest(mode=mode), patch.object(demo,'preflight',side_effect=AssertionError('preflight')):
                with self.assertRaises(SystemExit):demo.main([mode,'--unplug-check'])

    def test_prepare_reuses_initialized_display_and_arena(self):
        board=Mock(cursor=0)
        board.wait_for.return_value='PERF '+json.dumps({'display':{'lcd_initialized':True},'heap':{'psram':{'free':12000000,'largest':10000000}}})
        def command(text,*args):
            if text=='disp init' or text.startswith('disp arena'):raise AssertionError('duplicate initialization')
        board.command.side_effect=command
        display=SimpleNamespace(read=Mock(return_value={'flash':{'health':'ready'},'ram':{'budget':491520}}),setup=Mock(return_value={}))
        with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules',{'step9a_support':display}):
            demo.prepare_board(board,'http://host:7861',Path(temp)/'budget')
        display.setup.assert_called_once_with(board,initialized=True,arena_initialized=True)

    def test_actual_setup_skips_existing_arena(self):
        import step9a_support as display
        board=Mock()
        with tempfile.TemporaryDirectory() as temp:
            budget=Path(temp)/'budget.json';budget.write_text(json.dumps(dict(budget=491520,F=12000000,W=1287760,largest=10000000)))
            with patch.object(display,'BUDGET_PATH',budget),patch.object(display,'host_address',return_value='host'), \
                 patch.object(display,'pack',return_value=[]),patch.object(display,'read',return_value={}):
                display.setup(board,initialized=True,arena_initialized=True)
        self.assertEqual([c.args[0] for c in board.command.call_args_list],['disp origin http://host:7874'])

    def test_provision_requires_durable_results_and_inventory(self):
        for durable, inventory_durable in ((True,True),(False,True),(True,False)):
            board=Mock()
            display=SimpleNamespace(host_address=lambda:'host',request=Mock(),terminal=Mock(return_value={'error':'none','persisted':durable}),
                read=Mock(return_value={'items':[['hash',123,0,True,inventory_durable,inventory_durable,'none']], 'next':256}))
            setup={'images':[{'hash':'hash','bytes':123},{'hash':'hash','bytes':123}]}
            with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules',{'step9a_support':display}), \
                 patch.object(demo,'prepare_board',return_value=setup), \
                 patch('echoear_status_assets.provision_status',return_value=[]), \
                 patch('echoear_status_assets.status_assets',return_value=[]):
                if durable and inventory_durable:
                    result=demo.provision_assets(board,'http://host:7861',Path(temp)/'budget')
                    self.assertEqual(result['persisted_hashes'],['hash'])
                    self.assertEqual(display.request.call_count,1)
                else:
                    with self.assertRaisesRegex(RuntimeError,'persistence|inventory'):
                        demo.provision_assets(board,'http://host:7861',Path(temp)/'budget')
            self.assertEqual(board.command.call_args_list[0].args[0],'demo hold')
            self.assertIn('"volume_db":-6',board.command.call_args_list[1].args[0])

    def test_combined_provision_requires_durable_status_inventory(self):
        for durable in (True,False):
            board=Mock();images={'images':[{'hash':'cat','bytes':123}]}
            inventory=[['cat',123,0,True,True,True,'none'],['status',456,0,True,durable,durable,'none']]
            display=SimpleNamespace(host_address=lambda:'host',request=Mock(),terminal=Mock(return_value={'error':'none','persisted':True}),read=Mock(return_value={'items':inventory,'next':256}))
            with tempfile.TemporaryDirectory() as temp,patch.dict('sys.modules',{'step9a_support':display}), \
                 patch.object(demo,'prepare_board',return_value=images), \
                 patch('echoear_status_assets.provision_status',return_value=[{'persisted':True}]) as provision, \
                 patch('echoear_status_assets.status_assets',return_value=[{'hash':'status'}]):
                if durable:
                    result=demo.provision_assets(board,'http://host:7861',Path(temp)/'budget')
                    self.assertEqual(result['persisted_hashes'],['cat','status']);self.assertTrue(result['status_assets_available'])
                else:
                    with self.assertRaisesRegex(RuntimeError,'inventory'):demo.provision_assets(board,'http://host:7861',Path(temp)/'budget')
                provision.assert_called_once_with(board,'http://host:7874')

    def test_observe_mode_never_sends_console_or_starts_fixture_server(self):
        from contextlib import nullcontext
        board=Mock(cursor=0,_seq=0)
        board.lines_since.side_effect=[KeyboardInterrupt,[]]
        echoear=SimpleNamespace(bench=lambda:nullcontext(),verify=Mock(return_value='pinned'),flash=Mock(),check_firmware=Mock(return_value='elf'))
        modules={'step9a_echoear':echoear,'step9a_assets':SimpleNamespace(asset_server=Mock(side_effect=AssertionError('fixture service'))),
                 'step9a_support':SimpleNamespace(host_address=lambda:'192.0.2.1'),
                 'board_port':SimpleNamespace(resolve_port=lambda:'pinned')}
        with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules',modules), \
             patch('esp_serial.EspSerial',return_value=board), patch.object(demo,'EVIDENCE',Path(temp)), \
             patch.object(demo,'preflight',return_value={'demo_autostart':True}), \
             patch.object(demo,'demo_server',return_value=nullcontext(Mock(poll=lambda:None,pid=123))), \
             patch.object(demo,'prepare_board',side_effect=AssertionError('host configuration')), \
             patch.object(demo,'nic_snapshot',return_value={'interfaces':{},'monotonic_s':1}):
            self.assertEqual(demo.main(['--observe','--out',str(Path(temp)/'run')]),0)
            board.command.assert_not_called()

    def test_transcripts_are_logged_and_other_bot_output_is_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            demo.copy_bot_output(io.StringIO('startup\nINFO Transcript: user: hello\nINFO Transcript: assistant: hi\n'), out)
            self.assertEqual((out/'transcripts.log').read_text().splitlines(),
                             ['INFO Transcript: user: hello', 'INFO Transcript: assistant: hi'])
            self.assertIn('startup', (out/'bot.log').read_text())

    def test_cleanup_attempts_every_command_even_after_failure(self):
        board = Mock()
        board.command.side_effect = [RuntimeError('serial timeout'), None, None, None]
        errors = demo.cleanup_board(board)
        self.assertEqual(len(errors), 1)
        self.assertEqual([c.args[0] for c in board.command.call_args_list],
                         ['audio 0', 'disconnect', 'disp character off', 'disp bench off'])

    def test_storage_provisioning_only_when_not_ready(self):
        for health in ('ready', 'unavailable'):
            board = Mock(cursor=0)
            board.wait_for.return_value = 'PERF '+json.dumps({'heap': {'psram': {'free': 12000000, 'largest': 10000000}}})
            display = SimpleNamespace(read=Mock(side_effect=[{'flash': {'health': health}}, {'flash': {'health': 'ready'}}]), setup=Mock(return_value={}))
            with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules', {'step9a_support': display}):
                demo.prepare_board(board, 'http://example:7861', Path(temp)/'budget.json')
            commands = [c.args[0] for c in board.command.call_args_list]
            self.assertEqual('disp provision-empty' in commands, health != 'ready')
            self.assertFalse(any('erase' in c for c in commands))
            display.setup.assert_called_once_with(board, initialized=True)
            self.assertIn('disp character on', commands)

    def test_interrupt_and_setup_failure_clean_up_inside_bench_lock(self):
        for fail_setup, flash_image in ((False, False), (True, False), (False, True)):
            events = []
            @contextmanager
            def resource(name, value=None):
                events.append('enter '+name)
                try:
                    yield value
                finally:
                    events.append('exit '+name)
            board = Mock(cursor=0)
            board.lines_since.side_effect = [[], []] if fail_setup else [KeyboardInterrupt, []]
            board.close.side_effect = lambda: events.append("exit serial")
            board.command.side_effect = lambda command, *args: events.append(command)
            echoear = SimpleNamespace(bench=lambda: resource('bench'), verify=Mock(return_value='pinned-echoear'),
                                      flash=Mock(), check_firmware=Mock(return_value='elf'))
            modules = {'step9a_echoear': echoear,
                       'esp_serial': SimpleNamespace(EspSerial=lambda *a, **kw: board, SerialDisconnected=SerialDisconnected),
                       'board_port': SimpleNamespace(resolve_port=lambda: 'pinned-echoear'),
                       'step9a_assets': SimpleNamespace(asset_server=lambda: resource('assets')),
                       'step9a_support': SimpleNamespace(host_address=lambda: '192.0.2.1')}
            bot = SimpleNamespace(poll=lambda: None)
            with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules', modules), \
                 patch.object(demo, 'EVIDENCE', Path(temp)), \
                 patch.object(demo, 'preflight', return_value={'elf_sha256': 'elf'}), \
                 patch.object(demo, 'demo_server', side_effect=lambda _: resource('bot', bot)), \
                 patch.object(demo, 'prepare_board', side_effect=RuntimeError('setup failed') if fail_setup else None, return_value={}), \
                 patch.object(demo, 'nic_snapshot', return_value={'interfaces': {}, 'monotonic_s': 1}):
                out = Path(temp)/'run'
                self.assertEqual(demo.main(['--out', str(out)]+(['--flash'] if flash_image else [])), int(fail_setup))
                saved = json.loads((out/'session.json').read_text())
                self.assertEqual(saved['ready'], not fail_setup)
                self.assertEqual(saved['interrupted'], not fail_setup)
            self.assertEqual(events[0], 'enter bench')
            self.assertEqual(events[-1], 'exit bench')
            self.assertLess(events.index('disp bench off'), events.index('exit serial'))
            self.assertLess(events.index('exit bot'), events.index('exit bench'))
            self.assertEqual(echoear.flash.call_count, int(flash_image))
            echoear.verify.assert_called_once()

    def test_serial_loss_preserves_services_and_reattaches_without_reset(self):
        first, second = Mock(cursor=0, _seq=9), Mock(cursor=0, _seq=0)
        first.lines_since.side_effect = SerialDisconnected('unplugged')
        with tempfile.TemporaryDirectory() as temp, \
             patch('esp_serial.EspSerial', side_effect=[first, second]) as factory, \
             patch('board_port.resolve_port', return_value='pinned-echoear') as resolve, \
             patch.object(demo.time, 'monotonic', return_value=10):
            events = []
            link = demo.DemoSerial('pinned-echoear', Path(temp), events)
            self.assertIsNone(link.poll())
            first.close.assert_called_once()
            self.assertEqual(factory.call_count, 1)
            link.next_retry = 0
            self.assertIs(link.poll(), second)
            self.assertFalse(factory.call_args.kwargs['reset_on_open'])
            self.assertEqual(second._seq, 9)
            second.command.assert_called_once_with('ping', 2)
            second.reset.assert_not_called()
            self.assertEqual([e['kind'] for e in events], ['detached', 'reattached'])
            link.close()

    def test_absent_or_wrong_identity_is_not_opened_and_cleanup_does_not_reconnect(self):
        first = Mock(cursor=0, _seq=9)
        with tempfile.TemporaryDirectory() as temp, \
             patch('esp_serial.EspSerial', return_value=first) as factory, \
             patch('board_port.resolve_port', side_effect=RuntimeError('MAC mismatch')):
            link = demo.DemoSerial('pinned-echoear', Path(temp), [])
            self.assertIsNone(link.poll())
            link.next_retry = 0
            self.assertIsNone(link.poll())
            self.assertIsNone(link.poll(reconnect=False))
            self.assertEqual(factory.call_count, 1)
            first.command.assert_not_called()
            self.assertEqual(demo.cleanup_board(link.board), [])
            link.close()

    def test_cleanup_stops_as_soon_as_serial_becomes_unreachable(self):
        board = Mock()
        board.command.side_effect = SerialDisconnected('unplugged during cleanup')
        self.assertEqual(len(demo.cleanup_board(board)), 1)
        board.command.assert_called_once_with('audio 0', 5)

    def test_session_wait_survives_detach_until_ctrl_c(self):
        events = []
        @contextmanager
        def resource(name, value=None):
            events.append('enter '+name)
            try:
                yield value
            finally:
                events.append('exit '+name)
        first = Mock(cursor=0, _seq=9)
        first.lines_since.side_effect = SerialDisconnected('unplugged')
        echoear = SimpleNamespace(bench=lambda: resource('bench'), verify=Mock(return_value='pinned'),
                                  flash=Mock(), check_firmware=Mock(return_value='elf'))
        modules = {'step9a_echoear': echoear,
                   'step9a_assets': SimpleNamespace(asset_server=lambda: resource('assets')),
                   'step9a_support': SimpleNamespace(host_address=lambda: '192.0.2.1')}
        def stop_after_disconnect(_):
            self.assertNotIn('exit bot', events)
            self.assertNotIn('exit assets', events)
            raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules', modules), \
             patch('esp_serial.EspSerial', return_value=first), \
             patch.object(demo, 'EVIDENCE', Path(temp)), \
             patch.object(demo, 'preflight', return_value={}), \
             patch.object(demo, 'demo_server', side_effect=lambda _: resource('bot', Mock(poll=lambda: None, pid=123))), \
             patch.object(demo, 'prepare_board', return_value={}), \
             patch.object(demo.time, 'sleep', side_effect=stop_after_disconnect), \
             patch.object(demo, 'nic_snapshot', return_value={'interfaces': {}, 'monotonic_s': 1}):
            out = Path(temp)/'run'
            self.assertEqual(demo.main(['--out', str(out)]), 0)
            saved = json.loads((out/'session.json').read_text())
            self.assertTrue(saved['ready'])
            self.assertTrue(saved['interrupted'])
            self.assertIn('unreachable', saved['cleanup_skipped'])
            self.assertEqual([c.args[0] for c in first.command.call_args_list], ['connect', 'audio 1'])
        self.assertEqual(events[-1], 'exit bench')

    def test_bot_startup_failure_stops_only_owned_process(self):
        proc = Mock(pid=123456789, stdout=io.StringIO('startup failure\n'))
        proc.poll.return_value = 1
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(demo.socket, 'socket') as socket_factory, \
             patch.object(demo.subprocess, 'Popen', return_value=proc) as popen, \
             patch.object(demo.os, 'killpg') as killpg:
            socket_factory.return_value.__enter__.return_value.connect_ex.return_value = 1
            with self.assertRaisesRegex(RuntimeError, 'exited during startup'):
                with demo.demo_server(Path(temp)):
                    self.fail('startup failure must not yield')
            killpg.assert_called_once_with(proc.pid, demo.signal.SIGTERM)
            self.assertIn('7861', popen.call_args.args[0])
            self.assertTrue(popen.call_args.kwargs['start_new_session'])

    def test_busy_bot_port_never_starts_or_kills_any_process(self):
        with patch.object(demo.socket, 'socket') as socket_factory, \
             patch.object(demo.subprocess, 'Popen') as popen, \
             patch.object(demo.os, 'killpg') as killpg:
            socket_factory.return_value.__enter__.return_value.connect_ex.return_value = 0
            with self.assertRaisesRegex(RuntimeError, 'occupied'):
                with demo.demo_server(Path('/unused')):
                    self.fail('occupied port must not yield')
            popen.assert_not_called()
            killpg.assert_not_called()


if __name__ == '__main__':
    unittest.main()

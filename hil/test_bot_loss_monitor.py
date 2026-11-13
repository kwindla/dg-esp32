import unittest
from unittest.mock import patch
from step9a_r2_checks import DisplayMonitor

LOSS=b'E (86004) AGENT: 0 Peer not response for binding after 30000\n'
DISCONNECT=b'E (86004) PEER_DEF: Peer disconnected\n'
READY=b'I (12693) dg-app: STATE Ready gen=3 desired=1 t=11371686\n'

class BotLossMonitor(unittest.TestCase):
    def test_only_exact_records_within_declared_window_at_every_split(self):
        for line in (LOSS,DISCONNECT):
            for split in range(len(line)+1):
                m=DisplayMonitor();m.begin_bot_down('restart')
                m.feed(line[:split]);m.check();m.feed(line[split:]);m.check()
                m.feed(READY);r=m.result()
                self.assertTrue(r['passed']);self.assertEqual(len(r['expected_bot_losses']),1)
                self.assertEqual(r['expected_bot_losses'][0]['matched_line'],line.decode().strip())
                self.assertEqual(r['bot_down_windows'][0]['end_reason'],'STATE Ready')

    def test_no_exception_outside_window_or_after_ready_in_same_chunk(self):
        for prior in (b'',READY):
            m=DisplayMonitor()
            if prior:m.begin_bot_down('restart')
            m.feed(prior+LOSS)
            with self.assertRaises(RuntimeError):m.check()

    def test_display_codec_and_spliced_loss_lines_still_fail(self):
        for line in (b'E (10) lcd_panel.io.spi: recycle failed\n',
                     b'E (10) I2C_If: Fail to write to dev 30\n',
                     LOSS.rstrip()+b' extra\n',b'PERF {"x":"'+LOSS,
                     LOSS.replace(b'30000',b'30001'),b'E spi_master: DMA TX underflow\n'):
            m=DisplayMonitor();m.begin_bot_down('restart');m.feed(line)
            with self.assertRaises(RuntimeError):m.check()

    def test_deadline_and_preexisting_partial_line_fail_closed(self):
        m=DisplayMonitor()
        with patch('step9a_r2_checks.time.monotonic',return_value=100):m.begin_bot_down('restart',maximum_s=2)
        with patch('step9a_r2_checks.time.monotonic',return_value=103):
            m.feed(LOSS)
            with self.assertRaises(RuntimeError):m.check()
        m=DisplayMonitor();m.feed(b'E (')
        with self.assertRaises(RuntimeError):m.begin_bot_down('restart')

    def test_unterminated_candidate_cannot_pass_final_result(self):
        m=DisplayMonitor();m.begin_bot_down('restart');m.feed(LOSS[:-1]);m.check()
        self.assertFalse(m.result()['passed']);m.end_bot_down()
        with self.assertRaises(RuntimeError):m.check()

    def test_loss_before_ready_same_chunk_is_allowed_but_later_loss_is_not(self):
        m=DisplayMonitor();m.begin_bot_down('restart');m.feed(LOSS+READY+DISCONNECT)
        self.assertEqual(len(m.result()['expected_bot_losses']),1)
        with self.assertRaises(RuntimeError):m.check()

if __name__=='__main__':unittest.main()

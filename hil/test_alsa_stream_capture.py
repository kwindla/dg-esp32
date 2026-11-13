import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch
from alsa_stream_capture import StreamCapture


class FakeProcess:
    def __init__(self,*,eof=False):
        self.stopped=threading.Event()
        self.remaining=640*20+127
        self.eof=eof
        self.stdout=self

    def read(self,size):
        if self.remaining:
            size=min(size,self.remaining);self.remaining-=size
            time.sleep(.001)
            return b'\1'*size
        if self.eof:return b''
        self.stopped.wait(3)
        return b''

    def poll(self):return -15 if self.stopped.is_set() else None
    def terminate(self):self.stopped.set()
    def kill(self):self.stopped.set()
    def wait(self,timeout=None):return -15
    def close(self):pass


class StreamTests(unittest.TestCase):
    def test_complete_blocks_receipts_and_partial_tail_are_retained_without_padding(self):
        with tempfile.TemporaryDirectory() as root,patch('alsa_stream_capture.subprocess.Popen',return_value=FakeProcess()):
            path=Path(root)/'record.wav'
            stream=StreamCapture(path,'FAKE-UNIT-TEST')
            time.sleep(.04);stream.close()
            self.assertIsNone(stream.error)
            self.assertEqual(len(stream.samples()),6400)
            self.assertEqual(stream.partial_tail_bytes,127)
            self.assertTrue(all(a[1]<=b[1] for a,b in zip(stream.receipts,stream.receipts[1:])))
            self.assertTrue(path.with_suffix('.receipts.json').exists())

    def test_xrun_and_eof_cannot_be_successful_capture(self):
        with tempfile.TemporaryDirectory() as root,patch('alsa_stream_capture.subprocess.Popen',return_value=FakeProcess()):
            path=Path(root)/'record.wav';stream=StreamCapture(path,'FAKE-UNIT-TEST')
            stream.log.write('overrun!!!\n');stream.log.flush();stream.close()
            self.assertIn('discontinuity',stream.error)
        with tempfile.TemporaryDirectory() as root,patch('alsa_stream_capture.subprocess.Popen',return_value=FakeProcess(eof=True)):
            path=Path(root)/'record.wav'
            stream=StreamCapture(path,'FAKE-UNIT-TEST')
            time.sleep(.05);stream.close()
            self.assertIn('EOF',stream.error)

    def test_unexpected_child_failure_at_close_is_retained(self):
        process=FakeProcess()
        with tempfile.TemporaryDirectory() as root,patch('alsa_stream_capture.subprocess.Popen',return_value=process):
            path=Path(root)/'record.wav';stream=StreamCapture(path,'FAKE-UNIT-TEST')
            process.poll=lambda:1
            process.stopped.set()
            stream.close()
            self.assertIn('1',stream.error)

    def test_only_owned_documented_alsa_exit1_is_accepted(self):
        for diagnostic,accepted in [('Aborted by signal Terminated...\n',True),
                ('capture failed\nAborted by signal Terminated...\n',False),
                ('overrun!!!\nAborted by signal Terminated...\n',False),('',False)]:
            process=FakeProcess()
            def terminate():
                process.stopped.set();process.poll=lambda:1
            process.terminate=terminate
            with tempfile.TemporaryDirectory() as root,patch('alsa_stream_capture.subprocess.Popen',return_value=process):
                stream=StreamCapture(Path(root)/'record.wav','FAKE-UNIT-TEST')
                stream.log.write(diagnostic);stream.log.flush();stream.close()
                self.assertEqual(stream.error is None,accepted,diagnostic)


if __name__=='__main__':unittest.main()

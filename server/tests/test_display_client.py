import asyncio
import tempfile
import unittest
from pathlib import Path
from display_client import DisplayClient, DisplayNotReady, DisplayRejected, hil_commands

class Processor:
    def __init__(self):self.sent=[];self.errors=[];self.display=None
    async def send_server_message(self, packet):
        self.sent.append(packet)
        error=self.errors.pop(0) if self.errors else 'none'
        if error=='timeout':return
        d=packet['d']
        self.display.result(dict(epoch=d['epoch'],seq=d['seq'],id=d['id'],accepted=error=='none',error=error,parse_failed=False))
class Log:
    def __init__(self):self.events=[]
    def write(self,event,**kwargs):self.events.append(dict(event=event,**kwargs))
class DisplayTests(unittest.IsolatedAsyncioTestCase):
    def client(self,**kwargs):
        p=Processor();d=DisplayClient(p,Log(),**kwargs);p.display=d
        return p,d
    def ready(self,d,epoch=42):d.ready(dict(v=1,epoch=epoch,next_seq=7,wire_bytes=1023))
    async def test_epoch_and_monotonic_sequence_from_device(self):
        p,d=self.client()
        with self.assertRaises(DisplayNotReady):await d.send('fill',{'color':0},'early')
        self.ready(d)
        await d.send('fill',{'color':0},'a');await d.send('fill',{'color':1},'b')
        self.assertEqual([x['d']['seq'] for x in p.sent],[7,8])
        self.assertTrue(all(x['d']['epoch']==42 for x in p.sent))
        self.ready(d,43);await d.send('fill',{'color':0},'new-session')
        self.assertEqual(p.sent[-1]['d']['epoch'],43)
    async def test_actual_byte_limit_and_metadata_ownership(self):
        p,d=self.client();self.ready(d)
        for op,body in [('load',{'url':'x'*1024}),('fill',{'epoch':1}),('load',{'any_host':True})]:
            with self.assertRaises(ValueError):await d.send(op,body,'invalid')
        self.assertEqual(p.sent,[]);self.assertEqual(d.sequence,6)
    async def test_explicit_rejection_retries_are_bounded(self):
        p,d=self.client();self.ready(d);p.errors=['queue-full','queue-full']
        packet=await d.send('fill',{'color':1},'retry')
        self.assertEqual(packet['d']['seq'],9)
        p.errors=['queue-full']*3
        with self.assertRaises(DisplayRejected):await d.send('fill',{'color':1},'exhausted')
        self.assertEqual(len(p.sent),6)
    async def test_stale_epoch_pauses_and_late_ack_cannot_invalidate_new_ready(self):
        p,d=self.client();self.ready(d);p.errors=['stale-epoch']
        with self.assertRaises(DisplayNotReady):await d.send('fill',{'color':1},'stale')
        self.assertIsNone(d.epoch)
        self.ready(d,43)
        d.result(dict(epoch=42,seq=7,id='stale',accepted=False,error='stale-epoch'))
        await d.send('fill',{'color':1},'fresh');self.assertEqual(d.epoch,43)
    async def test_timeout_replays_identical_packet(self):
        p,d=self.client(ack_timeout=.01);self.ready(d);p.errors=['timeout']
        packet=await d.send('fill',{'color':1},'retry')
        self.assertEqual(len(p.sent),2);self.assertEqual(p.sent[0],p.sent[1])
        self.assertEqual(packet['d']['seq'],7);self.assertIsNone(d.pending)
        await d.send('fill',{'color':2},'next')
        self.assertEqual(p.sent[-1]['d']['seq'],8)
    async def test_timeout_budget_exhaustion_and_disabled_retries(self):
        for budget in (0,2):
            p,d=self.client(ack_timeout=.01,retry_budget=budget);self.ready(d);p.errors=['timeout']*3
            with self.assertRaisesRegex(DisplayRejected,'admission unknown.*exhausted'):
                await d.send('fill',{'color':1},'unknown')
            self.assertEqual(len(p.sent),budget+1);self.assertIsNone(d.pending)
            self.assertTrue(all(packet==p.sent[0] for packet in p.sent))
    async def test_queue_full_and_timeout_share_budget(self):
        p,d=self.client(ack_timeout=.01);self.ready(d);p.errors=['queue-full','timeout','timeout']
        with self.assertRaises(DisplayRejected):await d.send('fill',{'color':1},'mixed')
        self.assertEqual([x['d']['seq'] for x in p.sent],[7,8,8])
    async def test_delayed_original_ack_matches_replay_and_payload_is_snapshotted(self):
        p,d=self.client(ack_timeout=.01);self.ready(d)
        body={'ops':[{'color':1}]}
        async def send(packet):
            p.sent.append(packet)
            if len(p.sent)==1:
                body['ops'][0]['color']=2
            else:
                original=p.sent[0]['d']
                d.result(dict(epoch=original['epoch'],seq=original['seq'],id=original['id'],accepted=True,error='none',duplicate=True))
        p.send_server_message=send
        await d.send('batch',body,'late')
        self.assertEqual(len(p.sent),2);self.assertEqual(p.sent[0],p.sent[1])
        self.assertEqual(p.sent[1]['d']['ops'][0]['color'],1)
    async def test_epoch_change_during_timeout_backoff_stops_replay(self):
        p,d=self.client(ack_timeout=.01);self.ready(d);p.errors=['timeout']
        original=d.log.write
        def log(event,**kw):
            original(event,**kw)
            if event=='display-retry':self.ready(d,43)
        d.log.write=log
        with self.assertRaises(DisplayNotReady):await d.send('fill',{'color':1},'old')
        self.assertEqual(len(p.sent),1);self.assertIsNone(d.pending)
    async def test_evicted_replay_rejection_does_not_change_identity(self):
        p,d=self.client(ack_timeout=.01);self.ready(d);p.errors=['timeout','stale-sequence']
        with self.assertRaisesRegex(DisplayRejected,'stale-sequence'):
            await d.send('show',{'hash':'a'*64,'layer':7},'evicted')
        self.assertEqual(len(p.sent),2);self.assertEqual(p.sent[0],p.sent[1])
    async def test_cancel_does_not_retry_and_releases_pending(self):
        p,d=self.client();self.ready(d);p.errors=['timeout']
        task=asyncio.create_task(d.send('fill',{'color':1},'cancel'))
        await asyncio.sleep(0);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(p.sent),1);self.assertIsNone(d.pending)
    async def test_send_failure_is_not_an_ack_timeout_retry(self):
        p,d=self.client();self.ready(d)
        async def send(packet):
            p.sent.append(packet)
            raise asyncio.TimeoutError('send failed')
        p.send_server_message=send
        with self.assertRaises(asyncio.TimeoutError):await d.send('fill',{'color':1},'send-failed')
        self.assertEqual(len(p.sent),1);self.assertIsNone(d.pending)
    async def test_correlated_parse_failure_is_explicit(self):
        p,d=self.client();self.ready(d);p.errors=['timeout']
        task=asyncio.create_task(d.send('fill',{'color':1},'malformed'))
        await asyncio.sleep(0)
        d.result(dict(epoch=42,seq=7,id='',accepted=False,error='malformed',parse_failed=True))
        with self.assertRaisesRegex(DisplayRejected,'could not parse'):await task
    async def test_ready_change_cancels_pending_admission(self):
        p,d=self.client();self.ready(d);p.errors=['timeout']
        task=asyncio.create_task(d.send('fill',{'color':1},'old'))
        await asyncio.sleep(0);self.ready(d,43)
        with self.assertRaises(DisplayNotReady):await task
        self.assertEqual(d.epoch,43)
    async def test_hil_retries_not_ready_without_consuming_command(self):
        class Sender:
            epoch=42
            log=Log()
            calls=0
            async def send(self,*args):
                self.calls+=1
                if self.calls==1:raise DisplayNotReady('ready raced')
        d=Sender()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'command';p.write_text('{"op":"fill","data":{"color":1},"id":"a"}')
            task=asyncio.create_task(hil_commands(d,p))
            await asyncio.sleep(.15);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(d.calls,2)

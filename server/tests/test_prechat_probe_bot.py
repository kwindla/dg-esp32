import asyncio
from types import SimpleNamespace
from prechat_probe_bot import delay_chat


def test_initial_hold_preserves_bytes_order_and_does_not_block_other_tasks():
    async def run():
        sent = []
        async def send(peer, data):sent.append(data)
        inject = delay_chat(send, .03)
        peer = SimpleNamespace()
        first = asyncio.create_task(inject(peer, b'first'))
        await asyncio.sleep(.001)
        second = asyncio.create_task(inject(peer, b'second'))
        await asyncio.sleep(.001)
        assert not sent and not first.done() and not second.done()
        # An RTP sender coroutine can run while only SCTP is held.
        sent.append(b'independent-rtp')
        await asyncio.gather(first, second)
        assert sent == [b'independent-rtp', b'first', b'second']
        await inject(peer, b'third')
        assert sent[-1] == b'third'
        assert peer._dg_prechat_hold_until < asyncio.get_running_loop().time()
    asyncio.run(run())


def test_new_transport_has_a_separate_hold_deadline():
    async def run():
        sent = []
        async def send(peer, data):sent.append(data)
        inject = delay_chat(send, .01)
        a, b = SimpleNamespace(), SimpleNamespace()
        await inject(a, b'a')
        waiting = asyncio.create_task(inject(b, b'b'))
        await asyncio.sleep(.001)
        assert sent == [b'a'] and not waiting.done()
        await waiting
        assert sent == [b'a', b'b']
    asyncio.run(run())

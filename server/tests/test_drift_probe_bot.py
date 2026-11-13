import asyncio
from types import SimpleNamespace
from drift_probe_bot import startup_burst


def test_startup_batch_preserves_bytes_order_and_subsequent_passthrough():
    async def run():
        sent=[]
        async def send(peer,data):sent.append(data)
        inject=startup_burst(send,3);peer=SimpleNamespace()
        packets=[bytes([128,96,i])+bytes(9) for i in range(5)]
        await inject(peer,packets[0]);await inject(peer,packets[1]);assert sent==[]
        await inject(peer,packets[2]);assert sent==packets[:3]
        await inject(peer,packets[3]);await inject(peer,packets[4]);assert sent==packets
    asyncio.run(run())


def test_rtcp_is_never_held_or_counted_toward_audio_batch():
    async def run():
        sent=[]
        async def send(peer,data):sent.append(data)
        inject=startup_burst(send,2);peer=SimpleNamespace()
        rtcp=bytes([128,200])+bytes(10);audio=bytes([128,96])+bytes(10)
        await inject(peer,rtcp);assert sent==[rtcp]
        await inject(peer,audio);assert sent==[rtcp]
        await inject(peer,audio);assert sent==[rtcp,audio,audio]
    asyncio.run(run())

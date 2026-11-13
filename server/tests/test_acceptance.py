import asyncio
import json
import os
import time
import numpy as np
import pytest
from aiortc_client import Peer, read_wav, correlation, FIXTURES
from settings import GREETING, REPLY

@pytest.fixture
def continuous_server(monkeypatch,request):
    if os.getenv('BOT_MODE','offline-test')!='offline-test':
        pytest.skip('Continuous fixture stimulus is offline-only')
    monkeypatch.setenv('OFFLINE_BENCH_CONTINUOUS_AUDIO','1')
    monkeypatch.setenv('AUDIO_OUT_AUTO_SILENCE','1')
    return request.getfixturevalue('running_server')

@pytest.fixture
def idle_server(monkeypatch,request):
    if os.getenv('BOT_MODE','offline-test')!='offline-test':
        pytest.skip('Credential-free idle receiver check')
    monkeypatch.setenv('AUDIO_OUT_AUTO_SILENCE','0')
    monkeypatch.setenv('OFFLINE_BENCH_CONTINUOUS_AUDIO','0')
    return request.getfixturevalue('running_server')

async def test_suppressed_idle_keeps_complete_greeting_and_reply(idle_server):
    mode,out,port=idle_server
    peer=Peer(f'http://127.0.0.1:{port}',out)
    try:
        await peer.connect()
        await peer.assert_audio(read_wav(FIXTURES/'greeting.wav'))
        await asyncio.sleep(.6)
        start=len(peer.pcm())
        ident=peer.send('client-message',{'t':'play','d':{'fixture':'reply_pineapple.wav'}})
        await peer.wait_message(lambda m:m.get('type')=='server-response' and m.get('id')==ident)
        await peer.assert_audio(read_wav(FIXTURES/'reply_pineapple.wav'),start)
    finally:
        await peer.close()

async def test_continuous_fixture_survives_simultaneous_vad(continuous_server):
    mode,out,port=continuous_server
    peer=Peer(f'http://127.0.0.1:{port}',out)
    try:
        await peer.connect()
        await peer.assert_audio(read_wav(FIXTURES/'greeting.wav'))
        await asyncio.sleep(.4)
        start=len(peer.pcm());began=time.monotonic()
        ident=peer.send('client-message',{'t':'play','d':{
            'fixture':'reply_pineapple.wav','duration_ms':10000}})
        await peer.wait_message(lambda m:m.get('type')=='server-response' and m.get('id')==ident)
        await asyncio.sleep(.7)
        peer.track.play(read_wav(FIXTURES/'prompt_france.wav'))
        await asyncio.sleep(10.5)
        events=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
        assert any(e['pc_id']==peer.pc_id and e['event']=='vad-start' and e['t']>=began for e in events)
        reference=np.resize(read_wav(FIXTURES/'reply_pineapple.wav'),10*16000)
        # Continuous silent packets after cancellation cannot match the entire
        # requested speech waveform, even though RTP traffic remains active.
        assert correlation(peer.pcm()[start:],reference)>=.8, 'Continuous fixture canceled by simultaneous VAD'
    finally:
        await peer.close()

async def live_reference(out, pc_id, number):
    async with asyncio.timeout(25):
        path = out/"tts"/pc_id/f"tts-{number}.wav"
        while not path.exists():
            await asyncio.sleep(.1)
        return read_wav(path)

async def test_webrtc_round_trip(running_server):
    mode,out,port = running_server
    peer = Peer(f"http://127.0.0.1:{port}", out)
    result = {"mode":mode,"passed":False}
    try:
        await peer.connect()
        reference = (read_wav(FIXTURES/"greeting.wav") if mode=="offline-test"
                     else await live_reference(out,peer.pc_id,1))
        result["greeting_correlation"] = await peer.assert_audio(reference)
        # Two identical bot-ready frames can collide in the ESP's reserved
        # control slot before its dispatcher drains the first one.
        ready = [m for _,m in peer.messages if m.get("type")=="bot-ready"]
        result["bot_ready_messages"] = len(ready)
        assert len(ready)==1, "Expected one bot-ready per client-ready"
        if mode == "live-test":
            await peer.wait_message(lambda m:m.get("type")=="bot-output" and
                m.get("data",{}).get("text")==GREETING and m["data"].get("spoken"))
        await asyncio.sleep(.4)
        cursor = len(peer.messages)
        echo = peer.send("client-message", {"t":"echo","d":{"nonce":"step2-echo"}})
        _, reply = await peer.wait_message(lambda m:m.get("type")=="server-response" and m.get("id")==echo,since=cursor)
        assert reply["data"] == {"t":"echo","d":{"nonce":"step2-echo"}}
        rejected = peer.send("client-message", {"t":"play","d":{"fixture":"../settings.py"}})
        await peer.wait_message(lambda m:m.get("type")=="error-response" and m.get("id")==rejected,since=cursor)
        # Start a fresh output segment before the explicit play command.
        start = len(peer.pcm())
        ident = peer.send("client-message",{"t":"play","d":{"fixture":"reply_pineapple.wav"}})
        await peer.wait_message(lambda m:m.get("type")=="server-response" and m.get("id")==ident,since=cursor)
        result["reply_fixture_correlation"] = await peer.assert_audio(read_wav(FIXTURES/"reply_pineapple.wav"),start)
        await asyncio.sleep(.5)
        # Causal prompt segment; prior greeting/reply cannot satisfy these assertions.
        cursor = len(peer.messages)
        start = len(peer.pcm())
        peer.track.play(read_wav(FIXTURES/"prompt_france.wav"))
        await asyncio.sleep(len(read_wav(FIXTURES/"prompt_france.wav"))/16000 + .3)
        speech = [(stamp,m) for stamp,m in peer.messages[cursor:]
                  if m.get("type")=="server-message" and m.get("data",{}).get("t")=="user-audio"
                  and m["data"]["d"]["speaking"]
                  and peer.track.prompt_start <= stamp <= peer.track.prompt_end+.3]
        assert speech, "No VAD speaking=true during the transmitted speech fixture"
        result["speaking_reports"] = len(speech)
        await peer.wait_message(lambda m:m.get("type")=="server-message" and
            m.get("data",{}).get("t")=="user-audio" and not m["data"]["d"]["speaking"],
            since=len(peer.messages),timeout=5)
        if mode=="live-test":
            _, transcript = await peer.wait_message(lambda m:m.get("type")=="user-transcription" and
                "france" in m.get("data",{}).get("text","").lower() and
                m["data"].get("final"),since=cursor)
            result["transcript"] = transcript["data"]["text"]
            reference = await live_reference(out,peer.pc_id,2)
            result["live_reply_correlation"] = await peer.assert_audio(reference,start)
            await peer.wait_message(lambda m:m.get("type")=="bot-output" and
                m.get("data",{}).get("text")==REPLY and m["data"].get("spoken"),since=cursor)
        await peer.close()
        async with asyncio.timeout(5):
            while True:
                events = [json.loads(line) for line in (out/"events.jsonl").read_text().splitlines()]
                if any(e["event"]=="session-end" and e["pc_id"]==peer.pc_id for e in events):
                    break
                await asyncio.sleep(.05)
        kinds = {e["event"] for e in events if e["pc_id"]==peer.pc_id}
        assert {"transport-connected","transport-disconnected","vad-start","vad-stop",
                "rtvi-in","rtvi-out","session-end"} <= kinds
        result["passed"] = True
    finally:
        if peer.pc.connectionState != "closed":
            await peer.close()
        (out/"result.json").write_text(json.dumps(result,indent=2)+"\n")
        print(f"Evidence: {out}")

def test_correlation_rejects_wrong_audio():
    fixture = read_wav(FIXTURES/"greeting.wav")
    assert correlation(np.zeros(len(fixture)+16000),fixture) < .01
    noise = np.random.default_rng(123).normal(0,3000,len(fixture)*2)
    assert correlation(noise,fixture)<.1
    assert correlation(np.r_[np.zeros(7000),fixture,np.zeros(3000)],fixture)>.99

def test_correlation_silent_windows_do_not_amplify_fft_roundoff():
    fixture = read_wav(FIXTURES/"reply_pineapple.wav")
    # Mostly silence with only a short nonmatching burst: FFT roundoff at the
    # all-zero windows used to produce coefficients far above one.
    recording = np.zeros(160000)
    recording[1000:2000] = np.random.default_rng(4).normal(0,4000,1000)
    assert correlation(recording,fixture) < .1

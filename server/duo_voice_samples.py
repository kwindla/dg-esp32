"""Coordinator-only live Flux voice samples; never run as part of host tests.

uv run --project server --frozen --offline python server/duo_voice_samples.py
    --out ~/src/evidence/duo/step-3/voices
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlencode
import wave

from websockets.asyncio.client import connect

from duo_bot import configuration
from settings import load_credentials

LINES = {
    "angel": "A little kindness goes a long way. Let's make a good choice together.",
    "devil": "Go on, take the scenic route. Even your sensible side deserves a little fun.",
}


async def samples(out):
    config = configuration()
    load_credentials(approved_only=True)
    out.mkdir(parents=True, exist_ok=True)
    for character, voice in config["voices"].items():
        query = urlencode(dict(model=voice, encoding="linear16", sample_rate=16000))
        audio = bytearray()
        async with asyncio.timeout(30):
            async with connect("wss://api.deepgram.com/v2/speak?" + query,
                    additional_headers={"Authorization": "Token " + os.environ["DEEPGRAM_API_KEY"]}) as ws:
                await ws.send(json.dumps(dict(type="Speak", text=LINES[character])))
                await ws.send(json.dumps(dict(type="Flush")))
                async for message in ws:
                    if isinstance(message, bytes):
                        audio.extend(message)
                        continue
                    event = json.loads(message)
                    if event.get("type") == "Error":
                        raise RuntimeError(f"Flux synthesis failed for {character}")
                    if event.get("type") == "SpeechMetadata":
                        break
                else:
                    raise RuntimeError(f"Flux closed before completion for {character}")
        if not audio:
            raise RuntimeError(f"No audio for {character}")
        path = out / f"{character}-{voice}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(audio)
        print(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(samples(parser.parse_args().out))

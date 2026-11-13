"""Generate small committed inputs once; running the offline bot needs no key."""
import argparse
import hashlib
import io
import json
import os
import shutil
import wave
import httpx
from settings import FIXTURES, GREETING, REPLY, PROMPT, VOICE, load_credentials

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    texts = {"greeting.wav": GREETING, "reply_pineapple.wav": REPLY, "prompt_france.wav": PROMPT}
    missing = [name for name in texts if args.replace or not (FIXTURES / name).exists()]
    if missing:
        load_credentials()
    FIXTURES.mkdir(exist_ok=True)
    for name in missing:
        response = httpx.post("https://api.deepgram.com/v1/speak",
            params={"model": VOICE, "encoding": "linear16", "container": "wav", "sample_rate": 16000},
            headers={"Authorization": "Token " + os.environ["DEEPGRAM_API_KEY"]},
            json={"text": texts[name]}, timeout=45)
        response.raise_for_status()
        # Rewrite streamed WAV headers with a known actual length.
        with wave.open(io.BytesIO(response.content)) as source:
            assert source.getnchannels() == 1 and source.getsampwidth() == 2 and source.getframerate() == 16000
            pcm = source.readframes(source.getnframes())
        with wave.open(str(FIXTURES / name), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(pcm)
    manifest = {}
    for name, text in texts.items():
        path = FIXTURES / name
        with wave.open(str(path)) as wav:
            duration = wav.getnframes() / wav.getframerate()
        manifest[name] = dict(text=text, model=VOICE, sample_rate=16000, channels=1,
            seconds=duration, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    (FIXTURES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # Preserve the committed acoustic HIL stimulus byte-for-byte.
    hil = FIXTURES.parent.parent / "hil/fixtures"
    hil.mkdir(exist_ok=True)
    shutil.copyfile(FIXTURES / "prompt_france.wav", hil / "prompt_speech.wav")
    print("Fixtures ready:", ", ".join(manifest))

if __name__ == "__main__":
    main()

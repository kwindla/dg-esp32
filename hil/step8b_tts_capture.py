"""Capture two live Deepgram utterances once; reuse the WAV bank thereafter."""
import hashlib
import io
import json
import os
import sys
import wave
from pathlib import Path
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from settings import VOICE,load_credentials
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=False);load_credentials()
texts=['The weather is pleasant today. Take a short walk and enjoy the sunshine.',
       'Your next appointment is at three thirty. Remember to bring your notebook and a pencil.']
manifest={}
for i,text in enumerate(texts,1):
    r=httpx.post('https://api.deepgram.com/v1/speak',params=dict(model=VOICE,encoding='linear16',container='wav',sample_rate=16000),
                 headers={'Authorization':'Token '+os.environ['DEEPGRAM_API_KEY']},json={'text':text},timeout=45)
    if r.status_code!=200:raise RuntimeError(f'Deepgram TTS HTTP {r.status_code}')
    name=f'tts_{i}.wav';path=out/name
    with wave.open(io.BytesIO(r.content)) as source:
        assert (source.getnchannels(),source.getsampwidth(),source.getframerate())==(1,2,16000)
        data=source.readframes(source.getnframes())
    with wave.open(str(path),'wb') as w:w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(data)
    manifest[name]=dict(text=text,voice=VOICE,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),seconds=len(data)/32000)
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print('Captured two live TTS WAVs')

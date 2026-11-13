"""Immutable spoken conversation bank; checks are word-boundary alternatives."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import wave
import requests
from dotenv import dotenv_values
from esp_serial import ROOT
from speech_accuracy import speech_accuracy

BANK = ROOT / 'hil/fixtures/agent_conversation'
USER_VOICE = 'aura-2-orpheus-en'
TURNS = [
    dict(id='france', text='What is the capital of France?', stt=[['capital'], ['france']], answer=[['paris']]),
    dict(id='italy', text='What about Italy?', stt=[['italy']], answer=[['rome']]),
    dict(id='addition', text="What's two plus three?", stt=[['two', '2'], ['plus'], ['three', '3']], answer=[['five', '5']]),
    dict(id='name', text='My name is Sam.', stt=[['name'], ['sam']], answer=[['sam']]),
    dict(id='sky', text='What color is the sky on a clear day?', stt=[['sky'], ['clear']], answer=[['blue']]),
    dict(id='recall', text='What is my name?', stt=[['my'], ['name']], answer=[['sam']]),
]
EXTRA = [dict(id='long', text='Please count from one to twenty slowly.',
              stt=[['count'], ['twenty', '20']], answer=[['one', '1'], ['twenty', '20']]),
         dict(id='pause', text='What is the capital of France?',
              stt=[['capital'], ['france']], answer=[['paris']], pause_s=.8)]


def credentials():
    """Never take keys from shell overrides or the copied server .env."""
    values = dotenv_values(ROOT.parent / 'pipecat/.env')
    if not all(values.get(k) for k in ('DEEPGRAM_API_KEY', 'OPENAI_API_KEY')):
        raise RuntimeError('Live credentials missing from ../pipecat/.env')
    return values


def fixture_transcript(path, key):
    response = requests.post('https://api.deepgram.com/v1/listen',
        params=dict(model='nova-3', smart_format='true'),
        headers={'Authorization': 'Token '+key, 'Content-Type': 'audio/wav'},
        data=path.read_bytes(), timeout=45)
    if response.status_code != 200:
        raise RuntimeError(f'Fixture STT HTTP {response.status_code}')
    return response.json()['results']['channels'][0]['alternatives'][0]['transcript']


def generate(evidence):
    key = credentials()['DEEPGRAM_API_KEY']
    BANK.mkdir(parents=True, exist_ok=True)
    evidence = Path(evidence); evidence.mkdir(parents=True, exist_ok=False)
    manifest = {'voice': USER_VOICE, 'sample_rate': 16000, 'turns': []}
    for turn in TURNS + EXTRA:
        path = BANK / (turn['id'] + '.wav')
        old = json.loads((BANK/'manifest.json').read_text()) if (BANK/'manifest.json').exists() else {'turns': []}
        reusable = next((r for r in old['turns'] if r['id'] == turn['id'] and r['text'] == turn['text']), None)
        attempts = []
        for attempt in range(4):
            candidate = evidence/f"{turn['id']}-{attempt}.wav"
            if attempt == 0 and reusable and path.exists():
                candidate.write_bytes(path.read_bytes())
            else:
                data = bytearray()
                texts = ['What is the capital', 'of France?'] if turn.get('pause_s') else [turn['text']]
                for i, text in enumerate(texts):
                    if i: data.extend(bytes(round(turn['pause_s']*16000)*2))
                    response = requests.post('https://api.deepgram.com/v1/speak',
                        params=dict(model=USER_VOICE, encoding='linear16', container='wav', sample_rate=16000),
                        headers={'Authorization': 'Token ' + key}, json={'text': text}, timeout=45)
                    if response.status_code != 200:
                        raise RuntimeError(f'Prompt TTS HTTP {response.status_code}')
                    with wave.open(io.BytesIO(response.content)) as source:
                        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 16000)
                        data.extend(source.readframes(source.getnframes()))
                with wave.open(str(candidate), 'wb') as target:
                    target.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    target.writeframes(data)
            text = fixture_transcript(candidate, key)
            score = speech_accuracy(turn['text'], text)
            attempts.append(dict(path=str(candidate.resolve()), text=text, word_accuracy=score, wer=1-score))
            (evidence/f"{turn['id']}-validation.json").write_text(json.dumps(attempts, indent=2)+'\n')
            if score == 1:
                path.write_bytes(candidate.read_bytes())
                break
        else:
            raise RuntimeError('Fixture full-text validation failed: '+turn['id'])
        with wave.open(str(path)) as source: duration = source.getnframes()/16000
        manifest['turns'].append(dict(turn, file=path.name,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(), duration_s=duration,
            validation=dict(text=text, word_accuracy=score, wer=0)))
        print('Validated', turn['id'], repr(text), flush=True)
    (BANK / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


def validate():
    manifest = json.loads((BANK / 'manifest.json').read_text())
    assert manifest['voice'] == USER_VOICE and manifest['voice'] != 'aura-2-thalia-en'
    assert len(manifest['turns']) == len(TURNS + EXTRA)
    for row, definition in zip(manifest['turns'], TURNS + EXTRA):
        assert all(row[k] == v for k, v in definition.items()), 'Fixture definition changed'
        assert row['file'] == definition['id'] + '.wav'
        assert hashlib.sha256((BANK / row['file']).read_bytes()).hexdigest() == row['sha256']
        assert speech_accuracy(definition['text'], row['validation']['text']) == 1
        assert row['validation']['wer'] == 0
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--generate', action='store_true')
    p.add_argument('--evidence', type=Path); a = p.parse_args()
    if a.generate:
        if not a.evidence: p.error('--generate requires --evidence for retained validation attempts')
        generate(a.evidence)
    validate()

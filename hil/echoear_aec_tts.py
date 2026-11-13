"""Independent TTS onset: fixed 20 ms PCM power > 1024 LSB squared.

The threshold matches the existing playback-audibility diagnostic, not MIC3/AFE
output. It rejects the retained -69 dBFS digital lead-in. No fitted delays.
Legacy recorder events witness transport input; new tts-written events witness
successful transport writes. Neither is an RTP send or a physical DAC timestamp.
"""
import wave
import numpy as np

TTS_POWER_THRESHOLD = 1024
TTS_FRAME_SAMPLES = 320


def non_silent(samples):
    x = np.asarray(samples, dtype=float)
    n = len(x) // TTS_FRAME_SAMPLES
    return bool(n and np.any(np.mean(x[:n*TTS_FRAME_SAMPLES].reshape(n, -1)**2, axis=1)
                             > TTS_POWER_THRESHOLD))


def retained_tts_events(events, wav):
    """Annotate original chunks, retaining their actual delivery timestamps."""
    with wave.open(str(wav)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Expected 16 kHz mono S16 TTS')
        pcm = np.frombuffer(w.readframes(w.getnframes()), '<i2')
    result = []
    for e in events:
        if e.get('kind') == 'audio' and e.get('stream') == 'tts':
            a, b = e['sample_start'], e['sample_end']
            valid = 0 <= a < b <= len(pcm) and not e.get('drop')
            e = dict(e, non_silent=non_silent(pcm[a:b]) if valid else None,
                     anchor_boundary='legacy synchronous transport-input tap')
        result.append(e)
    return result


def tts_anchor(events, started_ns, next_started_ns=None):
    # The initial chunk reaches the direct input tap just BEFORE BotStarted.
    # Match the closest preceding chunk only, never an earlier reply's tail.
    writes = [e for e in events if e.get('kind') == 'tts-transport-write']
    has_write_witness = bool(writes) or any(e.get('transport_write_witness') is True
                                         for e in events if e.get('kind') == 'session-start')
    audio = writes if has_write_witness else [e for e in events
                        if e.get('kind') == 'audio' and e.get('stream') == 'tts']
    audio = sorted(audio, key=lambda e: e['monotonic_ns'])
    before = [e for e in audio if e['monotonic_ns'] <= started_ns]
    initial = before[-1:] if before and started_ns-before[-1]['monotonic_ns'] <= 20_000_000 else []
    candidates = initial + [e for e in audio if e['monotonic_ns'] > started_ns
                            and (next_started_ns is None or e['monotonic_ns'] < next_started_ns)]
    for e in candidates:
        if e.get('non_silent') is True and not e.get('drop'):
            stamp = e['monotonic_ns']/1e9
            return dict(server_interval=[stamp, stamp], frame_id=e['frame_id'],
                        boundary=e.get('anchor_boundary', 'successful transport write observation'),
                        rule='20ms PCM mean-square > 1024 LSB^2',
                        sample_start=e.get('sample_start'))
    return dict(server_interval=None, reason='No witnessed non-silent TTS chunk')

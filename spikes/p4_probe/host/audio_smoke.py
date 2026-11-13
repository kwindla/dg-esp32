"""P4 audio smoke (report-only): speaker -> Jabra, DAC -> MIC3 reference, Jabra -> MIC1/2, speech STT.

Run with the probe already flashed. Holds no lock itself; the caller wraps it in the bench lock.
"""
import base64, json, sys, threading, time, wave
from pathlib import Path
import numpy as np
import serial
from console_rate import decode_dump_line
from alsa import resolve_jabra, set_volumes, play_wav, record_wav, analyze_wav, band_rise_db

PORT = '/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00'
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
ROOT = Path(__file__).resolve().parents[0]
log = open(OUT / 'serial.log', 'a')
results = {}


class Board:
    def __init__(self):
        s = serial.Serial(); s.port = PORT; s.baudrate = 115200; s.timeout = 0.1
        s.dtr = False; s.rts = False; s.open(); self.s = s; self.buf = b''

    def reset(self):
        self.s.rts = True; time.sleep(0.15); self.s.rts = False
        return self.until(lambda l: l == 'P4_PROBE ready', 15)

    def lines(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.buf += self.s.read(4096)
            while b'\n' in self.buf:
                line, self.buf = self.buf.split(b'\n', 1)
                text = line.decode('utf-8', 'replace').rstrip('\r')
                log.write(f'{time.monotonic():.3f} {text}\n'); log.flush()
                yield text

    def until(self, pred, timeout):
        got = []
        for l in self.lines(timeout):
            got.append(l)
            if pred(l): return got
        raise TimeoutError(f'timeout; last lines: {got[-5:]}')

    def cmd(self, text, timeout=30):
        self.s.write((text + '\n').encode()); self.s.flush()
        return self.until(lambda l: l == 'OK', timeout)


def rec_lines(lines):
    out = {}
    for l in lines:
        if l.startswith('REC ch='):
            kv = dict(p.split('=') for p in l.split()[1:])
            out[kv['ch']] = {k: float(v) for k, v in kv.items() if k != 'ch'}
    return out


def make_tone_wav(path, hz, seconds, dbfs=-12, rate=48000):
    t = np.arange(int(rate * seconds)) / rate
    env = np.minimum(1, np.minimum(t / .02, (seconds - t) / .02))
    x = (10 ** (dbfs / 20) * env * np.sin(2 * np.pi * hz * t) * 32767).astype('<i2')
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(x.tobytes())


def dump_wav(b, path):
    lines = b.cmd('dump', 120)
    frames, bad = [], 0
    for l in lines:
        if l.startswith('DUMP ') and not l.startswith(('DUMP BEGIN', 'DUMP END')):
            try:
                _, raw = decode_dump_line(l.encode())
                a = np.frombuffer(raw, dtype='<i2')
                if a.size % 4: raise ValueError('partial frame')
                frames.append(a)
            except Exception:
                bad += 1; frames.append(np.zeros(160 * 4, dtype='<i2'))
    results['dump_bad_lines'] = bad; results['dump_lines'] = len(frames)
    pcm = np.concatenate(frames).reshape(-1, 4)
    for ch in range(4):
        with wave.open(str(path.with_name(f'{path.stem}-mic{ch + 1}.wav')), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm[:, ch].tobytes())
    return pcm


b = Board()
boot = b.reset()
time.sleep(0.5)
for attempt in range(1):  # Driver-backed console must acknowledge the first command
    b.s.write(b'vol -6\n'); b.s.flush()
    try:
        b.until(lambda l: l == 'OK', 3); break
    except TimeoutError:
        continue
else:
    raise RuntimeError('probe console never acknowledged')
results['boot'] = [l for l in boot if l.startswith(('I2S', 'I2C', 'CODEC', 'P4_PROBE'))]
card, dev = resolve_jabra()
set_volumes(card, OUT / 'mixer.log')

# A. speaker -> Jabra, 2 s tones at -12 dBFS digital (codec volume -6 dB)
results['speaker'] = {}
for hz in (500, 1000, 2000, 3000):
    wav = OUT / f'speaker-{hz}.wav'
    with record_wav(wav, dev, 5, 16000) as r:
        time.sleep(1.0)
        t0 = time.monotonic() - r.ready_at
        b.cmd(f'tone {hz} 2000 -12')
        r.wait()
    base = analyze_wav(wav, hz, 0.2, t0 - 0.1)
    tone = analyze_wav(wav, hz, t0 + 0.3, t0 + 1.8)
    results['speaker'][hz] = dict(rise_db=round(band_rise_db(tone, base), 1), snr_db=round(tone['band_snr_db'], 1),
                                  peak_hz=tone['peak_hz'], clips=tone['clipping_count'])

# B. DAC -> MIC3 electrical reference (and acoustic pickup on MIC1/2)
results['quiet'] = rec_lines(b.cmd('rec 2000 1000'))
results['duplex_1k'] = rec_lines(b.cmd('duplex 1000 2000 -12'))

# C. Jabra -> device mics
tone_wav = OUT / 'jabra-1k.wav'; make_tone_wav(tone_wav, 1000, 3.0)
th = threading.Thread(target=play_wav, args=(tone_wav, dev)); th.start()
time.sleep(0.6)
results['jabra_1k'] = rec_lines(b.cmd('rec 2000 1000'))
th.join()

# D. speech from the Jabra -> device capture -> STT
speech = ROOT.parents[0] / 'dummy'
speech = Path('/home/khkramer/src/dg-esp32/server/fixtures/prompt_france.wav')
with wave.open(str(speech)) as w: secs = w.getnframes() / w.getframerate()
th = threading.Thread(target=play_wav, args=(speech, dev)); th.start()
results['speech_levels'] = rec_lines(b.cmd(f'rec {int((secs + 1.0) * 1000)} 0', 60))
th.join()
pcm = dump_wav(b, OUT / 'speech.wav')
try:
    sys.path.insert(0, '/home/khkramer/src/dg-esp32/hil')
    from agent_conversation_test import transcribe_acoustic
    results['speech_stt'] = {f'mic{c}': transcribe_acoustic(OUT / f'speech-mic{c}.wav') for c in (1, 2)}
except Exception as e:  # report-only
    results['speech_stt_error'] = repr(e)

(OUT / 'result.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))

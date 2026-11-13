"""ALSA routing by USB identity and target-band audio measurements."""
from pathlib import Path
import re
import subprocess
import time
import wave
import json
import math
from scipy.signal import resample_poly
import numpy as np

ROOT = Path(__file__).resolve().parents[1]

def resolve_jabra():
    matches = [p.parent for p in Path("/proc/asound").glob("card*/usbid")
               if p.read_text().strip().lower() == "0b0e:0412"]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Jabra SPEAK 410, found {len(matches)}")
    card = (matches[0] / "id").read_text().strip()
    return card, f"plughw:CARD={card},DEV=0"

def set_volumes(card, log_path, *, pcm_percent=75):
    if pcm_percent not in (75,100):raise ValueError("Qualified PCM settings are 75 or loud-test 100 percent")
    log = []
    for control, percent in (("PCM", str(pcm_percent)+"%"), ("Mic", "80%")):
        # ALSA capture switches use cap, playback switches use unmute.
        for value in (percent, "cap" if control == "Mic" else "unmute"):
            result = subprocess.run(["amixer", "-c", card, "sset", control, value],
                                    capture_output=True, text=True, timeout=5, check=True)
            log.append(result.stdout)
        result = subprocess.run(["amixer", "-c", card, "sget", control],
                                capture_output=True, text=True, timeout=5, check=True)
        log.append(result.stdout)
        if "[off]" in result.stdout:
            raise RuntimeError(f"{control} remains muted")
        # Qualified Step-7 readbacks on this USB identity. ALSA quantizes the
        # requested 75/80 percent to PCM 8/11 and Mic 6/7 respectively.
        expected = {"PCM": ("Playback", 8, 73, -4.0) if pcm_percent==75 else ("Playback", 11, 100, 8.0),
                    "Mic": ("Capture", 6, 86, 3.0)}[control]
        match = re.search(r"Mono: (Playback|Capture) (\d+) \[(\d+)%\] \[([+-]?[\d.]+)dB\] \[on\]", result.stdout)
        observed = (match[1], int(match[2]), int(match[3]), float(match[4])) if match else None
        if observed != expected:
            Path(log_path).write_text("\n".join(log))
            raise RuntimeError(f"{control} differs from Step-7 mixer readback: {observed}")
    Path(log_path).write_text("\n".join(log))
    return "\n".join(log)

def play_wav(path, device):
    """Explicit 48 kHz raw playback, with a wall-duration integrity witness."""
    with wave.open(str(path), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        if w.getsampwidth() != 2 or channels != 1:
            raise ValueError("Playback requires mono signed 16-bit PCM")
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        duration = len(samples) / rate
    if not 0 < duration <= 60:
        raise ValueError("Playback must be at most 60 seconds")
    divisor = math.gcd(rate, 48000)
    converted = np.rint(resample_poly(samples.astype(float), 48000//divisor, rate//divisor))
    if np.any((converted < -32768) | (converted > 32767)):
        raise ValueError("48 kHz conversion clips")
    begin = time.monotonic()
    subprocess.run(["aplay", "-q", "-D", device, "-t", "raw", "-f", "S16_LE",
                    "-r", "48000", "-c", "1"], input=converted.astype("<i2").tobytes(),
                   check=True, timeout=duration + 5)
    elapsed = time.monotonic() - begin
    # Detect the observed 3x-fast device playback. Startup/drain can add latency.
    valid = duration * .9 <= elapsed <= duration + 2
    result = dict(rate=48000, source_rate=rate, samples=len(converted),
                  expected_seconds=duration, elapsed_seconds=elapsed, duration_valid=valid,
                  request_monotonic_s=begin)
    if not valid:
        raise RuntimeError("Jabra playback duration mismatch: " + json.dumps(result))
    return result

class Recording:
    def __init__(self, path, device, seconds, rate):
        if not 1 <= seconds <= 60:
            raise ValueError("Recording must be 1..60 seconds")
        self.path = Path(path)
        self.path.unlink(missing_ok=True)
        self.stderr = self.path.with_suffix(".alsa.log").open("w+")
        self.process = subprocess.Popen(
            ["arecord", "-v", "-D", device, "-f", "S16_LE", "-r", str(rate),
             "-c", "1", "-d", str(seconds), str(path)], stderr=self.stderr)
        self.seconds = seconds
        # arecord describes the configured PCM before starting capture.
        deadline = time.monotonic() + 5
        while "buffer_size" not in self.path.with_suffix(".alsa.log").read_text():
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.close()
                raise RuntimeError("ALSA recording did not become ready")
            time.sleep(0.02)
        self.ready_at = time.monotonic()

    def wait(self):
        try:
            code = self.process.wait(timeout=self.seconds + 5)
            if code:
                raise RuntimeError(f"arecord exited {code}")
            return self.path
        finally:
            self.close()

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

def record_wav(path, device, seconds, rate=16000):
    return Recording(path, device, seconds, rate)

def room_baseline_level(samples):
    """Signed 16-bit PCM room RMS; exactly -50 dBFS is still acceptable."""
    samples = np.asarray(samples, dtype=float)
    if samples.size == 0 or not np.isfinite(samples).all():
        raise ValueError("Room baseline requires finite PCM samples")
    level = float(20*np.log10(max(np.sqrt(np.mean(samples**2))/32768, 1e-6)))
    return dict(baseline_dbfs=level, limit_dbfs=-50,
                noise_contaminated=level > -50)

def record_room_baseline(out, arm):
    """Call only after the operator's acoustic go-ahead, before arm playback."""
    out = Path(out); out.mkdir(parents=True, exist_ok=False)
    card, device = resolve_jabra()
    set_volumes(card, out/'mixer.log')
    with record_wav(out/'room.wav', device, 3, 16000) as recording:
        ready_at = recording.ready_at
        recording.wait()
    with wave.open(str(out/'room.wav'), 'rb') as wav:
        if (wav.getsampwidth(), wav.getnchannels(), wav.getframerate()) != (2, 1, 16000):
            raise ValueError("Room baseline requires mono 16-bit 16 kHz PCM")
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
    result = room_baseline_level(samples[3200:])
    result.update(arm=str(arm), device=device, ready_monotonic_s=ready_at,
                  recording_seconds=3, scored_start_seconds=.2,
                  microphone_readback='6/7, 86%, +3 dB', wav=str(out/'room.wav'))
    (out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    status = 'noise-contaminated' if result['noise_contaminated'] else 'clean'
    print(f"ROOM BASELINE: {arm}: {result['baseline_dbfs']:.2f} dBFS; {status}", flush=True)
    return result

def analyze_wav(path, target_hz, start=0, end=None):
    if not 300 <= target_hz <= 4000:
        raise ValueError("Target must be in 300–4000 Hz band")
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("Expected signed 16-bit PCM")
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        samples = samples.reshape(-1, w.getnchannels())[:, 0]
    if start < 0 or (end is not None and
                     (end <= start or end > len(samples) / rate)):
        raise ValueError("Analysis window outside recording")
    samples = samples[int(start * rate):None if end is None else int(end * rate)]
    if len(samples) < rate // 4:
        raise ValueError("Missing or too short audio segment")
    clips = int(np.count_nonzero((samples == -32768) | (samples == 32767)))
    x = samples.astype(float) / 32768
    rms = float(20 * np.log10(max(np.sqrt(np.mean(x*x)), 1e-12)))
    x -= x.mean()
    window = np.hanning(len(x))
    # One-sided mean-square power, normalized for window energy and FFT length.
    spec = 2 * np.abs(np.fft.rfft(x * window)) ** 2 / (len(x) * np.sum(window**2))
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    band = (freqs >= 300) & (freqs <= 4000)
    tone = band & (np.abs(freqs - target_hz) <= 30)
    signal = spec[tone].sum()
    harmonics = np.zeros(len(freqs), dtype=bool)
    for k in range(2, int(4000 / target_hz) + 1):
        harmonics |= np.abs(freqs - k * target_hz) <= 30
    noise = spec[band & ~tone & ~harmonics].sum()
    peak = float(freqs[np.flatnonzero(band)[np.argmax(spec[band])]])
    return dict(peak_hz=peak, band_snr_db=float(10*np.log10(max(signal, 1e-24) /
                max(noise, 1e-24))), rms_dbfs=rms, clipping_count=clips,
                band_power_dbfs=float(10*np.log10(max(signal, 1e-24))))

def band_rise_db(tone, baseline):
    """Difference of normalized target-band powers, not wideband RMS."""
    return tone["band_power_dbfs"] - baseline["band_power_dbfs"]

def tone_windows(ready_at, command_at, ack_at, tone_ms):
    """Recording-relative baseline and tone windows, anchored to serial receipt."""
    baseline = (.2, command_at - ready_at - .1)
    tone = (ack_at - ready_at + .3, ack_at - ready_at + tone_ms / 1000 - .2)
    if min(baseline[1] - baseline[0], tone[1] - tone[0]) < .5:
        raise ValueError("Baseline and tone windows must each be at least 0.5 s")
    return baseline, tone

"""Offline v3 validation on retained quiet windows; never changes old verdicts."""
import json
from pathlib import Path
import numpy as np
import wave
from scipy.signal import butter, sosfiltfilt
from echoear_aec_bench import load_capture, sha
from echoear_aec_noise_gate import baseline_level, quiet_gate


def speech_band_controls(raw, ref, baseline, policy, speech_path):
    """Fixed transient controls, normalized over the entire 2 s quiet window.

    A stationary -56 dBFS signal is below the approved -55 RMS ceiling and may
    pass. The -56/-58 pair exercises the unchanged -46 dBFS 20 ms peak gate.
    """
    rng = np.random.default_rng(9)
    burst = np.zeros(32000)
    burst[12800:16000] = rng.normal(size=3200)  # fixed 200 ms, not tuned per run
    band = sosfiltfilt(butter(4, [300, 3400], btype='bandpass', fs=16000, output='sos'), burst)
    def at_level(x, level):
        return x*(32768*10**(level/20))/np.sqrt(np.mean(x*x))
    controls = {}
    for level, expected in [(-56, False), (-58, True)]:
        controls[f'speech_band_{level}'] = dict(
            description='300-3400 Hz bandpass, seeded 200 ms noise burst in 2 s; RMS normalized over 2 s',
            seed=9, injected_rms_dbfs=level, expected_passed=expected,
            **quiet_gate(raw+at_level(band, level), baseline, policy, mic3=ref))
    stationary = sosfiltfilt(butter(4, [300, 3400], btype='bandpass', fs=16000, output='sos'), rng.normal(size=32000))
    controls['stationary_band_-56'] = dict(
        description='Stationary band noise: documents that -56 RMS is below the approved ceiling',
        seed=9, injected_rms_dbfs=-56, expected_passed=True,
        **quiet_gate(raw+at_level(stationary, -56), baseline, policy, mic3=ref))
    with wave.open(str(speech_path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000,1,2):
            raise ValueError('Expected mono16k S16 speech control')
        speech = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(float)
    controls['real_speech_-52'] = dict(
        description='Retained real speech, entire file normalized to -52 dBFS RMS',
        source=str(speech_path), sha256=sha(speech_path), injected_rms_dbfs=-52,
        expected_passed=False, **quiet_gate(at_level(speech, -52), baseline, policy))
    return controls


def validate_retained(evidence, policy):
    evidence = Path(evidence)
    hold = evidence/'step-9/campaign-01/qualification-01'
    baseline = hold/'baseline-1/capture.npz'
    raw, _, ref, _ = load_capture(baseline)
    level = baseline_level(raw, policy)
    sources = {str(baseline): sha(baseline)}
    results = [dict(path=str(baseline), window='settled-baseline',
                    **quiet_gate(raw, level, policy, mic3=ref))]
    captures = sorted(hold.glob('qualification-*/capture.npz'))
    if len(captures) != 6:
        raise ValueError('Expected six retained campaign-01 captures')
    for path in captures:
        raw, _, ref, _ = load_capture(path)
        sources[str(path)] = sha(path)
        for name, sl in [('pre', slice(0, 32000)), ('post', slice(-32000, None))]:
            results.append(dict(path=str(path), window=name,
                                **quiet_gate(raw[sl], level, policy, mic3=ref[sl])))
    old = evidence/'step-8b/ab-1'
    path = old/'reply-17-A-a1/capture.npz'
    old_baseline = old/'quiet.npz'
    old_level = baseline_level(load_capture(old_baseline)[0], policy)
    raw, _, ref, _ = load_capture(path)
    sources.update({str(p): sha(p) for p in (path, old_baseline)})
    results.append(dict(path=str(path), window='post',
                        **quiet_gate(raw[-32000:], old_level, policy, mic3=ref[-32000:])))
    # Deterministic broadband positive control added to the retained quiet signal.
    noise = np.random.default_rng(9).normal(size=32000)
    noise *= (32768*10**(-45/20))/np.sqrt(np.mean(noise**2))
    positive = quiet_gate(raw[-32000:]+noise, old_level, policy, mic3=ref[-32000:])
    clean = hold/'qualification-01-echo-pair-4-a1/capture.npz'
    raw, _, ref, _ = load_capture(clean)
    speech = Path(__file__).parent/'fixtures/prompt_speech.wav'
    controls = speech_band_controls(raw[:32000], ref[:32000], level, policy, speech)
    sources[str(speech)] = sha(speech)
    return dict(policy=policy, sources_sha256=sources, windows=results, controls=controls,
                positive_control=dict(description='step-8b post + seeded RMS -45 dBFS white noise',
                                      seed=9, **positive),
                passed=len(results)==14 and all(r['passed'] for r in results) and not positive['passed'] and all(c['passed']==c['expected_passed'] for c in controls.values()))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('declaration', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = validate_retained(args.evidence, json.loads(args.declaration.read_text())['noise_gate'])
    with args.output.open('x') as f:
        json.dump(result, f, indent=2, allow_nan=False); f.write('\n')
    if not result['passed']:
        raise SystemExit('Retained noise validation FAIL')
    print('Retained noise validation PASS: 13 step-9 windows + step-8b post; white/speech controls matched expectations')

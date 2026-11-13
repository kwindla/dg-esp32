"""File-only, lossless insertion of the decision-25 sentence04 pause."""
import argparse
import json
from pathlib import Path
import wave
import numpy as np
from echoear_aec_bench import sha, write

SOURCE_SHA = 'fd73eb7a77238ad4fe7b4b20db8a998ca0fda696342a4d3e57258f3e73eee650'
TEXT = 'Please change the subject to cooking.'


def derive(source):
    if sha(source) != SOURCE_SHA:
        raise ValueError('Expected retained calibrated sentence04')
    with wave.open(str(source)) as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (48000, 1, 2):
            raise ValueError('Expected mono 48 kHz S16LE')
        data = stream.readframes(stream.getnframes())
    x = np.frombuffer(data, '<i2').astype(float)
    # Lowest-energy complete 5 ms bin, entirely >=600 ms from either end.
    starts = range(28800, len(x)-28800-240+1, 240)
    start = min(starts, key=lambda i: (float(np.mean(x[i:i+240]**2)), i))
    # Put the splice at the nearest-to-zero sample in that bin; ties prefer
    # its midpoint. No fade, normalization, resampling or source sample removal.
    index = min(range(start, start+240), key=lambda i: (abs(x[i]), abs(i-(start+120)), i))
    gap = 33600
    return data[:index*2] + bytes(gap*2) + data[index*2:], dict(
        source=str(Path(source).resolve()), source_sha256=SOURCE_SHA,
        expected_text=TEXT, rate=48000, source_samples=len(x), gap_index=index,
        gap_samples=gap, gap_seconds=.7, output_samples=len(x)+gap,
        boundary='low-energy change / the boundary (energy-selected, lexical alignment approximate)',
        envelope=dict(bin_samples=240, selected_start=start, selected_end=start+240,
                      rms_lsb=float(np.sqrt(np.mean(x[start:start+240]**2))),
                      search_margin_samples=28800, algorithm='minimum complete 5 ms RMS bin; nearest-zero sample, then nearest midpoint'),
        calibration='Source speech PCM is byte-identical before and after the inserted zero samples; no gain change.')


def generate(source, output, manifest):
    output, manifest = Path(output), Path(manifest)
    if output.exists() or manifest.exists() or output.is_symlink() or manifest.is_symlink():
        raise FileExistsError('Fresh fixture and manifest required')
    data, value = derive(source)
    with wave.open(str(output), 'wb') as stream:
        stream.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        stream.writeframes(data)
    value.update(output=str(output.resolve()), output_sha256=sha(output),
                 generator=str(Path(__file__).resolve()), generator_sha256=sha(__file__))
    write(manifest, value)
    return value


def verify(manifest):
    saved = json.loads(Path(manifest).read_text())
    data, expected = derive(saved['source'])
    if any(saved.get(k) != v for k, v in expected.items()):
        raise ValueError('Pause derivation manifest changed')
    if sha(saved['output']) != saved['output_sha256'] or sha(__file__) != saved['generator_sha256']:
        raise ValueError('Pause fixture/generator hash changed')
    with wave.open(saved['output']) as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (48000, 1, 2) or stream.readframes(stream.getnframes()) != data:
            raise ValueError('Pause fixture differs from exact derivation')
    return saved


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'manifest'): parser.add_argument(name, type=Path)
    args = parser.parse_args()
    print(json.dumps(generate(args.source, args.output, args.manifest), indent=2))

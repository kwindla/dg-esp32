#!/usr/bin/env python3
"""Clean fixture round trips to distinguish codec distortion from transport loss.

Run with server/.venv/bin/python; board correlation thresholds stay unchanged.
"""
from collections import Counter
import fractions
import json
from pathlib import Path
import sys
import wave

import numpy as np
from av import AudioFrame, AudioResampler

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'server/tests')]
from aiortc.codecs.opus import OpusEncoder, OpusDecoder
from aiortc.jitterbuffer import JitterFrame
from lean_opus import LeanOpusEncoder
from audio_analysis import correlation


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    result = {}
    for mode, cls in [('aiortc', OpusEncoder), ('lean', LeanOpusEncoder)]:
        result[mode] = {}
        for name in ['greeting', 'reply_pineapple']:
            with wave.open(str(ROOT/'server/fixtures'/f'{name}.wav')) as stream:
                reference = np.frombuffer(stream.readframes(stream.getnframes()), '<i2')
            encoder, decoder = cls(), OpusDecoder()
            resampler = AudioResampler(format='s16', layout='mono', rate=16000)
            output, packets = [], []
            # Idle prefix and tail retain codec state over fixture transitions.
            source = np.r_[np.zeros(16000, dtype=np.int16), reference, np.zeros(16000, dtype=np.int16)]
            for pts in range(0, len(source)-319, 320):
                frame = AudioFrame.from_ndarray(source[pts:pts+320].reshape(1, -1), format='s16', layout='mono')
                frame.sample_rate, frame.pts, frame.time_base = 16000, pts, fractions.Fraction(1, 16000)
                payloads, stamp = encoder.encode(frame)
                for packet in payloads:
                    packets.append(packet)
                    for decoded in decoder.decode(JitterFrame(data=packet, timestamp=stamp)):
                        output.extend(f.to_ndarray().ravel() for f in resampler.resample(decoded))
            output.extend(f.to_ndarray().ravel() for f in resampler.resample(None))
            pcm = np.concatenate(output)
            score = float(correlation(pcm.astype(float), reference.astype(float)))
            with wave.open(str(out/f'{mode}-{name}.wav'), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(pcm.astype('<i2').tobytes())
            result[mode][name] = dict(correlation=score, passes_board_correlation=score >= .95,
                toc=dict(Counter(f'{p[0] >> 3}/{bool(p[0] & 4)}' for p in packets)),
                sizes=dict(Counter(map(len, packets))),
                kbps=sum(map(len, packets))*8/(len(packets)*.02)/1000)
    (out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({m: {n: r['correlation'] for n, r in rows.items()} for m, rows in result.items()}, indent=2))


if __name__ == '__main__':
    main()

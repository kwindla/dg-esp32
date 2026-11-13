"""Export AEC raw mic/reference/processed PCM without inventing alignment."""
import base64
import re
import numpy as np


def decode_capture(lines):
    header = next((line.strip() for line in lines if line.startswith('AEC_DUMP BEGIN')), '')
    start = re.fullmatch(
        r'AEC_DUMP BEGIN (schema=2 )?frames=(\d+) channels=(3|5) rate=16000 order=([^ ]+) '
        r'adapter_delay=(\d+) measured_delay=unknown truncated=(\d) ns=0 agc=0', header)
    if not start:
        raise ValueError('Missing AEC capture schema')
    schema, frames, channels, order, adapter, truncated = start.groups()
    frames, channels, adapter, truncated = map(int, (frames, channels, adapter, truncated))
    names = ['mic1', 'mic2', 'mic3', 'mic4', 'processed'] if channels == 5 else ['microphone', 'reference', 'processed']
    expected_order = 'mic1,mic2,mic3,mic4,processed' if channels == 5 else 'mic,ref,processed'
    if bool(schema) != (channels == 5) or order != expected_order:
        raise ValueError('AEC schema/channel order mismatch')
    if not 0 < frames <= 30*16000 or truncated:
        raise ValueError('Empty, oversized or truncated AEC capture')
    data = bytearray()
    ended = False
    expected_timing = None
    for line in lines:
        match = re.fullmatch(r'AEC_PCM (\d+) ([A-Za-z0-9+/=]+)', line.strip())
        if match:
            if int(match[1])*2 != len(data):
                raise ValueError('Missing/reordered/duplicate AEC PCM block')
            payload = base64.b64decode(match[2], validate=True)
            if not payload or len(payload)%2 or len(payload)>384:
                raise ValueError('Invalid PCM extent')
            data.extend(payload)
        match = re.fullmatch(r'AEC_DUMP END frames=(\d+)(?: timing_blocks=(\d+))?', line.strip())
        if match:
            if int(match[1]) != frames:
                raise ValueError('Capture footer mismatch')
            ended = True
            expected_timing = int(match[2]) if match[2] is not None else None
    if not ended or len(data) != frames*channels*2:
        raise ValueError('Incomplete AEC PCM capture')
    timing = []
    for line in lines:
        match = re.fullmatch(r'AEC_TIME start=(\d+) end=(\d+) lower_us=(-?\d+) upper_us=(-?\d+) gap=([01])', line.strip())
        if line.startswith('AEC_TIME') and not match:
            raise ValueError('Malformed capture timing row')
        if match:
            row = list(map(int, match.groups()))
            if row[0] != (timing[-1][1] if timing else 0) or not row[0] < row[1] <= frames or row[2] > row[3]:
                raise ValueError('Invalid capture timing extent')
            timing.append(row)
    if expected_timing is not None and len(timing) != expected_timing:
        raise ValueError('Missing capture timing blocks')
    if timing and timing[-1][1] != frames:
        raise ValueError('Incomplete capture timing')
    return dict(timing=np.asarray(timing, dtype=np.int64).reshape(-1, 5), pcm=np.frombuffer(data, dtype='<i2').copy().reshape(frames, channels),
                adapter_delay_samples=adapter, measured_delay_samples=None,
                channels=names, schema=2 if schema else 1, rate=16000)


def dump_capture(board, path, *, timeout=150):
    cursor = board.cursor
    board.command('dump-aec', timeout)
    lines = [line for _, _, line in board.lines_since(cursor)]
    capture = decode_capture(lines)
    # Keep the artifact loadable with allow_pickle=False. -1 is explicitly
    # unknown, never a measured delay or an alignment chosen for best ERLE.
    np.savez(path, timing=capture['timing'], pcm=capture['pcm'], rate=capture['rate'], channels=capture['channels'],
             schema=capture['schema'],adapter_delay_samples=capture['adapter_delay_samples'], measured_delay_samples=-1)
    return capture

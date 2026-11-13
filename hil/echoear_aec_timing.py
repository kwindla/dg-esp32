"""Interval clock mapping v1. Serial receipt is ONLY a transaction upper bound.

No extrapolation: clocks require bracketing anchors for every scored event.
Each anchor is an independent measured interval, never an ERLE optimum.
"""
import math


def map_interval(value, anchors):
    """anchors: [{'coordinate': x, 'lower_s': l, 'upper_s': u}, ...].

    A monotonic clock/sample coordinate between two anchors maps inside their
    outer bounds. This deliberately does not assume a clock rate or drift bound.
    Dense anchors tighten uncertainty; missing anchors make timing unscorable.
    """
    if not math.isfinite(value):
        raise ValueError('Nonfinite clock coordinate')
    rows = sorted(anchors, key=lambda r: r['coordinate'])
    for i, row in enumerate(rows):
        if not all(math.isfinite(row[k]) for k in ('coordinate', 'lower_s', 'upper_s')):
            raise ValueError('Nonfinite anchor')
        if row['lower_s'] > row['upper_s'] or (i and row['coordinate'] <= rows[i-1]['coordinate']):
            raise ValueError('Invalid/duplicate clock anchor')
    for row in rows:
        if row['coordinate'] == value:
            return [row['lower_s'], row['upper_s']]
    before = [r for r in rows if r['coordinate'] < value]
    after = [r for r in rows if r['coordinate'] > value]
    if not before or not after:
        return None
    lo, hi = before[-1]['lower_s'], after[0]['upper_s']
    if lo > hi:
        raise ValueError('Nonmonotonic clock mapping')
    return [lo, hi]


def transaction_anchor(device_us, sent_ns, received_ns):
    if received_ns < sent_ns:
        raise ValueError('Reversed transaction')
    return dict(coordinate=device_us, lower_s=sent_ns/1e9, upper_s=received_ns/1e9)


def sample_interval(sample, blocks, device_anchors):
    """Each block bounds every contained physical sample, without rate fitting."""
    for start, end, lower, upper, gap in blocks:
        if start <= sample < end:
            if gap:
                return None
            a = map_interval(lower, device_anchors)
            b = map_interval(upper, device_anchors)
            return [a[0], b[1]] if a is not None and b is not None else None
    return None


def mapped_sample_coverage(blocks, anchors):
    """Certain interior coverage only; never bridge an internal gap/unmapped row."""
    mapped = [sample_interval(int(b[0]), blocks, anchors) for b in blocks]
    indices = [i for i, value in enumerate(mapped) if value is not None]
    if not indices:
        return None
    first, last = indices[0], indices[-1]
    if any(mapped[i] is None for i in range(first, last+1)):
        return None
    if any(blocks[i-1][1] != blocks[i][0] for i in range(first+1, last+1)):
        return None
    lo, hi = mapped[first][1], mapped[last][0]
    return [lo, hi] if lo <= hi else None


def require_bracketed_capture(blocks, anchors):
    """Acquisition check: a pre-record anchor must precede EVERY block bound."""
    if not len(blocks) or not anchors:
        raise ValueError('Missing capture timing or anchors')
    first = min(a['coordinate'] for a in anchors)
    last = max(a['coordinate'] for a in anchors)
    if any(b[2] < first for b in blocks):
        raise ValueError('Capture block precedes first anchor')
    if any(b[3] > last for b in blocks):
        raise ValueError('Capture block follows last anchor')
    if any(b[4] for b in blocks):
        raise ValueError('Capture timing gap')
    return dict(passed=True, blocks=len(blocks), first_anchor=first, last_anchor=last)

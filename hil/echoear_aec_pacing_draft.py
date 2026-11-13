"""Prospective server-pacing proposal. NOT imported by acceptance classification.

First sends are selected from the entire retained ring before window selection.
Repeated full RTP identities are retransmissions, including across sequence wraps.
Completeness is an explicit observer witness, never inferred from edge coverage.
"""
import math


def server_pacing(records, pc_id, lo, hi, *, complete, sctp, max_gap_ms=60):
    result = dict(draft_only=True, applied_to_acceptance=False, passed=False)
    if not complete or not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return dict(result, reason='Complete observer ring and ordered finite window required')
    own = [r for r in records if r.get('pc_id') == pc_id]
    if any(not all(type(r.get(k)) is int for k in ('ssrc', 'seq', 'timestamp', 'send_ms'))
           or not 0 <= r['seq'] < 65536 or not 0 <= r['timestamp'] < 2**32 for r in own):
        return dict(result, reason='Malformed RTP identity/time')
    first = {}
    for r in sorted(own, key=lambda r: r['send_ms']):
        first.setdefault((r['pc_id'], r['ssrc'], r['seq'], r['timestamp']), r)
    original = [r for r in first.values() if lo*1000 <= r['send_ms'] <= hi*1000]
    raw = [r for r in own if lo*1000 <= r['send_ms'] <= hi*1000]
    coverage = bool(original and original[0]['send_ms']-lo*1000 <= 60
                    and hi*1000-original[-1]['send_ms'] <= 60)
    contiguous = bool(len(original) >= 2 and len({r['ssrc'] for r in original}) == 1
                      and all((b['seq']-a['seq']) % 65536 == 1
                              and (b['timestamp']-a['timestamp']) % 2**32 == 960
                              for a, b in zip(original, original[1:])))
    gap = max((b['send_ms']-a['send_ms'] for a, b in zip(original, original[1:])), default=None)
    chunks = [e for e in sctp if e.get('pc_id') == pc_id
              and lo <= e['monotonic_ns']/1e9 <= hi]
    sctp_ok = bool(chunks) and not any(e.get('event') == 't3-expired'
                                    or e.get('chunk_type') == 'AbortChunk' for e in chunks)
    result.update(passed=coverage and contiguous and gap is not None and gap <= max_gap_ms and sctp_ok,
                  coverage=coverage, contiguous_originals=contiguous, original_count=len(original),
                  repeated_observations=len(raw)-len(original), max_original_gap_ms=gap, sctp_ok=sctp_ok,
                  complete_observer_ring=True)
    return result

"""Gate the EchoEar WiFi path; retain wired host traffic as observation only."""
from pathlib import Path
import time

INTERFACES = ('enp7s0', 'wlp8s0')
GATED_INTERFACE = 'wlp8s0'


def snapshot():
    counters = {}
    for line in Path('/proc/net/dev').read_text().splitlines():
        if ':' not in line:
            continue
        name, raw = line.split(':', 1)
        if name.strip() in INTERFACES:
            fields = raw.split()
            counters[name.strip()] = dict(rx_bytes=int(fields[0]), tx_bytes=int(fields[8]))
    return dict(monotonic_s=time.monotonic(), interfaces=counters)


def gate(before, after):
    result = dict(before=before, after=after, passed=False, available=False,
                  limit_bytes_per_300s=20000000,
                  policy='echoear-wifi-only-v2', gated_interface=GATED_INTERFACE,
                  observation_only_interfaces=['enp7s0'], byte_deltas={}, observation_errors={},
                  basis='Gate total wlp8s0 RX+TX on the board path; enp7s0 is a separate host-internet subnet and observation only. No board-flow subtraction.')
    try:
        elapsed = after['monotonic_s']-before['monotonic_s']
        if elapsed <= 0:
            raise ValueError('Nonpositive interval')
        result['elapsed_s']=elapsed
        for name in INTERFACES:
            try:
                row={key: after['interfaces'][name][key]-before['interfaces'][name][key]
                     for key in ('rx_bytes', 'tx_bytes')}
                result['byte_deltas'][name]=row
                if any(v < 0 for v in row.values()):
                    raise ValueError('Reset NIC counter')
            except (KeyError, TypeError, ValueError) as exc:
                result['observation_errors'][name]=str(exc)
        if GATED_INTERFACE in result['observation_errors']:
            raise ValueError('Unavailable board-path counters: '+result['observation_errors'][GATED_INTERFACE])
        total = sum(result['byte_deltas'][GATED_INTERFACE].values())
        limit = 20000000*elapsed/300
        result.update(available=True,
                      total_bytes=total, allowed_total_bytes=limit, passed=total <= limit)
    except (KeyError, TypeError, ValueError) as exc:
        result['reason'] = str(exc)
    return result

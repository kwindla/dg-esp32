"""Strict board-pair wire evidence and independent peer-submit timing checks.

A capture covers one session (beginning with WIRE_CONTRACT start=). Pair metadata
must identify the same bot_instance, bot source hashes and profile_manifest;
these are host-side evidence, never firmware credentials. Step 8 fixes measured
bounds; this module deliberately requires explicit stall/p99 parameters.
"""
import argparse
import copy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re


class ContractError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ContractError(message)


def keys(value, expected, label):
    require(type(value) is dict and set(value) == set(expected), f'{label}: keys/types')


def integer(value):
    return type(value) is int


def start_schema(start, intended_aec):
    # Independent of firmware builders AND of the permissive AEC-off policy.
    keys(start, ('body', 'transport'), 'start')
    require(start['transport'] == 'webrtc', 'start transport')
    keys(start['body'], ('audio',), 'start body')
    audio = start['body']['audio']
    keys(audio, ('v', 'aec', 'ref_gain_db', 'guard_ms'), 'audio')
    require(integer(audio['v']) and audio['v'] == 1, 'audio version')
    require(type(audio['aec']) is bool and audio['aec'] is intended_aec, 'audio aec')
    require(integer(audio['ref_gain_db']) and audio['ref_gain_db'] in range(0, 34, 3), 'audio gain')
    require(integer(audio['guard_ms']) and 0 <= audio['guard_ms'] <= 1000, 'audio guard')
    # After this schema the real policy assertion has no independent negative input.
    # Keep it as a positive witness of the unchanged bot policy.
    from demo_policy import capability_aec
    require(capability_aec(start['body']) is intended_aec, 'bot capability policy')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def load_json(text):
    try:
        return json.loads(text, object_pairs_hook=unique_object)
    except (ValueError, TypeError) as error:
        raise ContractError('malformed JSON evidence') from error


@dataclass
class Capture:
    records: dict = field(default_factory=dict)
    intervals: list = field(default_factory=list)
    capability: list = field(default_factory=list)


def parse_capture(lines):
    capture = Capture()
    generations = set()
    for raw in lines:
        if 'WIRE_CONTRACT ' in raw:
            text = raw.split('WIRE_CONTRACT ', 1)[1].strip()
            if text.startswith('msg='):
                match = re.fullmatch(r'msg=(pong|display-ready|display-result) (.+)', text)
                require(match is not None, 'malformed message evidence')
                key, data = 'msg=' + match[1], match[2]
            else:
                match = re.fullmatch(r'(start|client_ready|sdp|opus_enc|dc_open)=(.+)', text)
                require(match is not None, 'unknown/malformed wire record')
                key, data = match.groups()
            require(key == 'start' or 'start' in capture.records, 'wire record before session start')
            require(key not in capture.records, f'duplicate {key}')
            capture.records[key] = load_json(data)
        elif 'UPLINK_INTERVALS ' in raw:
            require('start' in capture.records, 'intervals before session start')
            text = raw.split('UPLINK_INTERVALS ', 1)[1].strip()
            fields = ('gen', 'start_us', 'end_us', 'n', 'attempts', 'ok', 'max_us', 'last_ok_age_us')
            pattern = ' '.join(name + r'=(\d+)' for name in fields) + r' hist=(\[[0-9,]+\])'
            match = re.fullmatch(pattern, text)
            require(match is not None, 'malformed intervals record')
            row = dict(zip(fields, map(int, match.groups()[:-1])))
            row['hist'] = load_json(match.groups()[-1])
            require(row['gen'] not in generations, 'duplicate interval generation')
            generations.add(row['gen'])
            capture.intervals.append(row)
        elif 'DEMO_CAPABILITY ' in raw:
            match = re.search(r'DEMO_CAPABILITY aec=(True|False) user_mute=False interruptions=True\s*$', raw)
            require(match is not None, 'malformed bot capability')
            capture.capability.append(match[1] == 'True')
    return capture


REQUIRED = {
    'product': {'start', 'client_ready', 'sdp', 'opus_enc', 'dc_open', 'msg=pong', 'msg=display-ready'},
    'bench-display': {'start', 'client_ready', 'sdp', 'dc_open', 'msg=display-ready', 'msg=display-result'},
    'headless': {'start', 'client_ready', 'sdp', 'opus_enc', 'dc_open', 'msg=pong', 'msg=display-ready'},
}
SCHEMAS = {
    'msg=pong': {'seq': 'integer'},
    'msg=display-ready': dict(v='integer', epoch='integer', next_seq='integer', wire_bytes='integer', max_ops='integer'),
    'msg=display-result': dict(id='string', epoch='integer', seq='integer', accepted='boolean', error='string',
                               duplicate='boolean', parse_failed='boolean', wait_us='integer'),
}


def sdp_mline(line):
    """Validate the port, retaining every other byte for the exact pair diff.

    Coordinator decision 2026-10-31 (step-8-fix-3): RFC 8839 sections 3 and
    4.3.1 describe ICE's default destination port and the placeholder 9.
    This per-session transport value is not a bot policy input. Accept decimal
    ports 1..65535, including 9; only their value is excluded from pair equality.
    https://www.rfc-editor.org/rfc/rfc8839.html#section-3
    """
    match = re.fullmatch(r'(m=\S+ +)(\S+)( +\S+ +\S+(?: +\S+)*)', line)
    require(match is not None, 'malformed SDP m-line')
    port = match[2]
    digits = port.lstrip('0') or '0'
    require(re.fullmatch(r'[0-9]+', port) is not None and
            len(digits) <= 5 and 1 <= int(digits) <= 65535, 'invalid SDP m-line port: need integer 1..65535')
    return match[1], int(digits), match[3]


def validate_capture(capture, *, profile, intended_aec, target, request_id=None):
    require(profile in REQUIRED, 'unknown evidence profile')
    records = capture.records
    require(len(capture.intervals) <= 1, 'capture must cover one session generation')
    require(REQUIRED[profile] <= records.keys(), 'missing mandatory wire evidence')
    require(set(records) <= {'start', 'client_ready', 'sdp', 'opus_enc', 'dc_open', *SCHEMAS}, 'unknown evidence')
    if profile in ('product', 'headless'):
        require('msg=display-result' not in records, 'display-result forbidden in profile')
        require(bool(capture.intervals), 'missing interval evidence')
    if profile == 'product':
        require(capture.capability == [intended_aec], 'missing/duplicate/wrong DEMO_CAPABILITY')
    elif profile == 'bench-display':
        require(not capture.capability, 'DEMO_CAPABILITY forbidden for bench bot')
    else:
        require(not capture.capability or capture.capability == [intended_aec], 'wrong headless bot capability')
    start_schema(records['start'], intended_aec)
    ready = records['client_ready']
    keys(ready, ('version', 'about'), 'client_ready')
    require(type(ready['version']) is str and ready['version'], 'RTVI version')
    about = ready['about']
    keys(about, ('library', 'library_version', 'platform', 'platform_version', 'platform_details'), 'about')
    require(all(type(about[name]) is str and about[name] for name in about if name != 'platform_details'), 'about types')
    details = about['platform_details']
    keys(details, ('arch', 'compiler', 'target'), 'platform details')
    require(all(type(value) is str and value for value in details.values()), 'platform detail types')
    require(details['target'] == target, 'wrong device target')
    sdp = records['sdp']
    require(type(sdp) is list and sdp and all(type(line) is str and '\n' not in line and '\r' not in line for line in sdp), 'SDP lines')
    media = False
    for line in sdp:
        if line.startswith('m='):
            sdp_mline(line)
            media = True
        else:
            require(media and line.startswith(('a=rtpmap:', 'a=fmtp:', 'a=ptime:', 'a=sctp-port:', 'a=max-message-size:')), 'unsafe SDP attribute')
    require(any(line.startswith('m=audio ') for line in sdp) and any(line.startswith('m=application ') for line in sdp), 'missing SDP media')
    require(any(line.startswith('a=rtpmap:') and 'opus/' in line.lower() for line in sdp), 'missing Opus rtpmap')
    channel = records['dc_open']
    keys(channel, ('label', 'local_ordered', 'api_protocol_default'), 'data channel')
    require(channel == {'label': 'chat', 'local_ordered': True, 'api_protocol_default': ''} and type(channel['local_ordered']) is bool, 'data channel config')
    if 'opus_enc' in records:
        encoder = records['opus_enc']
        numeric = ('sample_rate', 'channels', 'frame_ms', 'bitrate', 'complexity', 'application', 'bits_per_sample')
        keys(encoder, (*numeric, 'fec', 'dtx', 'vbr'), 'encoder')
        require(all(integer(encoder[key]) for key in numeric), 'encoder numeric types')
        require(all(type(encoder[key]) is bool for key in ('fec', 'dtx', 'vbr')), 'encoder boolean types')
        require(encoder['sample_rate'] == 16000 and encoder['channels'] == 1 and encoder['frame_ms'] == 20 and
                encoder['bits_per_sample'] == 16 and not encoder['dtx'], 'encoder media format')
    for key, schema in SCHEMAS.items():
        if key not in records:
            continue
        message = records[key]
        keys(message, ('schema',) if key == 'msg=pong' else ('schema', 'values'), key)
        require(message['schema'] == schema, f'{key} schema changed')
        if key == 'msg=display-ready':
            require(message['values'] == {'v': 1, 'wire_bytes': 1023, 'max_ops': 8} and
                    all(integer(x) for x in message['values'].values()), 'display limits changed')
        elif key == 'msg=display-result':
            require(type(request_id) is str and bool(request_id), 'known display request id required')
            keys(message['values'], ('id',), 'display-result values')
            require(message['values']['id'] == request_id, 'wrong display request id')


@dataclass(frozen=True)
class Bounds:
    stall_us: int
    p99_delta_us: int
    coverage: float = 0.95
    min_intervals: int = 500
    median_us: int = 20000
    median_tolerance_us: int = 2000

    def __post_init__(self):
        require(self.stall_us > 0 and self.p99_delta_us >= 0 and 0 < self.coverage <= 1 and
                self.min_intervals >= 500 and self.median_us == 20000 and 0 <= self.median_tolerance_us <= 2000, 'invalid measured bounds')


def measured(row, bounds):
    hist = row['hist']
    require(len(hist) == 201 and all(integer(x) and x >= 0 for x in hist), 'invalid histogram')
    require(row['gen'] > 0 and row['start_us'] > 0 and row['end_us'] > row['start_us'], 'invalid active window')
    require(row['attempts'] >= row['ok'] >= 1 and row['n'] == row['ok'] - 1, 'inconsistent send counts')
    require(sum(hist) == row['n'], 'histogram count mismatch')
    require(row['n'] >= bounds.min_intervals, 'fewer than 500 required intervals')
    window = row['end_us'] - row['start_us']
    require(row['ok'] >= bounds.coverage * window / 20000, 'insufficient successful-send coverage')
    require(0 <= row['last_ok_age_us'] <= min(window, bounds.stall_us), 'trailing open gap exceeds stall bound')
    require(row['max_us'] <= bounds.stall_us, 'exact maximum exceeds stall bound')
    nonzero = max(i for i, value in enumerate(hist) if value)
    require(nonzero * 1000 <= row['max_us'] and (nonzero == 200 or row['max_us'] < (nonzero + 1) * 1000), 'max/histogram mismatch')
    require(sum(i * 1000 * count for i, count in enumerate(hist)) <= window - row['last_ok_age_us'], 'histogram exceeds active span')
    def quantile(percent, *, lower=False):
        target = math.ceil(row['n'] * percent / 100)
        total = 0
        for i, count in enumerate(hist):
            total += count
            if total >= target:
                return row['max_us'] if i == 200 else min(row['max_us'], i*1000 if lower else (i+1)*1000-1)
        raise ContractError('empty histogram')
    # Median uses the occupied bin's lower edge so 22.0 ms is inclusive.
    # Resolution is 1 ms (a 22.x ms bin cannot resolve sub-ms median detail).
    # p99 remains a conservative upper bound, with exact max kept separately.
    median, p99 = quantile(50, lower=True), quantile(99)
    require(abs(median - bounds.median_us) <= bounds.median_tolerance_us, 'median outside frame cadence')
    return {'median_us': median, 'p99_us': p99, 'window_us': window}


def identity_schema(identity):
    keys(identity, ('bot_instance', 'bot_source_sha', 'policy_source_sha', 'profile_manifest'), 'bot identity')
    require(type(identity['bot_instance']) is str and bool(identity['bot_instance']), 'bot instance required')
    require(all(type(identity[key]) is str and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', identity[key])
                for key in ('bot_source_sha', 'policy_source_sha')), 'bot source hashes required')
    require(type(identity['profile_manifest']) is dict and bool(identity['profile_manifest']), 'bot profile manifest required')


def compare(left, right, *, profile, left_aec, right_aec, bounds, left_identity, right_identity,
            left_target='esp32s3', right_target='esp32p4', request_id=None):
    identity_schema(left_identity)
    identity_schema(right_identity)
    require(left_identity == right_identity, 'paired bot identity differs')
    for capture, aec, target in ((left, left_aec, left_target), (right, right_aec, right_target)):
        validate_capture(capture, profile=profile, intended_aec=aec, target=target, request_id=request_id)
    def normalize(records):
        value = copy.deepcopy(records)
        for field in ('aec', 'ref_gain_db', 'guard_ms'):
            value['start']['body']['audio'][field] = None
        for field in ('target', 'compiler'):
            value['client_ready']['about']['platform_details'][field] = None
        # Validate before normalization; preserve media/proto/formats, spacing,
        # order and every attribute exactly. Never mutate the raw capture.
        for index, line in enumerate(value['sdp']):
            if line.startswith('m='):
                prefix, _, suffix = sdp_mline(line)
                value['sdp'][index] = prefix+'<ice-port>'+suffix
        return {key: value[key] for key in sorted(value)}
    require(bool(left.capability) == bool(right.capability), 'asymmetric optional bot evidence')
    # JSON containers retain insertion order; compare it as well as values/types.
    require(json.dumps(normalize(left.records), separators=(',', ':')) ==
            json.dumps(normalize(right.records), separators=(',', ':')), 'exact wire contract differs')
    require(len(left.intervals) == len(right.intervals), 'asymmetric interval generations')
    results = []
    for a, b in zip(left.intervals, right.intervals):
        ma, mb = measured(a, bounds), measured(b, bounds)
        require(abs(mb['p99_us'] - ma['p99_us']) <= bounds.p99_delta_us, 'paired p99 exceeds bound')
        results.append({'left': ma, 'right': mb})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('left'); parser.add_argument('right')
    parser.add_argument('--profile', choices=REQUIRED, required=True)
    parser.add_argument('--left-aec', type=int, choices=(0, 1), required=True)
    parser.add_argument('--right-aec', type=int, choices=(0, 1), required=True)
    parser.add_argument('--left-target', default='esp32s3'); parser.add_argument('--right-target', default='esp32p4')
    parser.add_argument('--left-identity', required=True); parser.add_argument('--right-identity', required=True)
    parser.add_argument('--stall-us', type=int, required=True); parser.add_argument('--p99-delta-us', type=int, required=True)
    parser.add_argument('--coverage', type=float, default=0.95); parser.add_argument('--request-id')
    args = parser.parse_args()
    result = compare(parse_capture(Path(args.left).read_text().splitlines()), parse_capture(Path(args.right).read_text().splitlines()),
                     profile=args.profile, left_aec=bool(args.left_aec), right_aec=bool(args.right_aec),
                     left_target=args.left_target, right_target=args.right_target, request_id=args.request_id,
                     left_identity=load_json(Path(args.left_identity).read_text()), right_identity=load_json(Path(args.right_identity).read_text()),
                     bounds=Bounds(args.stall_us, args.p99_delta_us, args.coverage))
    print(json.dumps({'result': 'PASS', 'measured': result}))


if __name__ == '__main__':
    main()

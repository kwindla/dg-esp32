import copy
import json
from pathlib import Path
import subprocess

import pytest

from wire_contract import (Bounds, Capture, ContractError, SCHEMAS, compare, measured,
                           parse_capture, start_schema, validate_capture)

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = dict(bot_instance='one-unchanged-bot-process', bot_source_sha='a'*40,
                policy_source_sha='b'*40, profile_manifest={'profile': 'demo', 'config_sha256': 'c'*64})
BOUNDS = Bounds(stall_us=60000, p99_delta_us=5000)


def intervals():
    hist = [0]*201
    hist[20] = 600
    return dict(gen=1, start_us=3000000, end_us=15020000, n=600, attempts=601,
                ok=601, max_us=20000, last_ok_age_us=20000, hist=hist)


def capture(target='esp32s3', aec=False, profile='product'):
    result = Capture(records={
        'start': {'body': {'audio': {'aec': aec, 'guard_ms': 400 if aec else 0,
                                   'ref_gain_db': 15 if aec else 0, 'v': 1}}, 'transport': 'webrtc'},
        'client_ready': {'about': {'library': 'pipecat-client-cxx', 'library_version': '0.0.test',
                                   'platform': 'esp32', 'platform_details': {'arch': 'unknown',
                                   'compiler': 'GCC '+target, 'target': target}, 'platform_version': 'v6.1'},
                         'version': '1.0.0'},
        'sdp': ['m=audio 9 UDP/TLS/RTP/SAVPF 111', 'a=rtpmap:111 opus/48000/2',
                'a=fmtp:111 minptime=10;useinbandfec=1', 'a=ptime:20',
                'm=application 9 UDP/DTLS/SCTP webrtc-datachannel', 'a=sctp-port:5000',
                'a=max-message-size:16384'],
        'opus_enc': dict(sample_rate=16000, channels=1, frame_ms=20, bitrate=24000, complexity=0,
                         application=0, bits_per_sample=16, fec=False, dtx=False, vbr=False),
        'dc_open': dict(label='chat', local_ordered=True, api_protocol_default=''),
        'msg=pong': {'schema': SCHEMAS['msg=pong'].copy()},
        'msg=display-ready': {'schema': SCHEMAS['msg=display-ready'].copy(),
                              'values': dict(v=1, wire_bytes=1023, max_ops=8)},
    }, intervals=[intervals()], capability=[aec] if profile == 'product' else [])
    if profile == 'bench-display':
        result.records['msg=display-result'] = dict(schema=SCHEMAS['msg=display-result'].copy(), values={'id': 'known-dgi-id'})
    return result


def pair(left=None, right=None, profile='product', **options):
    left = capture(profile=profile) if left is None else left
    right = capture('esp32p4', True, profile) if right is None else right
    return compare(left, right, profile=profile, left_aec=False, right_aec=True, bounds=BOUNDS,
                   left_identity=IDENTITY, right_identity=options.pop('right_identity', IDENTITY),
                   request_id='known-dgi-id' if profile == 'bench-display' else None, **options)


def serialize(value):
    result = []
    for key, payload in value.records.items():
        result.append('WIRE_CONTRACT '+key+(' ' if key.startswith('msg=') else '=')+json.dumps(payload, separators=(',', ':')))
    for row in value.intervals:
        result.append('UPLINK_INTERVALS '+' '.join(f'{k}={row[k]}' for k in
                      ('gen', 'start_us', 'end_us', 'n', 'attempts', 'ok', 'max_us', 'last_ok_age_us'))+
                      ' hist='+json.dumps(row['hist'], separators=(',', ':')))
    result += [f'DEMO_CAPABILITY aec={x} user_mute=False interruptions=True' for x in value.capability]
    return result


@pytest.mark.parametrize('profile', ['product', 'bench-display', 'headless'])
def test_profiles_and_allowed_calibration_platform_differences_pass(profile):
    assert pair(profile=profile)
    left, right = capture(profile=profile), capture('esp32p4', True, profile)
    if profile == 'bench-display':
        for value in (left, right):
            value.records.pop('opus_enc'); value.records.pop('msg=pong'); value.intervals.clear()
        assert pair(left, right, profile=profile) == []
    else:
        # Arrival order across independent workers is not part of the wire bytes.
        right.records = dict(reversed(list(right.records.items())))
        assert pair(left, right, profile=profile)


@pytest.mark.parametrize('key,value,reason', [('v', 2, 'audio version'), ('v', True, 'audio version'),
    ('aec', 'false', 'audio aec'), ('ref_gain_db', 34, 'audio gain'), ('ref_gain_db', True, 'audio gain'),
    ('guard_ms', 1001, 'audio guard'), ('guard_ms', -1, 'audio guard'), ('extra', 0, 'audio: keys/types')])
def test_malformed_aec_off_must_fail_independent_schema(key, value, reason):
    left = capture()
    left.records['start']['body']['audio'][key] = value
    with pytest.raises(ContractError, match=reason):
        pair(left)


def test_start_schema_precedes_real_permissive_policy():
    from demo_policy import capability_aec
    start = capture().records['start']
    start['body']['audio']['v'] = 9
    assert capability_aec(start['body']) is False
    with pytest.raises(ContractError, match='audio version'):
        start_schema(start, False)
    for aec in (False, True):
        assert capability_aec(capture(aec=aec).records['start']['body']) is aec


@pytest.mark.parametrize('mutation,reason', [('fmtp', 'exact wire contract differs'),
    ('ptime', 'exact wire contract differs'), ('frame', 'encoder media format'),
    ('extra-ready', 'client_ready: keys/types'), ('max-ops', 'display limits changed'),
    ('schema', 'msg=pong schema changed'), ('protocol', 'data channel config'),
    ('start-key', 'start: keys/types'), ('target', 'wrong device target'), ('ice', 'unsafe SDP attribute')])
def test_exact_contract_negative_fixtures(mutation, reason):
    right = capture('esp32p4', True)
    if mutation == 'fmtp': right.records['sdp'][2] += ';stereo=1'
    elif mutation == 'ptime': right.records['sdp'][3] = 'a=ptime:40'
    elif mutation == 'frame': right.records['opus_enc']['frame_ms'] = 40
    elif mutation == 'extra-ready': right.records['client_ready']['extra'] = 0
    elif mutation == 'max-ops': right.records['msg=display-ready']['values']['max_ops'] = 9
    elif mutation == 'schema': right.records['msg=pong']['schema']['seq'] = 'string'
    elif mutation == 'protocol': right.records['dc_open']['api_protocol_default'] = 'different'
    elif mutation == 'start-key': right.records['start']['extra'] = 0
    elif mutation == 'target': right.records['client_ready']['about']['platform_details']['target'] = 'wrong'
    elif mutation == 'ice': right.records['sdp'].append('a=ice-pwd:must-not-leave-device')
    with pytest.raises(ContractError, match=reason): pair(right=right)


@pytest.mark.parametrize('profile', ['product', 'bench-display', 'headless'])
def test_each_required_record_and_forbidden_profile_evidence(profile):
    from wire_contract import REQUIRED
    for record in REQUIRED[profile]:
        right = capture('esp32p4', True, profile)
        right.records.pop(record)
        with pytest.raises(ContractError, match='missing mandatory wire evidence'): pair(right=right, profile=profile)
    right = capture('esp32p4', True, profile)
    if profile == 'bench-display': right.capability = [True]
    else: right.records['msg=display-result'] = dict(schema=SCHEMAS['msg=display-result'], values={'id': 'known-dgi-id'})
    reason = 'DEMO_CAPABILITY forbidden' if profile == 'bench-display' else 'display-result forbidden'
    with pytest.raises(ContractError, match=reason): pair(right=right, profile=profile)


def test_bot_capability_missing_wrong_duplicate_and_bot_identity():
    for values in ([], [False], [True, True]):
        right = capture('esp32p4', True); right.capability = values
        with pytest.raises(ContractError, match='missing/duplicate/wrong DEMO_CAPABILITY'): pair(right=right)
    identity = copy.deepcopy(IDENTITY); identity['bot_instance'] = 'different-process'
    with pytest.raises(ContractError, match='paired bot identity differs'): pair(right_identity=identity)


def test_asymmetric_optional_evidence_and_known_request_id():
    right = capture('esp32p4', True, 'bench-display'); right.records.pop('opus_enc')
    with pytest.raises(ContractError, match='exact wire contract differs'): pair(right=right, profile='bench-display')
    right = capture('esp32p4', True, 'bench-display')
    right.records['msg=display-result']['values']['id'] = 'wrong-id'
    with pytest.raises(ContractError, match='wrong display request id'): pair(right=right, profile='bench-display')


def cadence(count=600, step_us=20000, age_us=20000):
    row = intervals()
    row.update(n=count, ok=count+1, attempts=count+1, max_us=step_us,
               last_ok_age_us=age_us, end_us=row['start_us']+count*step_us+age_us)
    row['hist'] = [0]*201
    row['hist'][min(200, step_us//1000)] = count
    return row


def test_trailing_gap_alone_with_coverage_ok():
    row = cadence(count=3000, age_us=300000)
    assert row['ok'] >= BOUNDS.coverage * (row['end_us']-row['start_us']) / 20000
    with pytest.raises(ContractError, match='trailing open gap exceeds stall bound'):
        measured(row, BOUNDS)


@pytest.mark.parametrize('step_us', [10000, 40000])
def test_median_frame_cadence_independently(step_us):
    # A looser coverage bound admits the slow fixture; only median must reject.
    bounds = Bounds(stall_us=60000, p99_delta_us=5000, coverage=0.4)
    with pytest.raises(ContractError, match='median outside frame cadence'):
        measured(cadence(step_us=step_us), bounds)


@pytest.mark.parametrize('step_us', [18000, 21999])
def test_median_frame_cadence_edges_pass(step_us):
    assert measured(cadence(step_us=step_us), Bounds(60000, 5000, coverage=0.8))


def test_minimum_500_intervals_independently():
    with pytest.raises(ContractError, match='fewer than 500 required intervals'):
        measured(cadence(count=499), BOUNDS)
    assert measured(cadence(count=500), BOUNDS)


def test_one_generation_per_capture_independently():
    right = capture('esp32p4', True)
    second = intervals(); second['gen'] = 2
    right.intervals.append(second)
    # Distinct generations parse successfully, but must not share one capture.
    right = parse_capture(serialize(right))
    with pytest.raises(ContractError, match='capture must cover one session generation'):
        validate_capture(right, profile='product', intended_aec=True, target='esp32p4')


def test_histogram_span_independently():
    row = intervals(); row['end_us'] -= 20000
    with pytest.raises(ContractError, match='histogram exceeds active span'):
        measured(row, BOUNDS)


@pytest.mark.parametrize('attribute', ['a=ice-pwd:PRIVATE', 'a=ice-ufrag:PRIVATE',
    'a=fingerprint:PRIVATE', 'a=candidate:PRIVATE', 'a=rtpmapx:PRIVATE'])
def test_checker_sdp_allowlist_independently_on_both_boards(attribute):
    left, right = capture(), capture('esp32p4', True)
    left.records['sdp'].append(attribute); right.records['sdp'].append(attribute)
    with pytest.raises(ContractError, match='unsafe SDP attribute'):
        pair(left, right)


@pytest.mark.parametrize('case,reason', [
    ('coverage', 'insufficient successful-send coverage'),
    ('burst', 'exact maximum exceeds stall bound'),
    ('overflow', 'exact maximum exceeds stall bound'),
    ('max', 'max/histogram mismatch'), ('hist-count', 'histogram count mismatch'),
    ('send-count', 'inconsistent send counts'), ('hist-shape', 'invalid histogram'),
    ('window', 'invalid active window')])
def test_measurement_checks_fail_for_intended_reason(case, reason):
    row = intervals()
    if case == 'coverage': row['end_us'] += 2000000
    elif case == 'burst':
        # Normal mean: one 600ms stall followed by a compensating burst.
        row['hist'][20] -= 30; row['hist'][0] = 29; row['hist'][200] = 1; row['max_us'] = 600000
    elif case == 'overflow':
        row['hist'][20] -= 50; row['hist'][0] = 49; row['hist'][200] = 1; row['max_us'] = 1000000
    elif case == 'max': row['max_us'] = 19999
    elif case == 'hist-count': row['hist'][20] -= 1
    elif case == 'send-count': row['attempts'] = row['ok']-1
    elif case == 'hist-shape': row['hist'].pop()
    elif case == 'window': row['gen'] = 0
    with pytest.raises(ContractError, match=reason): measured(row, BOUNDS)


def test_jitter_passes_and_p99_pair_bound_is_explicit():
    left, right = capture(), capture('esp32p4', True)
    row = right.intervals[0]
    row['hist'][20] = 580; row['hist'][18] = 10; row['hist'][22] = 10; row['max_us'] = 22999
    assert pair(left, right)
    row['hist'][20] = 560; row['hist'][18] = 0; row['hist'][22] = 0; row['hist'][0] = 20; row['hist'][40] = 20; row['max_us'] = 40999
    with pytest.raises(ContractError, match='p99'): pair(left, right)


def test_parser_roundtrip_duplicate_missing_truncated_and_json_keys():
    original = capture()
    lines = serialize(original)
    assert parse_capture(lines) == original
    for bad, reason in ((lines + lines[:1], 'duplicate start'),
                (lines + [lines[-2]], 'duplicate interval generation'),
                (lines[1:] + lines[:1], 'wire record before session start'),
                ([lines[0].replace('"v":1', '"v":1,"v":1')]+lines[1:], 'malformed JSON evidence'),
                (lines[:1]+['WIRE_CONTRACT client_ready={'], 'malformed JSON evidence')):
        with pytest.raises(ContractError, match=reason): parse_capture(bad)
    with pytest.raises(ContractError, match='missing mandatory wire evidence'): pair(parse_capture(lines[:1]))
    right = capture('esp32p4', True); right.intervals.clear()
    with pytest.raises(ContractError, match='missing interval evidence'): pair(right=right)


def test_bot_does_not_consume_platform_details():
    # The bot is the Python under server/. The web simulator's bundled client library names the field it sends.
    result = subprocess.run(['grep', '-rn', '--include=*.py', '--exclude-dir=.venv', 'platform_details', 'server/'],
                            cwd=ROOT, capture_output=True)
    assert result.returncode == 1 and not result.stdout


@pytest.mark.parametrize('options', [dict(min_intervals=499), dict(median_us=40000),
                                   dict(median_tolerance_us=3000), dict(coverage=0)])
def test_parameters_cannot_weaken_fixed_contract_requirements(options):
    with pytest.raises(ContractError, match='invalid measured bounds'):
        Bounds(stall_us=60000, p99_delta_us=5000, **options)


def test_intentional_stop_freezes_before_two_second_teardown():
    # Mirrors the C++ stop-order regression: capture is disabled at freeze;
    # the later disconnect/producer wait must not extend end_us or trailing age.
    row = intervals()
    stopped_at = row['end_us']
    disconnected_at = stopped_at + 2000000
    assert measured(row, BOUNDS)
    late = copy.deepcopy(row)
    late['end_us'] = disconnected_at
    late['last_ok_age_us'] += disconnected_at - stopped_at
    with pytest.raises(ContractError, match='insufficient successful-send coverage'):
        measured(late, BOUNDS)


@pytest.mark.parametrize('port', ['1', '9', '50712', '65535'])
def test_ice_mline_port_is_validated_but_excluded_from_pair_diff(port):
    left, right = capture(), capture('esp32p4', True)
    left.records['sdp'][4] = 'm=application 50712 UDP/DTLS/SCTP webrtc-datachannel'
    right.records['sdp'][0] = 'm=audio '+port+' UDP/TLS/RTP/SAVPF 111'
    right.records['sdp'][4] = 'm=application '+port+' UDP/DTLS/SCTP webrtc-datachannel'
    before = copy.deepcopy((left, right))
    assert pair(left, right)
    assert (left, right) == before, 'pair normalization must preserve raw captured ports'


@pytest.mark.parametrize('port', ['bogus', '9.0', '-1', '+9', '0', '65536', '999999999999999999999999', '９'])
@pytest.mark.parametrize('index', [0, 4])
def test_every_mline_rejects_noninteger_or_out_of_range_port(port, index):
    value = capture()
    parts = value.records['sdp'][index].split(); parts[1] = port
    value.records['sdp'][index] = ' '.join(parts)
    with pytest.raises(ContractError, match='invalid SDP m-line port: need integer 1..65535'):
        validate_capture(value, profile='product', intended_aec=False, target='esp32s3')


@pytest.mark.parametrize('line', ['m=audio', 'm=audio 9', 'm=audio 9 UDP/TLS/RTP/SAVPF'])
def test_mline_requires_media_protocol_and_format(line):
    value = capture(); value.records['sdp'][0] = line
    with pytest.raises(ContractError, match='malformed SDP m-line'):
        validate_capture(value, profile='product', intended_aec=False, target='esp32s3')


def test_port_validation_covers_additional_media_too():
    value = capture(); value.records['sdp'].append('m=video nope UDP/TLS/RTP/SAVPF 96')
    with pytest.raises(ContractError, match='invalid SDP m-line port'):
        validate_capture(value, profile='product', intended_aec=False, target='esp32s3')


@pytest.mark.parametrize('index,line', [
    (0, 'm=audio 50712 RTP/AVP 111'),
    (0, 'm=audio 50712 UDP/TLS/RTP/SAVPF 112'),
    (0, 'm=audio 50712 UDP/TLS/RTP/SAVPF 111 112'),
    (4, 'm=application 50712 UDP/DTLS/SCTP changed-format'),
    (4, 'm=application 50712 TCP/DTLS/SCTP webrtc-datachannel'),
    (5, 'a=sctp-port:5001'), (6, 'a=max-message-size:262144'),
])
def test_ice_port_exception_does_not_weaken_protocol_formats_or_attributes(index, line):
    right = capture('esp32p4', True); right.records['sdp'][index] = line
    with pytest.raises(ContractError, match='exact wire contract differs'):
        pair(right=right)


def test_port_exception_preserves_extra_media_type_and_format_order():
    left, right = capture(), capture('esp32p4', True)
    left.records['sdp'].append('m=video 40000 UDP/TLS/RTP/SAVPF 96')
    right.records['sdp'].append('m=text 50000 UDP/TLS/RTP/SAVPF 96')
    with pytest.raises(ContractError, match='exact wire contract differs'): pair(left, right)
    left, right = capture(), capture('esp32p4', True)
    left.records['sdp'][0] = 'm=audio 40000 UDP/TLS/RTP/SAVPF 111 112'
    right.records['sdp'][0] = 'm=audio 50000 UDP/TLS/RTP/SAVPF 112 111'
    with pytest.raises(ContractError, match='exact wire contract differs'): pair(left, right)

"""Device-reported public profile of the step-10 requal-n1 images (decision 134; Astra r1 #4/r2 #1-#2, Fable F2/B1/N2).

Only what the running firmware prints on serial is used; the generated build configuration is never read.

Every line that carries one of the report keywords is a *candidate* record and must parse in full (anchored
regex on the text from the keyword to the end of the line, the keyword exactly once). A malformed, ambiguous or
truncated candidate fails the gate; it is never filtered out. Firmware sources of each record:

- `startup verified ... MIC1_GAIN=` echoear_codec.cpp:175 (every boot);
- `AUDIO_CAPABILITY aec= ref_gain_db= guard_ms=` app.cpp:375, on every ActionKind::Start. Console `connect` and the
  demo autostart (demo_boot.cpp:80 request_connect -> EventKind::Connect -> StateMachine::advance ActionKind::Start)
  take the same branch; Pipeline::capability() returns the zero Capability unless CONFIG_DG_ECHOEAR_AEC
  (pipeline.cpp:425-431), so an AEC-off image reports aec=0 ref_gain_db=0 guard_ms=0 on both images;
- `MIC_GAIN_PATH analog_db= reg= digital_percent=` echoear_codec.cpp:230 (mic-gain-status);
- `MIC_PROTECTION schema= knee= ceiling= limited_samples= adc_clips=` pipeline.cpp:609;
- `OUTPUT_VOLUME relative_db= max_relative_db=0 default_relative_db=-6` pipeline.cpp:624 (every volume change);
- `PERF {json}` performance.cpp (stats / disconnect); the `aec` block exists only under CONFIG_DG_ECHOEAR_AEC.

PERF record roles:
- pre-session: neither `pc_id` nor `output_volume_db` (stats before the first peer). Identity checks only, and only
  valid before the first AUDIO_CAPABILITY (Start) since the last boot; afterwards such a row is a missing field.
- session (including the disconnect PERF of console.cpp:167): both keys. Its `output_volume_db` must equal the
  device's current reported volume state: the last OUTPUT_VOLUME report since the last boot, or the firmware
  default -6 dB (pipeline.cpp:151) when none was reported since boot. This validates intentional setup (0) and
  cleanup (-6) volumes as the device reported them; it does not decide the measurement volume.
- a PERF with only one of the two keys is malformed.
The measurement volume is a separate, scoped gate on the scored records embedded in each stage result
(measurement_volume_gate)."""
import json
import re

COMMON = dict(aec=False, analog_db=30, mic1_gain_register=0x1a, digital_percent=125,
              limiter=dict(schema=1, knee=8192, ceiling=16384), volume_db=0, default_volume_db=-6,
              setup_cleanup_volumes=(0, -6))
PROFILES = {'normal': dict(COMMON, autostart=False), 'demo': dict(COMMON, autostart=True)}

RECORDS = {
    'codec': ('startup verified ', re.compile(r'startup verified REG0D=0x([0-9a-f]{2}) REG44=0x([0-9a-f]{2}) MIC1_GAIN=0x([0-9a-f]{2}) EXPECTED_REG44=0x([0-9a-f]{2}) no_dac_ref=([01])\s*')),
    'capability': ('AUDIO_CAPABILITY', re.compile(r'AUDIO_CAPABILITY aec=(\d+) ref_gain_db=(\d+) guard_ms=(\d+)\s*')),
    'gain': ('MIC_GAIN_PATH', re.compile(r'MIC_GAIN_PATH analog_db=(\d+) reg=0x([0-9a-f]{2}) digital_percent=(\d+)\s*')),
    'limiter': ('MIC_PROTECTION', re.compile(r'MIC_PROTECTION schema=(\d+) knee=(\d+) ceiling=(\d+) limited_samples=(\d+) adc_clips=(\d+)\s*')),
    'volume': ('OUTPUT_VOLUME', re.compile(r'OUTPUT_VOLUME relative_db=(-?\d+) max_relative_db=0 default_relative_db=-6\s*')),
}


def _unique_object(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)): raise ValueError('duplicate PERF key')
    return dict(pairs)


def parse(lines):
    """Ordered (kind, line_no, parsed) events, plus every malformed/ambiguous candidate."""
    events, malformed = [], []
    for n, raw in enumerate(lines):
        line = raw.rstrip('\r\n')
        if 'elf_sha256=' in line: events.append(('boot', n, None))
        if line.startswith('PERF'):
            try:
                if not line.startswith('PERF {'): raise ValueError('not a PERF object')
                row = json.loads(line[5:], object_pairs_hook=_unique_object)
                if not isinstance(row, dict): raise ValueError('not an object')
                events.append(('perf', n, row))
            except ValueError as exc: malformed.append(dict(line=n, kind='perf', error=str(exc), text=line[:120]))
            continue
        for kind, (keyword, pattern) in RECORDS.items():
            if keyword.strip() not in line: continue
            at = line.find(keyword.strip())
            m = pattern.fullmatch(line[at:])
            if line.count(keyword.strip()) != 1 or not m:
                malformed.append(dict(line=n, kind=kind, error='ambiguous' if line.count(keyword.strip()) != 1 else 'malformed', text=line[:160]))
            else: events.append((kind, n, m.groups()))
    return events, malformed


def _perf_role(row):
    pc, vol = 'pc_id' in row, 'output_volume_db' in row
    return 'pre-session' if not pc and not vol else 'session' if pc and vol else 'malformed'


def _demo_boot_task(row):
    return any(isinstance(t, dict) and t.get('name') == 'demo-boot' for t in row.get('tasks') or [])


def boot_gate(lines, profile):
    """Facts available right after a boot: MIC1 analog register, no AEC MIC3 path, and (normal) no autostart worker."""
    p = PROFILES[profile]
    events, malformed = parse(lines)
    codec = [g for k, _, g in events if k == 'codec']
    checks = dict(
        well_formed=not [m for m in malformed if m['kind'] == 'codec'],
        mic1_gain=bool(codec) and all(int(g[2], 16) == p['mic1_gain_register'] for g in codec),
        aec_mic3_absent=not any('MIC3_GAIN=' in line for line in lines),
        autostart=(True if p['autostart'] else not any('demo-boot' in line for line in lines)))
    return dict(passed=all(checks.values()), checks=checks, profile=profile, expected=p,
                basis='Boot serial only; generated configuration never read')


def session_gate(lines, profile, *, require_cleanup=False):
    """Whole-hold image identity plus device-reported volume-state consistency (not the measurement volume)."""
    p = PROFILES[profile]
    events, malformed = parse(lines)
    def values(kind): return [g for k, _, g in events if k == kind]
    capability, gains, limiter, volumes = values('capability'), values('gain'), values('limiter'), values('volume')
    perf = [row for k, _, row in events if k == 'perf']
    roles = [_perf_role(r) for r in perf]
    want_limiter = (p['limiter']['schema'], p['limiter']['knee'], p['limiter']['ceiling'])
    # Replay the device-reported volume state in log order.
    state, consistent, session_rows, started, misplaced = p['default_volume_db'], True, 0, False, 0
    for kind, _, item in events:
        if kind == 'boot': state, started = p['default_volume_db'], False
        elif kind == 'capability': started = True
        elif kind == 'volume': state = int(item[0])
        elif kind == 'perf' and _perf_role(item) == 'pre-session' and started: misplaced += 1
        elif kind == 'perf' and _perf_role(item) == 'session':
            session_rows += 1
            v = item['output_volume_db']
            if type(v) is not int or v != state: consistent = False
    reported = [int(g[0]) for g in volumes]
    demo_lines = [line for line in lines if 'demo-boot' in line]
    checks = dict(
        well_formed=not malformed,
        boot_seen=any(k == 'boot' for k, _, _ in events),   # Fable r3 N6: volume state starts at a real boot
        boot=boot_gate(lines, profile)['passed'],
        aec_capability=bool(capability) and all(g == ('0', '0', '0') for g in capability),
        perf_aec_absent=bool(perf) and all('aec' not in r or (isinstance(r['aec'], dict) and r['aec'].get('enabled') is False) for r in perf),
        perf_roles=bool(perf) and 'malformed' not in roles and session_rows > 0 and not misplaced,
        gain_path=bool(gains) and all((int(g[0]), int(g[1], 16), int(g[2])) == (p['analog_db'], p['mic1_gain_register'], p['digital_percent']) for g in gains),
        limiter=bool(limiter) and all(tuple(map(int, g[:3])) == want_limiter for g in limiter),
        volume_state_consistent=consistent,
        volume_reports_intentional=all(v in p['setup_cleanup_volumes'] for v in reported),
        autostart=(bool(demo_lines) and any(_demo_boot_task(r) for r in perf)) if p['autostart']
                  else (not demo_lines and not any(_demo_boot_task(r) for r in perf)))
    if require_cleanup:
        # item2: a measurement setup to 0 dB must be followed by the harness's intentional -6 dB cleanup.
        checks['cleanup_restored'] = 0 not in reported or (bool(reported) and reported[-1] == p['default_volume_db'])
    return dict(passed=all(checks.values()), checks=checks, profile=profile, malformed=malformed[:20],
                counts=dict(capability=len(capability), gain_path=len(gains), limiter=len(limiter), perf=len(perf),
                            session_perf=session_rows, pre_session_perf=roles.count('pre-session'), misplaced_pre_session=misplaced, volume_reports=reported),
                basis='Device-reported serial only; every candidate record must parse; generated configuration never read')


def session_gate_text(texts, profile, **kw):
    return session_gate([line for text in texts for line in text.splitlines()], profile, **kw)


# ---- scoped measurement volume --------------------------------------------------------------------------------

E2E_ROWS = ('before_perf', 'after_perf')
WINDOW_ROWS = ('before', 'after')


def measurement_volume_gate(result, stage, expected_db=0):
    """Volume of the scored measurement records embedded in the stage result (Astra r2 #1, Fable B1).

    e2e stages (strict_voice_cat, quiet_offline, quiet_live): every scored run's before_perf/after_perf and
    reply_window_transport.before/after. Echo stress / analog probe: every point's before/after. a6_hot_stt is
    uplink-only and declares no playback-volume expectation in the plan (it records the values it ran at).
    Every applicable record must carry an integer output_volume_db equal to expected_db; missing fails."""
    rows = []
    if stage == 'a6_hot_stt':
        observed = [p.get(k, {}).get('output_volume_db') for p in result.get('points', []) for k in WINDOW_ROWS if isinstance(p.get(k), dict)]
        return dict(passed=True, applicable=False, observed=observed, basis='uplink-only; no playback volume expectation (plan order[2])')
    if stage in ('a6_echo_stress', 'a6_analog_probe'):
        for p in result.get('points', []):
            rows += [(p.get('point', p.get('index')), k, p.get(k)) for k in WINDOW_ROWS]
    else:
        for r in result.get('runs', []):
            if r.get('scored') is not True: continue
            rows += [(r.get('run'), k, r.get(k)) for k in E2E_ROWS]
            window = r.get('reply_window_transport') or {}
            rows += [(r.get('run'), 'reply_window_transport.'+k, window.get(k)) for k in WINDOW_ROWS]
    bad = [dict(record=i, key=k, value=(row or {}).get('output_volume_db') if isinstance(row, dict) else None)
           for i, k, row in rows if not (isinstance(row, dict) and type(row.get('output_volume_db')) is int and row['output_volume_db'] == expected_db)]
    return dict(passed=bool(rows) and not bad, applicable=True, expected_db=expected_db, records=len(rows), failures=bad[:20],
                basis='Scored measurement records only; setup/cleanup volumes are validated by session_gate')

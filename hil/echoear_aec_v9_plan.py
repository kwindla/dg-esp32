"""v9 acceptance plan and frozen logical schedule (docs/echoear-aec-acceptance-v9.md sections 2-8).

File-only: deriving, freezing and validating a plan never opens hardware. The plan is the frozen
v8.7 Nova plan (sha-bound) with the aec-v87 candidate identity, the v9 logical cells, the two
planned parts' block schedules, the smoke schedule and the v9 policy section.
"""
import copy
import json
import random
import subprocess
from pathlib import Path

from echoear_aec_bench import ROOT, NORMAL, NORMAL_HASH, sha, verify_bundle
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45 as NORMAL, HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore
from echoear_aec_gate_registry import V9_PLAN_VALUES

EVIDENCE = Path.home()/'src/evidence/echoear/aec-demo/step-9'
SOURCE = EVIDENCE/'freeze-v8.7-nova-v86/plan.json'
SOURCE_SHA = 'bef556271a1acdf8af0cffa75a5738479b65fa9955f50d4fef6334a738747a75'
TEXT_BANK = ROOT/'hil/plans/echoear-aec-accept-v8.5.json'       # T01-T10, baker = T08
WITNESS_SOURCE_ID = 'qualification-01-near-03'                  # sentence-01-calibrated near-only cell
CANDIDATE = dict(
    name='aec-v87', firmware_commit='58f567ea8d4aeab9b51290cc5548dc083aafc9f8',
    parent_commit='bce5d931eead4e83c9d2658c6c765a490733a61f',
    admission_commit='a503d2b28195e06ffbb9934143fc5491c03731f6',
    elf_sha256='cbabb650bcc218b1e993b99c5cc055f5bf8ae41e9a56568d3d60071ba205f056',
    image=EVIDENCE/'candidate-58f567e-ref15', decisions=(70, 71, 72))
DESTINATION = EVIDENCE/'freeze-v9.12-aec-v87'
OUTPUTS = dict(acceptance=EVIDENCE/'v9.12-acceptance-01', smoke=EVIDENCE/'v9.12-smoke-01')
DECISIONS = ROOT.parent/'dg-esp32/proj-2026-10-15-0645/codex-jobs/step-9-user-decisions-2026-10-19-b.md'
SEED = 'v9-aec-v87-2026-10-20'
GROUPS = (('barge-firm-stop', 'stop', 'firm', 'qualification-01-stop', 'qualification-01-stop-spare'),
          ('barge-firm-sentence', 'sentence', 'firm', 'qualification-01-sentence', 'qualification-01-sentence-spare'),
          ('barge-normal-sentence', 'sentence', 'normal', 'qualification-normal-sentence',
           'qualification-normal-sentence-spare'))
VOLUMES = (-6, 0)
# Planning windows per attempt kind (section 8): typical (median) and stress (p90), seconds, plus the
# 2 s O(1) scoring allowance. Block admission uses stress; the capacity check uses typical.
WINDOWS_S = dict(echo=(43, 57), **{'barge-firm-stop': (54, 69), 'barge-firm-sentence': (60, 65),
                 'barge-normal-sentence': (60, 65), 'near-witness': (42, 43)})
SCORING_S = 2
V9 = dict(
    rule='aec-acceptance-v9.12',
    parts=dict(planned=2, recovery=1, planned_total_s=3600, recovery_total_s=None, restore_reserve_s=360,
               ceiling_s=9000, setup_s=330, minimum_start_remaining_s=210,
               # v9.8: the last part's total is also capped so its connected media cannot exceed the 400000-packet
               # per-stream RTP evidence cap at 50 packets/s (8000 s): 7800 s total = 390000 packets max (97.5 %),
               # since the connected interval lies inside the part's wall time.
               last_part_total_max_s=7800, rtp_packets_per_s=50,
               ceiling_rule='Part wall time including restoration; gaps between parts do not count. No fourth part. '
                            'v9.5: capacity is the remaining campaign wall time under the ceiling over the entered parts '
                            'still allowed; every part (incl. recovery) may use the 60 min per-part cap while the ceiling '
                            'holds; the 6 min restore reserve stays per part. v9.6 (decision 81): the last allowed '
                            'part (recovery) is not capped at 60 min; its total is the ceiling minus campaign time used.'),
    smoke=dict(ceiling_s=1800, part_total_s=1200, restore_reserve_s=360, credit=False, repetition=False),
    blocks=dict(max_acquired_attempts=5, witness_delay_samples=1024, witness_tolerance_samples=20,
                witness_spread_samples=20, witness_acquisitions=3, witness_retake_rule='v9.10: an invalid witness (noise-contaminated, or ambiguous/out-of-tolerance alignment) is retaken up to 2 more times per position (open and close) while it fits the part budget and admission; STOP-class findings stop at once; witnesses are outside the invalid-attempt cap; the closing retake keeps the block open (cells pending); spread disagreement between two individually valid witnesses is not retaken', witness_matcher='v9.8: fixture match (unconstrained, else the opposite-polarity half-pitch alias with its own checks), envelope and relative (AFE vs raw fixture interval, 1 % ambiguity margin) estimates all within +-20 inside 1004-1044; otherwise ambiguous (PAUSE-PART)', relative_ambiguity_margin=.01, relative_competitor_span_samples=64, admission='stress windows; every attempt and the closing witness'),
    peer_loss=dict(status='PENDING USER CONFIRMATION (v9.12)', witness='bot client-disconnected for the connected pc_id bound to its '
                   'evaluation-start and session-owner, or the anchored dg-app SESSION FAIL gen=<n> record after that generation\'s STATE Connected',
                   exit_associated=['drop_reasons.send_cutoff', 'send_budget_cutoffs'],
                   exit_interval='the failed acquisition\'s newest drained before/before-boundary observation -> the lost epoch\'s newest observation',
                   strict='every other drop reason and admission_overflow (admission.control_loss), send_errors, malformed/reset counters, all device leaves'),
    caps=dict(attempts_per_logical_cell=2, invalid_attempts_per_campaign=8, consecutive_invalid_pause=3),
    windows_s=WINDOWS_S, scoring_s=SCORING_S,
    admission=dict(drain_allowance_s=4.0, smoke_drain_allowance_s=4.0, readiness_boundary_max_s=2.0,
                   acceptance_rule='v9.9: fixed declared drain allowance 4.0 s for smoke and acceptance, from the pooled '
                   'v9 smoke boundary distribution (v9.1-v9.8 smokes: 90 ordinary attempt boundaries, no-backlog <= '
                   '0.76 s, 3 backlog boundaries 1.40 / 1.52 / 1.54 s; worst 1.54 s, 2.6x headroom), within the 5 s '
                   'burst receipt timeout. Smoke readiness requires every ordinary boundary <= 2.0 s (half the '
                   'allowance) and none unbounded; else readiness review, no launch. The burst latency is MONITOR.',
                   reply_shortfall='recorder-vs-device shortfall is always unknown.unclassified (PAUSE-PART); host '
                                   'evidence never resolves delivery',
                   counters='data_admission received/dispatched/deferred, '
                   'drop_reasons (complete aec-v87 schema incl. admission_overflow)'),
    burst=dict(messages=40, max_bursts=10, stop_when_covered=True, receipt_timeout_s=5.0, path='datachannel-direct',
               message='RTVI server-message {"t":"burst","d":{"b":k,"id":i}} written synchronously to the aiortc '
                       'data channel (SmallWebRTCConnection.send_app_message, no await between sends)',
               quiescence=dict(spacing_s=0.25, confirmation_s=1.0, barrier='host receipt of every application record',
                               rule='all declared ids applied (whole-part app log, duplicates kept), then a sustained '
                                    'quiescent pair (>= 0.25 s apart, receipt-time deadlines) of fresh, acknowledged stats '
                                    'requests issued after the host received every application receipt (causal '
                                    'barrier; no device/host clock comparison); no pipeline counting'),
               latency='MONITOR: last send -> sustained quiescence, must be within receipt_timeout_s',
               witness='SERVER <json> app_log line per message (quiet 1 does not suppress app_log)'),
    transport_window=dict(pre_s=0.2, flush_tail_s=2.0, downlink_send_gap_s=0.12, next_reply_deadline_s=1.0,
                          rule='barge: invalid by host-timestamped evidence (downlink send gap, SCTP T3 without clean '
                               'same-peer recovery) overlapping [Jabra request - pre_s, next-reply first write + 1 s]; an '
                               'attempt-interval device playout fault (registry transport.downlink_barge) cannot be '
                               'localized with the acquired evidence and is unresolved (PAUSE-PART; reviewed option iv); '
                               'RF attribution inside the window is unmeasured (no per-second counters): a whole-capture '
                               'total alone has no effect and is never a pass (registry rf.capture_window); echo '
                               'transport is MONITOR'),
    rf=dict(part_pre_gate_s=30, cell_pre_gate_s=5, limit_bytes_per_300s=20000000, scaling='elapsed time'),
    floors=dict(record=['raw MIC1 full band', 'raw MIC1 100 Hz HPF', 'pre-guard AFE full band', 'AFE 100 Hz HPF'],
                windows=['part baseline', 'cell pre quiet (first 2 s)', 'cell post quiet (last 2 s)'], gate=False),
    outcome=('PASS', 'FAIL', 'INCONCLUSIVE'),
    seed=SEED)


def _text_bank():
    template = json.loads(TEXT_BANK.read_text())
    texts = [next(c['text'] for c in template['cases'] if c['id'] == f'qualification-01-echo-{i:02d}') for i in range(1, 11)]
    if 'baker' not in texts[7] or len(set(texts)) != 10: raise ValueError('Text bank changed (T08 must be baker)')
    return texts


def logical_cells(source, template):
    """Echo: 10 texts x 2 generations x 2 volumes. Barge: 3 groups x ranks 1-12 (ten v8.7 cases then the
    first two v8.7 spares, unchanged fixtures and offsets). Ids are stable logical ids."""
    texts = _text_bank()
    echo_template = next(c for c in source['cases'] if c['id'] == 'qualification-01-echo-01')
    cells = []
    for volume in VOLUMES:
        for t, text in enumerate(texts, 1):
            for generation in (1, 2):
                c = copy.deepcopy(echo_template)
                c.update(id=f'v9-echo-{"m6" if volume == -6 else "0"}-t{t:02d}-g{generation}', text=text, volume_db=volume,
                         capture_group=None, aec_state='warm', report_only=False)
                cells.append(dict(logical_id=c['id'], kind='echo', volume_db=volume, text_id=f'T{t:02d}',
                                  generation=generation, cell=c))
    for kind, campaign_kind, voice, prefix, spare_prefix in GROUPS:
        ranked = [next(c for c in source['cases'] if c['id'] == f'{prefix}-{i:02d}') for i in range(1, 11)]
        ranked += [next(c for c in source['spares'] if c['id'] == f'{spare_prefix}-{i:02d}') for i in (1, 2)]
        for rank, original in enumerate(ranked, 1):
            if (original['kind'], original.get('voice_class', 'firm'), original['volume_db']) != (campaign_kind, voice, -6):
                raise ValueError('Barge source cell changed: '+original['id'])
            c = copy.deepcopy(original)
            c.update(id=f'v9-{campaign_kind}-{voice}-r{rank:02d}', capture_group=None, report_only=False,
                     source_cell=original['id'])
            cells.append(dict(logical_id=c['id'], kind=kind, volume_db=-6, rank=rank, cell=c))
    return cells


def witness_template(template):
    w = copy.deepcopy(next(c for c in template['controls'] if c['id'] == WITNESS_SOURCE_ID))
    w.update(session_id='qualification-01', capture_group=None, report_only=True, real_bot_reply=False)
    return w


def witness_cell(template_cell, part, index):
    c = copy.deepcopy(template_cell)
    c['id'] = f'v9-witness-p{part}-{index:02d}'
    return dict(logical_id=c['id'], kind='near-witness', volume_db=-6, cell=c)


def _echo_order(cells, generation, first_volume, rng):
    """20 slots alternating volume; texts per volume in a seeded permutation."""
    by_volume = {v: [c for c in cells if c['kind'] == 'echo' and c['generation'] == generation and c['volume_db'] == v]
                 for v in VOLUMES}
    for v in VOLUMES: rng.shuffle(by_volume[v])
    second = 0 if first_volume == -6 else -6
    return [by_volume[first_volume if i % 2 == 0 else second][i//2] for i in range(20)]


def planned_parts(cells):
    """Two planned parts, 8 blocks each in E B E B E B E B order: echo blocks 5, barge blocks 5,5,4,4.
    Part 1 holds echo generation 1 and barge ranks 1-6, part 2 generation 2 and ranks 7-12. Volumes
    alternate inside echo blocks (part 1 starts at -6 dB, part 2 at 0 dB); barge ranks round-robin
    over the three groups, lower ranks first so membership resolves early."""
    rng = random.Random(SEED)
    parts = []
    for part, (generation, first_volume, ranks) in enumerate(((1, -6, range(1, 7)), (2, 0, range(7, 13))), 1):
        echo = [c['logical_id'] for c in _echo_order(cells, generation, first_volume, rng)]
        barge = [c['logical_id'] for r in ranks for g, *_ in GROUPS for c in cells if c['kind'] == g and c['rank'] == r]
        sizes = iter((5, 5, 4, 4))
        blocks, e, b = [], 0, 0
        for i in range(8):
            if i % 2 == 0:
                blocks.append(dict(kind='echo', attempts=echo[e:e+5])); e += 5
            else:
                n = next(sizes); blocks.append(dict(kind='barge', attempts=barge[b:b+n])); b += n
        for k, block in enumerate(blocks): block['id'] = f'p{part}-b{k+1}'
        parts.append(dict(part=part, blocks=blocks))
    return parts


def smoke_parts(cells):
    """Section 8 smoke: part 1 witness, echo -6, echo 0, firm Stop, witness; part 2 witness, firm sentence,
    normal sentence, witness, then the decision-71 burst check."""
    ids = {c['logical_id'] for c in cells}
    p1 = ['v9-echo-m6-t01-g1', 'v9-echo-0-t08-g1', 'v9-stop-firm-r01']
    p2 = ['v9-sentence-firm-r01', 'v9-sentence-normal-r01']
    if not set(p1+p2) <= ids: raise ValueError('Smoke cells missing')
    return [dict(part=1, blocks=[dict(id='s1-b1', kind='mixed', attempts=p1)], burst=False),
            dict(part=2, blocks=[dict(id='s2-b1', kind='mixed', attempts=p2)], burst=True)]


def plan_from():
    if sha(SOURCE) != SOURCE_SHA: raise ValueError('Frozen v8.7 plan changed')
    source = json.loads(SOURCE.read_text())
    template = json.loads(TEXT_BANK.read_text())
    p = copy.deepcopy(source)
    for k in ('inputs_sha256', 'runtime_sha256', 'prepared_from_commit', 'installed_packages_sha256', 'user_go',
              'candidate_change', 'freeze', 'output'):
        p.pop(k, None)
    image = Path(CANDIDATE['image']); manifest, review = image/'manifest.json', image/'review-build.json'
    m, r = json.loads(manifest.read_text()), json.loads(review.read_text())
    old = p['candidate']
    if ((m['firmware_commit'], m['elf_sha256'], m['boot_ref_gain_db'], m['config']) !=
            (CANDIDATE['firmware_commit'], CANDIDATE['elf_sha256'], 15, old['resolved_config']) or
            (r['firmware_commit'], r['elf_sha256'], r['manifest_sha256'], r['parent_commit']) !=
            (CANDIDATE['firmware_commit'], CANDIDATE['elf_sha256'], sha(manifest), CANDIDATE['parent_commit'])):
        raise ValueError('aec-v87 candidate bundle identity/config mismatch')
    previous = {k: old[k] for k in ('firmware_commit', 'elf_sha256', 'image_bundle_sha256')}
    old.update(firmware_commit=CANDIDATE['firmware_commit'], elf_sha256=CANDIDATE['elf_sha256'],
               image_bundle_sha256=sha(manifest), review_artifact_manifest=str(manifest),
               review_build_record=dict(path=str(review), sha256=sha(review)),
               playback_config=dict(old['playback_config'], elf_sha256=CANDIDATE['elf_sha256']))
    p.update(image=str(image), elf_sha256=CANDIDATE['elf_sha256'], schema='aec-acceptance-v9.12-1',
             declaration_id='step-9-acceptance-v9.12-aec-v87', status='PREPARED; reviews and USER-GO required',
             qualification=False, campaign_authorized=False, resume_from=None,
             candidate_change=dict(name=CANDIDATE['name'], decisions=list(CANDIDATE['decisions']), previous=previous,
                                   parent_commit=CANDIDATE['parent_commit'], admission_commit=CANDIDATE['admission_commit'],
                                   source_plan_sha256=SOURCE_SHA,
                                   header='review-build: sdkconfig.h identical to aec-v86 (1173 keys)'),
             outputs={k: str(v) for k, v in OUTPUTS.items()}, user_go=str(DESTINATION/'USER-GO'))
    p['timing']['timer_lateness_max_s'] = V9_PLAN_VALUES['timing.timer_lateness_max_s']
    for name in p['nova_live']['event_schema']['files']:
        p['nova_live']['event_schema']['files'][name] = sha(ROOT/name)
    p['preflight_contract']['real_reply'].update(prompt_file_sha256=sha(ROOT/'server/demo_bot.py'))
    p['budget'] = dict(active_s=V9['parts']['planned_total_s']-V9['parts']['restore_reserve_s'],
                       restore_reserve_s=V9['parts']['restore_reserve_s'], total_s=V9['parts']['planned_total_s'],
                       minimum_cell_remaining_s=V9['parts']['minimum_start_remaining_s'])
    p['time_estimate'].update(hard_wallclock_bound_s=V9['parts']['ceiling_s'],
                              budget_note='v9: per-part budgets from the v9 section; 150 min campaign ceiling.')
    cells = logical_cells(source, template)
    p.update(cases=[], spares=[], controls=[])
    if V9['admission']['drain_allowance_s'] > V9['burst']['receipt_timeout_s']:
        raise ValueError('drain allowance must stay within the burst receipt timeout')
    cap = p['time_estimate']['v85_budget']['rtp_evidence_max_packets']
    if V9['parts']['last_part_total_max_s']*V9['parts']['rtp_packets_per_s'] > .975*cap:
        raise ValueError('last-part cap would let connected media exceed the RTP evidence cap')
    p['v9'] = copy.deepcopy(V9)
    p['v9'].update(cells=cells, witness=witness_template(template), schedule=planned_parts(cells),
                   smoke_schedule=smoke_parts(cells))
    return p


def runtime_files():
    import echoear_aec_nova_live as live
    return sorted(set(live.runtime_files()) | {'docs/echoear-aec-acceptance-v9.md'})


def input_files(p):
    files = {str(SOURCE): SOURCE_SHA, str(TEXT_BANK): sha(TEXT_BANK)}
    for name in ('manifest.json', 'review-build.json', 'dg-esp32.elf', 'dg-esp32.bin'):
        files[str(Path(p['image'])/name)] = sha(Path(p['image'])/name)
    for c in [x['cell'] for x in p['v9']['cells']] + [p['v9']['witness']]:
        if c.get('fixture'): files[c['fixture']] = sha(c['fixture'])
    return files


BINDINGS = ('inputs_sha256', 'runtime_sha256', 'prepared_from_commit', 'installed_packages_sha256')


def validate(p, *, analyzer_amendment=None):
    """Frozen contract, inputs, runtime, profile, packages and both images. analyzer_amendment: runtime
    hashes approved by a ledger provenance amendment (analyzer files only, section 9)."""
    from freeze_aec_nova_live import installed_package_hashes
    from demo_policy import profile_manifest
    if {k: v for k, v in p.items() if k not in BINDINGS} != json.loads(json.dumps(plan_from())):   # as frozen on disk
        raise ValueError('v9 frozen contract changed')
    if p['inputs_sha256'] != input_files(p): raise ValueError('Retained inputs changed')
    runtime = {f: sha(ROOT/f) for f in runtime_files()}
    expected = dict(p['runtime_sha256'], **(analyzer_amendment or {}))
    if runtime != expected: raise ValueError('Live runtime changed: '+', '.join(sorted(
        k for k in set(runtime) | set(expected) if runtime.get(k) != expected.get(k)))[:400])
    if profile_manifest('dev') != p['profile']: raise ValueError('Profile changed')
    if p['installed_packages_sha256'] != {k: v['tree_sha256'] for k, v in installed_package_hashes().items()}:
        raise ValueError('Installed packages changed')
    verify_bundle(Path(p['image']), CANDIDATE['elf_sha256'], 15)
    verify_bundle(NORMAL, NORMAL_HASH)
    return True


def bind(p):
    from freeze_aec_nova_live import installed_package_hashes
    p.update(prepared_from_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
             inputs_sha256=input_files(p), runtime_sha256={f: sha(ROOT/f) for f in runtime_files()},
             installed_packages_sha256={k: v['tree_sha256'] for k, v in installed_package_hashes().items()})
    return p


def cell_index(p):
    out = {c['logical_id']: c for c in p['v9']['cells']}
    return out

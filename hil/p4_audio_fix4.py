#!/usr/bin/env python3
"""Step-12b fix 4: product voice evidence and measured live stack provenance.

Additive adapter: frozen p4_audio.py / board_smoke.py are not changed. Live
voice commands remain coordinator-only; costs and replay are offline only.
"""
import argparse
import json
from pathlib import Path

import p4_audio as audio
import board_smoke as smoke
from make_bundle import digest
from wire_contract import require

STACK_TASKS = dict(tx_stack='audio-tx', decode_stack='audio-playback',
                   owner_stack='peer-loop', rx_stack='peer-rx')
STACK_FLOOR = 4096
RX_REFERENCE_FLOOR = 1744
STACK_POLICY = 'decision-2026-11-01-p4-task-stack-floors'
STACK_POLICY_ADDENDUM = STACK_POLICY+'-addendum'
RX_REFERENCE = Path.home()/'src/evidence/echoear/p4/step-12b/fix-1/attempt1-voice-echoear-missing-display-ready'


def product_capture(lines, capability, *, manifest, board, calibration=False, bounds=None, coverage=.95, coverage_decision=None):
    """One authoritative profile for both legs and for the offline pair."""
    return smoke.check_capture(lines, capability, manifest=manifest, board=board,
        headless=False, bounds=bounds, calibration=calibration, coverage=coverage, coverage_decision=coverage_decision)


class VoiceIO(audio.VoiceIO):
    def prepare_manual(self, board):
        # --headless means skip UI/assets/touch. S3 still needs the physical
        # runtime initialized before connect to advertise display-ready.
        super().prepare_manual(board)
        self.display_initialized = False
        at = board.cursor; board.command('stats')
        row = json.loads(board.wait_for(r'^PERF \{', 10, since=at)[5:])
        if not row.get('display', {}).get('lcd_initialized'):
            if self.args.board == 'echoear':
                board.command('disp init', 60)
                at = board.cursor; board.command('stats')
                row = json.loads(board.wait_for(r'^PERF \{', 10, since=at)[5:])
        require(row.get('display', {}).get('lcd_initialized') is True, self.args.board+' display runtime did not initialize')
        self.display_initialized = True

    def snapshot(self, board):
        row = super().snapshot(board)
        if not hasattr(self, 'stack_samples'): self.stack_samples = []
        self.stack_samples.append(row)
        return row

    def finish(self, board, label, *, hold=False):
        lines, capability = super().finish(board, label, hold=hold)
        if label == 'failed': return lines, capability
        # Validate product evidence before the inherited restoration hold can
        # mark acquisition successful. Frozen execute's headless check remains
        # an additional check; it cannot weaken this product check.
        capture, _, _ = product_capture(lines, capability, manifest=self.manifest, board=self.args.board, calibration=self.args.calibration,
            bounds=None if self.args.calibration else smoke.bounds_for(self.args.bounds, 'normal'),
            coverage=getattr(self.args, 'coverage', .95), coverage_decision=getattr(self.args, 'coverage_decision', None))
        self.product_evidence_verified = True
        generation = capture.intervals[0]['gen']
        samples = [row for row in getattr(self, 'stack_samples', []) if row.get('generation') == generation]
        require(len(samples) >= 2, 'before/after stack snapshots from the measured generation required')
        report = dict(label=label, generation=generation, **stack_report(samples, board=self.args.board))
        if not hasattr(self, 'task_stack_reports'): self.task_stack_reports = []
        self.task_stack_reports.append(report)
        require(report['stack_checks_passed'], '; '.join(report['stack_findings']))
        return lines, capability


def voice_smoke(args):
    manifest, _ = audio.verify_bundle(args.bundle, args.board)
    require(manifest['profile'] == 'normal' and not manifest['aec_enabled'], 'normal AEC-off voice bundle required')
    require(args.headless is True, '--headless skips UI checks; display runtime initialization is retained')
    sweep = None
    require(args.board == 'p4xc' or getattr(args, 'acoustic_reanalysis', None) is None, 'acoustic reanalysis decision is P4 only')
    if args.board == 'p4xc':
        audio.audio_settings(manifest)
        reanalysis = getattr(args, 'acoustic_reanalysis', None)
        require(bool(args.acoustic_run) != bool(reanalysis), '--acoustic-run or --acoustic-reanalysis required before P4 voice smoke')
        sweep = audio.reanalysis_gate(reanalysis, manifest) if reanalysis else audio.sweep_gate(args.acoustic_run, manifest)
    identity = json.loads(args.bot_identity.read_text())
    io = VoiceIO(args, args.out, manifest, identity)
    result = smoke.execute(args, backend=io)
    result.update(acoustic_sweep=sweep, boot_audio=getattr(io, 'boot_audio', None),
        helper_sha256=digest(Path(__file__)), base_helper_sha256=digest(Path(audio.__file__)),
        voice_evidence_profile='product', ui_checks_skipped=True,
        display_initialized=getattr(io, 'display_initialized', False),
        task_stack_reports=getattr(io, 'task_stack_reports', []),
        product_evidence_verified=getattr(io, 'product_evidence_verified', False))
    require(not result['acquisition_passed'] or result['product_evidence_verified'], 'successful voice acquisition lacks product evidence')
    smoke.write(args.out/'result.json', result)
    return 0 if result['acquisition_passed'] and not result.get('error') else 1


def stack_measurements(samples):
    """Exit-only zero fields are unavailable; census zero is a real low mark.

    PERF tasks[].stack_free_bytes comes from uxTaskGetSystemState, in bytes,
    independent of the exit-only Session atomics. Decoder pull already samples
    its caller, audio-playback. Use every in-generation snapshot, not just one
    convenient final mark. Positive direct fields are retained as extra evidence.
    Census marks remain conservative task-lifetime minima across generations
    when PersistentWorker tasks survive a reconnect on the same boot.
    """
    measured = {}
    for key, name in STACK_TASKS.items():
        values, sources, missing, task_ids, allocations, allocation_missing = [], [], [], set(), [], []
        for index, sample in enumerate(samples):
            sizes = []
            if key == 'rx_stack' and sample.get('rx_stack_bytes') is not None:
                sizes.append(sample['rx_stack_bytes'])
            direct = sample.get(key)
            require(direct is None or type(direct) is int and direct >= 0, 'invalid stack field '+key)
            observed = False
            if direct is not None and direct > 0:
                values.append(direct); sources.append(dict(snapshot=index, source='PERF.'+key, free_bytes=direct)); observed = True
            tasks = [task for task in sample.get('tasks', []) if task.get('name') == name]
            require(len(tasks) <= 1, 'ambiguous task census '+name)
            if tasks:
                task = tasks[0]; free = task.get('stack_free_bytes'); size = task.get('stack_bytes')
                require(type(free) is int and free >= 0, 'invalid census high-water mark '+name)
                require(size is None or type(size) is int and size > 0 and free <= size, 'invalid census stack size '+name)
                if task.get('id') is not None: task_ids.add(task['id'])
                values.append(free); sources.append(dict(snapshot=index, source='PERF.tasks', task=name,
                    task_id=task.get('id'), stack_bytes=size, free_bytes=free)); observed = True
                if size is not None: sizes.append(size)
            require(all(type(size) is int and size > 0 for size in sizes), 'invalid allocation '+name)
            require(len(set(sizes)) <= 1, 'allocation sources disagree: '+name)
            if sizes and direct is not None: require(direct <= sizes[0], 'direct stack exceeds allocation: '+name)
            allocations.extend(sizes)
            if not sizes: allocation_missing.append(index)
            if not observed: missing.append(index)
        require(len(task_ids) <= 1, 'task identity changed within generation: '+name)
        require(len(set(allocations)) <= 1, 'allocation changed within generation: '+name)
        available = bool(values) and not missing
        measured[key] = dict(task=name, available=available, free_bytes=min(values) if values else None,
            allocation_bytes=allocations[0] if allocations and not allocation_missing else None,
            missing_snapshots=missing, sources=sources,
            direct_zero_meaning='unmeasured (exit-only)' if key != 'decode_stack' else 'unmeasured (no completed pull)')
    return measured


def stack_report(samples, *, board):
    require(board in ('echoear', 'p4xc'), 'unknown stack policy board')
    report_only = ['rx_stack'] if board == 'echoear' else []
    stacks = stack_measurements(samples); findings = []; floors = {}
    for key, measurement in stacks.items():
        allocation = measurement['allocation_bytes']
        floor = max(RX_REFERENCE_FLOOR, (allocation+3)//4) if key == 'rx_stack' and allocation is not None else (
            None if key == 'rx_stack' else STACK_FLOOR)
        if key in report_only:
            floors[key] = None
            continue
        floors[key] = floor
        if not measurement['available']: findings.append(key+': unavailable measurement')
        elif floor is None: findings.append(key+': unavailable allocation')
        elif measurement['free_bytes'] < floor: findings.append(key+': measured free stack below '+str(floor))
    # Report every census task, retaining IDs when names such as pipecat repeat.
    minima = {}
    for sample in samples:
        for task in sample.get('tasks', []):
            name, task_id, free = task.get('name'), task.get('id'), task.get('stack_free_bytes')
            require(type(name) is str and type(free) is int and free >= 0, 'invalid task census minimum')
            key = (name, task_id)
            if key not in minima: minima[key] = dict(task=name, task_id=task_id, free_bytes=free, stack_bytes=task.get('stack_bytes'))
            else: minima[key]['free_bytes'] = min(minima[key]['free_bytes'], free)
    return dict(stack_measurements=stacks, stack_findings=findings, stack_checks_passed=not findings,
        stack_floors_bytes=floors, stack_report_only=report_only, task_minima=list(minima.values()),
        stack_policy=STACK_POLICY, stack_policy_addendum=STACK_POLICY_ADDENDUM,
        rx_reference=dict(free_bytes=RX_REFERENCE_FLOOR, allocation_fraction=.25, evidence=str(RX_REFERENCE),
            source='step-12b fix-1 uncommitted S3 normal bundle; not image of record 55152f25'),
        census_basis='task-lifetime high-water marks; snapshots filtered by session generation')


def session_costs(directory, out):
    """Retain valid codec figures even when a real stack gate needs follow-up."""
    require(not out.exists(), 'new cost output required')
    run = json.loads((directory/'result.json').read_text())
    require(run['board'] in ('echoear', 'p4xc') and run['variant'] == 'normal' and run['acquisition_passed'] is True,
            'successful normal smoke required')
    audio.clean_run(run)
    paths = sorted(directory.glob('perf-*.json')); all_samples = [json.loads(path.read_text()) for path in paths]
    require(len(all_samples) >= 2, 'before/after live PERF required')
    final = max(all_samples, key=lambda row: row['encode_frames'])
    require(final['encode_frames'] >= 500 and final['speech_decode_frames'] >= 100, 'insufficient live codec work')
    require(type(final.get('generation')) is int and final['generation'] > 0, 'missing session generation')
    samples = [row for row in all_samples if row.get('generation') == final['generation']]
    require(len(samples) >= 2, 'before/after PERF from the measured generation required')
    require(all(row['uplink_frame_ms'] == 20 for row in samples), '20 ms Opus required')
    for key in ('encode_p50_us', 'encode_p95_us', 'encode_max_us', 'decode_p50_us', 'decode_p95_us',
                'decode_max_us', 'speech_decode_p50_us', 'speech_decode_p95_us'):
        require(type(final[key]) is int and final[key] > 0, 'missing live codec timing '+key)
    require(all(row['decode_errors'] == 0 for row in samples), 'live decoder errors')
    resources = smoke.resource_check(samples, run['board'])
    report = stack_report(samples, board=run['board'])
    keys = [key for key in final if any(word in key for word in ('encode', 'decode', 'stack', 'per_core_idle'))]
    metrics = {key: final[key] for key in keys}
    # Keep raw exit markers from masquerading as measured bytes in this report.
    for key in STACK_TASKS:
        if metrics.get(key) == 0: metrics[key] = None
    result = dict(checks_passed=report['stack_checks_passed'], codec_costs_available=True, **report,
        qualification=False, units='microseconds (live wall cost)',
        basis='PERF codec histograms; p50/p95 quantized by codec_histogram_bucket_us; exact maxima; no p99 claim',
        result_sha256=digest(directory/'result.json'), sources={path.name: digest(path) for path in paths},
        resources=resources, restore=run['restore'], generation=final['generation'], metrics=metrics,
        codec_histogram_bucket_us=final['codec_histogram_bucket_us'])
    with out.open('x') as stream: stream.write(json.dumps(result, indent=2)+'\n')
    return result


def replay_capture(directory):
    """Read historical raw capture as product; never repair or bless a run."""
    from wire_contract import ContractError
    result = json.loads((directory/'result.json').read_text())
    lines = (directory/'main-serial.log').read_text().splitlines()
    capability = [line for line in (directory/'main-bot.log').read_text().splitlines() if 'DEMO_CAPABILITY ' in line]
    try:
        capture, _, _ = product_capture(lines, capability,
            manifest=dict(aec_enabled=result['variant'].startswith('aec-')), board=result['board'], calibration=True)
        return dict(profile='product', evidence_passed=True, board=result['board'],
                    interval_count=capture.intervals[0]['n'], original_acquisition_passed=result['acquisition_passed'], qualification=False)
    except ContractError as exc:
        return dict(profile='product', evidence_passed=False, board=result['board'],
                    error=str(exc), original_acquisition_passed=result['acquisition_passed'], qualification=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest='action', required=True)
    costs = sub.add_parser('costs'); costs.add_argument('directory', type=Path); costs.add_argument('--out', type=Path, required=True)
    pair = sub.add_parser('pair'); pair.add_argument('left', type=Path); pair.add_argument('right', type=Path)
    pair.add_argument('--out', type=Path, required=True); pair.add_argument('--bounds', type=Path, default=smoke.BOUNDS_FILE)
    replay = sub.add_parser('replay'); replay.add_argument('directory', type=Path); replay.add_argument('--out', type=Path, required=True)
    voice = sub.add_parser('voice-smoke'); voice.add_argument('--board', choices=('echoear', 'p4xc'), required=True)
    for name in ('bundle', 'out', 'bot-dir', 'bot-identity'): voice.add_argument('--'+name, type=Path, required=True)
    voice.add_argument('--bot-repo', type=Path, default=Path.home()/'src/dg-esp32')
    eligibility = voice.add_mutually_exclusive_group()
    eligibility.add_argument('--acoustic-run', type=Path); eligibility.add_argument('--acoustic-reanalysis', type=Path)
    voice.add_argument('--pair-id', required=True); voice.add_argument('--headless', action='store_true', required=True)
    voice.add_argument('--calibration', action='store_true', required=True)
    voice.add_argument('--p4-factory-backup', type=Path, default=smoke.FACTORY_BACKUP)
    voice.set_defaults(reference_only=False, p4_restore_bundle=None, p4_restore_elf=None,
                       bounds=smoke.BOUNDS_FILE, coverage=.95, coverage_decision=None)
    args = parser.parse_args(argv)
    if args.action == 'voice-smoke': return voice_smoke(args)
    if args.action == 'pair':
        print(json.dumps(audio.product_pair(args.left, args.right, args.out, args.bounds), indent=2)); return 0
    result = session_costs(args.directory, args.out) if args.action == 'costs' else replay_capture(args.directory)
    if args.action == 'replay':
        with args.out.open('x') as stream: stream.write(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))
    return 0 if result.get('checks_passed', result.get('evidence_passed', False)) else 1


if __name__ == '__main__': raise SystemExit(main())

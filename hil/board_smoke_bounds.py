#!/usr/bin/env python3
"""Additive fixed-bounds adapter for frozen board_smoke.py.

Use this entrypoint for --calibration --bounds and --recheck --bounds.
Bot-content findings stay report-only; passed/qualification remain false.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sys

import board_smoke as smoke
from make_bundle import digest
from wire_contract import measured


def check_capture(lines, capability_lines, *, bounds_path=None, **kwargs):
    if bounds_path is not None:
        manifest = kwargs['manifest']
        variant = manifest.get('profile', ('aec-' if manifest['aec_enabled'] else '')+'normal')
        kwargs.update(bounds=smoke.bounds_for(bounds_path, variant), calibration=False)
    return _check_capture(lines, capability_lines, **kwargs)


_check_capture = smoke.check_capture


@contextmanager
def fixed_bounds(path):
    """Scoped adaptation of frozen execute; no changes to its bot-content policy."""
    previous = smoke.check_capture
    smoke.check_capture = lambda lines, capability, **kw: check_capture(lines, capability, bounds_path=path, **kw)
    try: yield
    finally: smoke.check_capture = previous


def execute(args, *, backend=None):
    # Reject absent/invalid fixed bounds before constructing live IO or flashing.
    manifest, _ = smoke.verify_bundle(args.bundle, args.board, args.reference_only)
    if not args.reference_only: smoke.bounds_for(args.bounds, manifest['profile'])
    with fixed_bounds(args.bounds): result = _execute(args, backend=backend)
    if args.calibration: result.update(passed=False, qualification=False)
    result.update(bounds_sha256=digest(args.bounds), bounds_status='fixed measured bounds')
    smoke.write(args.out/'result.json', result)
    return result


_execute = smoke.execute


def recheck_run(directory, output=None, *, bounds_path=smoke.BOUNDS_FILE):
    directory = Path(directory).resolve()
    output = Path(output).resolve() if output else directory/'result-recheck-bounds.json'
    smoke.require(output.parent == directory and output.name.startswith('result-recheck') and output.suffix == '.json',
                  'recheck output must be a new result-recheck*.json next to result.json')
    smoke.require(not output.exists(), 'recheck output exists')
    source = directory/'result.json'; original = json.loads(source.read_text())
    result = dict(schema=1, kind='offline-fixed-bounds-recheck', checks_passed=False, passed=False, qualification=False,
                  source_result=str(source), source_result_sha256=digest(source), source_passed=original.get('passed'),
                  source_acquisition_passed=original.get('acquisition_passed'), source_error=original.get('error'), sessions=[])
    try:
        smoke.require(not original.get('reference_only'), 'reference-only has no candidate wire evidence')
        board, variant = original['board'], original['variant']
        smoke.require(board in smoke.CHIPS and variant in smoke.VARIANTS, 'unknown board/variant')
        smoke.require(type(original['calibration']) is bool, 'missing calibration mode')
        identity_path = directory/'identity.json'; identity = json.loads(identity_path.read_text())
        smoke.identity_schema(identity); smoke.require(identity == original['identity'], 'recorded bot identity differs')
        bounds = smoke.bounds_for(bounds_path, variant)
        result.update(board=board, variant=variant, calibration=original['calibration'],
                      bounds_sha256=digest(bounds_path), bounds_status='fixed measured bounds', identity_sha256=digest(identity_path))
        for label in ('main', 'resume', 'cold') if variant.endswith('demo') else ('main',):
            serial, bot = directory/(label+'-serial.log'), directory/(label+'-bot.log')
            bot_lines = bot.read_text().splitlines()
            smoke.require(not any('\x1b[' in line for line in bot_lines), 'bot log must be uncoloured')
            capture, _, timing = _check_capture(serial.read_text().splitlines(), [l for l in bot_lines if 'DEMO_CAPABILITY ' in l],
                manifest=dict(aec_enabled=variant.startswith('aec-')), board=board,
                headless=original.get('headless', False), bounds=bounds, calibration=False)
            row = capture.intervals[0]
            result['sessions'].append(dict(label=label, intervals=row, timing=timing,
                serial_sha256=digest(serial), bot_sha256=digest(bot), ports=smoke.check_ports(capture),
                observed_coverage=row['ok']*20000/(row['end_us']-row['start_us'])))
        result['checks_passed'] = True
    except Exception as exc: result['error'] = type(exc).__name__+': '+str(exc)
    with output.open('x') as stream: stream.write(json.dumps(result, indent=2, allow_nan=False)+'\n')
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Opt-in preserves historical acquisition-only calibration without --bounds.
    if not any(a == '--bounds' or a.startswith('--bounds=') for a in argv): return smoke.main(argv)
    previous_execute, previous_recheck = smoke.execute, smoke.recheck_run
    smoke.execute, smoke.recheck_run = execute, recheck_run
    try: return smoke.main(argv)
    finally: smoke.execute, smoke.recheck_run = previous_execute, previous_recheck


if __name__ == '__main__': raise SystemExit(main())

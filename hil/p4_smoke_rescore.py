#!/usr/bin/env python3
"""Offline AEC product-smoke re-score; originals are immutable, missing gates fail."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path

import board_smoke as smoke
from make_bundle import digest
from p4_aec_policy import write_new
from wire_contract import require

DECISION = 'decision-2026-11-02-smoke-llm-content-report-only'
SESSION_GATES = ('resources', 'stacks', 'wire/uplink')
# Mirrors strict content errors in board_smoke.py:197-209 plus the step-14
# negation checks. Keep these historical strings for retained-result replay.
CONTENT_ERRORS = {
    'ContractError: France/Paris turn failed',
    'ContractError: AEC must hear Italy and answer Rome',
    'ContractError: count reply did not complete through twenty',
    'ContractError: qualification reply is off-script or negates paris',
    'ContractError: qualification reply is off-script or negates rome',
}


def script(variant):
    require(variant in ('aec-normal', 'aec-demo'), 'AEC product smoke required')
    turns = [('main', k) for k in ['france', 'talkover']+['long']*10]
    if variant == 'aec-demo':
        turns += [('resume', 'france'), ('cold', 'france')]
    return turns


def turn_checks(events, kind, aec, *, calibration=False):
    """Soften content alone, retaining the frozen STT/token and event rules.

    calibration=True is used only on the pure checker, never on the executor
    or LiveIO: recording deadlines, measured bounds and all other gates stay
    in qualification mode. The step-14 adapter keeps its default strict path.
    """
    from p4_step14 import _turn_checks
    require(not calibration and aec is True, 'report-only is for scored AEC product smokes')
    require(kind in ('france', 'long', 'talkover'), 'unknown scripted prompt')
    result = _turn_checks(events, kind, aec, calibration=True)
    need = 2 if kind == 'talkover' else 1
    require(smoke.turn_finished(events, need), 'reply to latest user did not complete')
    if kind == 'talkover':
        require(result['interruptions_during_bot'] == 1, 'unexpected self-interruption during talk-over')
    assistants = [e for e in events if e.get('kind') == 'aggregator-assistant-turn-stopped']
    require(assistants[-1].get('interrupted') is not True, 'final reply self-interrupted')
    expected = 'Paris' if kind == 'france' else 'Rome' if kind == 'talkover' else 'one through twenty'
    findings = result.pop('bot_content_findings')
    result.update(stt_passed=True, turn_completed=True, expected_answer=expected,
                  llm_content='LLM content: report-only' if findings else 'pass',
                  llm_content_findings=findings, decision=DECISION)
    return result


@contextmanager
def content_mode(mode):
    """Scoped inside step-14 adapters; no permanent mutation of frozen code."""
    require(mode in ('off', 'report-only'), 'unknown LLM content mode')
    if mode == 'off':
        yield
        return
    previous = smoke.turn_checks
    smoke.turn_checks = turn_checks
    try:
        yield
    finally:
        smoke.turn_checks = previous


def completeness(row):
    """Explicit ledger of scripted steps, including post-turn/session work."""
    expected = script(row['variant'])
    missing = []
    turns = row.get('turns', [])
    for index, (label, kind) in enumerate(expected, 1):
        if index > len(turns):
            missing.append(f'{label}:turn-{index:02d}:{kind}')
        else:
            require(turns[index-1].get('kind') == kind, 'missing/reordered scripted turn')
    require(len(turns) <= len(expected), 'unexpected extra scripted turn')
    labels = list(dict.fromkeys(label for label, _ in expected))
    sessions = row.get('sessions', [])
    require([s.get('label') for s in sessions] == labels[:len(sessions)], 'missing/reordered sessions')
    for label in labels:
        session = next((s for s in sessions if s['label'] == label), None)
        if session is None:
            missing.extend(f'{label}:{gate}' for gate in SESSION_GATES)
    if row['variant'] == 'aec-demo':
        if not row.get('hold'):
            missing.append('main:held-state/no-reconnect')
        # The second held() has no result field. A cold session proves execute
        # advanced past it; a failed cold turn is evidence of that too.
        if len(turns) < 14 and not any(s['label'] == 'cold' for s in sessions):
            missing.append('resume:held-state/no-reconnect (no completion witness)')
        if not row.get('cold_assets'):
            missing.append('cold:persisted-assets')
    return missing


def finalize_live(row):
    """Reject incomplete acquisition even if a backend incorrectly returned success."""
    missing = completeness(row)
    row.update(llm_content_mode='report-only', llm_content_decision=DECISION,
               unmeasured_steps=missing,
               llm_content_findings=[dict(f, turn=i, session=script(row['variant'])[i-1][0],
                                          expected_answer=t.get('expected_answer'))
                   for i, t in enumerate(row.get('turns', []), 1)
                   for f in t.get('llm_content_findings', [])])
    if missing:
        row.update(passed=False, acquisition_passed=False)
        row.setdefault('error', 'unmeasured scripted steps: '+', '.join(missing))
    return row


def before_disconnect(events):
    """Discard only recorder rows after the harness's explicit disconnect request."""
    for index, event in enumerate(events):
        message = event.get('message') or {}
        if message.get('type') == 'disconnect-bot':
            return events[:index]
    return events


def rescore(directory, bounds_path=smoke.BOUNDS_FILE):
    """Replay retained turn, wire, resource and stack evidence, failing closed."""
    from p4_aec_product import image_audio, applied_gate
    from p4_audio_fix4 import stack_report
    directory = Path(directory).resolve()
    source = directory/'result.json'
    row = json.loads(source.read_text())
    expected = script(row['variant'])
    sources = {str(source): digest(source)}
    failures, turns, sessions = [], [], []

    def read(name, *, raw=False):
        path = directory/name
        text = path.read_text()
        sources[str(path)] = digest(path)
        return text if raw else json.loads(text)

    def check(label, operation):
        try:
            return operation()
        except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
            failures.append(label+': '+str(exc))
            return None

    def turn(events, index, origin):
        label, kind = expected[index-1]
        entry = dict(index=index, session=label, kind=kind, evidence=origin,
                     stt=smoke.text_rows(events, 'user'), llm=smoke.text_rows(events, 'assistant'),
                     expected_answer='Paris' if kind == 'france' else 'Rome' if kind == 'talkover' else 'one through twenty',
                     verdict='FAIL')
        verdict = check(f'{label}:turn-{index:02d}', lambda: turn_checks(events, kind, True))
        if verdict is not None:
            entry.update(verdict=verdict['llm_content'], checks=verdict)
        turns.append(entry)
        step = f'{label}:turn-{index:02d}:{kind}'
        if step in missing:
            missing.remove(step)
        if label == 'cold':
            held = 'resume:held-state/no-reconnect (no completion witness)'
            if held in missing:
                missing.remove(held)

    missing = completeness(row)
    for index, retained in enumerate(row.get('turns', []), 1):
        def replay(index=index, retained=retained):
            stored = read(f'turn-{index:02d}.json')
            require(stored == retained, 'turn file/result differs')
            turn(stored['events'], index, f'turn-{index:02d}.json')
        check(f'turn-{index:02d}:original', replay)

    # A strict content failure happens before LiveIO writes its turn JSON.
    # Its complete recorder is still retained by failed-session cleanup.
    failed_index = len(row.get('turns', []))+1
    if row.get('error') and failed_index <= len(expected) and (directory/'failed-bot-events.json').is_file():
        def replay_failed():
            events = before_disconnect(read('failed-bot-events.json'))
            previous = [e['monotonic_ns'] for t in row.get('turns', []) for e in t.get('events', [])]
            if previous:
                last = max(previous)
                events = [e for e in events if e['monotonic_ns'] > last]
            turn(events, failed_index, 'failed-bot-events.json (no completed scripted turn record)')
        check('failed turn evidence', replay_failed)

    def globals_check():
        require(row.get('calibration') is False and row.get('reference_only') is False and row.get('headless') is False,
                'scored product originals required')
        require(not row.get('interrupted') and row.get('cleanup_errors') == [], 'interruption/cleanup failure or missing cleanup evidence')
        require(row.get('restore', {}).get('clean') is True, 'clean restore unproven')
        require(read('restore.json') == row['restore'], 'restore receipt differs')
        if row['board'] == 'p4xc':
            require(row['restore'].get('action') == 'p4-product-of-record', 'wrong P4 restore')
        require(row.get('room', {}).get('noise_contaminated') is False, 'room gate unproven')
        require(read('identity.json') == row['identity'], 'bot identity differs')
        smoke.identity_schema(row['identity'])
        if row.get('error'):
            require(row['error'] in CONTENT_ERRORS and any(t.get('checks', {}).get('llm_content_findings') for t in turns),
                    'original error not proven content-only')
        else:
            require(row.get('acquisition_passed') is True and row.get('passed') is True, 'original acquisition incomplete')
    check('global gates', globals_check)
    image = check('image', lambda: read('image.json'))
    if image:
        def boot():
            require(image['elf_sha256'] == row['elf_sha256'] and image['profile'] == row['variant'] and image['aec_enabled'] is True,
                    'image/result differs')
            smoke.boot_check(read('boot.log', raw=True).splitlines(), image, row['board'])
            if row.get('applied_calibration'):
                from p4_aec_applied import readback
                applied = applied_gate(image, Path(row['applied_calibration']['recommendation_file']))
                require(applied == row['applied_calibration'], 'applied provenance differs')
                require(readback(read('boot.log', raw=True).splitlines(), image, applied['selected']) == row.get('applied_readback'),
                        'applied boot readback differs')
        check('boot/applied', boot)
        if row['board'] == 'p4xc':
            def retained_erle():
                # ERLE itself is report-only. The smoke preflight required an
                # available same-image report; preserve and authenticate that
                # retained measurement rather than changing its score/rules.
                report = row.get('erle_report', {})
                require(report.get('available') is True and report.get('report_only') is True,
                        'same-image ERLE report unavailable')
                require(report.get('elf_sha256') == image['elf_sha256'] and
                        report.get('profile') == image['profile'] and report.get('gain_db') == image['boot_ref_gain_db'],
                        'ERLE report image/gain differs')
                files = list(report.get('sources', {}).items())
                require(bool(files), 'ERLE source evidence missing')
                files += [(report['plan_file'], report['plan_sha256']),
                          (report['delay_file'], report['delay_sha256'])]
                for name, sha256 in files:
                    require(digest(Path(name)) == sha256, 'ERLE source digest differs: '+name)
                    sources[name] = sha256
            check('ERLE provenance (measurement report-only)', retained_erle)
    for session in row.get('sessions', []):
        label = session['label']
        def replay_session():
            require(image is not None, 'image unavailable')
            lines = read(label+'-serial.log', raw=True).splitlines()
            bot = read(label+'-bot.log', raw=True).splitlines()
            require(not any('\x1b[' in line for line in bot), 'coloured bot log')
            capture, wire, timing = smoke.check_capture(lines, [l for l in bot if 'DEMO_CAPABILITY ' in l],
                manifest=image, board=row['board'], bounds=smoke.bounds_for(bounds_path, row['variant']))
            image_audio(capture, image, row['board'])
            require(digest(directory/session['wire_file']) == session['wire_sha256'], 'wire digest differs')
            require(read(session['wire_file'], raw=True) == '\n'.join(wire)+'\n', 'wire original differs')
            require(capture.intervals[0] == session['intervals'] and timing == session['timing'], 'interval/timing differs')
            generation = capture.intervals[0]['gen']
            samples = [json.loads(line[5:]) for line in lines if line.startswith('PERF {')]
            samples = [s for s in samples if s.get('generation') == generation]
            require(len(samples) >= 2, 'before/after generation snapshots unavailable')
            smoke.resource_check(samples, row['board'])
            require(session.get('resources', {}).get('passed') is True, 'original resources failed')
            report = stack_report(samples, board=row['board'])
            require(report['stack_checks_passed'], '; '.join(report['stack_findings']))
            reports = [s for s in row.get('task_stack_reports', []) if s.get('label') == label]
            require(len(reports) == 1 and reports[0].get('stack_checks_passed') is True, 'original product stack gate unmeasured/failed')
            sessions.append(dict(label=label, verdict='PASS', resources=True, stacks=True, wire_uplink=True))
        check(label+':session gates', replay_session)
    if row['variant'] == 'aec-demo':
        if row.get('hold'):
            check('demo held', lambda: require(row['hold'].get('held') is True, 'hold gate failed'))
        if row.get('cold_assets'):
            check('cold assets', lambda: require(row['cold_assets'].get('passed') is True, 'persisted assets failed'))
    # A failed turn may be measured from its recorder; missing post-turn/session
    # gates are never synthesized from cleanup captures or earlier sessions.
    passed = not missing and not failures and len(turns) == len(expected)
    return dict(schema=1, kind='p4-aec-product-smoke-rescore', decision=DECISION,
        source_result=str(source), source_result_sha256=sources[str(source)], sources=sources,
        passed=passed, verdict='PASS' if passed else 'FAIL', qualification=False,
        all_scripted_steps_and_sessions_completed=not missing, unmeasured_steps=missing,
        failures=failures, turns=turns, sessions=sessions)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--bounds', type=Path, default=smoke.BOUNDS_FILE)
    args = parser.parse_args(argv)
    output = args.directory/'rescore.json'
    require(not output.exists(), 'rescore.json already exists; refusing overwrite')
    result = rescore(args.directory, args.bounds)
    write_new(output, result)
    print(json.dumps(dict(path=str(output), verdict=result['verdict'],
                         unmeasured_steps=result['unmeasured_steps'])))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

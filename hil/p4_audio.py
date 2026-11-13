#!/usr/bin/env python3
"""Step-12b coordinator adapters. Imports and offline pair analysis do no IO to devices.

Live commands reuse board_smoke's flash, boot checks and signal-safe restore hold.
No bot is launched, and frozen HIL modules are never monkeypatched.
"""
import argparse
import json
import math
import re
from pathlib import Path

from board_smoke import LiveIO, BOUNDS_FILE, FACTORY_BACKUP, bounds_for, write
from make_bundle import verify_bundle, digest
from wire_contract import require, parse_capture, compare, measured

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT/'hil/fixtures/p4xc/audio-plan.json'
DECISION_ID = 'decision-2026-11-01-p4-clean-band'
SNR_RULE = 'p4-chain-snr-contiguous-v1'


class HardwareIO(LiveIO):
    """Use only LiveIO's hardware/restore methods; acoustic work needs no bot."""
    def __init__(self, args, manifest):
        import boards
        boards.require_bench()
        self.args, self.out, self.manifest = args, args.out, manifest
        self.python = boards.checked_esptool_python()


def response(board, command, *, record=None):
    at = board.cursor
    seq = board.send(command)
    ack = board.wait_for(rf'^(?:OK|ERR) #{seq}(?: |$)', 10, since=at)
    if ack.startswith('OK ') and record is not None:
        # The best-effort log drain may finish after the reserved ACK. Wait
        # briefly for this command's record; never retry the command itself.
        board.wait_for(record, 2, since=at)
    lines = [line for _, _, line in board.lines_since(at)]
    return ack.startswith('OK '), lines


def console_check(board):
    """AEC-off S3 regression, including the explicit unsupported-build reasons."""
    rows = []
    for command in ('cache-counters 0', 'mic-analog-gain 30', 'aec-ref-gain-status'):
        ok, lines = response(board, command)
        if command.startswith('cache-counters'):
            require(ok or 'unsupported build=cache-counters-disabled cmd=cache-counters' in lines,
                    'cache command must succeed when enabled or reject with cache-counters-disabled')
        elif command.startswith('mic-analog'):
            require(ok and any(re.search(r'MIC_GAIN_PATH analog_db=30 reg=0x1a\b', line) for line in lines),
                    'analog gain must succeed with MIC1 register readback')
        else:
            require(not ok and 'unsupported build=aec-disabled cmd=aec-ref-gain-status' in lines,
                    'AEC-off must reject aec-ref-gain-status explicitly')
        rows.append(dict(command=command, accepted=ok, lines=lines))
    ok, _ = response(board, 'ping')
    require(ok, 'console must remain usable after rejected AEC command')
    return dict(checks_passed=True, commands=rows)


def preflight_acoustic(out):
    """Import all acoustic dependencies and check the room BEFORE any flash."""
    import time
    import numpy as np
    from types import SimpleNamespace
    from alsa import resolve_jabra, set_volumes, record_room_baseline
    from bench_calibration import record_prompt, pcm, dbfs
    from speaker_dual_sweep_test import capture, correct, dump_mic
    from dump_verify import verify_dump
    plan = json.loads(PLAN.read_text())
    card, device = resolve_jabra(); set_volumes(card, out/'mixer.log')
    room = record_room_baseline(out/'room', 'P4 step 12b')
    require(not room['noise_contaminated'], 'quiet-room baseline failed')
    return SimpleNamespace(np=np, sleep=time.sleep, device=device, room=room, plan=plan,
        record_prompt=record_prompt, pcm=pcm, dbfs=dbfs, capture=capture,
        correct=correct, dump_mic=dump_mic, verify_dump=verify_dump)


def audio_settings(manifest):
    """Only newly stamped P4 bundles declare enough audio state for bench use."""
    settings = manifest.get('p4_audio')
    required = ('analog_db','digital_percent','playback_volume_db','playback_seed_dbfs_x10','tx_stack_bytes')
    require(type(settings) is dict and set(settings)==set(required) and
            all(type(settings[k]) is int for k in required), 'P4 bundle audio metadata required')
    require(1<=settings['digital_percent']<=1600 and settings['analog_db'] in (24,30,33) and
            -600<=settings['playback_seed_dbfs_x10']<=-100 and 16384<=settings['tx_stack_bytes']<=49152,
            'invalid P4 bundle audio settings')
    overlay = manifest.get('p4_overlay_values', {})
    require(overlay.get('CONFIG_DG_MIC_GAIN_PERCENT')==str(settings['digital_percent']),
            'P4 bundle mic overlay metadata differs')
    diff = manifest.get('p4_overlay_keydiff', {})
    require(diff.get('differ')==[] and diff.get('removed')==[] and
            'CONFIG_DG_MIC_GAIN_PERCENT' in diff.get('equal',[]), 'P4 bundle overlay keydiff required')
    return settings


def verify_audio(board, manifest):
    expected = audio_settings(manifest)
    ok, lines = response(board, 'mic-gain-status', record=r'MIC_GAIN_PATH analog_db=')
    gains = [re.search(r'MIC_GAIN_PATH analog_db=(\d+) reg=0x([0-9a-fA-F]+) digital_percent=(\d+)', line) for line in lines]
    gains = [gain for gain in gains if gain]
    require(ok and len(gains)==1, 'missing boot mic-gain-status readback')
    gain = gains[0]
    observed = dict(analog_db=int(gain[1]), digital_percent=int(gain[3]))
    require(int(gain[2],16)==(0x10|observed['analog_db']//3), 'boot MIC1 register mismatch')
    ok, lines = response(board, 'stats', record=r'OUTPUT_PATH volume_relative_db=')
    levels = [re.search(r'OUTPUT_PATH volume_relative_db=(-?\d+)\b', line) for line in lines]
    levels = [level for level in levels if level]
    require(ok and len(levels)==1, 'missing boot playback level readback')
    observed['playback_volume_db'] = int(levels[0][1])
    require(all(observed[key]==expected[key] for key in observed), 'boot audio differs from bundle declaration')
    return observed


def clean_band(points, doubling_db, frequencies, amplitudes):
    # Original rule: a contiguous prefix, both chains, every frequency and
    # both original repeats. Frequency choice is explicit in offline analysis.
    clean = []
    linear = all(abs(v-6.0206)<=1 for c in ('board','jabra') for v in doubling_db[c][:2])
    for amp in amplitudes:
        rows = [r for r in points if r['hz'] in frequencies and r['amplitude_percent']==amp]
        if not linear or len(rows)!=2*len(frequencies) or not all(r['passed'] for r in rows): break
        clean.append(amp)
    bands = {str(hz):{c: [min(r['chains'][c]['fundamental_dbfs'] for r in points
        if r['hz']==hz and r['amplitude_percent'] in clean), max(r['chains'][c]['fundamental_dbfs'] for r in points
        if r['hz']==hz and r['amplitude_percent'] in clean)] for c in ('board','jabra')} for hz in frequencies} if clean else None
    return dict(clean_amplitudes_percent=clean,level_band_dbfs=bands)


def classify_chain(chain, min_snr_db):
    fundamental, noise = chain['fundamental_dbfs'], chain['noise_dbfs']
    require(all(type(value) in (int,float) and math.isfinite(value) for value in (fundamental,noise)),
            'finite fundamental/noise levels required')
    snr = fundamental-noise
    result = dict(snr_db=snr,fundamental_dbfs=fundamental,noise_dbfs=noise)
    if snr<min_snr_db: return dict(result,classification='unmeasurable')
    thd, harmonics, clips = chain['corrected_thd_percent'], chain['corrected_harmonics_dbc'], chain['clipping_count']
    h2,h3 = harmonics['H2'],harmonics['H3']
    require(all(type(value) in (int,float) and math.isfinite(value) for value in (thd,h2)) and
            (h3 is None or type(h3) in (int,float) and math.isfinite(h3)) and type(clips) is int and clips>=0,
            'valid measured distortion/clip metrics required')
    passed = thd<=5 and h2<=-26 and (h3 is None or h3<=-26) and clips==0
    return dict(result,classification='pass' if passed else 'fail',corrected_thd_percent=thd,
                corrected_h2_dbc=h2,corrected_h3_dbc=h3,clipping_count=clips)


def snr_band(points, doubling_db, frequencies, amplitudes, min_snr_db):
    require(type(min_snr_db) in (int,float) and math.isfinite(min_snr_db) and min_snr_db>=0,
            'finite nonnegative chain SNR threshold required')
    classified = []
    for point in points:
        chains = {name:classify_chain(point['chains'][name],min_snr_db) for name in ('board','jabra')}
        states = [chain['classification'] for chain in chains.values()]
        state = 'fail' if 'fail' in states else 'pass' if 'pass' in states else 'unmeasurable'
        classified.append(dict(hz=point['hz'],amplitude_percent=point['amplitude_percent'],repeat=point['repeat'],
            name=point['name'],excluded=point['hz'] not in frequencies,classification=state,chains=chains))
    clean = []; started = False
    linear = all(abs(v-6.0206)<=1 for c in ('board','jabra') for v in doubling_db[c][:2])
    for amp in amplitudes:
        rows = [p for p in classified if not p['excluded'] and p['amplitude_percent']==amp]
        complete = len(rows)==2*len(frequencies) and all(
            sum(p['hz']==hz and p['repeat']==repeat for p in rows)==1 for hz in frequencies for repeat in (1,2))
        require(complete,'need both original repeats at every retained frequency/amplitude')
        failed = any(p['classification']=='fail' for p in rows)
        if not started:
            # The first band amplitude must satisfy BOTH decision conditions:
            # all points pass/are unmeasurable, and each frequency has a
            # measurable pass across either chain/repeat. Earlier failing or
            # wholly unmeasurable amplitudes cannot start the band.
            coverage = all(any(p['hz']==hz and any(c['classification']=='pass' for c in p['chains'].values())
                               for p in rows) for hz in frequencies)
            if failed or not coverage: continue
            started = True
        # Once started, a measurable failure ends the band. Never restart at
        # a later isolated pass; unmeasurable points neither pass nor fail.
        if not linear or failed: break
        clean.append(amp)
    bands = {}
    if clean:
        for hz in frequencies:
            bands[str(hz)] = {}
            for name in ('board','jabra'):
                levels = [p['chains'][name]['fundamental_dbfs'] for p in classified
                    if p['hz']==hz and p['amplitude_percent'] in clean and p['chains'][name]['classification']=='pass']
                bands[str(hz)][name] = [min(levels),max(levels)] if levels else None
    return dict(clean_amplitudes_percent=clean,level_band_dbfs=bands or None,points=classified,
        rule=dict(id=SNR_RULE,min_chain_snr_db=min_snr_db,comparison='fundamental-noise < threshold is unmeasurable',
            start='lowest all-pass-or-unmeasurable amplitude with at least one measurable pass per retained frequency',
            continuation='every retained frequency/repeat passes or is unmeasurable; stop at first measurable failure',
            corrected_thd_percent_max=5,h2_h3_dbc_max=-26,clips_max=0,linearity_doubling_tolerance_db=1),
        linearity_valid=linear)


def reanalyze(directory, exclude_hz, out, min_chain_snr_db=None):
    """Labelled offline analysis; the adopted SNR rule applies only to P4."""
    require(not out.exists(), 'new reanalysis output required')
    require(not out.resolve().is_relative_to(directory.resolve()), 'reanalysis output must be outside the read-only acquisition')
    source = directory/'acoustic.json'; run_source = directory/'result.json'
    run = json.loads(run_source.read_text()); clean_run(run)
    saved = json.loads(source.read_text())
    require(run.get('acquisition_passed') is True and saved.get('acquisition_passed') is True and
            not saved.get('error'), 'completed acoustic acquisition required')
    frequencies = sorted({r['hz'] for r in saved['points']})
    excluded = sorted(set(exclude_hz))
    require(excluded and set(excluded)<=set(frequencies), 'exclusions must name captured frequencies')
    retained = [hz for hz in frequencies if hz not in excluded]
    require(retained, 'cannot exclude every frequency')
    amplitudes = sorted({r['amplitude_percent'] for r in saved['points']})
    # Recompute doubling from the saved original levels, not cached summaries.
    linearity = saved['linearity']
    doubling = {c:[b['chains'][c]['fundamental_dbfs']-a['chains'][c]['fundamental_dbfs']
        for a,b in zip(linearity,linearity[1:])] for c in ('board','jabra')}
    if min_chain_snr_db is None:
        band = clean_band(saved['points'],doubling,retained,amplitudes)
    else:
        require(run.get('board')=='p4xc', 'chain-SNR decision is P4 only')
        band = snr_band(saved['points'],doubling,retained,amplitudes,min_chain_snr_db)
    adopted = min_chain_snr_db==10 and excluded==[500] and run.get('board')=='p4xc'
    result = dict(schema=1,kind='offline-acoustic-frequency-exclusion',qualification=False,
        decision_applied=adopted,decision_id=DECISION_ID if adopted else None,post_hoc=True,board=run.get('board'),
        source_elf_sha256=run.get('elf_sha256'),scope='offline reanalysis; original acoustic verdict unchanged',
        source_acoustic=str(source.resolve()),source_acoustic_sha256=digest(source),
        source_result_sha256=digest(run_source),source_plan_sha256=saved['plan_sha256'],excluded_hz=excluded,retained_hz=retained,
        source_clean_amplitudes_percent=saved['clean_amplitudes_percent'],
        linearity_doubling_db=doubling,restore=run['restore'],**band)
    with out.open('x') as stream: stream.write(json.dumps(result,indent=2,allow_nan=False)+'\n')
    return result


def acoustic(board, out, prepared, initial):
    # preflight_acoustic completed before the signal-safe flash/restore hold.
    np, plan, device = prepared.np, prepared.plan, prepared.device
    capture, correct, dump_mic = prepared.capture, prepared.correct, prepared.dump_mic
    record_prompt, pcm, dbfs = prepared.record_prompt, prepared.pcm, prepared.dbfs
    verify_dump = prepared.verify_dump
    result = dict(acquisition_passed=False, qualification=False, plan_sha256=digest(PLAN),
                  volume_db=-6, room=prepared.room, calibration=[], linearity=[], points=[], microphones=[])
    save = lambda: write(out/'acoustic.json', result)
    try:
        board.command('audio 0'); board.command('quiet 1'); board.command('playback-volume -6')

        def tone(name, hz, amp):
            at = board.cursor
            row = capture(board, device, out, name, hz, amp)
            verify_dump([line for _, _, line in board.lines_since(at)], prefix='MIC_DUMP')
            return row

        frequencies = plan['frequencies_hz']; amplitudes = plan['amplitudes_percent']
        for hz in sorted({f*h for f in frequencies for h in range(1, 6) if f*h < 8000}):
            result['calibration'].append(tone(f'cal-{hz}', hz, 2)); save()
        response_dbfs = {c:{str(r['hz']):r['chains'][c]['fundamental_dbfs'] for r in result['calibration']}
                         for c in ('board', 'jabra')}
        result['response_dbfs'] = response_dbfs
        for amp in (1, 2, 4, 8):
            result['linearity'].append(tone(f'linear-{amp}', 1000, amp)); save()
        result['linearity_doubling_db'] = {c:[b['chains'][c]['fundamental_dbfs']-a['chains'][c]['fundamental_dbfs']
            for a,b in zip(result['linearity'],result['linearity'][1:])] for c in response_dbfs}
        for repeat in (1, 2):
            for hz in frequencies:
                for amp in amplitudes:
                    row = correct(tone(f'r{repeat}-{hz}-{amp}', hz, amp), response_dbfs)
                    row['repeat'] = repeat; result['points'].append(row); save()
        band = clean_band(result['points'], result['linearity_doubling_db'], frequencies, amplitudes)
        result.update(band)
        for gain in plan['mic_analog_candidates_db']:
            board.command(f'mic-analog-gain {gain}'); board.command('mic-gain 100')
            board.command('mic-record 1'); prepared.sleep(1.5); board.command('mic-record 0')
            at = board.cursor; floor = out/f'mic-{gain}-floor.wav'; dump_mic(board, floor)
            verify_dump([line for _, _, line in board.lines_since(at)], prefix='MIC_DUMP')
            for prompt in plan['prompts']:
                directory = out/f'mic-{gain}-{prompt}'; directory.mkdir()
                at = board.cursor
                row = record_prompt(board, device, directory, ROOT/plan['prompts'][prompt], .8, 'prompt')
                verify_dump([line for _, _, line in board.lines_since(at)], prefix='MIC_DUMP')
                raw = pcm(row['raw_path'])
                row.update(analog_db=gain, digital_percent=100, prompt=prompt, floor_dbfs=dbfs(pcm(floor)),
                           peak=int(np.max(np.abs(raw))), adc_clips=int(np.count_nonzero(np.abs(raw)>=32767)))
                result['microphones'].append(row); save()
    except BaseException as error:
        result['error'] = type(error).__name__+': '+str(error)
        raise
    finally:
        failures = []
        for command in (f"mic-analog-gain {initial['analog_db']}",f"mic-gain {initial['digital_percent']}"):
            try: board.command(command)
            except Exception as error: failures.append(type(error).__name__+': '+str(error))
        result['gain_restore'] = dict(analog_db=initial['analog_db'],digital_percent=initial['digital_percent'],errors=failures)
        save()
        if failures: raise RuntimeError('mic gain restore failed: '+'; '.join(failures))
    result['acquisition_passed'] = True; save()
    return result


def target_lock(bundle, manifest, board):
    # Root lock membership changes with S3 AEC mode, so the current checkout's
    # lock cannot establish what was used to build an earlier bundle.
    if board=='echoear':
        value = manifest.get('dependencies_lock_sha256')
    else:
        row = json.loads((bundle/'board-provenance.json').read_text())
        require(row['board']=='p4xc' and row['target_dependencies_lock']=='dependencies.p4xc.lock' and
                row['manifest_sha256']==digest(bundle/'manifest.json') and
                row['elf_sha256']==manifest['elf_sha256'], 'P4 bundle lock provenance binding')
        value = row['target_dependencies_lock_sha256']
    require(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value), 'bundle target lock hash required')
    return value


def live(args):
    manifest, _ = verify_bundle(args.bundle, args.board)
    require(manifest['profile']=='normal' and not manifest['aec_enabled'], 'normal AEC-off bundle required')
    require(args.board==('echoear' if args.action=='console-check' else 'p4xc'), 'wrong board for action')
    args.out.mkdir(parents=True, exist_ok=False)
    result = dict(acquisition_passed=False, qualification=False, board=args.board,
                  elf_sha256=manifest['elf_sha256'], bundle_manifest_sha256=digest(args.bundle/'manifest.json'),
                  target_dependencies_lock_sha256=target_lock(args.bundle,manifest,args.board),
                  helper_sha256=digest(Path(__file__)))
    io = None
    try:
        if args.board=='p4xc': audio_settings(manifest)
        io = HardwareIO(args, manifest); io.preflight_tools(); io.preflight_restore()
        prepared = preflight_acoustic(args.out) if args.action=='acoustic' else None
        with io.hold() as guard:
            guard.touch(); io.flash(args.bundle, manifest, 'candidate')
            with io.open() as board:
                io.boot(board)
                if args.board=='p4xc': result['boot_audio'] = verify_audio(board, manifest)
                result['checks'] = console_check(board) if args.action=='console-check' else acoustic(board, args.out, prepared, result['boot_audio'])
                result['acquisition_passed'] = True; guard.success = True
        require(guard.record.get('clean') is True, 'cleanup/restore failed or required retry')
    except BaseException as error:
        result['error'] = type(error).__name__+': '+str(error)
        result['acquisition_passed'] = False
        raise
    finally:
        result['restore'] = getattr(getattr(io, 'guard', None), 'record', None)
        write(args.out/'result.json', result)
        guard = getattr(io, 'guard', None)
        if guard is not None: guard.release_signals()
    return 0


def clean_run(run):
    require(not run.get('error') and not run.get('cleanup_errors') and not run.get('interrupted') and
            type(run.get('restore')) is dict and run['restore'].get('clean') is True,
            'acquisition error or failed/missing clean restore')
    return run['restore']


def sweep_gate(directory, manifest):
    run = json.loads((directory/'result.json').read_text()); clean_run(run)
    require(run.get('acquisition_passed') is True and run.get('board')=='p4xc' and
            run.get('elf_sha256')==manifest['elf_sha256'], 'sweep must use this P4 bundle')
    sweep = json.loads((directory/'acoustic.json').read_text())
    require(sweep.get('acquisition_passed') is True and not sweep.get('error') and
            bool(sweep.get('clean_amplitudes_percent')) and bool(sweep.get('level_band_dbfs')),
            'non-empty measured P4 clean band required before voice smoke')
    return dict(result_sha256=digest(directory/'result.json'),acoustic_sha256=digest(directory/'acoustic.json'),
                clean_amplitudes_percent=sweep['clean_amplitudes_percent'],level_band_dbfs=sweep['level_band_dbfs'])


def reanalysis_gate(path, manifest):
    row = json.loads(path.read_text())
    require(row.get('kind')=='offline-acoustic-frequency-exclusion' and row.get('qualification') is False and
            row.get('decision_applied') is True and row.get('decision_id')==DECISION_ID and
            row.get('post_hoc') is True and row.get('board')=='p4xc', 'approved P4 clean-band decision required')
    require(row.get('excluded_hz')==[500] and row.get('rule',{}).get('id')==SNR_RULE and
            row['rule'].get('min_chain_snr_db')==10, 'decision exclusions/threshold differ')
    source = Path(row['source_acoustic']); run_source = source.parent/'result.json'
    require(row['source_acoustic_sha256']==digest(source) and row['source_result_sha256']==digest(run_source),
            'acoustic reanalysis input hash mismatch')
    original = json.loads(source.read_text()); run = json.loads(run_source.read_text()); clean_run(run)
    require(run.get('board')=='p4xc' and run.get('acquisition_passed') is True and
            original.get('acquisition_passed') is True and not original.get('error'), 'clean original P4 acquisition required')
    require(run.get('elf_sha256')==manifest['elf_sha256']==row.get('source_elf_sha256'),
            'reanalysis must use this P4 bundle ELF')
    require(row['source_plan_sha256']==original['plan_sha256'], 'reanalysis plan hash differs')
    retained = sorted({p['hz'] for p in original['points']} - {500})
    amplitudes = sorted({p['amplitude_percent'] for p in original['points']})
    doubling = {c:[b['chains'][c]['fundamental_dbfs']-a['chains'][c]['fundamental_dbfs']
        for a,b in zip(original['linearity'],original['linearity'][1:])] for c in ('board','jabra')}
    expected = snr_band(original['points'],doubling,retained,amplitudes,10)
    require(row.get('retained_hz')==retained and all(row.get(key)==value for key,value in expected.items()),
            'reanalysis result differs from the approved rule')
    require(bool(row['clean_amplitudes_percent']) and bool(row['level_band_dbfs']),
            'non-empty reanalyzed P4 clean band required before voice smoke')
    return dict(reanalysis_file=str(path.resolve()),reanalysis_sha256=digest(path),decision_id=DECISION_ID,
        post_hoc=True,result_sha256=digest(run_source),acoustic_sha256=digest(source),restore=run['restore'],
        clean_amplitudes_percent=row['clean_amplitudes_percent'],level_band_dbfs=row['level_band_dbfs'])


class VoiceIO(LiveIO):
    def boot(self, board):
        at = super().boot(board)
        if self.args.board=='p4xc': self.boot_audio = verify_audio(board,self.manifest)
        return at


def voice_smoke(args):
    from board_smoke import execute
    manifest, _ = verify_bundle(args.bundle,args.board)
    require(manifest['profile']=='normal' and not manifest['aec_enabled'], 'normal AEC-off voice bundle required')
    sweep = None
    require(args.board=='p4xc' or getattr(args,'acoustic_reanalysis',None) is None, 'acoustic reanalysis decision is P4 only')
    if args.board=='p4xc':
        audio_settings(manifest)
        reanalysis = getattr(args,'acoustic_reanalysis',None)
        require(bool(args.acoustic_run) != bool(reanalysis), '--acoustic-run or --acoustic-reanalysis required before P4 voice smoke')
        sweep = reanalysis_gate(reanalysis,manifest) if reanalysis else sweep_gate(args.acoustic_run,manifest)
    identity = json.loads(args.bot_identity.read_text())
    io = VoiceIO(args,args.out,manifest,identity)
    result = execute(args,backend=io)
    result.update(acoustic_sweep=sweep,boot_audio=getattr(io,'boot_audio',None),helper_sha256=digest(Path(__file__)))
    write(args.out/'result.json',result)
    return 0 if result['acquisition_passed'] and not result.get('error') else 1


def product_pair(left, right, out, bounds_path=BOUNDS_FILE):
    """Apply FIXED bounds even when board_smoke acquisition used --calibration.

    --headless controls acquisition's UI only. Reconstruct each capture with
    mandatory product DEMO_CAPABILITY and compare through the frozen checker.
    """
    require(not out.exists(), 'new offline pair output required')
    sources = []
    identities = []
    for directory, board, target in ((left,'echoear','esp32s3'),(right,'p4xc','esp32p4')):
        result = json.loads((directory/'result.json').read_text())
        require(result['acquisition_passed'] is True and result['board']==board and result['variant']=='normal',
                'successful normal AEC-off acquisition required')
        clean_run(result)
        identity = json.loads((directory/'identity.json').read_text())
        require(identity==result['identity'], 'acquisition identity changed')
        identities.append(identity)
        serial, bot = directory/'main-serial.log', directory/'main-bot.log'
        lines = serial.read_text().splitlines()
        from board_smoke import slice_sessions
        sessions = slice_sessions(lines); require(len(sessions)==1, 'one closed session required')
        lines = sessions[0]+[line for line in bot.read_text().splitlines() if 'DEMO_CAPABILITY ' in line]
        sources.append((parse_capture(lines), target, dict(result_sha256=digest(directory/'result.json'),
            serial_sha256=digest(serial), bot_sha256=digest(bot), identity_sha256=digest(directory/'identity.json'),restore=result['restore'])))
    bounds = bounds_for(bounds_path, 'normal')
    a,b = sources
    # compare() validates required product evidence, exact schema and opus_enc,
    # identity, both interval histograms, coverage and paired p99.
    comparison = compare(a[0], b[0], profile='product', left_aec=False, right_aec=False,
                         left_target=a[1], right_target=b[1], bounds=bounds,
                         left_identity=identities[0], right_identity=identities[1])
    result = dict(checks_passed=True, qualification=False, profile='product',
        bounds_sha256=digest(bounds_path), sources=[a[2],b[2]], comparison=comparison,
        measured=[measured(row[0].intervals[0], bounds) for row in sources])
    with out.open('x') as stream: stream.write(json.dumps(result, indent=2)+'\n')
    return result


def session_costs(directory, out):
    """Report existing live Opus instrumentation; never run standalone codec-bench."""
    require(not out.exists(), 'new cost output required')
    run = json.loads((directory/'result.json').read_text())
    require(run['board']=='p4xc' and run['variant']=='normal' and run['acquisition_passed'] is True,
            'successful P4 normal smoke required')
    clean_run(run)
    paths = sorted(directory.glob('perf-*.json'))
    samples = [json.loads(path.read_text()) for path in paths]
    require(len(samples)>=2, 'before/after live PERF required')
    final = max(samples, key=lambda row:row['encode_frames'])
    require(final['encode_frames']>=500 and final['speech_decode_frames']>=100, 'insufficient live codec work')
    require(final['uplink_frame_ms']==20, '20 ms Opus required')
    for key in ('encode_p50_us','encode_p95_us','encode_max_us','decode_p50_us','decode_p95_us',
                'decode_max_us','speech_decode_p50_us','speech_decode_p95_us'):
        require(type(final[key]) is int and final[key]>0, 'missing live codec timing '+key)
    require(final['decode_errors']==0, 'live decoder errors')
    for key in ('tx_stack','decode_stack','owner_stack','rx_stack'):
        require(type(final[key]) is int and final[key]>=4096, 'free stack below 4096: '+key)
    from board_smoke import resource_check
    resources = resource_check(samples, 'p4xc')
    keys = [key for key in final if any(word in key for word in ('encode','decode','stack','per_core_idle'))]
    result = dict(checks_passed=True, qualification=False, units='microseconds (live wall cost)',
        basis='PERF histograms from real session with WiFi; p50/p95 quantized by codec_histogram_bucket_us; maxima exact; no p99 claim',
        result_sha256=digest(directory/'result.json'), sources={path.name:digest(path) for path in paths},
        resources=resources, restore=run['restore'], generation=final['generation'], metrics={key:final[key] for key in keys},
        codec_histogram_bucket_us=final['codec_histogram_bucket_us'])
    with out.open('x') as stream: stream.write(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('console-check','acoustic'):
        p = sub.add_parser(action)
        p.add_argument('--board', choices=('echoear','p4xc'), required=True)
        p.add_argument('--bundle', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
        p.add_argument('--p4-factory-backup', type=Path, default=FACTORY_BACKUP)
        p.set_defaults(reference_only=False, p4_restore_bundle=None, p4_restore_elf=None)
    p = sub.add_parser('pair')
    p.add_argument('left', type=Path); p.add_argument('right', type=Path)
    p.add_argument('--out', type=Path, required=True); p.add_argument('--bounds', type=Path, default=BOUNDS_FILE)
    p = sub.add_parser('costs')
    p.add_argument('directory', type=Path); p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('voice-smoke')
    p.add_argument('--board',choices=('echoear','p4xc'),required=True)
    for name in ('bundle','out','bot-dir','bot-identity'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    eligibility = p.add_mutually_exclusive_group()
    eligibility.add_argument('--acoustic-run',type=Path)
    eligibility.add_argument('--acoustic-reanalysis',type=Path)
    p.add_argument('--pair-id',required=True)
    p.add_argument('--headless',action='store_true',required=True)
    p.add_argument('--calibration',action='store_true',required=True)
    p.add_argument('--p4-factory-backup',type=Path,default=FACTORY_BACKUP)
    p.set_defaults(reference_only=False,p4_restore_bundle=None,p4_restore_elf=None,
                   bounds=BOUNDS_FILE,coverage=.95,coverage_decision=None)
    p = sub.add_parser('reanalyze')
    p.add_argument('directory',type=Path); p.add_argument('--exclude-hz',type=int,nargs='+',required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--min-chain-snr-db',type=float)
    args = parser.parse_args()
    if args.action=='reanalyze':
        print(json.dumps(reanalyze(args.directory,args.exclude_hz,args.out,args.min_chain_snr_db),indent=2)); return 0
    if args.action=='voice-smoke': return voice_smoke(args)
    if args.action=='costs':
        print(json.dumps(session_costs(args.directory,args.out),indent=2)); return 0
    if args.action=='pair':
        print(json.dumps(product_pair(args.left,args.right,args.out,args.bounds),indent=2)); return 0
    return live(args)


if __name__=='__main__': raise SystemExit(main())

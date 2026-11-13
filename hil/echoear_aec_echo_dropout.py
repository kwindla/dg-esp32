"""Prepare/analyze the exploratory echo dropout diagnostic; run is explicit.

No import, prepare, validate, or analyze action opens hardware or contacts TTS.
The run subcommand is for the coordinator AFTER review, never part of prep.
"""
import argparse
import copy
import json
from pathlib import Path
import signal
import shlex
import subprocess
import time

import numpy as np
from echoear_aec_bench import (ROOT, NORMAL, NORMAL_HASH, load_capture, sha,
                               verify_bundle, write)
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45 as NORMAL, HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore
from echoear_aec_metrics import (RATE, FRAME, AFE_DELAY, activity, frame_power,
                                 reduction, reference_threshold)

CANDIDATE = '61aa38d58559abe87bc54397fa8df0cec2411c1d37070d809b98d9d4e3b17242'
TARGET = 'A friendly baker opens the shop before sunrise. The fresh loaves cool beside the open window.'
CONTROLS = (
    'The red kite rises above the orchard while the children count the clouds. A small dog watches from the garden.',
    'Please place the silver spoon beside the green cup. The breakfast table is ready for our guests.',
)
# Active work includes candidate flash, baseline, connection and final audit.
ACTIVE_SECONDS = 840
RESTORE_RESERVE_SECONDS = 360
CELL_RESERVE_SECONDS = 210  # 28 capture + 150 export + boundary/audit allowance
ESPTOOL_PYTHON = '/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python'


def plan_from(source):
    p = json.loads(Path(source).read_text())
    if p['elf_sha256'] != CANDIDATE or (p['gain_db'], p['boot_ref_gain_db']) != (15, 15):
        raise ValueError('Expected retained 61aa38d5 / 15 dB source declaration')
    keep = ('image', 'elf_sha256', 'gain_db', 'boot_ref_gain_db', 'bot_config',
            'noise_gate', 'preflight_contract', 'host_environment',
            'first_hold_periodicity', 'gates', 'v85', 'fixture_policy', 'failure_policy')
    result = {key: copy.deepcopy(p[key]) for key in keep}
    cells = []
    # First baker is cold; others warm. Controls interleave after baker 4 and 8.
    for i in range(1, 9):
        cells.append(dict(id=f'dropout-baker-{i:02d}', role='target', text=TARGET))
        if i in (4, 8):
            index = i//4
            cells.append(dict(id=f'dropout-control-{index:02d}', role='control', text=CONTROLS[index-1]))
    for i, cell in enumerate(cells):
        cell.update(kind='echo', volume_db=-6, guard_ms=400, stop_offset_s=None,
                    quick_gap_s=None, session_id='qualification-01',
                    aec_state='cold-boot' if i == 0 else 'warm', capture_group=cell['id'])
    result.update(schema='echo-dropout-prep-v1', status='PREPARED; coordinator launch only after review',
                  qualification=False, source=str(Path(source).resolve()), source_sha256=sha(source),
                  sessions=[dict(id='qualification-01', volume_db=-6)], cases=cells,
                  reconnects=[], controls=[], spares=[], attempts_per_cell=1,
                  time_estimate=dict(v85_budget=dict(rtp_evidence_max_packets=200000)),
                  budget=dict(active_s=ACTIVE_SECONDS, restore_reserve_s=RESTORE_RESERVE_SECONDS,
                              total_s=1200, minimum_cell_remaining_s=CELL_RESERVE_SECONDS),
                  retry_policy='None; noise, acquisition, vendor or integrity failure ends hold; retain all attempts',
                  restore=dict(image=str(NORMAL), elf_sha256=NORMAL_HASH, mixer_equal_required=True),
                  analysis=dict(delay_samples=AFE_DELAY, frame_samples=FRAME,
                                selection='frozen MIC3 activity against separate settled quiet; no AFE selection',
                                worst_five='largest five active-frame AFE energies, not necessarily contiguous; descriptive only'))
    return result


def runtime_files():
    paths = subprocess.check_output(['git', 'ls-files', 'hil', 'server', 'main/demo_assets.h'],
                                    cwd=ROOT, text=True).splitlines()
    paths.append('hil/echoear_aec_echo_dropout.py')
    return sorted({p for p in paths if p.endswith('.py') or p.endswith('uv.lock') or p == 'main/demo_assets.h'})


def launcher(plan_path, run_out):
    """Generate the review-only launcher, including flash's separate interpreter."""
    return '\n'.join([
        '#!/usr/bin/env bash',
        '# PREP ONLY. Coordinator may execute after review. Never auto-launch.',
        'set -euo pipefail',
        ': "${ECHO_DROPOUT_REVIEWED_PLAN_SHA256:?Set the reviewed plan hash only after review}"',
        'export ESPTOOL_PYTHON='+shlex.quote(ESPTOOL_PYTHON),
        'cd '+shlex.quote(str(ROOT)),
        'exec env OPENBLAS_NUM_THREADS=1 PYTHONPATH=hil:server uv run --project server --frozen --offline '
        '--with pyserial --with pyelftools --with idf-component-manager==3.1.2 '
        'python hil/echoear_aec_echo_dropout.py run '
        '--plan '+shlex.quote(str(Path(plan_path).resolve()))+' '
        '--out '+shlex.quote(str(Path(run_out).resolve()))+' '
        '--reviewed-plan-sha256 "$ECHO_DROPOUT_REVIEWED_PLAN_SHA256"',
        '',
    ])


def prepare(source, out, *, run_out=None):
    p = plan_from(source)
    p['runtime_sha256'] = {name: sha(ROOT/name) for name in runtime_files()}
    p['prepared_from_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    validate(p)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    write(out/'plan.json', p)
    if run_out is not None:
        (out/'launch-after-review.sh').write_text(launcher(out/'plan.json', run_out))
    return p


def validate(p):
    expected = plan_from(p['source'])
    if {k: v for k, v in p.items() if k not in ('runtime_sha256', 'prepared_from_commit')} != expected:
        raise ValueError('Diagnostic plan differs from fixed schedule or retained configuration')
    if set(p['runtime_sha256']) != set(runtime_files()):
        raise ValueError('Runtime file set changed since preparation')
    for name, wanted in p['runtime_sha256'].items():
        if sha(ROOT/name) != wanted:
            raise ValueError('Prepared runtime changed: '+name)
    verify_bundle(Path(p['image']), CANDIDATE, 15)
    verify_bundle(NORMAL, NORMAL_HASH)
    return True


def frame_analysis(raw, afe, ref, quiet_ref):
    """Frozen activity/reduction; fixed 1024 shift, never pad or search delay."""
    if len({len(raw), len(afe), len(ref)}) != 1 or len(raw) <= AFE_DELAY+FRAME:
        raise ValueError('Incomplete or unequal PCM channels')
    mask = activity(ref, quiet_ref, [(0, len(ref))])
    whole = reduction(raw, afe, mask)
    # Do not hide reference activity in the final delayed-output-uncovered tail.
    n = (len(afe)-AFE_DELAY)//FRAME
    rp, fp = frame_power(raw)[:n], frame_power(ref)[:n]
    ap = frame_power(afe[AFE_DELAY:])[:n]
    indices = np.flatnonzero(mask[:n])
    worst = sorted(indices, key=lambda i: (-ap[i], i))[:5]
    denom = float(ap[indices].sum())
    fraction = float(ap[worst].sum()/denom) if denom and whole['scorable'] else None
    without = mask.copy()
    without[worst] = False
    def level(p): return float(10*np.log10(max(float(p), 1e-20)/32768**2))
    frames = [dict(frame=i, raw_start_sample=i*FRAME, time_s=i*FRAME/RATE,
                   afe_start_sample=i*FRAME+AFE_DELAY, active=bool(mask[i]),
                   mic1_dbfs=level(rp[i]), mic3_dbfs=level(fp[i]), afe_dbfs=level(ap[i]),
                   reduction_db=float(10*np.log10(max(rp[i], 1e-20)/max(ap[i], 1e-20))),
                   residual_energy=float(ap[i]*FRAME), worst_five=i in worst) for i in range(n)]
    return dict(qualification=False, definitions='echoear-aec-metrics-v1',
                delay_samples=AFE_DELAY, frame_samples=FRAME,
                reference_threshold_power=reference_threshold(quiet_ref), whole=whole,
                active_without_delayed_output=int(mask[n:].sum()),
                worst_five_frames=[int(i) for i in worst], worst_five_share=fraction,
                without_worst_five_report_only=reduction(raw, afe, without), frames=frames)


def analyze_capture(capture, quiet):
    capture, quiet = Path(capture), Path(quiet)
    before = {str(p): sha(p) for p in (capture, quiet)}
    raw, afe, ref, timing = load_capture(capture)
    _, _, qref, _ = load_capture(quiet)
    value = frame_analysis(raw, afe, ref, qref)
    value.update(inputs_sha256=before, gap_blocks=int(sum(row[4] for row in timing)),
                 timing_rows=len(timing), samples=len(raw))
    if value['gap_blocks'] or not len(timing):
        invalid = dict(scorable=False, reason='Missing or gapped capture timing')
        value['whole'] = invalid.copy()
        value['without_worst_five_report_only'] = invalid.copy()
        value['worst_five_share'] = None
    if before != {str(p): sha(p) for p in (capture, quiet)}:
        raise RuntimeError('Analysis input changed')
    return value


class BudgetExpired(TimeoutError):
    pass


class ActivityBudget:
    """One-shot deadline; restoration is never killed by a budget or repeat signal."""
    def __enter__(self):
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
            raise RuntimeError('Existing process alarm; cannot own diagnostic deadline')
        self.started = time.monotonic()
        self.old = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}
        def stop(signum, frame):
            raise BudgetExpired('Active budget elapsed' if signum == signal.SIGALRM else 'Operator stop')
        for s in self.old: signal.signal(s, stop)
        signal.setitimer(signal.ITIMER_REAL, ACTIVE_SECONDS)
        return self

    def remaining(self):
        return ACTIVE_SECONDS-(time.monotonic()-self.started)

    def cleanup(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        # A second Ctrl-C/TERM must not interrupt the normal-image restore.
        for s in self.old: signal.signal(s, signal.SIG_IGN)

    def __exit__(self, *exc):
        signal.setitimer(signal.ITIMER_REAL, 0)
        for s, handler in self.old.items(): signal.signal(s, handler)


def diagnostic_hold(budget):
    # Lazy import: prep and analysis do not even import the acquisition adapter.
    from echoear_aec_campaign import (BenchHold, capture_start, capture_stop, clock_anchor,
                                     command, dump_capture, check_capture_timing)
    from echoear_aec_noise_gate import baseline_level, quiet_gate

    class EchoHold(BenchHold):
        def baseline_capture(self):
            # One settled attempt, anchored start/stop, no silent retries.
            folder = self.out/'baseline-1'
            folder.mkdir()
            time.sleep(5)
            anchors = [capture_start(self.board)]
            try:
                until = time.monotonic()+6.1
                while time.monotonic() < until:
                    anchors.append(clock_anchor(self.board))
                    time.sleep(.2)
            finally:
                anchors.append(capture_stop(self.board))
                write(folder/'anchors.json', anchors)
                self.export_capture(folder/'capture.npz')
            raw, _, ref, blocks = load_capture(folder/'capture.npz')
            check_capture_timing(blocks, anchors)
            self.baseline = baseline_level(raw, self.p['noise_gate'])
            gate = quiet_gate(raw, self.baseline, self.p['noise_gate'], mic3=ref)
            gate['passed'] &= self.baseline <= -53
            write(folder/'noise-gate.json', gate)
            if not gate['passed']: raise RuntimeError('No clean settled baseline; no retry')
            (self.out/'quiet.npz').write_bytes((folder/'capture.npz').read_bytes())
            self.qref = ref

        def finish(self):
            # Flash/baseline can fail before evaluation exists. Do not invent a
            # live audit or send health commands to a bot that was never started.
            if not hasattr(self, 'evaluation') or self.bot is None:
                self.result.update(completed=False, final_audit=dict(
                    status='not-started', reason='Evaluation/bot acquisition did not start'))
                self.save()
                return
            return super().finish()

        def __exit__(self, typ, exc, tb):
            budget.cleanup()
            # Also cover interruptions during start/baseline/export, outside the
            # standard capture try/finally. Keep failure evidence, then restore.
            if exc and self.board:
                try:
                    anchor = capture_stop(self.board)
                    write(self.out/'emergency-stop-anchor.json', anchor)
                    command(self.board, 'audio 0')
                except Exception as error:
                    self.result['cleanup_errors'].append('emergency stop: '+str(error))
            try:
                return super().__exit__(typ, exc, tb)
            finally:
                if isinstance(exc, BudgetExpired):
                    self.result.update(error_class='diagnostic-budget-or-operator-stop', completed=False)
                    self.save()
    return EchoHold


def acquire(p, out, budget, hold_type):
    """No scheduler, spares, reconnect, probe, Jabra fixture, or retry path."""
    with hold_type(p, p['sessions'][0], out) as hold:
        hold.connect(p['cases'], [])
        for cell in p['cases']:
            if budget.remaining() < CELL_RESERVE_SECONDS:
                raise BudgetExpired('Insufficient remaining budget to start another cell')
            noise, _ = hold.capture(dict(run_id=cell['id'], cells=[cell]), 1)
            if not noise: raise RuntimeError('Noise-contaminated cell retained; no retry')
        hold.finish()
        if not hold.result.get('completed'): raise RuntimeError('Final integrity audit failed')
    r = hold.result
    if (r.get('restored_elf_sha256') != NORMAL_HASH or r.get('mixer_equal') is not True
            or r.get('lock_released') is not True or r.get('cleanup_errors')):
        raise RuntimeError('Restoration/mixer/lock witness incomplete')


def analyze_hold(p, out):
    out = Path(out)
    summary = []
    for cell in p['cases']:
        folder = out/(cell['id']+'-a1')
        row = dict(id=cell['id'], role=cell['role'], text=cell['text'], status='not-run')
        if folder.exists():
            row['status'] = 'incomplete'
        if (folder/'capture.npz').exists() and (out/'quiet.npz').exists():
            value = analyze_capture(folder/'capture.npz', out/'quiet.npz')
            # Descriptive metrics never substitute for acquisition validity.
            result = json.loads((folder/'result.json').read_text()) if (folder/'result.json').exists() else {}
            own = result.get('cases', [])
            accepted = any(c['run_id'] == cell['id']+'-a1' for c in
                           json.loads((out/'result.json').read_text()).get('cases', []))
            clean = bool(accepted and own and own[0].get('noise_gate_passed')
                         and own[0].get('quiet_windows_valid') and value['whole']['scorable'])
            value['clean_acquisition'] = clean
            write(folder/'dropout-analysis.json', value)
            row.update(status='clean' if clean else 'retained-excluded',
                       whole=value['whole'], worst_five_share=value['worst_five_share'])
        summary.append(row)
    write(out/'dropout-summary.json', dict(qualification=False, replies=summary))


def run(plan_path, out, reviewed_sha):
    if sha(plan_path) != reviewed_sha:
        raise ValueError('Explicit reviewed plan SHA256 required')
    p = json.loads(Path(plan_path).read_text())
    validate(p)  # Read-only file checks before any lock, board or mixer access.
    out = Path(out)
    if out.exists() or not out.parent.is_dir(): raise ValueError('Use a fresh output under an existing parent')
    # Uses the same esptool_python() resolver as flash(), including its fallback.
    # Import-only subprocess: no lock, serial, mixer, output directory or alarm.
    from echoear_aec_accept import check_esptool
    check_esptool()
    started = time.monotonic()
    original = None
    try:
        with ActivityBudget() as budget:
            acquire(p, out, budget, diagnostic_hold(budget))
        if time.monotonic()-started > 1200:
            raise BudgetExpired('Hold exceeded 20 minute planning cap; restoration completed')
    except BaseException as error:
        original = error
        raise
    finally:
        # Analysis happens only after hold cleanup; never prolong bench ownership.
        if out.exists():
            try:
                elapsed = time.monotonic()-started
                write(out/'elapsed.json', dict(seconds=elapsed, planned_limit_s=1200,
                                              within_planned_limit=elapsed <= 1200,
                                              restore_never_interrupted=True))
                analyze_hold(p, out)
            except BaseException as error:
                if original is None: raise
                original.add_note('Post-cleanup analysis failed: '+str(error))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='op', required=True)
    prep = sub.add_parser('prepare'); prep.add_argument('--source', type=Path, required=True); prep.add_argument('--out', type=Path, required=True); prep.add_argument('--run-out', type=Path)
    val = sub.add_parser('validate'); val.add_argument('--plan', type=Path, required=True)
    ana = sub.add_parser('analyze'); ana.add_argument('--capture', type=Path, required=True); ana.add_argument('--quiet', type=Path, required=True); ana.add_argument('--out', type=Path, required=True)
    live = sub.add_parser('run', help='COORDINATOR ONLY AFTER REVIEW; opens bench')
    live.add_argument('--plan', type=Path, required=True); live.add_argument('--out', type=Path, required=True); live.add_argument('--reviewed-plan-sha256', required=True)
    a = ap.parse_args(argv)
    if a.op == 'prepare': prepare(a.source, a.out, run_out=a.run_out)
    elif a.op == 'validate': validate(json.loads(a.plan.read_text()))
    elif a.op == 'analyze':
        if a.out.exists(): raise ValueError('Analysis output already exists')
        write(a.out, analyze_capture(a.capture, a.quiet))
    else: run(a.plan, a.out, a.reviewed_plan_sha256)


if __name__ == '__main__': main()

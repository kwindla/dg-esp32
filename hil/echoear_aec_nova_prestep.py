"""Prepare/validate/analyze without hardware; only reviewed `run` opens the bench."""
import argparse
import copy
import json
import signal
import shlex
import subprocess
import time
from pathlib import Path
from echoear_aec_bench import ROOT, sha, write, verify_bundle
# gap 3: the step-9 Nova campaign (frozen plans, retained holds) stays bound to its declared 9936da45 restore image.
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45 as NORMAL, HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH
import echoear_aec_echo_dropout as diagnostic
from echoear_aec_pause_fixture import generate, verify as verify_fixture
from freeze_aec_nova_live import BASE, JOBS, installed_package_hashes, GROUPS

SOURCE = BASE/'freeze-v8.6-nova/declaration-v8.6-nova.draft.json'
COORDINATOR = BASE/'freeze-v8.6-nova/coordinator-prestep.md'
FIXTURE = BASE.parent/'step-8/sentences/sentence-04-48k.wav'
OUTPUT = BASE/'nova-prestep-01'
REVIEW_ENV = 'NOVA_PRESTEP_REVIEWED_PLAN_SHA256'
BUDGET = dict(active_s=840, restore_reserve_s=360, total_s=1200, minimum_cell_remaining_s=210)
MAIN_BUDGET = dict(active_s=3240, restore_reserve_s=360, total_s=3600, minimum_cell_remaining_s=210)


def plan_from(source, fixture_manifest, output):
    from demo_policy import profile_manifest
    source = Path(source).resolve()
    original = json.loads(source.read_text())
    fixture = verify_fixture(fixture_manifest)
    p = diagnostic.plan_from(source)
    def choose(predicate):
        return copy.deepcopy(next(c for c in original['cases'] if predicate(c)))
    cells = [choose(lambda c, v=v: c['kind']=='echo' and c['volume_db']==v and c['id'].endswith('echo-01')) for v in (-6, 0)]
    cells += [choose(lambda c: c['kind']=='stop' and c.get('voice_class')=='firm'),
              choose(lambda c: c.get('fixture_id')=='sentence-01-calibrated'),
              choose(lambda c: c.get('fixture_id')=='sentence-04-normal')]
    for c, offset in zip(cells[2:], (.9, 1.2, 1.5)): c['stop_offset_s'] = offset
    cells.append(dict(kind='soak-near', volume_db=-6, guard_ms=400, stop_offset_s=None,
        text='', real_bot_reply=True, voice_class='firm', fixture_id='sentence-04-pause-700ms',
        fixture=fixture['output'], fixture_sha256=fixture['output_sha256'],
        expected_text=fixture['expected_text'], initially_silent=True))
    names = ('echo-minus6', 'echo-zero', 'firm-stop', 'firm-sentence01', 'normal-sentence04', 'paused-sentence04')
    for i, (c, name) in enumerate(zip(cells, names)):
        c.update(id='nova-prestep-'+name, session_id='qualification-01', capture_group='nova-prestep-'+name,
                 quick_gap_s=None, aec_state='cold-boot' if i==0 else 'warm', report_only=True)
    p.pop('analysis', None)
    p.update(schema='nova-prestep-v1', declaration_id='step-9-nova-prestep-01',
        status='PREPARED; coordinator review/go required', qualification=False,
        output=str(Path(output).resolve()), cases=cells, budget=BUDGET.copy(),
        sessions=[dict(id='qualification-01', volume_db=-6)], bot_port=7861,
        fixture_manifest=str(Path(fixture_manifest).resolve()), fixture_manifest_sha256=sha(fixture_manifest),
        profile=profile_manifest('dev'), vendor=original['nova_live']['vendor_model'],
        retained_profile_config_sha256=original['nova_live']['stt_profile']['config_sha256'],
        transcript_wait=dict(seconds=1.0, measured_p99=False), rf_pre_gate_s=5,
        main_session=dict(launchable=False, accepted_estimate_minutes=original['time_estimate']['nominal_minutes'],
            budget=MAIN_BUDGET.copy(), group_denominators=[dict(voice=v, kind=k, eligible=10, minimum_successes=9, spares=n) for v,k,n in GROUPS],
            failure_policy=original['failure_policy'], gates=original['gates'],
            incomplete_at_cap='Stop acquisition, restore; never pass or reduce denominators',
            spares='Only declared retake/opportunity conditions; never score-selected'),
        measurement_policy=dict(acceptance_credit=False, statistical_summary=['count','median','maximum'],
            population_p99=False, source_metric='initial active span >=4 s, <=100 ms silence bridge; unchanged',
            missing_witness='incomplete; never substitute old Flux timing',
            regression='Exactly one full accepted turn and one LLM request; no prefix/suffix reply',
            capture_cap_s=28, quiet_tail_s=2.3, retries=0))
    p['bot_config']['ECHOEAR_STT_PROFILE'] = 'dev'
    return p


def runtime_files():
    return sorted(set(diagnostic.runtime_files()) | {
        str(f.relative_to(ROOT)) for pattern in ('hil/*.py','server/*.py','hil/plans/*.json') for f in ROOT.glob(pattern)
    } | {'server/pyproject.toml','docs/echoear-aec-nova-prestep.md'})


def launcher(plan, output):
    plan = Path(plan).resolve()
    digest = sha(plan)
    q = shlex.quote
    return '\n'.join([
        '#!/usr/bin/env bash', '# PREP ONLY. Coordinator review/go required.', 'set -euo pipefail',
        ': "${'+REVIEW_ENV+':?Set only after review}"',
        'expected='+q(digest), 'plan='+q(str(plan)),
        '[[ "$'+REVIEW_ENV+'" == "$expected" ]] || { echo "Unreviewed plan hash" >&2; exit 2; }',
        'actual=$(sha256sum -- "$plan")',
        '[[ "${actual%% *}" == "$expected" ]] || { echo "Plan bytes changed" >&2; exit 2; }',
        'export ESPTOOL_PYTHON='+q(diagnostic.ESPTOOL_PYTHON), 'cd '+q(str(ROOT)),
        'exec env OPENBLAS_NUM_THREADS=1 PYTHONPATH=hil:server uv run --project server --frozen --offline '
        '--with pyserial --with pyelftools --with idf-component-manager==3.1.2 '
        'python hil/echoear_aec_nova_prestep.py run --plan "$plan" --out '+q(str(Path(output).resolve()))+
        ' --reviewed-plan-sha256 "$'+REVIEW_ENV+'"', ''])


def prepare(destination, output=OUTPUT):
    destination, output = Path(destination).resolve(), Path(output).resolve()
    if destination.exists() or destination.is_symlink() or output.exists() or output.is_symlink():
        raise FileExistsError('Fresh prep directory and absent run output required')
    if output != OUTPUT: raise ValueError('Only declared nova-prestep-01 output allowed')
    if subprocess.check_output(['git','status','--porcelain'], cwd=ROOT, text=True):
        raise ValueError('Commit preparation source first')
    from demo_policy import profile_manifest
    original = json.loads(SOURCE.read_text())
    current = profile_manifest('dev')
    retained = original['nova_live']['stt_profile']
    if any(current[k] != retained[k] for k in ('config','versions','local_model_sha256')):
        raise ValueError('Reviewed profile settings/packages/models changed')
    packages = installed_package_hashes()
    if {k:v['tree_sha256'] for k,v in packages.items()} != original['nova_live']['installed_packages_sha256']:
        raise ValueError('Installed package implementations changed')
    verify_bundle(Path(original['image']), diagnostic.CANDIDATE, 15)
    verify_bundle(NORMAL, NORMAL_HASH)
    destination.mkdir(parents=True)
    manifest = destination/'pause-fixture-manifest.json'
    generate(FIXTURE, destination/'sentence-04-pause-700ms-48k.wav', manifest)
    p = plan_from(SOURCE, manifest, output)
    inputs = (SOURCE, COORDINATOR, JOBS/'step-9-nova-prestep-request.md', JOBS/'step-9-user-decisions-2026-10-18-c.md')
    p.update(inputs_sha256={str(f):sha(f) for f in inputs},
             runtime_sha256={f:sha(ROOT/f) for f in runtime_files()},
             prepared_from_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
             installed_packages_sha256={k:v['tree_sha256'] for k,v in packages.items()})
    write(destination/'plan.json', p)
    write(destination/'installed-package-hashes.json', packages)
    write(destination/'main-session-duration-contract.json', p['main_session'])
    script = destination/'launch-after-review.sh'
    script.write_text(launcher(destination/'plan.json', output)); script.chmod(0o755)
    subprocess.run(['bash','-n',str(script)], check=True)
    validate(p)
    digest = sha(destination/'plan.json')
    write(destination/'validation.json', dict(valid=True, run_executed=False, output_absent=not output.exists(), plan_sha256=digest))
    command = REVIEW_ENV+'='+digest+' bash '+shlex.quote(str(script))
    (destination/'COORDINATOR.md').write_text('# Prepared only — not executed\n\nAfter coordinator review/go, exact command:\n\n```bash\n'+command+'\n```\n\n'
        'No outer flock. The Python runner owns the lock. Output must remain absent before launch: `'+str(output)+'`.\n\n'
        'Stop: find the Python process whose complete argv matches `hil/echoear_aec_nova_prestep.py run`, this plan, this output and this reviewed hash; send TERM to that Python PID only. Never signal uv/nohup, SIGKILL, or force release the lock.\n\n'
        'Wait for restored ELF '+NORMAL_HASH+', mixer_equal=true, lock_released=true and no cleanup_errors. Restore or mixer failure parks with the lock.\n\n'
        'The pre-step budget is 20 minutes (14 active + 6 restore reserve); the separate main-session contract is 60 minutes (54 + 6). Expiry cannot pass. The main Nova freeze remains blocked pending fresh measurements, integration and separate review/go.\n')
    return p


def validate(p):
    if p.get('schema') == 'nova-prestep-completion-v1':
        from echoear_aec_nova_completion import validate as validate_completion
        return validate_completion(p)
    expected = plan_from(p['source'], p['fixture_manifest'], p['output'])
    extra = {'inputs_sha256','runtime_sha256','prepared_from_commit','installed_packages_sha256'}
    if {k:v for k,v in p.items() if k not in extra} != expected:
        raise ValueError('Frozen six-capture schedule or contract changed')
    if Path(p['output']) != OUTPUT: raise ValueError('Undeclared output')
    return validate_bindings(p)


def validate_bindings(p):
    if set(p['runtime_sha256']) != set(runtime_files()): raise ValueError('Runtime file set changed')
    for name, digest in p['runtime_sha256'].items():
        if sha(ROOT/name) != digest: raise ValueError('Runtime changed: '+name)
    for name, digest in p['inputs_sha256'].items():
        if sha(name) != digest: raise ValueError('Input changed: '+name)
    for c in p['cases']:
        if c.get('fixture') and sha(c['fixture']) != c['fixture_sha256']: raise ValueError('Stimulus changed')
    from demo_policy import profile_manifest
    if profile_manifest('dev') != p['profile']: raise ValueError('Profile/config/model/version changed')
    if {k:v['tree_sha256'] for k,v in installed_package_hashes().items()} != p['installed_packages_sha256']:
        raise ValueError('Installed implementation changed')
    verify_bundle(Path(p['image']), diagnostic.CANDIDATE, 15); verify_bundle(NORMAL, NORMAL_HASH)
    return True


class Budget(diagnostic.ActivityBudget):
    """Active alarm reserves cleanup time. Total alarm marks failure without killing restore."""
    def __init__(self, contract=BUDGET):
        self.contract = contract; self.total_expired = False

    def __enter__(self):
        super().__enter__()
        signal.setitimer(signal.ITIMER_REAL, self.contract['active_s'])
        return self

    def remaining(self):
        return self.contract['active_s']-(time.monotonic()-self.started)

    def cleanup(self):
        super().cleanup()
        remaining = self.contract['total_s']-(time.monotonic()-self.started)
        if remaining <= 0: self.total_expired = True
        def expired(signum, frame): self.total_expired = True
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(.001, remaining))


def profile_witness(p, events, require_vendor=True):
    profiles = [e for e in events if e.get('event')=='stt-profile']
    vendors = [e for e in events if e.get('event')=='stt-model-observed']
    profile_ok = bool(profiles) and all(all(e.get(k)==v for k,v in p['profile'].items()) for e in profiles)
    vendor_ok = True
    for e in vendors:
        infos = e.get('model_info', {})
        expected = p['vendor']
        # Streaming Results carry flat model_info + model_uuid; batch/metadata
        # schemas may carry a UUID-keyed map. Bind both shapes strictly.
        if 'model_uuid' in e:
            vendor_ok &= (e['model_uuid']==expected['model_uuid'] and
                infos.get('name')==expected['name'] and infos.get('version')==expected['version'])
        else:
            vendor_ok &= (e.get('models')==[expected['model_uuid']] and
                infos.get(expected['model_uuid'], {}).get('name')==expected['name'] and
                infos.get(expected['model_uuid'], {}).get('version')==expected['version'])
    return dict(passed=bool(profile_ok and vendor_ok and (vendors or not require_vendor)),
                profile_count=len(profiles), vendor_count=len(vendors), profile_matches=profile_ok,
                vendor_matches=vendor_ok, expected_vendor=p['vendor'])


def hold_type(budget):
    from echoear_aec_campaign import BenchHold
    class NovaHold(diagnostic.diagnostic_hold(budget)):
        rf_pre_gate_seconds = 5
        require_clean_cleanup = True

        def export_capture(self, path, *, timeout=150):
            remaining = budget.remaining()-1
            started = time.monotonic()
            try:
                if remaining <= 0: raise diagnostic.BudgetExpired('No active time left for export; restore now')
                return super().export_capture(path, timeout=min(timeout, remaining))
            finally:
                write(Path(path).with_name('export-duration.json'), dict(start_s=started, end_s=time.monotonic(),
                    elapsed_s=time.monotonic()-started, active_remaining_s=budget.remaining()))

        def connect(self, cells, reconnects):
            super().connect(cells, reconnects)
            proof = profile_witness(self.p, self.events(), require_vendor=False)
            write(self.out/'connected-profile.json', proof)
            if not proof['passed']: raise RuntimeError('Unexpected live Nova profile/vendor')

        def capture(self, *args, **kwargs):
            value = super().capture(*args, **kwargs)
            proof = profile_witness(self.p, self.events())
            write(self.out/'latest-profile.json', proof)
            if not proof['passed']: raise RuntimeError('Missing or changed Nova profile/vendor')
            return value

        def finish(self):
            if self.result.get('error'):
                self.result['completed'] = False; self.save(); return
            if hasattr(self, 'evaluation') and self.bot:
                proof = profile_witness(self.p, self.events())
                write(self.out/'final-profile.json', proof)
                if not proof['passed']: raise RuntimeError('Incomplete live Nova binding')
            return super().finish()

        def __exit__(self, typ, exc, tb):
            try: return super().__exit__(typ, exc, tb)
            finally:
                if exc or budget.total_expired:
                    self.result['completed'] = False
                    self.result['budget_expired'] = budget.total_expired or isinstance(exc, diagnostic.BudgetExpired)
                    self.save()
    return NovaHold


def run(plan, out, reviewed_sha):
    if sha(plan) != reviewed_sha: raise ValueError('Explicit reviewed plan SHA256 required')
    p = json.loads(Path(plan).read_text()); validate(p)
    out = Path(out).resolve()
    if str(out) != p['output'] or out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError('Fresh declared output required')
    from echoear_aec_accept import check_esptool
    check_esptool()  # Before alarm, output, lock, NIC queries or hardware.
    from echoear_aec_host_gate import prelaunch
    if not prelaunch(p['host_environment'])['passed']: raise RuntimeError('Active UUID/BSSID prelaunch guard failed')
    started = time.monotonic(); original = None
    try:
        with Budget(p['budget']) as budget:
            diagnostic.acquire(p, out, budget, hold_type(budget))
        if budget.total_expired or time.monotonic()-started > p['budget']['total_s']:
            raise diagnostic.BudgetExpired('Total cap elapsed; restored, no pass')
    except BaseException as error:
        original = error; raise
    finally:
        if out.exists():
            write(out/'elapsed.json', dict(seconds=time.monotonic()-started, total_s=p['budget']['total_s'],
                completed=original is None, error=str(original) if original else None, restore_never_interrupted=True))
            try:
                from echoear_aec_nova_preflight import analyze
                write(out/'nova-preflight.json', analyze(p, out))
                if p.get('schema') == 'nova-prestep-completion-v1':
                    from echoear_aec_nova_completion import combine
                    write(out/'combined-nova-preflight.json', combine(p, out))
            except BaseException as error:
                if original is None: raise
                original.add_note('Post-cleanup analysis failed: '+str(error))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='op', required=True)
    prep = subs.add_parser('prepare'); prep.add_argument('--destination', type=Path, required=True)
    for op in ('validate','run','analyze'):
        sub = subs.add_parser(op); sub.add_argument('--plan',type=Path,required=True)
        if op!='validate': sub.add_argument('--out',type=Path,required=True)
        if op=='run': sub.add_argument('--reviewed-plan-sha256',required=True)
    args = parser.parse_args(argv)
    if args.op=='prepare': prepare(args.destination)
    elif args.op=='validate': validate(json.loads(args.plan.read_text()))
    elif args.op=='run': run(args.plan,args.out,args.reviewed_plan_sha256)
    else:
        from echoear_aec_nova_preflight import analyze
        print(json.dumps(analyze(json.loads(args.plan.read_text()),args.out),indent=2))


if __name__=='__main__': main()

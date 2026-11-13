#!/usr/bin/env python3
"""Offline five-image capture. Entry point owns the global build lock throughout.

AEC demo uses the frozen candidate builder. AEC normal uses its documented
build/accept recipe with a late autostart=n overlay, because the frozen builder
only produces autostart=y. Config contents never leave ignored build paths.
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

_config_spec = importlib.util.spec_from_file_location('baseline_keydiff', Path(__file__).with_name('sdkconfig_keydiff.py'))
_config = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_config)

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path.home()/'src/evidence/echoear/p4/baselines'
VARIANTS = ('normal', 'demo', 'aec-normal', 'aec-demo', 'waveshare_146')
AEC_SHA = '670639c3d136c7e3909aa90bcaac573600b7e98b6850be8fd5484b4cdea96dcc'
CONFIG_POLICY = 'fresh-defaults-v1'


def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical_config_sha(config):
    # Reuse the names-only tool's parser/last-assignment semantics. Serialize
    # only in memory and emit ONE aggregate digest, never values or a copy.
    canonical = json.dumps(_config.settings(config), sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()


def write(path, data):
    temp = Path(str(path)+'.tmp'); temp.write_text(json.dumps(data, indent=2)+'\n'); temp.replace(path)


def link_variant(output, variant, destination):
    # A crash after link publication but before the journal write leaves a
    # stale link on resume. Replace links atomically; retain old attempts.
    target = output/variant
    if target.exists() and not target.is_symlink(): raise RuntimeError('Baseline variant path is not a link')
    pending = output/(variant+'.link-tmp')
    pending.unlink(missing_ok=True)
    pending.symlink_to(destination, target_is_directory=True)
    pending.replace(target)


def run(command, log, env=None, valid=(0,)):
    with log.open('w') as stream:
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = child.wait()
            if code not in valid: raise subprocess.CalledProcessError(code, command)
            return code
        except BaseException:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try: child.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL); child.wait()
            raise


@contextmanager
def keydiff_name(config):
    """Temporarily rename the frozen builder's config-accept; never copy it.

    sdkconfig_keydiff deliberately accepts only sdkconfig* filenames. The whole
    matrix holds the build lock, so no other builder observes this rename.
    """
    if config.name.startswith('sdkconfig'):
        yield config; return
    alias = config.parent/'sdkconfig-baseline-aec-demo'
    if alias.exists(): raise RuntimeError('Temporary config alias already exists')
    config.rename(alias)
    try: yield alias
    finally: alias.rename(config)


def snapshot(build, config, destination, normal, aec, demo):
    project = json.loads((build/'project_description.json').read_text())
    if project['target'] != 'esp32s3': raise RuntimeError('Expected ESP32-S3 baseline')
    artifacts = {}
    for name in ('dg-esp32.bin', 'dg-esp32.elf', 'bootloader/bootloader.bin', 'bootloader/bootloader.elf', 'partition_table/partition-table.bin'):
        target = destination/name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build/name, target)
        artifacts[name] = dict(bytes=target.stat().st_size, sha256=sha(target))
    config_hash = sha(config)  # Digest only, no config file in evidence.
    counts = {}
    for key, expected in (('DG_ECHOEAR_AEC', aec), ('DG_DEMO_AUTOSTART', demo)):
        result = subprocess.run(['grep', '-c', f'^CONFIG_{key}=y$', str(config)], capture_output=True, text=True)
        if result.returncode not in (0, 1): raise RuntimeError('Config count failed')
        counts[key] = int(result.stdout)
        if counts[key] != int(expected): raise RuntimeError('Resolved baseline profile mismatch')
    with keydiff_name(config) as named:
        canonical_hash = canonical_config_sha(named)
        run([sys.executable, str(ROOT/'scripts/sdkconfig_keydiff.py'), str(normal), str(named)],
            destination/'keydiff-from-normal.json', valid=(0, 1))
        expected_keys = ('IDF_TARGET,DG_BOARD_WAVESHARE_146' if build.name == 'build-ws146'
                         else 'DG_ECHOEAR_AEC,DG_DEMO_AUTOSTART,DG_BOARD_ECHOEAR')
        run([sys.executable, str(ROOT/'scripts/sdkconfig_keydiff.py'), str(named), '--expect-keys',
             expected_keys], destination/'expected-keys.json')
    return dict(target='esp32s3', build=str(build), config_sha256=config_hash,
                canonical_config_sha256=canonical_hash, config_policy=CONFIG_POLICY,
                profile_enabled_counts=counts, files=artifacts)


def capture_variant(variant, destination, env):
    build = ROOT/'build'; config = ROOT/'sdkconfig'
    aec = variant.startswith('aec-'); demo = variant in ('demo', 'aec-demo')
    env = dict(env, DG_BOARD='echoear', DG_DEMO_AUTOSTART='1' if demo else '0', DG_AEC='0')
    if variant in ('normal', 'demo'):
        if demo: config = build/'sdkconfig-demo'
        config.unlink(missing_ok=True) # Regenerate from the chain, not cached values.
        run([str(ROOT/'scripts/idf.sh'), 'build'], destination/'build.log', env)
    elif variant == 'aec-normal':
        build = ROOT/'build/accept'; config = build/'sdkconfig-aec-normal'
        overlay = ROOT/'build/baseline-aec-normal.defaults'
        overlay.write_text('CONFIG_DG_ECHOEAR_AEC=y\nCONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15\nCONFIG_DG_DEMO_AUTOSTART=n\nCONFIG_DG_WIFI_OBSERVE=y\n')
        config.unlink(missing_ok=True)
        defaults = 'sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear;build/baseline-aec-normal.defaults'
        run(['idf.py', '-B', str(build), '-D', f'SDKCONFIG={config}', '-D', f'SDKCONFIG_DEFAULTS={defaults}', 'build'],
            destination/'build.log', env)
    elif variant == 'aec-demo':
        build = ROOT/'build/accept'; config = ROOT/'build/config-accept'
        config.unlink(missing_ok=True) # The frozen builder also removes this task-owned file.
        # The frozen helper needs numpy via hil's environment. Its idf.py
        # child must still use IDF's own interpreter/dependencies.
        with tempfile.TemporaryDirectory(prefix='baseline-idf-', dir=os.environ['TMPDIR']) as temp:
            shim = Path(temp)/'idf.py'
            shim.write_text('#!/usr/bin/env bash\nexec "$BASELINE_IDF_PYTHON" "$BASELINE_IDF_SCRIPT" "$@"\n'); shim.chmod(0o755)
            child_env = dict(env, PATH=temp+os.pathsep+env['PATH'], BASELINE_IDF_PYTHON=sys.executable,
                             BASELINE_IDF_SCRIPT=str(Path(env['IDF_PATH'])/'tools/idf.py'))
            run(['uv', 'run', '--project', 'hil', '--frozen', '--offline', 'python',
                 'hil/build_echoear_aec_candidate.py', str(destination/'candidate')], destination/'build.log', child_env)
    else:
        build = ROOT/'build-ws146'; config = build/'sdkconfig'
        config.unlink(missing_ok=True)
        env.update(DG_BOARD='waveshare_146', DG_DEMO_AUTOSTART='0')
        run([str(ROOT/'scripts/idf.sh'), '-B', str(build), f'-DSDKCONFIG={config}', 'build'], destination/'build.log', env)
    if aec:
        run([sys.executable, str(ROOT/'scripts/check-aec-image.py'), '--elf', str(build/'dg-esp32.elf'),
             '--archive', str(ROOT/'managed_components/espressif__esp-sr/lib/esp32s3/libesp_audio_processor.a'),
             '--ar', 'xtensa-esp32s3-elf-ar', '--expected-sha256', AEC_SHA], destination/'aec-placement.log', env)
    return snapshot(build, config, destination, ROOT/'sdkconfig', aec, demo)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('step_label'); parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.step_label): parser.error('Safe step label required')
    if os.environ.get('DG_BUILD_LOCK_HELD') != '1' or os.environ.get('DG_BENCH_LOCK_FD') or os.environ.get('DG_SERIAL_LOCK_HELD') == '1':
        raise RuntimeError('Use capture_s3_baselines.sh under only its build lock')
    subprocess.run(['git', 'diff', '--exit-code', '--quiet', 'HEAD', '--', 'dependencies.lock'], cwd=ROOT, check=True)
    initial = sha(ROOT/'dependencies.lock')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    output = EVIDENCE/args.step_label
    output.mkdir(parents=True, exist_ok=args.resume)
    journal = output/'manifest.json'
    if args.resume and journal.exists():
        state = json.loads(journal.read_text())
        if state['commit'] != commit or state['dependencies_sha256'] != initial: raise RuntimeError('Baseline resume identity mismatch')
        if state.get('config_policy') != CONFIG_POLICY: raise RuntimeError('Baseline resume config policy mismatch; start a new label')
    else:
        state = dict(commit=commit, dependencies_sha256=initial, config_policy=CONFIG_POLICY, variants={}, complete=False)
    write(journal, state)
    def interrupted(signum, frame): raise InterruptedError(f'Baseline interrupted by signal {signum}')
    for sig in (signal.SIGTERM, signal.SIGHUP): signal.signal(sig, interrupted)
    env = dict(os.environ, OPENBLAS_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
    for variant in VARIANTS:
        if variant in state['variants']:
            previous = output/variant
            if all(sha(previous/name) == info['sha256'] for name, info in state['variants'][variant]['files'].items()): continue
            raise RuntimeError('Previously captured baseline image changed')
        attempts = output/'attempts'; attempts.mkdir(exist_ok=True)
        number = len(list(attempts.glob(variant+'-*')))+1
        destination = attempts/f'{variant}-{number:03d}'; destination.mkdir()
        lock_record = dict(before=sha(ROOT/'dependencies.lock'))
        failure = None
        try:
            print(f'Building {variant}', flush=True)
            result = capture_variant(variant, destination, env)
        except BaseException as exc:
            failure = exc
            state['error'] = f'{type(exc).__name__}: {exc}'
            try: write(journal, state)
            except BaseException as journal_exc: exc.add_note(f'Failure journal write failed: {journal_exc}')
            raise
        finally:
            try:
                lock_record['after_build'] = sha(ROOT/'dependencies.lock') if (ROOT/'dependencies.lock').exists() else None
                subprocess.run(['git', 'checkout', '--', 'dependencies.lock'], cwd=ROOT, check=True)
                lock_record['restored'] = sha(ROOT/'dependencies.lock')
                write(destination/'dependencies.json', lock_record)
                if lock_record['restored'] != initial: raise RuntimeError('Dependency lock restoration mismatch')
            except BaseException as restore_exc:
                state['restoration_error'] = f'{type(restore_exc).__name__}: {restore_exc}'
                try: write(journal, state)
                except BaseException as journal_exc: restore_exc.add_note(f'Failure journal write failed: {journal_exc}')
                if failure is None: raise
                failure.add_note(f'Dependency lock restoration failed: {restore_exc}')
        result['dependencies'] = lock_record
        write(destination/'metadata.json', result)
        link_variant(output, variant, destination)
        state['variants'][variant] = result; state.pop('error', None); state.pop('restoration_error', None); write(journal, state)
        print(f'Captured {variant}; dependency lock restored', flush=True)
    state['complete'] = True; write(journal, state)
    print(f'Five baselines complete: {output}', flush=True)


if __name__ == '__main__': main()

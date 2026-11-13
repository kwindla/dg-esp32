#!/usr/bin/env python3
"""Freeze an already-built image; never build, flash, or copy private configs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CHIPS = {'echoear': 'esp32s3', 'p4xc': 'esp32p4'}
REFERENCE = {
    '69d5d7642d2c7650a236ada6ac5915a1ba5d3caaf88bcfc697efdab3156e4c07': (True, True),
    '5bbd868adceb8f4d8ff99cfe0e077d5b249a29d29a5aae766b8c61f664939057': (False, True),
    '55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e': (False, False),
}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def local_file(directory, name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Bundle path must be relative: '+str(name))
    result = (Path(directory)/path).resolve()
    if not result.is_relative_to(Path(directory).resolve()):
        raise ValueError('Bundle path escapes directory')
    return result


def flash_files(directory):
    flash = json.loads(local_file(directory, 'flasher_args.json').read_text())
    files = flash.get('flash_files')
    if not isinstance(files, dict) or not files or not isinstance(flash.get('write_flash_args'), list):
        raise ValueError('Missing flash file map/arguments')
    offsets = []
    for offset, name in files.items():
        if not isinstance(offset, str) or not re.fullmatch(r'0x[0-9a-fA-F]+', offset):
            raise ValueError('Invalid flash offset')
        if not isinstance(name, str) or not name.endswith('.bin'):
            raise ValueError('Only explicit binary flash inputs are supported')
        local_file(directory, name)
        offsets.append(int(offset, 16))
    if len(offsets) != len(set(offsets)):
        raise ValueError('Duplicate flash offset')
    # No response files, erase switches, or arbitrary esptool operations.
    allowed = {'--flash-mode', '--flash_mode', '--flash-freq', '--flash_freq', '--flash-size', '--flash_size'}
    args = flash['write_flash_args']
    if len(args) % 2 or any(args[i] not in allowed or not isinstance(args[i+1], str)
                            or not re.fullmatch(r'[A-Za-z0-9_.]+', args[i+1]) for i in range(0, len(args), 2)):
        raise ValueError('Unexpected write-flash arguments')
    return flash


def verify_bundle(directory, board='echoear', reference_only=False):
    directory = Path(directory).resolve()
    manifest = json.loads((directory/'manifest.json').read_text())
    sha = manifest.get('sha256') or {k: v['sha256'] for k, v in manifest.get('files', {}).items()}
    if manifest.get('sha256') and manifest.get('files'):
        if sha != {k: v['sha256'] for k, v in manifest['files'].items()}:
            raise ValueError('Bundle files/sha256 disagree')
        if any(local_file(directory, k).stat().st_size != v['bytes'] for k, v in manifest['files'].items()):
            raise ValueError('Bundle file size mismatch')
    required = {'flasher_args.json', 'dg-esp32.elf'}
    if not isinstance(sha, dict) or not required <= sha.keys():
        raise ValueError('Missing bundle hashes')
    # Hash the flash map before trusting its paths or arguments.
    for name, expected in sha.items():
        if not re.fullmatch(r'[0-9a-f]{64}', expected) or digest(local_file(directory, name)) != expected:
            raise ValueError('Bundle hash mismatch: '+name)
    flash = flash_files(directory)
    if not set(flash['flash_files'].values()) <= sha.keys():
        raise ValueError('Unhashed flash input')
    expected = manifest.get('elf_sha256')
    if flash.get('extra_esptool_args', {}).get('chip', CHIPS[board]) != CHIPS[board]:
        raise ValueError('Flash map chip mismatch')
    if expected != sha['dg-esp32.elf']:
        raise ValueError('ELF identity mismatch')
    aec, autostart = manifest.get('aec_enabled'), manifest.get('autostart')
    if type(aec) is not bool or type(autostart) is not bool:
        raise ValueError('Bundle requires boolean capability/autostart metadata')
    profile = ('aec-' if aec else '') + ('demo' if autostart else 'normal')
    if manifest.get('profile', profile) != profile:
        raise ValueError('Bundle profile contradicts flags')
    if reference_only:
        if board != 'echoear' or REFERENCE.get(expected) != (aec, autostart):
            raise ValueError('Reference-only requires an image of record')
    elif manifest.get('board') != board or manifest.get('target') != CHIPS[board]:
        raise ValueError('Bundle board/target mismatch')
    commit = manifest.get('firmware_commit', manifest.get('commit'))
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Missing firmware commit')
    return dict(manifest, profile=profile, firmware_commit=commit, board=board,
                target=CHIPS[board], sha256=sha), flash


def config_enabled(config, key):
    # Approved names only; never load a resolved config into Python or print values.
    result = subprocess.run(['grep', '-c', '^CONFIG_'+key+'=y$', str(config)],
                            capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1) or result.stdout.strip() not in ('0', '1'):
        raise ValueError('Config count failed for '+key)
    return result.stdout.strip() == '1'


def config_gain(config):
    gains = []
    for gain in range(0, 34, 3):
        result = subprocess.run(['grep', '-c', '^CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB='+str(gain)+'$', str(config)],
                                capture_output=True, text=True, check=False)
        if result.returncode not in (0, 1) or result.stdout.strip() not in ('0', '1'):
            raise ValueError('Config count failed for AEC ref gain')
        if result.stdout.strip() == '1': gains.append(gain)
    if len(gains) != 1: raise ValueError('Need one approved AEC ref gain')
    return gains[0]


def make_bundle(build, output, board='echoear'):
    build, output = Path(build).resolve(), Path(output).resolve()
    project = json.loads((build/'project_description.json').read_text())
    if project['target'] != CHIPS[board] or Path(project['project_path']).resolve() != ROOT:
        raise ValueError('Build target/worktree mismatch')
    if project.get('app_elf') != 'dg-esp32.elf' or project.get('app_bin') != 'dg-esp32.bin':
        raise ValueError('Unexpected application artifacts')
    version = subprocess.check_output(['git', 'describe', '--always', '--tags', '--dirty'], cwd=ROOT, text=True).strip()
    if project.get('project_version', '').removesuffix('-dirty') != version.removesuffix('-dirty'):
        raise ValueError('Stale build version; rebuild this worktree before bundling')
    # A matching dirty version alone cannot prove that changed firmware was built.
    # Conservatively reject a newer firmware input (including headers/assets).
    elf_time = (build/'dg-esp32.elf').stat().st_mtime_ns
    inputs = subprocess.check_output(['git', 'ls-files', 'main', 'components', 'third_party', 'boards', 'cmake',
        'CMakeLists.txt', 'partitions.csv', 'sdkconfig.defaults*', 'server/fixtures', 'hil/fixtures/lean_resume.opus'],
        cwd=ROOT, text=True).splitlines()
    if any((ROOT/name).is_file() and (ROOT/name).stat().st_mtime_ns > elf_time for name in inputs):
        raise ValueError('Firmware input newer than ELF; rebuild before bundling')
    config = Path(project['config_file'])
    aec = config_enabled(config, 'DG_ECHOEAR_AEC')
    autostart = config_enabled(config, 'DG_DEMO_AUTOSTART')
    if not config_enabled(config, 'DG_BOARD_'+board.upper()):
        raise ValueError('Resolved board mismatch')
    flash = flash_files(build)
    names = set(flash['flash_files'].values()) | {'dg-esp32.elf', 'flasher_args.json'}
    if 'dg-esp32.bin' not in names:
        raise ValueError('Application missing from flash map')
    # Provenance includes every changed/untracked source by hash, never file values.
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    paths = set(subprocess.check_output(['git', 'diff', '--name-only', 'HEAD'], cwd=ROOT, text=True).splitlines())
    paths.update(subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=ROOT, text=True).splitlines())
    source = {p: digest(ROOT/p) if (ROOT/p).is_file() else None for p in sorted(paths)}
    fields = dict(CONFIG_DG_ECHOEAR_AEC=int(aec), CONFIG_DG_DEMO_AUTOSTART=int(autostart),
                  CONFIG_DG_WIFI_OBSERVE=int(config_enabled(config, 'DG_WIFI_OBSERVE')))
    gain = config_gain(config) if aec else None
    if aec: fields['CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB'] = gain
    output.mkdir(parents=True, exist_ok=False)
    for name in names:
        target = local_file(output, name); target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_file(build, name), target)
    hashes = {name: digest(output/name) for name in sorted(names)}
    manifest = dict(schema=1, firmware_commit=commit, commit=commit, board=board, target=CHIPS[board],
                    profile=('aec-' if aec else '')+('demo' if autostart else 'normal'),
                    aec_enabled=aec, autostart=autostart, elf_sha256=hashes['dg-esp32.elf'],
                    sha256=hashes, files={n: dict(sha256=h, bytes=(output/n).stat().st_size) for n, h in hashes.items()},
                    flash_args=flash, source_files_sha256=source, boot_verified=False, bench_used=False,
                    project_version=version, build_dir=str(build),
                    config=fields, source_diff_sha256=hashlib.sha256(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT)).hexdigest(),
                    dependencies_lock_sha256=digest(ROOT/'dependencies.lock') if (ROOT/'dependencies.lock').exists() else None,
                    config_basis='Only grep -c of approved booleans and gain grid; private configs are not copied')
    if aec:
        manifest.update(boot_ref_gain_db=gain, expected_boot_line=f'MIC3_GAIN=0x{0x10+gain//3:02x} analog_db={gain}')
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    verify_bundle(output, board)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--board', choices=CHIPS, default='echoear')
    args = parser.parse_args()
    if os.environ.get('DG_BUILD_LOCK_HELD') != '1':
        # A build cannot replace inputs halfway through the bundle snapshot.
        return subprocess.call([str(ROOT/'scripts/build_locked.sh'), sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
    manifest = make_bundle(args.build, args.out, args.board)
    print(json.dumps(dict(bundle=str(args.out), elf_sha256=manifest['elf_sha256'], profile=manifest['profile'])))


if __name__ == '__main__':
    raise SystemExit(main())

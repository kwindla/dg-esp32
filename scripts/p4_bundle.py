#!/usr/bin/env python3
"""Build-time P4 audio metadata adapter for the frozen bundle writer.

Only sdkconfig_keydiff's approved parser inspects resolved configs. Export
public overlay keys and a fixed public audio allow-list, never private values.
Run under build_locked.sh; the profile emitter links the actual P4 AudioHw.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import sdkconfig_keydiff as keydiff

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'hil'))
from make_bundle import make_bundle, verify_bundle, digest, local_file

AUDIO_KEYS = dict(digital_percent='CONFIG_DG_MIC_GAIN_PERCENT',
                  playback_seed_dbfs_x10='CONFIG_DG_PLAYBACK_SEED_DBFS_X10',
                  tx_stack_bytes='CONFIG_DG_AUDIO_TX_STACK_BYTES')


def resolve_audio_defaults(project):
    """Resolve current board/audio/peer Kconfig with the build's defaults chain.

    A focused root avoids depending on the other target's managed-component
    graph. Only IDF infrastructure is stubbed; all audio defaults/conditions
    come from the real sources, exactly as in test_kconfig_boards.py.
    """
    temporary = Path(os.environ.get('TMPDIR',str(Path.home()/'.cache/gap3-tmp')))
    temporary.mkdir(parents=True,exist_ok=True)
    python = Path(os.environ.get('IDF_PYTHON_ENV_PATH',str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env')))/'bin/python'
    with tempfile.TemporaryDirectory(prefix='p4-audio-defaults-',dir=temporary) as work:
        work = Path(work); kconfig = work/'Kconfig'; config = work/'sdkconfig'
        kconfig.write_text('''config IDF_TARGET
    string "Target"
config IDF_TARGET_ESP32S3
    bool
    default y if IDF_TARGET = "esp32s3"
config IDF_TARGET_ESP32P4
    bool
    default y if IDF_TARGET = "esp32p4"
config HEAP_ABORT_WHEN_ALLOCATION_FAILS
    bool "Fail stop"
config FREERTOS_TASK_CREATE_ALLOW_EXT_MEM
    bool
    default y
'''+''.join(f'source "{ROOT/path}"\n' for path in
            ('main/Kconfig.projbuild','components/pipecat_esp_peer/Kconfig','components/dg_audio/Kconfig')))
        target = work/'target.defaults'; target.write_text('CONFIG_IDF_TARGET="esp32p4"\n')
        command = [str(python),'-m','kconfgen','--kconfig',str(kconfig),'--config',str(config),
                   '--defaults',str(target),'--output','config',str(config)]
        for name in project['config_defaults'].split(';'):
            path = Path(name)
            if not path.is_absolute(): path = ROOT/path
            # Private WiFi defaults cannot affect this public audio-only root.
            if path.name == 'sdkconfig.local': continue
            command += ['--defaults',str(path)]
            if Path(str(path)+'.esp32p4').exists(): command += ['--defaults',str(path)+'.esp32p4']
        result = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=30)
        if result.returncode:
            # confgen diagnostics can contain input lines; do not expose them.
            raise ValueError('P4 audio Kconfig re-resolution failed')
        current = keydiff.settings(config)
        return {key:current[key] for key in (AUDIO_KEYS['playback_seed_dbfs_x10'],AUDIO_KEYS['tx_stack_bytes'])}


def metadata(build, profile, recommendation=None):
    project = json.loads((build/'project_description.json').read_text())
    if project['target']!='esp32p4' or Path(project['project_path']).resolve()!=ROOT:
        raise ValueError('P4 build in this worktree required')
    resolved = keydiff.settings(Path(project['config_file']))
    overlay = keydiff.settings(ROOT/'boards/p4xc/sdkconfig.defaults')
    diff = keydiff.compare(overlay, resolved)
    if diff['differ'] or diff['removed']:
        raise ValueError('P4 overlay differs: regenerate the bench config')
    current = resolve_audio_defaults(project)
    stale = [key for key,value in current.items() if resolved.get(key)!=value]
    if stale: raise ValueError('P4 Kconfig defaults differ: '+','.join(sorted(stale))+'; regenerate the bench config')
    # This host-only executable uses fake GPIO/log includes and the real table.
    # Refuse an emitter older than its source/table/header; never trust a stale
    # analog/output default when the follow-up calibration changes the table.
    for name in ('components/dg_board/test_host/audio_profile.cpp',
                 'components/dg_board/src/p4xc/audio_hw.cpp',
                 'components/dg_board/include/dg_board/audio_hw.h'):
        if (ROOT/name).stat().st_mtime_ns > profile.stat().st_mtime_ns:
            raise ValueError('Rebuild the P4 host profile emitter')
    table = json.loads(subprocess.check_output([str(profile.resolve())], text=True))
    if set(table)!={'analog_db','playback_volume_db'} or any(type(v) is not int for v in table.values()):
        raise ValueError('Invalid P4 table profile')
    audio = dict(table, **{name:int(resolved[key]) for name,key in AUDIO_KEYS.items()})
    public = dict(p4_audio=audio, p4_kconfig_defaults=current, p4_overlay_values={key:resolved[key] for key in sorted(overlay)},
                p4_overlay_keydiff={key:diff[key] for key in ('equal','differ','removed')},
                p4_audio_profile_sha256=digest(profile), resolved_config_sha256=digest(Path(project['config_file'])))
    if resolved.get('CONFIG_DG_ECHOEAR_AEC')=='y':
        public['p4_aec_calibration'] = dict(status='UNMEASURED',measured=False,measurement_step='15b',
            ref_gain_db=int(resolved['CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB']),
            guard_ms=int(resolved['CONFIG_DG_ECHOEAR_AEC_GUARD_MS']),
            rearm_ms=int(resolved['CONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS']),
            afe_delay_samples=1024,scope='Inherited implementation/config placeholders; no P4 calibration claimed')
    if recommendation is not None:
        from p4_aec_close import checked_recommendation,labels,calibration_labels
        row=checked_recommendation(recommendation)
        calibration=public.get('p4_aec_calibration',{})
        if any(calibration.get(key)!=value for key,value in row['selected'].items()):
            raise ValueError('Rebuilt P4 values differ from the recommendation')
        if digest(build/'dg-esp32.elf')==row['source_elf_sha256']:
            raise ValueError('Fresh applied P4 ELF required')
        calibration.update(status='APPLIED',measured=False,measurement_step='15b',
            recommendation_file=str(Path(recommendation).resolve()),recommendation_sha256=digest(recommendation),
            **labels(row['delay_provenance']),
            scope='Gain 30 dB selected by the pre-declared sweep rule from single trials per prompt; '
                'guard 400/2000 provisional, event detection only, not physically validated in-window; '
                'delay 1024 measured-diagnostic under closing decision; '
                'provisional: bench speaker not final hardware; no qualification claim')
        calibration_labels(calibration,row['delay_provenance'])
    return public


def bundle(build, out, profile, recommendation=None):
    public = metadata(build, profile, recommendation)  # Refuse stale gains before creating anything.
    manifest = make_bundle(build, out, 'p4xc')
    manifest.update(public)
    manifest.update(target_dependencies_lock='dependencies.p4xc.lock',
        target_dependencies_lock_sha256=digest(ROOT/'dependencies.p4xc.lock'),
        dependencies_lock_sha256=digest(ROOT/'dependencies.p4xc.lock'),
        dependencies_lock_basis='P4 target lock; supersedes frozen writer root-lock field')
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    provenance = dict(board='p4xc', target_dependencies_lock='dependencies.p4xc.lock',
        target_dependencies_lock_sha256=digest(ROOT/'dependencies.p4xc.lock'),
        manifest_sha256=digest(out/'manifest.json'), elf_sha256=manifest['elf_sha256'])
    (out/'board-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    verify_bundle(out,'p4xc')
    return manifest


def refreeze(source,build,out,profile,recommendation):
    """Update metadata around verified old image bytes; retain image provenance."""
    source,build,out=Path(source).resolve(),Path(build).resolve(),Path(out).resolve()
    if out.exists():raise ValueError('Fresh refreeze output required')
    original,_=verify_bundle(source,'p4xc')
    public=metadata(build,profile,recommendation)
    if public['resolved_config_sha256']!=original.get('resolved_config_sha256'):
        raise ValueError('Refreeze generated configuration changed')
    if any(public.get(key)!=original.get(key) for key in
        ('p4_audio','p4_kconfig_defaults','p4_overlay_values','p4_overlay_keydiff')):
        raise ValueError('Refreeze public audio settings changed')
    if original.get('target_dependencies_lock_sha256')!=digest(ROOT/'dependencies.p4xc.lock'):
        raise ValueError('Refreeze target lock changed')
    if original.get('profile') not in ('aec-normal','aec-demo') or original.get('aec_enabled') is not True:
        raise ValueError('Refreeze requires an existing P4 AEC image')
    for key in ('ref_gain_db','guard_ms','rearm_ms','afe_delay_samples'):
        if original.get('p4_aec_calibration',{}).get(key)!=public['p4_aec_calibration'][key]:
            raise ValueError('Refreeze would change configured calibration values')
    for name,sha in original['sha256'].items():
        if digest(local_file(build,name))!=sha:raise ValueError('Refreeze build artifact differs: '+name)
    paths=set(subprocess.check_output(['git','diff','--name-only','HEAD'],cwd=ROOT,text=True).splitlines())
    paths.update(subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=ROOT,text=True).splitlines())
    manifest=dict(original,**public)
    manifest['metadata_refreeze']=dict(source_bundle=str(source),source_manifest_sha256=digest(source/'manifest.json'),
        tooling_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        metadata_source_files_sha256={name:digest(ROOT/name) if (ROOT/name).is_file() else None for name in sorted(paths)},
        firmware_rebuilt=False)
    out.mkdir(parents=True,exist_ok=False)
    for name in original['sha256']:
        target=local_file(out,name);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(local_file(source,name),target)
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    provenance=dict(board='p4xc',target_dependencies_lock='dependencies.p4xc.lock',
        target_dependencies_lock_sha256=manifest['target_dependencies_lock_sha256'],
        manifest_sha256=digest(out/'manifest.json'),elf_sha256=manifest['elf_sha256'])
    (out/'board-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    verify_bundle(out,'p4xc')
    return manifest


def main():
    if os.environ.get('DG_BUILD_LOCK_HELD')!='1':
        return subprocess.call([str(ROOT/'scripts/build_locked.sh'),sys.executable,str(Path(__file__).resolve()),*sys.argv[1:]])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build',type=Path,required=True); parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--profile',type=Path,default=ROOT/'build/host-board/p4_audio_profile')
    parser.add_argument('--recommendation',type=Path)
    parser.add_argument('--refreeze-from',type=Path,help='Verified existing bundle; preserve image provenance without rebuilding')
    args = parser.parse_args()
    if args.refreeze_from is not None:
        if args.recommendation is None:parser.error('--refreeze-from requires --recommendation')
        manifest=refreeze(args.refreeze_from,args.build,args.out,args.profile.resolve(),args.recommendation)
    else:manifest = bundle(args.build.resolve(),args.out.resolve(),args.profile.resolve(),args.recommendation)
    print(json.dumps(dict(bundle=str(args.out),elf_sha256=manifest['elf_sha256'],p4_audio=manifest['p4_audio'])))
    return 0


if __name__=='__main__': raise SystemExit(main())

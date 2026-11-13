"""Host-only prospective campaign-10 v8.5 freeze. Never launch or touch hardware."""
import argparse
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from echoear_aec_bench import ROOT,sha,write
from echoear_aec_campaign import validate_campaign
from freeze_aec_link_soak import runner_pattern as soak_pattern

BASE=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9')
JOBS=Path('/home/khkramer/src/dg-esp32/proj-2026-10-15-0645/codex-jobs')


PRIOR_DECLARATION_SHA256='3b4ff2cb4e5aaf63b0d93affcb6e3bfe9c3ff2b407258577b251a985aa8a6cae'


def retained_playback_config(plan):
    # Reuse the reviewed, hash-bound public config; no local candidate build needed.
    source=BASE/'freeze-v8.3.1/declaration-v8.3.1.json'
    config_path=source.parent/'candidate-playback-config.json'
    if sha(source)!=PRIOR_DECLARATION_SHA256:raise ValueError('Prior playback declaration changed')
    prior=json.loads(source.read_text());config=json.loads(config_path.read_text())
    if config!=prior['candidate']['playback_config'] or config!=plan['candidate']['playback_config']:
        raise ValueError('Retained playback config changed')
    if config['elf_sha256']!=plan['elf_sha256'] or sha(Path(plan['image'])/'dg-esp32.elf')!=config['elf_sha256']:
        raise ValueError('Retained playback config ELF mismatch')
    if sha(ROOT/config['implementation'])!=config['implementation_sha256']:
        raise ValueError('Candidate playback implementation changed')
    return config,dict(declaration=str(source),declaration_sha256=sha(source),
                       config=str(config_path),config_sha256=sha(config_path),
                       local_build_read=False,candidate_rebuild_required=False)


from freeze_aec_campaign_v8_3_1 import launch_script


def host_guard_script(script):
    lines=script.splitlines()
    if not lines[-1].startswith('exec uv '):raise ValueError('Unexpected final exec')
    lines[-1]='echo "LIVE HOST GUARD PASSED; runner NOT EXECUTED"'
    return '\n'.join(lines)+'\n'


def validate_bundle(destination):
    destination=Path(destination);declaration=destination/'declaration-v8.5.json';p=json.loads(declaration.read_text())
    output=Path(p['output'])
    if output.exists() or output.is_symlink():raise ValueError('Campaign output must remain absent')
    validate_campaign(p)
    review=p['candidate']['review_build_record']
    if sha(review['path'])!=review['sha256']:raise ValueError('Review build record changed')
    record=json.loads(Path(review['path']).read_text())
    if record['elf_sha256']!=p['elf_sha256'] or record['firmware_commit']!=p['candidate']['firmware_commit']:raise ValueError('Review build identity mismatch')
    for name,digest in record['artifacts'].items():
        if sha(Path(p['image'])/name)!=digest:raise ValueError('Review build evidence changed: '+name)
    config,provenance=retained_playback_config(p)
    if json.loads((destination/'candidate-playback-config.json').read_text())!=config:raise ValueError('Frozen playback config changed')
    if json.loads((destination/'playback-config-provenance.json').read_text())!=provenance:raise ValueError('Playback config provenance changed')
    script=(destination/'launch-campaign-10.sh').read_text()
    if script!=launch_script(sha(declaration),declaration,output,p['host_environment']):raise ValueError('Frozen launch script mismatch')
    guard=(destination/'check-live-host-guard.sh').read_text()
    if guard!=host_guard_script(script):raise ValueError('Frozen host guard mismatch')
    for text in (script,guard):subprocess.run(['bash','-n'],input=text,text=True,capture_output=True,check=True)
    return dict(user_adoption_granted=True,qualification=False,prepared_only=True,passed=True,output_exists=False,launch_status='NOT EXECUTED',hardware_access=False,declaration_sha256=sha(declaration))


def freeze(destination,output,connection):
    destination=Path(destination).resolve();output=Path(output).resolve()
    if destination.exists() or destination.is_symlink() or output.exists() or output.is_symlink():raise FileExistsError('Fresh freeze and absent output required')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True):raise RuntimeError('Clean committed worktree required')
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    template=ROOT/'hil/plans/echoear-aec-accept-v8.5.json';p=json.loads(template.read_text())
    bundle=json.loads((Path(p['image'])/'manifest.json').read_text());firmware=bundle['firmware_commit']
    import hashlib
    if bundle['source_diff_sha256']!=hashlib.sha256(b'').hexdigest():raise ValueError('Dirty candidate sources')
    changed=subprocess.check_output(['git','diff','--name-only',firmware,commit],cwd=ROOT,text=True).splitlines()
    if any(not f.startswith(('hil/','server/','docs/')) for f in changed):raise ValueError('Candidate firmware sources changed')
    config,config_provenance=retained_playback_config(p)
    from echoear_aec_lull import declared_rule,RULE
    if declared_rule(p)!=RULE:raise ValueError('Exact v8.5 policy required')
    destination.mkdir(parents=True)
    write(destination/'candidate-playback-config.json',config)
    write(destination/'playback-config-provenance.json',config_provenance)
    write(destination/'firmware-source-check.json',dict(candidate_commit=firmware,current_commit=commit,changed_files=changed,firmware_relevant_changes=[],candidate_rebuild_required=False))
    files={str(f.relative_to(ROOT)):sha(f) for pattern in ('hil/*.py','server/*.py') for f in ROOT.glob(pattern)}
    for name in ('server/uv.lock','server/pyproject.toml','hil/plans/echoear-aec-accept-v1.json','hil/plans/echoear-aec-accept-v8.5.json','docs/echoear-aec-campaign-v8.5.md','docs/echoear-aec-lull-v84-prep.md'):files[name]=sha(ROOT/name)
    for name in ('playout.h','playout.cpp','transport.cpp','playback_selftest.cpp','playout_run_selftest.h'):
        path=Path('components/pipecat_esp_peer/src')/name;files[str(path)]=sha(ROOT/path)
    path=Path('components/pipecat_esp_peer/test_host/test_playout.cpp');files[str(path)]=sha(ROOT/path)
    files['components/dg_audio/src/playback_dsp.cpp']=sha(ROOT/'components/dg_audio/src/playback_dsp.cpp')
    write(destination/'code-manifest.json',files)
    write(destination/'fixture-manifest.json',p['fixture_policy']['final_calibrated_paths_and_hashes'])
    from echoear_aec_v8_calibration import calibration
    measured=calibration()
    if measured!=p['fixture_policy']['duration_calibration']:raise ValueError('Duration calibration changed')
    write(destination/'duration-calibration.json',measured)
    shutil.copy2(BASE/'freeze-v6/source-preflight.json',destination/'source-preflight.json')
    witness=json.loads((destination/'source-preflight.json').read_text())
    p.update(status='declared',qualification=True,campaign_authorized=True,resume_from=None,
        review_approval=p['review_of_record'],reviewed_spec_sha256=sha(template),declaration_id='step-9-acceptance-10-v8.5',
        output=str(output),pending_before_freeze=[],
        implementation_status=p['implementation_status'],
        source_preflight=dict(path=str(destination/'source-preflight.json'),sha256=sha(destination/'source-preflight.json'),passed=witness['passed'],continuous_active_seconds=witness['continuous_active_seconds']))
    p['host_environment']['connection']=connection
    p['candidate'].update(bot_commit=commit,runner_commit=commit,code_sha256=files,
        code_manifest_path=str(destination/'code-manifest.json'),code_manifest_sha256=sha(destination/'code-manifest.json'),
        fixture_manifest_path=str(destination/'fixture-manifest.json'),fixture_manifest_sha256=sha(destination/'fixture-manifest.json'),
        runner_sha256=sha(ROOT/p['runner']),analyzer_sha256=sha(ROOT/p['analyzer']))
    declaration=destination/'declaration-v8.5.json';write(declaration,p);digest=sha(declaration)
    launch=destination/'launch-campaign-10.sh';launch.write_text(launch_script(digest,declaration,output,p['host_environment']));launch.chmod(0o755)
    # Generated only; the check script contains no runner execution.
    check=destination/'check-live-host-guard.sh';check.write_text(host_guard_script(launch.read_text()));check.chmod(0o755)
    (destination/'PREPARED.md').write_text(
        '# v8.5 user-decided freeze — NOT EXECUTED\n\n'
        'Prepared for review only. The declaration describes the prospective qualification contract; '
        'no qualification or acceptance credit is claimed. No firmware rebuild. '
        'Only the coordinator launches the unchanged command after review.\n')
    pattern=soak_pattern(digest,declaration,output).replace('--link-soak ','')
    (destination/'stop-instructions.txt').write_text(
        '# NOT EXECUTED. Identify the anchored Python child, never the nohup/uv wrapper.\n'
        'pgrep -af '+shlex.quote(pattern)+'\n'
        '# After confirming the exact Python argv: kill -TERM <python-pid>\n'
        '# Wait for restored_elf_sha256=9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758, mixer_equal and lock_released in hold result.json.\n'
        '# Never SIGKILL, signal the wrapper, force release flock, or use the bench before cleanup. Restore failure retains flock.\n')
    (destination/'launch-command.txt').write_text('PREPARED ONLY; NOT EXECUTED\n'+str(launch)+'\n')
    result=validate_bundle(destination);write(destination/'freeze-result.json',result)
    (destination/'validator.log').write_text('PASS: declaration, source preflight, candidate, fixtures, code manifest, launch exports/hash/output and active-profile guard statically validated; no environment queries; campaign NOT EXECUTED\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('destination',type=Path);p.add_argument('--output',type=Path);p.add_argument('--connection');p.add_argument('--validate',action='store_true');a=p.parse_args()
    if a.validate:print(json.dumps(validate_bundle(a.destination),indent=2))
    elif a.output is None or not a.connection:p.error('--output and --connection required')
    else:freeze(a.destination,a.output,a.connection)

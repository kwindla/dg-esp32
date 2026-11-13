"""File-only Nova live freeze preparation. Incomplete evidence never authorizes a run.

The short matrix differs from v8.5. Until its measured preflight and regression
fixture are reviewed, emit a blocked handoff, never send it to the v8.5 runner.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shlex
import statistics
import subprocess
from echoear_aec_bench import ROOT, sha, write, verify_bundle, NORMAL, NORMAL_HASH
from echoear_aec_host_interrupt import cell
from freeze_aec_campaign_v8_5 import retained_playback_config
from freeze_aec_campaign_v8_3_1 import launch_script
from freeze_aec_link_soak import runner_pattern

BASE=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9')
JOBS=Path('/home/khkramer/src/dg-esp32/proj-2026-10-15-0645/codex-jobs')
PROFILE_RUN=JOBS/'impl-step-9-nova-profile/replay-final/runs/campaign-10--qualification-01-echo-01-a1--dev'
GROUPS=(('firm','stop',7),('firm','sentence',6),('normal','sentence',6))
BLOCKERS=[
    'Fresh dev-profile bot/reply preflight has not been measured; the old Flux preflight is invalid.',
    'Long-sentence/internal-pause regression fixture and exact expected turn are not yet supplied/reviewed.',
    'Short-session runner/scorer integration must be completed and validated after the pending contract inputs; the v8.5 full-campaign runner cannot execute this matrix.']


def make_plan(base,full):
    p=copy.deepcopy(base)
    p.update(status='blocked',qualification=False,campaign_authorized=False,resume_from=None,
        declaration_id='step-9-nova-live-v8.6',pending_before_freeze=BLOCKERS.copy())
    p['cases']=[];p['spares']=[];p['controls']=[];p['reconnects']=[];p['blocks']=[]
    # Two different echo texts at each volume, no incomplete quick-pair grouping.
    echoes=[c for c in full['cases'] if c['kind']=='echo' and c['id'].endswith(('echo-01','echo-08'))]
    for c in echoes:
        c=copy.deepcopy(c);c.pop('capture_group',None);c.pop('quick_gap_s',None)
        c.update(session_id='qualification-01',report_only=False,aec_state='cold' if not p['cases'] else 'warm')
        p['cases'].append(c)
    for voice,kind,spares in GROUPS:
        for key in ('cases','spares'):
            selected=[copy.deepcopy(c) for c in full[key] if c['volume_db']==-6 and c['kind']==kind and
                      c.get('voice_class','firm')==voice and c.get('expected_class')=='eligible']
            assert len(selected)==(10 if key=='cases' else spares)
            for c in selected:c.update(session_id='qualification-01',report_only=False,voice_class=voice)
            p[key]+=selected
    normal=[copy.deepcopy(c) for c in full['cases'] if c.get('voice_class')=='normal' and c['kind']=='stop'][:2]
    for c in normal:c.update(session_id='qualification-01',report_only=True)
    p['cases']+=normal
    for i,offset in enumerate((1.0,1.5),1):
        c=cell(f'nova-host-interrupt-{i:02}',p['text'],
               'The interruption test is complete and this is the new recovery reply.',offset_s=offset)
        c.update(session_id='qualification-01',report_only=True,aec_state='warm',timing_report_only=True)
        p['controls'].append(c)
    p['sessions']=[dict(id='qualification-01',volume_db=-6,role='nova-live',
        cold_definition='One candidate boot. First -6 echo cold; all later replies warm. No reconnect or soak.')]
    p['bot_config']['ECHOEAR_STT_PROFILE']='dev'
    p['gates']['barge_in'].update(gated_voice_kind_pairs=[list(g[:2]) for g in GROUPS],
        gated_voice_classes=['firm','normal'],voice_class_rule='Three separate groups: firm Stop, firm sentence, normal sentence. Each needs 10 eligible and >=9 successes; same firm sentence rules for normal sentences. Normal single words report-only; no pooling or score-based replacement.')
    p['gates']['false_turns'].update(accepted_max=0,interruptions_max=0,minimum_core_echo_per_volume={'-6':2,'0':2},
        scope='All four clean declared echo replies, including both volumes; zero accepted turns and interruptions.')
    p['gates']['echo']['scope']='All four clean declared echo replies, same firmware acoustic thresholds.'
    p['gates']['reconnect']['scope']='Not included in this short live validation; no reconnect qualification credit.'
    p['failure_policy']['retakes']='Existing v8.5 noise/harness/long-TTS policy; opportunity-only spares 7 firm Stop, 6 firm sentence, 6 normal sentence. No report-only opportunity spares, link retakes, score retries or resume.'
    p['nova_live']=dict(scope='Short live dev-profile validation only; never full campaign/shipping qualification',
        echo_counts={'-6':2,'0':2},normal_stop_report_only=2,host_interrupt_report_only=2,
        preservation=dict(gate=False,tolerances=None,reason='User has not set preservation tolerances'),
        sentence_split_regression=dict(id='nova-sentence-split-01',fixture=None,fixture_sha256=None,expected_text=None,
            rule='One long single sentence with declared internal pause; exactly one complete accepted turn/LLM request, no prefix-only or suffix-only reply. Must retain word timing and trailing final segments.',
            gate=True,counts_toward_barge_denominator=False),
        stt_profile=None,vendor_model=None,measured_bot_preflight=None,
        implementation_ready=False,review_required=True,launch_authorized=False)
    p['preflight_contract']['real_reply']['status']='STALE: old Flux observation retained for provenance only'
    p.pop('source_preflight',None);p.pop('reviewed_spec_sha256',None)
    p['time_estimate']={}
    return p


def validate_draft(p):
    if (p.get('status'),p.get('qualification'),p.get('campaign_authorized'))!=('blocked',False,False):
        raise ValueError('This preparer cannot authorize acquisition')
    if p.get('pending_before_freeze')!=BLOCKERS:raise ValueError('Unresolved evidence must remain explicit')
    if p['bot_config'].get('ECHOEAR_STT_PROFILE')!='dev':raise ValueError('Nova dev profile required')
    if p['nova_live']['measured_bot_preflight'] is not None or p['nova_live']['implementation_ready'] or p['nova_live']['launch_authorized']:
        raise ValueError('Use a reviewed final freeze after resolving the pre-step; no in-place promotion')
    for voice,kind,spares in GROUPS:
        own=lambda c:c['kind']==kind and c.get('voice_class','firm')==voice and not c.get('report_only')
        if sum(map(own,p['cases']))!=10 or sum(map(own,p['spares']))!=spares:raise ValueError('Separate voice/kind denominator or spare budget changed')
    if any(c.get('report_only') is not True for c in p['cases'] if c.get('voice_class')=='normal' and c['kind']=='stop'):
        raise ValueError('Normal single words must remain report-only')
    for volume in (-6,0):
        if sum(c['kind']=='echo' and c['volume_db']==volume and not c.get('report_only') for c in p['cases'])!=2:raise ValueError('Echo exposure missing')
    if p['reconnects'] or p['blocks'] or len(p['sessions'])!=1:raise ValueError('Short one-hold scope changed')
    from echoear_aec_host_interrupt import validate
    if len(p['controls'])!=2:raise ValueError('Two report-only host interruption cells required')
    for c in p['controls']:
        validate(c)
        if not c.get('report_only') or not c.get('timing_report_only'):raise ValueError('Host timing must remain report-only')
    ids=[c['id'] for key in ('cases','spares','controls') for c in p[key]]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate cell IDs')
    return dict(preparation_valid=True,freeze_ready=False,launch_allowed=False,blockers=p['pending_before_freeze'])


def duration_estimate(p):
    rows=[]
    for folder in sorted((BASE/'campaign-10/qualification-01').glob('*')):
        window=folder/'attempt-window.json';result=folder/'result.json'
        if not window.exists() or not result.exists():continue
        cases=json.loads(result.read_text()).get('cases',[])
        if not cases:continue
        w=json.loads(window.read_text())['window_s']
        rows.append(dict(path=str(window),sha256=sha(window),result_path=str(result),result_sha256=sha(result),kind=cases[0]['kind'],seconds=w[1]-w[0]))
    groups={k:statistics.median(r['seconds'] for r in rows if r['kind']==k) for k in {r['kind'] for r in rows}}
    # Conservative stand-ins for new controls; explicitly unmeasured, no fake p99.
    durations=[groups.get(c['kind'],65) for c in p['cases']+p['controls']]
    overhead=540 # retained four-minute link probe plus five-minute setup/restore allowance
    total=sum(durations)+65+overhead # pending regression control
    return dict(nominal_s=total,nominal_minutes=total/60,ordinary_captures=len(durations),pending_regression_captures=1,
        per_kind_retained_medians_s=groups,unmeasured_host_and_regression_allowance_s=65,
        setup_link_restore_allowance_s=overhead,spare_captures=len(p['spares']),
        all_spares_added_s=sum(groups[c['kind']] for c in p['spares']),hard_wallclock_bound_s=None,
        basis=rows,limitations='Old Flux acquisition/export cadence only, not Nova preflight. Noise/harness/TTS retries and spares extend elapsed time. No denominator reduction at 40 minutes; an incomplete hold cannot pass.')



def installed_package_hashes():
    """Actual installed implementation/binary hashes, in addition to uv.lock pins."""
    from importlib.metadata import distribution
    result={}
    for name in ('pipecat-ai','deepgram-sdk','onnxruntime'):
        dist=distribution(name);files={}
        for item in sorted(dist.files or [],key=str):
            path=Path(dist.locate_file(item))
            if path.suffix not in ('.py','.so','.onnx') or not path.is_file():continue
            h=hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
            files[str(item)]=h.hexdigest()
        if not files:raise ValueError('Installed package has no hashable implementation: '+name)
        result[name]=dict(version=dist.version,files_sha256=files,
            tree_sha256=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest())
    return result


def blocked_launcher():
    return '#!/usr/bin/env bash\nset -euo pipefail\nprintf "%s\\n" "BLOCKED: Nova preflight, regression fixture and short-runner final validation required; no launch authorized." >&2\nexit 2\n'


def prepare(destination,output):
    destination=Path(destination).resolve();output=Path(output).resolve()
    if destination.exists() or output.exists() or destination.is_symlink() or output.is_symlink():raise FileExistsError('Fresh handoff and absent run output required')
    base_path=ROOT/'hil/plans/echoear-aec-accept-v8.5.json';full_path=ROOT/'hil/plans/echoear-aec-accept-v1.json'
    base=json.loads(base_path.read_text());p=make_plan(base,json.loads(full_path.read_text()))
    from demo_policy import profile_manifest
    profile=profile_manifest('dev')
    retained=json.loads((PROFILE_RUN/'config.json').read_text())['profile_manifest']
    if profile!=retained:raise ValueError('Reviewed Nova profile/package/model binding changed')
    vendor=dict(name='general-nova-3',version='2025-04-17.21547',model_uuid='40bd3654-e622-47c4-a111-63a61b23bfe8')
    messages=json.loads((PROFILE_RUN/'vendor-events.json').read_text())
    metadata=[e['message']['metadata'] for e in messages if 'metadata' in e.get('message',{})]
    if not metadata or any(m.get('model_info',{}).get('name')!=vendor['name'] or m['model_info'].get('version')!=vendor['version'] or m.get('model_uuid')!=vendor['model_uuid'] for m in metadata):raise ValueError('Retained vendor witness mismatch')
    p['nova_live'].update(stt_profile=profile,vendor_model=vendor,vendor_policy='Must match live stt-model-observed; missing/change blocks credit and requires coordinator review. Cloud weights cannot be hashed.',
        safety_budget=dict(NOVA_TTFS_P99_S=1.0,measured_p99=False),
        event_schema=dict(version='nova-dev-events-1',files={name:sha(ROOT/name) for name in ('server/aec_evaluation.py','server/aec_session_recorder.py','server/demo_policy.py','hil/echoear_aec_campaign_analysis.py')},
            required=['stt-profile','stt-model-observed','VADUserStartedSpeakingFrame','VADUserStoppedSpeakingFrame','UserStartedSpeakingFrame','InterruptionFrame','frame_id','bot_active','direction','turn strategy names'],
            echo_rule='zero accepted turns AND zero interruptions; VAD starts reported separately'))
    p['time_estimate']=duration_estimate(p)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True):raise ValueError('Commit preparation source first')
    changed=subprocess.check_output(['git','diff','--name-only',base['candidate']['firmware_commit'],commit],cwd=ROOT,text=True).splitlines()
    if any(not f.startswith(('hil/','server/','docs/')) for f in changed):raise ValueError('Candidate firmware changed')
    verify_bundle(Path(p['image']),p['elf_sha256'],p['boot_ref_gain_db']);verify_bundle(NORMAL,NORMAL_HASH)
    config,provenance=retained_playback_config(p)
    p.update(output=str(output),host_commit=commit,proposed_runner='hil/echoear_aec_campaign.py')
    p['candidate'].update(bot_commit=commit,runner_commit=commit)
    p['inputs']={str(f):sha(f) for f in [base_path,full_path,PROFILE_RUN/'config.json',PROFILE_RUN/'vendor-events.json',
        JOBS/'step-9-nova-live-freeze-request.md',JOBS/'fable-step-9-nova-profile-review.md',JOBS/'step-9-user-decisions-2026-10-18-b.md']}
    files={str(f.relative_to(ROOT)):sha(f) for pattern in ('hil/*.py','server/*.py','hil/plans/*.json') for f in ROOT.glob(pattern)}
    for name in ('server/uv.lock','server/pyproject.toml','docs/echoear-aec-nova-profile.md','docs/echoear-aec-nova-live-prep.md'):files[name]=sha(ROOT/name)
    p['code_sha256']=files
    p['fixture_sha256']={c['fixture']:sha(c['fixture']) for key in ('cases','spares') for c in p[key] if c.get('fixture')}
    packages=installed_package_hashes()
    p['nova_live']['installed_packages_sha256']={k:v['tree_sha256'] for k,v in packages.items()}
    result=validate_draft(p)
    destination.mkdir(parents=True)
    spec={k:p[k] for k in ('cases','spares','controls','sessions','gates','noise_gate','bot_config','failure_policy','nova_live','time_estimate')}
    write(destination/'spec-v8.6-nova.draft.json',spec)
    p['draft_spec_sha256']=sha(destination/'spec-v8.6-nova.draft.json')
    p['reviewed_spec_sha256']=None # no completed/reviewed freeze is claimed
    write(destination/'declaration-v8.6-nova.draft.json',p)
    write(destination/'installed-package-hashes.json',packages)
    write(destination/'profile-binding.json',dict(profile=profile,vendor=vendor,retained_evidence=p['inputs']))
    write(destination/'code-manifest.json',files);write(destination/'fixture-manifest.json',p['fixture_sha256'])
    write(destination/'duration-estimate.json',p['time_estimate'])
    write(destination/'candidate-playback-config.json',config);write(destination/'playback-config-provenance.json',provenance)
    write(destination/'firmware-source-check.json',dict(candidate_commit=base['candidate']['firmware_commit'],host_commit=commit,changed_files=changed,firmware_relevant_changes=[],rebuild=False))
    launch=destination/'launch-nova-live.sh';launch.write_text(blocked_launcher());launch.chmod(0o755)
    declaration=destination/'declaration-v8.6-nova.draft.json';digest=sha(declaration)
    guarded=launch_script(digest,declaration,output,p['host_environment'])
    (destination/'launch-after-final-freeze.sh.in').write_text('# TEMPLATE ONLY: blocked draft is rejected by the runner. Final freeze must regenerate hash/argv after implementation and preflight.\n'+guarded)
    subprocess.run(['bash','-n',str(launch)],check=True)
    subprocess.run(['bash','-n'],input=guarded,text=True,check=True,capture_output=True)
    pattern=runner_pattern(digest,declaration,output).replace('--link-soak ','')
    (destination/'stop-instructions.txt').write_text('NOT EXECUTED. No process is launched by this handoff. Regenerate this anchor in the final freeze.\npgrep -af '+shlex.quote(pattern)+'\nAfter confirming exact Python argv, kill -TERM <python-pid>; never signal uv/nohup, SIGKILL, or force release flock.\nWait for normal ELF '+NORMAL_HASH+', mixer_equal=true, lock_released=true and no cleanup_errors. Restore failure retains lock.\n')
    write(destination/'validation.json',dict(**result,declaration_sha256=digest,launch_status='NOT EXECUTED',hardware_access=False,firmware_bundles_verified=True,shell_syntax=True))
    (destination/'PREPARED.md').write_text('# BLOCKED — not a launchable freeze\n\n'+ '\n'.join('- '+v for v in BLOCKERS)+'\n\nThe .sh.in is a non-executable template. launch-nova-live.sh always exits 2 before environment queries. Original v8.5 frozen evidence is unchanged.\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(prepare(args.destination,args.output),indent=2))

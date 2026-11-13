"""Frozen short Nova session. Prepare/analyze are file-only; run requires USER-GO."""
import argparse
import copy
import json
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import echoear_aec_nova_prestep as n
from echoear_aec_bench import ROOT, sha, write
# gap 3: the step-9 Nova campaign stays bound to its declared 9936da45 restore image.
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH
from freeze_aec_nova_live import BASE, JOBS, GROUPS, installed_package_hashes

SOURCE = n.SOURCE
COMBINED = JOBS/'impl-step-9-nova-prestep-02-analysis-evidence/analysis/combined-nova-preflight.rejudged.json'
COMBINED_SHA = '3c3d11857bb9e53acb424d832b55dc87df7fc170cfcfa733b63a3458b9f44d4b'
FIXTURE = BASE/'freeze-v8.6-nova-prestep/pause-fixture-manifest.json'
REVIEW_ENV = 'NOVA_LIVE_REVIEWED_PLAN_SHA256'
PAUSED = 'nova-sentence-split-01'
# These standalone offline experiments are never imported by the live runner.
OFFLINE = {'hil/echoear_aec_nova_turn_sweep.py', 'server/nova_turn_sweep.py',
           'hil/test_echoear_aec_nova_turn_sweep.py'}
# Decision 54 adopted the +40 ms playout reserve; bound by a copy in the bundle.
DECISIONS = JOBS/'step-9-user-decisions-2026-10-19.md'
DECISIONS_SHA = '284d13109a27d0b713d161febe04f69899a1426ef60d2cccb80f58fb4a5f83fb'
PLAYOUT_KEYS = {'CONFIG_DG_PLAYOUT_MAX_TARGET_FRAMES':['8','10'],'CONFIG_DG_PLAYOUT_TARGET_FRAMES':['5','7']}
# Frozen session variants. They differ only in candidate identity and their own
# bundle/output; v8.6 (no override) re-derives the original Nova final plan.
V86 = dict(name='v8.6-nova-final', destination=BASE/'freeze-v8.6-nova-final',
           output=BASE/'nova-live-01', candidate=None)
V87 = dict(name='v8.7-nova-v86', destination=BASE/'freeze-v8.7-nova-v86',
           output=BASE/'nova-live-02', candidate=dict(
               firmware_commit='0e440512d6644ac70352be5a5c44845c416282b8',
               parent_commit='51fdd73d4f487840e2172da9cb8deb95585944f0',
               elf_sha256='0382684efcc840ebd9fa3fa4ba98f87c594313b4b8523e2793ef999f4d9b1cd8',
               image=BASE/'candidate-0e44051-ref15', decision=54,
               playout=dict(target_frames=7, target_ms=140, max_target_frames=10,
                            previous_target_frames=5, previous_max_target_frames=8),
               previous_plan_sha256='a5f36d71ff013d20fc6b62ae53d80d9976daf4bd23af70b7538d6287d6c34a3a'))
VARIANTS = {v['name']: v for v in (V86, V87)}


def variant_of(p):
    name = p.get('freeze', V86['name'])
    if name not in VARIANTS: raise ValueError('Undeclared freeze variant')
    return VARIANTS[name]


def candidate_override(p, v):
    """Swap only the candidate identity; every other field stays the v8.6 contract."""
    c = v['candidate']; image = Path(c['image'])
    manifest_path, review_path = image/'manifest.json', image/'review-build.json'
    manifest = json.loads(manifest_path.read_text()); review = json.loads(review_path.read_text())
    old = p['candidate']
    if ((manifest['firmware_commit'], manifest['elf_sha256'], manifest['boot_ref_gain_db']) !=
            (c['firmware_commit'], c['elf_sha256'], p['boot_ref_gain_db']) or manifest['config'] != old['resolved_config']):
        raise ValueError('Candidate bundle identity/config mismatch')
    header = review['resolved_config_comparison']['sdkconfig_h']
    if ((review['firmware_commit'], review['elf_sha256'], review['manifest_sha256']) !=
            (c['firmware_commit'], c['elf_sha256'], sha(manifest_path)) or
            header['old_sha256'] != old['playback_config']['sdkconfig_header_sha256'] or header['differing'] != PLAYOUT_KEYS):
        raise ValueError('Review build must show only the two playout keys changed')
    playback = dict(old['playback_config'], elf_sha256=c['elf_sha256'], sdkconfig_header_sha256=header['new_sha256'])
    if sha(ROOT/playback['implementation']) != playback['implementation_sha256']:
        raise ValueError('Candidate playback implementation changed')
    previous = {k: old[k] for k in ('firmware_commit', 'elf_sha256', 'image_bundle_sha256',
                                    'review_artifact_manifest', 'review_build_record')}
    previous.update(image=p['image'], playback_sdkconfig_header_sha256=old['playback_config']['sdkconfig_header_sha256'])
    old.update(firmware_commit=c['firmware_commit'], elf_sha256=c['elf_sha256'],
               image_bundle_sha256=sha(manifest_path), review_artifact_manifest=str(manifest_path),
               review_build_record=dict(path=str(review_path), sha256=sha(review_path)),
               playback_config=playback, playout=copy.deepcopy(c['playout']))
    p.update(image=str(image), elf_sha256=c['elf_sha256'], freeze=v['name'], declaration_id='step-9-nova-live-v8.7')
    p['candidate_change'] = dict(decision=c['decision'], parent_commit=c['parent_commit'], previous=previous,
        previous_plan_sha256=c['previous_plan_sha256'],
        decisions_record=dict(path=str(v['destination']/'inputs'/DECISIONS.name), sha256=DECISIONS_SHA,
                              copied_from=str(DECISIONS)),
        rule='Decision 54 +40 ms playout reserve only. Cells, fixtures, bot config/profile, gates, timing, '
             'RF/host gates, restore image and budget are the v8.6 Nova final contract.')


def runtime_files():
    return sorted((set(n.runtime_files()) | {'docs/echoear-aec-nova-live-prep.md'}) - OFFLINE)


def plan_from(variant=V86):
    if sha(COMBINED)!=COMBINED_SHA:raise ValueError('Expected e32eaff rejudged combined evidence')
    p = json.loads(SOURCE.read_text())
    measured = json.loads(COMBINED.read_text())
    from echoear_aec_nova_preflight import stats
    strict_turns=[t for m in measured['measurements'] if m['strict_measurement_complete']
                  for t in m['turn']['accepted_turns']]
    strict_stats={key:stats(t.get(key) for t in strict_turns) for key in
                  ('final_segment_delay_s','turn_completion_delay_s')}
    if measured['summary']['retained_acquisitions'] != 6 or not all(
            r['retained_acquisition_usable'] for r in measured['measurements']):
        raise ValueError('Six usable combined acquisitions required')
    paused = copy.deepcopy(n.plan_from(SOURCE, FIXTURE, variant['output'])['cases'][-1])
    paused.update(id=PAUSED, capture_group=PAUSED, report_only=True,
                  application_findings_report_only=True)
    for k in ('code_sha256','inputs','fixture_sha256','draft_spec_sha256','reviewed_spec_sha256',
              'pending_before_freeze','proposed_runner','host_commit'):
        p.pop(k, None)
    p.update(schema='nova-live-v8.6-final-1', status='PREPARED; review and USER-GO required',
             output=str(variant['output']), qualification=False, campaign_authorized=False, resume_from=None,
             profile=measured['current_profile'], vendor=p['nova_live']['vendor_model'],
             budget=n.MAIN_BUDGET.copy(), user_go=str(variant['destination']/'USER-GO'),
             source=str(SOURCE), combined_preflight=str(COMBINED), fixture_manifest=str(FIXTURE))
    p['controls'].append(paused)
    for name in p['nova_live']['event_schema']['files']:
        p['nova_live']['event_schema']['files'][name]=sha(ROOT/name)
    # The source contract and all firmware gates remain unchanged. The complete
    # winding response is not inferred from its interrupted retained prefixes.
    p['preflight_contract']['real_reply'] = dict(
        status='MEASURED NOVA; descriptive, not a population bound', source=str(COMBINED),
        source_sha256=sha(COMBINED), retained_replies=3, median_s=8.72, max_s=9.36,
        prompt_file='server/demo_bot.py', prompt_file_sha256=sha(ROOT/'server/demo_bot.py'),
        prompt_rule='Observed sample maximum only; unchanged 24 s workload / 28 s capture caps.')
    p['nova_live'].update(implementation_ready=True, launch_authorized=False,
        stt_profile=measured['current_profile'],
        measured_bot_preflight=dict(path=str(COMBINED), sha256=sha(COMBINED),
            summary=measured['summary'], strict_turn_timing_summary=strict_stats,
            pooled_timing_note='Original pooled delays include both failed paused fragments; use strict_turn_timing_summary for successful-turn timing.',
            limits=measured['limits'],
            source_continuity='Interrupted winding prefixes are censored; per-attempt source/opportunity gates still apply.'),
        sentence_split_regression=dict(id=PAUSED, fixture=paused['fixture'],
            fixture_sha256=paused['fixture_sha256'], expected_text=paused['expected_text'],
            gate=False, report_only=True, counts_toward_barge_denominator=False,
            rule='Decision 30: retain each turn, request and timeline; splits never gate or use/create opportunity spares.'),
        sentence04_regression=dict(fixture_id='sentence-04-normal',
            rule='Normal sentence04 stays in its ten-eligible gated group; retained real-aggregator regression is also tested.'),
        preservation=dict(gate=False,tolerances=None,reason='No approved tolerances'))
    p['time_estimate'].update(hard_wallclock_bound_s=3600,
        # Same conservative 100 records/s per-association envelope as v8.5,
        # rounded up for the entire 3600-second cap (including restore reserve).
        v85_budget=dict(rtp_evidence_max_packets=400000),
        budget_note='Accepted ~46 minute nominal plan, not a guarantee. Spares conditional; incomplete at cap fails.')
    p['failure_policy']['harness_retries']['resume']='No resume in this short session.'
    p['failure_policy']['serial_host_resume']='No resume; preserve failure and restore.'
    p['candidate'].pop('bot_commit',None);p['candidate'].pop('runner_commit',None)
    if variant['candidate']: candidate_override(p, variant)
    return p


def input_manifest(p):
    from echoear_aec_nova_completion import manifest,check_manifest
    check_manifest(json.loads(COMBINED.read_text())['analysis_provenance']['retained_inputs_sha256'])
    files={str(f):sha(f) for f in (SOURCE,COMBINED,FIXTURE)}
    # Bind original runner analyses as well as every retained raw source. Never
    # recursively validate historical plans' embedded mutable decision hashes.
    for run in ('nova-prestep-01','nova-prestep-02'):
        files.update(manifest(BASE/run))
    for key in ('cases','spares','controls'):
        for c in p[key]:
            if c.get('fixture'): files[c['fixture']]=sha(c['fixture'])
    record=p.get('candidate_change',{}).get('decisions_record')
    if record:
        if sha(record['path'])!=DECISIONS_SHA: raise ValueError('Bound decision copy changed')
        files[record['path']]=DECISIONS_SHA
    return files


def validate(p):
    extras={'inputs_sha256','runtime_sha256','prepared_from_commit','installed_packages_sha256'}
    variant=variant_of(p)
    if {k:v for k,v in p.items() if k not in extras} != plan_from(variant):
        raise ValueError('Short-session frozen contract changed')
    if p['inputs_sha256'] != input_manifest(p): raise ValueError('Retained evidence changed')
    if p['runtime_sha256'] != {f:sha(ROOT/f) for f in runtime_files()}:
        raise ValueError('Live runtime changed')
    from demo_policy import profile_manifest
    if profile_manifest('dev') != p['profile']: raise ValueError('Profile changed')
    if p['installed_packages_sha256'] != {k:v['tree_sha256'] for k,v in installed_package_hashes().items()}:
        raise ValueError('Installed packages changed')
    elf=variant['candidate']['elf_sha256'] if variant['candidate'] else n.diagnostic.CANDIDATE
    n.verify_bundle(Path(p['image']),elf,15)
    n.verify_bundle(n.NORMAL,NORMAL_HASH)
    return True


def launcher(plan, digest, variant=V86):
    q=shlex.quote;destination=variant['destination']
    return '\n'.join(['#!/usr/bin/env bash','set -euo pipefail',
        f'test "${{{REVIEW_ENV}:-}}" = {q(digest)} || {{ echo "Reviewed plan hash required" >&2; exit 2; }}',
        f'test -f {q(str(destination/"USER-GO"))} || {{ echo "USER-GO required" >&2; exit 2; }}',
        f'test "$(cat {q(str(destination/"USER-GO"))})" = {q(digest)} || {{ echo "USER-GO must contain this plan hash" >&2; exit 2; }}',
        'test -t 0 && test -t 1 || { echo "Interactive user terminal required" >&2; exit 2; }',
        'export ESPTOOL_PYTHON='+q(n.diagnostic.ESPTOOL_PYTHON), 'cd '+q(str(ROOT)),
        'exec env OPENBLAS_NUM_THREADS=1 PYTHONPATH=hil:server uv run --project server --frozen --offline '
        '--with pyserial --with pyelftools --with idf-component-manager==3.1.2 '
        'python hil/echoear_aec_nova_live.py run --plan '+q(str(plan))+' --out '+q(str(variant['output']))+
        ' --reviewed-plan-sha256 '+q(digest),''])


def prepare(variant):
    destination=variant['destination']
    if destination.exists() or variant['output'].exists(): raise FileExistsError('Fresh freeze and absent run required')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True):
        raise ValueError('Commit source before final freeze')
    if variant['candidate'] and sha(DECISIONS)!=DECISIONS_SHA: raise ValueError('Decision file changed; re-pin')
    p=plan_from(variant)
    destination.mkdir()
    try:
        if variant['candidate']:
            (destination/'inputs').mkdir();shutil.copy2(DECISIONS,destination/'inputs'/DECISIONS.name)
        p.update(prepared_from_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            inputs_sha256=input_manifest(p),runtime_sha256={f:sha(ROOT/f) for f in runtime_files()},
            installed_packages_sha256={k:v['tree_sha256'] for k,v in installed_package_hashes().items()})
        validate(p)
    except BaseException:
        shutil.rmtree(destination);raise
    plan=destination/'plan.json';write(plan,p);digest=sha(plan)
    launch=destination/'launch-nova-live.sh';launch.write_text(launcher(plan,digest,variant));launch.chmod(0o755)
    subprocess.run(['bash','-n',str(launch)],check=True)
    write(destination/'validation.json',dict(validated=True,plan_sha256=digest,launch_executed=False,
        user_go_created=False,hardware_access=False,prepared_from_commit=p['prepared_from_commit']))
    (destination/'launch-command.txt').write_text(f'{REVIEW_ENV}={digest} {launch}\n')
    (destination/'stop-instructions.txt').write_text(
        'Use Ctrl-C in the launching terminal, or identify the exact Python child argv:\n'
        'pgrep -af '+shlex.quote('python hil/echoear_aec_nova_live.py run --plan '+str(plan))+'\n'
        'After verifying argv, kill -TERM <python-pid>. Never SIGKILL, signal uv, or release the lock. '
        'Wait for normal ELF '+NORMAL_HASH+', mixer_equal=true, lock_released=true, no cleanup_errors. '
        'Restore failure parks with the lock; a 60 minute overrun never earns a pass.\n')
    return dict(plan_sha256=digest,commit=p['prepared_from_commit'])


def regression(events,recorded):
    """Keep all accepted turns, including a prefix request canceled before PCM."""
    from echoear_aec_nova_preflight import turn_measurements
    return dict(report_only=True,gate=False,turns=turn_measurements(
        {'expected_text':'Please change the subject to cooking.'},events,recorded),
        requests=[e for e in events if e.get('event')=='real-bot-llm-request'],
        timeline=[e for e in events if e.get('event') in ('user-turn-started',
            'user-turn-stopped','real-bot-llm-request','nova-transcript-boundary') or
            e.get('frame') in ('VADUserStartedSpeakingFrame','VADUserStoppedSpeakingFrame',
                'TranscriptionFrame','InterimTranscriptionFrame','UserStartedSpeakingFrame','InterruptionFrame')])


def generation_gate(events,recorded,disconnects,active_ends,excluded):
    """Pause application metrics report-only; global lifecycle/errors still gate."""
    from echoear_aec_campaign_analysis import generation_audit
    paused=lambda e: str(e.get('case_id','')).startswith(PAUSED+'-a')
    keys={(e.get('pc_id'),e.get('context_id')) for e in events if paused(e) and
        e.get('event')=='tts-context-request' and e.get('context_id')}
    # Keep failures and all connection/lifecycle events, even when case-tagged.
    app={'tts-generation-metric-start','tts-context-metric','tts-context-request',
         'script-request','real-bot-llm-request','host-next-reply-queued'}
    gate_events=[e for e in events if not (e.get('event') in app and
        ((e.get('pc_id'),e.get('context_id')) in keys if e.get('context_id') else paused(e)))]
    gate_recorded=[e for e in recorded if not (e.get('kind')=='tts-transport-write' and
        (e.get('pc_id'),e.get('context_id')) in keys)]
    gate=generation_audit(gate_events,gate_recorded,disconnects,active_ends,excluded)
    raw=generation_audit(events,recorded,disconnects,active_ends,excluded)
    gate['all_context_lifecycle_passed']=bool(raw['sessions']) and all(
        s['connection_windows']['passed'] and not s['active_errors'] for s in raw['sessions'])
    gate['passed'] &= gate['all_context_lifecycle_passed']
    return gate


def verdict(p,holds):
    from echoear_aec_lull import eligible,declared_rule,cancelled_check,stratum_summary
    rows=[r for h in holds if h.get('acceptance_credit') for r in h.get('cases',[])]
    clean=[r for r in rows if r.get('clean')];gated=[r for r in clean if not r.get('report_only')]
    scores={}
    for voice,kind,_ in GROUPS:
        selected=[r for r in gated if r['kind']==kind and r.get('voice_class','firm')==voice and
            r.get('expected_class')=='eligible' and eligible(r.get('opportunity'),declared_rule(p))]
        success=sum(bool(r.get('barge_success')) for r in selected)
        scores[voice+':'+kind]=dict(eligible=len(selected),successes=success,required_eligible=10,
            passed=len(selected)==10 and success>=9,strata=stratum_summary(selected))
    required={c['id'] for c in p['cases']+p['controls'] if c['id']!=PAUSED}
    seen={r.get('cell_id') for r in clean}
    unique=[r.get('cell_id') for r in clean]
    echoes={str(v):[r for r in gated if r['kind']=='echo' and r['volume_db']==v] for v in (-6,0)}
    echo_ok=all(len(rs)==2 and all(r.get('acoustic_passed') and r.get('accepted_false_turns')==0 and
        r.get('turn_events',{}).get('interruptions')==0 for r in rs) for rs in echoes.values())
    cancel=cancelled_check([r for r in rows if r.get('cell_id')!=PAUSED])
    complete=not required-seen and len(unique)==len(set(unique)) and all(s['passed'] for s in scores.values())
    passed=bool(len(holds)==1 and holds[0].get('id')=='qualification-01' and holds[0].get('passed') and
        holds[0].get('nova_profile',{}).get('passed') and complete and echo_ok and cancel['passed'])
    return dict(passed=passed,qualification=False,scope='short Nova live validation only',
        complete_denominators=bool(complete),barge_in=scores,echo_passed=echo_ok,
        missing_declared_ids=sorted(required-seen),cancelled_lull_check=cancel,
        report_only_exposures=[r for r in rows if r.get('report_only')],default_changed=False)


def hold_type(budget):
    from echoear_aec_campaign import BenchHold
    class LiveHold(n.hold_type(budget)):
        rf_pre_gate_seconds=30
        # Preserve the campaign's three-attempt baseline policy.
        baseline_capture=BenchHold.baseline_capture
        def capture(self,*args,**kwargs):
            if budget.remaining()<self.p['budget']['minimum_cell_remaining_s']:
                raise n.diagnostic.BudgetExpired('Insufficient active time for next cell; restore')
            return super().capture(*args,**kwargs)
    return LiveHold


def acquire(p,out,budget,factory):
    from echoear_aec_campaign import run_schedule
    out.mkdir(exist_ok=False);write(out/'declaration.json',p)
    with factory(p,p['sessions'][0],out/'qualification-01') as h:
        h.environmental_counts={'campaign':0}
        h.connect(p['cases']+p['spares']+p['controls'],[])
        h.link_probe()
        run_schedule(h,p['cases']+p['controls'],p['spares'],p['noise_gate'])
        h.finish()
        if not h.result.get('completed'):raise RuntimeError('Final integrity audit failed')
    r=h.result
    if r.get('restored_elf_sha256')!=NORMAL_HASH or r.get('mixer_equal') is not True or r.get('lock_released') is not True or r.get('cleanup_errors'):
        raise RuntimeError('Cleanup incomplete')


def run(plan,out,reviewed_sha):
    plan=Path(plan).resolve()
    variant=next((v for v in VARIANTS.values() if plan==v['destination']/'plan.json'),None)
    if variant is None:raise ValueError('Plan is not a declared freeze bundle')
    if sha(plan)!=reviewed_sha:raise ValueError('Reviewed plan SHA required')
    # Check before validating inputs, environment, output creation or lock.
    marker=variant['destination']/'USER-GO'
    if not marker.is_file() or marker.read_text().strip()!=reviewed_sha:
        raise ValueError('USER-GO containing the reviewed plan SHA required')
    p=json.loads(plan.read_text());validate(p)
    if variant_of(p) is not variant:raise ValueError('Plan variant does not match its bundle')
    out=Path(out).resolve()
    if out!=variant['output'] or out.exists() or out.is_symlink():raise ValueError('Fresh declared output required')
    from echoear_aec_accept import check_esptool
    from echoear_aec_host_gate import prelaunch
    check_esptool()
    if not prelaunch(p['host_environment'])['passed']:raise RuntimeError('Host UUID/BSSID guard failed')
    started=time.monotonic();error=None
    try:
        with n.Budget(p['budget']) as budget:acquire(p,out,budget,hold_type(budget))
        if budget.total_expired or time.monotonic()-started>3600:
            raise n.diagnostic.BudgetExpired('Total cap expired; no pass')
    except BaseException as exc:error=exc;raise
    finally:
        if out.exists():
            write(out/'elapsed.json',dict(seconds=time.monotonic()-started,completed=error is None,
                total_s=3600,error=str(error) if error else None))
            from echoear_aec_campaign_analysis import analyze_campaign
            try:
                result=analyze_campaign(out)
                if error:result['summary']['passed']=False
                write(out/'analysis.json',result)
            except BaseException as exc:
                if error is None:raise
                error.add_note('Post-restore analysis failed: '+str(exc))


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='op',required=True)
    sub.add_parser('prepare').add_argument('--variant',choices=sorted(VARIANTS),required=True)
    for op in ('validate','run','analyze'):
        s=sub.add_parser(op);s.add_argument('--plan',type=Path,required=True)
        if op!='validate':s.add_argument('--out',type=Path,required=True)
        if op=='run':s.add_argument('--reviewed-plan-sha256',required=True)
    a=parser.parse_args()
    if a.op=='prepare':print(json.dumps(prepare(VARIANTS[a.variant]),indent=2))
    elif a.op=='validate':validate(json.loads(a.plan.read_text()))
    elif a.op=='run':run(a.plan,a.out,a.reviewed_plan_sha256)
    else:
        from echoear_aec_campaign_analysis import analyze_campaign
        print(json.dumps(analyze_campaign(a.out),indent=2))


if __name__=='__main__':main()

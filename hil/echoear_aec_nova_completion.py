"""File-only partial-run audit and fixed Nova completion retake preparation.

Only echoear_aec_nova_prestep.run may acquire hardware. Decision 27 permits
retaining the first four captures of this particular cleanly restored abort;
it is not a general exception for failed holds or failed capture gates.
"""
import argparse
import copy
from pathlib import Path
import shlex
import subprocess

import echoear_aec_nova_prestep as n
import echoear_aec_nova_preflight as a
from echoear_aec_bench import ROOT, sha, write, load_capture

PRIOR = n.BASE/'nova-prestep-01'
PRIOR_PLAN = n.BASE/'freeze-v8.6-nova-prestep/plan.json'
PRIOR_DIGEST = 'b33b49439bc37a9c4c6fe804295281cd6101a58ca998c4f153e4b387ccfdb2e2'
OUTPUT = n.BASE/'nova-prestep-02'
WARMUP = 'nova-prestep02-warmup-echo-minus6'
FAILURE = 'RuntimeError: Capture-interval or integrity failure; v8.5 has no link retake'
DERIVED = {'nova-preflight.json', 'combined-nova-preflight.json'}


def manifest(directory, *, derived=True):
    return {str(f):sha(f) for f in sorted(Path(directory).rglob('*'))
            if f.is_file() and (derived or f.name not in DERIVED)}


def check_manifest(expected):
    for name, digest in expected.items():
        if not Path(name).is_file() or sha(name) != digest:
            raise ValueError('Retained source changed: '+name)


def acquisition_audit(p, out, cell, recorded, baseline, qref):
    """Recompute saved acquisition gates without opening hardware or services."""
    from echoear_aec_campaign import check_capture_timing, observer_ok
    from echoear_aec_resources import check_reply_resources
    from echoear_aec_checks import live_admission, outgoing_rtvi_count
    from echoear_aec_accept import require_zero_i2s
    from echoear_aec_campaign_analysis import transport, delivery_counters
    from echoear_aec_noise_gate import quiet_gate
    from echoear_aec_metrics import frame_power, reference_threshold
    import host_nic
    cid = cell['id']+'-a1'; folder = out/cid
    if not folder.exists(): return dict(passed=False, status='not-run')
    try:
        retained = a.read(folder/'result.json')
        c = next(c for c in retained['cases'] if c['run_id']==cid)
        before, end, after = [a.read(folder/f) for f in ('before.json','capture-end.json','after.json')]
        observed = [a.read(f) for f in sorted(folder.glob('before-boundary-*.json'))]+[end]+[
            a.read(f) for f in sorted(folder.glob('after-boundary-*.json'))]
        require_zero_i2s(observed)
        raw, _, ref, blocks = load_capture(folder/'capture.npz')
        check_capture_timing(blocks,a.read(folder/'anchors.json'))
        quiet = [quiet_gate(raw[sl],baseline,p['noise_gate'],mic3=ref[sl])
                 for sl in (slice(0,32000),slice(-32000,None))]
        rf = [a.read(folder/f) for f in ('rf-pre-gate.json','rf.json')]
        checks = dict(resources=check_reply_resources(before,after,intermediate=observed),
            admission=live_admission(before,after,recorder_messages=outgoing_rtvi_count(recorded,c['start_ns'],c['end_ns'])),
            transport=transport(before,end,p['gates']['transport']['rule']),
            delivery=delivery_counters(before,end),
            rf=dict(passed=all(host_nic.gate(r['before'],r['after'])['passed'] for r in rf)),
            noise=dict(passed=baseline<=-53 and all(q['passed'] for q in quiet), windows=quiet),
            quiet_windows=dict(passed=not bool(blocks[:,4].sum()) and
                all(not (frame_power(x)>=reference_threshold(qref)).any() for x in (ref[:32000],ref[-32000:]))),
            observer=dict(passed=observer_ok(c['events'])), capture_timing=dict(passed=True))
        if cell.get('fixture'):
            checks['jabra'] = dict(passed=a.read(folder/('jabra-outcome-'+cid+'.json'),{}).get('passed') is True)
        if cell.get('stop_offset_s') is not None:
            timer = a.read(folder/('timer-'+cid+'.json'),{})
            checks['timer'] = dict(passed=timer.get('timing_passed') is True and not timer.get('skipped',True)
                and timer.get('requested_offset_s')==cell['stop_offset_s'] and timer.get('active_tts_witness',{}).get('active') is True,
                actual_offset_s=timer.get('actual_invocation_offset_s'), error_ms=timer.get('actual_minus_requested_ms'))
        checks['resources']['passed'] = checks['resources']['all_passed']
        saved_ok = bool(retained.get('checks')) and all(
            v.get('all_passed',v.get('passed')) is True for v in retained['checks'].values())
        return dict(passed=saved_ok and all(v['passed'] for v in checks.values()),
                    status='captured', checks=checks, saved_checks_passed=saved_ok)
    except (OSError,ValueError,KeyError,TypeError,StopIteration,RuntimeError) as exc:
        return dict(passed=False,status='incomplete',error=type(exc).__name__+': '+str(exc))


def partial():
    if sha(PRIOR_PLAN) != PRIOR_DIGEST: raise ValueError('Wrong retained plan')
    p = a.read(PRIOR_PLAN); sources = manifest(PRIOR)
    result = a.analyze(p,PRIOR,include_rejected=True)
    saved = a.read(PRIOR/'result.json'); elapsed = a.read(PRIOR/'elapsed.json')
    first = [c['id']+'-a1' for c in p['cases'][:4]]
    expected_failure = (not saved.get('completed') and saved.get('error')==FAILURE and
        saved.get('hard_failure',{}).get('capture_dir')==p['cases'][4]['id']+'-a1' and
        [c['run_id'] for c in saved['cases']]==first)
    mixer_equal = (PRIOR/'mixer-before.state').read_bytes()==(PRIOR/'mixer-after.state').read_bytes()
    restore_raw = 'elf_sha256='+n.NORMAL_HASH in (PRIOR/'restore-serial.log').read_text()
    shared = dict(expected_specific_abort=expected_failure,
        cleanup=result['cleanup_complete'] and mixer_equal and restore_raw,
        profile=result['vendor_binding']['passed'], generations=result['generation_audit']['passed'],
        host=result['host_audit']['passed'], periodicity=saved.get('periodicity',{}).get('passed') is True,
        within_budget=elapsed['seconds']<=p['budget']['total_s'] and not saved.get('budget_expired',True))
    recorded = sorted([r for f in (PRIOR/'sessions').glob('*/events.jsonl') for r in a.rows(f)],key=lambda r:r['monotonic_ns'])
    from echoear_aec_noise_gate import baseline_level
    from sctp_recovery import check_active_recovery
    raw,_,qref,_ = load_capture(PRIOR/'quiet.npz')
    baseline = baseline_level(raw,p['noise_gate'])
    for index,(cell,m) in enumerate(zip(p['cases'],result['measurements'])):
        audit = acquisition_audit(p,PRIOR,cell,recorded,baseline,qref)
        m['acquisition_audit'] = audit
        accepted = next((r for r in saved['cases'] if r['run_id']==m['id']),None)
        if index<4 and accepted:
            end_ns = accepted['end_ns']
            audit['sctp_recovery_through_capture'] = check_active_recovery(a.rows(PRIOR/'rtp/sctp.jsonl'),end_ns)
            audit['passed'] &= audit['sctp_recovery_through_capture']['passed']
        m['retained_acquisition_usable'] = bool(index<4 and all(shared.values()) and audit['passed'])
        m['use'] = ('retained-components-only' if m['retained_acquisition_usable'] else
                    'excluded-transport-failure-report-only' if index==4 else 'not-run' if index==5 else 'incomplete')
        m['source_run'] = str(PRIOR)
        m['strict_measurement_complete'] = m['retained_acquisition_usable'] and m['complete']
    result.update(schema='nova-partial-prestep-01-v1', whole_hold_completed=False,
        shared_retention_checks=shared, retained_sources_sha256=sources,
        retained_plan=dict(path=str(PRIOR_PLAN),sha256=PRIOR_DIGEST),
        summary=summary([m for m in result['measurements'] if m['retained_acquisition_usable']]),
        limits=['Capture 5 excluded from all qualified summaries; its complete retained events are descriptive only.',
                'Capture 6 never ran. No inference from absent evidence.',
                'Stop has no VAD start/stop witness: VAD-relative delays unavailable, not zero.',
                'Interrupted winding-path PCM is censored; no complete >=4 s source qualification.',
                'Partial-run retention is decision 27 scope, not whole-hold success or acceptance credit.'])
    check_manifest(sources)
    if sources != manifest(PRIOR): raise ValueError('Retained file set changed during analysis')
    return result


def summary(measurements):
    return dict(retained_acquisitions=len(measurements),
        strict_complete_captures=sum(m['complete'] for m in measurements),
        capture_s=a.stats(m['capture'].get('elapsed_s') for m in measurements),
        export_s=a.stats(m['export'].get('elapsed_s') for m in measurements),
        end_to_end_s=a.stats(m['elapsed_with_pre_gate_s'] for m in measurements),
        final_segment_delay_s=a.stats(t['final_segment_delay_s'] for m in measurements for t in m['turn']['accepted_turns']),
        turn_completion_delay_s=a.stats(t['turn_completion_delay_s'] for m in measurements for t in m['turn']['accepted_turns']),
        real_reply_pcm_s=a.stats(c['pcm_seconds'] for m in measurements if m['complete_reply'] for c in m['contexts']
            if c['complete'] and c['metrics'].get('caller_anchor')=='real-bot-llm-request'),
        real_reply_lead_s=a.stats(c['metrics'].get('lead_s') for m in measurements if m['complete_reply'] for c in m['contexts']
            if c['complete'] and c['metrics'].get('caller_anchor')=='real-bot-llm-request'))


def plan_from(prior_analysis):
    old = a.read(PRIOR_PLAN)
    p = n.plan_from(n.SOURCE,old['fixture_manifest'],OUTPUT)
    warm = copy.deepcopy(p['cases'][0])
    warm.update(id=WARMUP,capture_group=WARMUP,measurement_role='conditioning-only')
    p.update(schema='nova-prestep-completion-v1', declaration_id='step-9-nova-prestep-02',
        cases=[warm,*p['cases'][4:]],
        prior_analysis=str(Path(prior_analysis).resolve()),
        prior_run=str(PRIOR), prior_plan=str(PRIOR_PLAN), prior_plan_sha256=PRIOR_DIGEST,
        combination_policy=dict(prior_ids=[c['id']+'-a1' for c in old['cases'][:4]],
            current_ids=[c['id']+'-a1' for c in old['cases'][4:]],
            excluded_prior_id=old['cases'][4]['id']+'-a1', warmup_id=WARMUP+'-a1',
            selection='Fixed assignment by run path and case ID; no best-of selection or substitution',
            warmup_reason='Fresh candidate boot needs echo exposure before the warm normal cell; one -6 dB echo. '
                'Conditions AEC but cannot reproduce the exact four-capture history of run 01. No measurement credit.'))
    return p


def validate(p):
    if sha(PRIOR_PLAN)!=PRIOR_DIGEST: raise ValueError('Retained plan changed')
    expected = plan_from(p['prior_analysis'])
    extra = {'inputs_sha256','runtime_sha256','prepared_from_commit','installed_packages_sha256','prior_sources_sha256'}
    if {k:v for k,v in p.items() if k not in extra} != expected:
        raise ValueError('Frozen completion schedule or contract changed')
    if manifest(PRIOR) != p['prior_sources_sha256']: raise ValueError('Retained source set/bytes changed')
    prior = a.read(p['prior_analysis'])
    if prior['retained_sources_sha256'] != p['prior_sources_sha256']: raise ValueError('Analysis source binding changed')
    if not all(m['retained_acquisition_usable'] for m in prior['measurements'][:4]):
        raise ValueError('First four acquisitions are not retained')
    required = {str(n.SOURCE),str(n.COORDINATOR),str(PRIOR_PLAN),p['prior_analysis'],
                str(n.JOBS/'step-9-nova-prestep-02-request.md')}
    if set(p['inputs_sha256']) != required: raise ValueError('Unexpected input dependencies')
    return n.validate_bindings(p)


def current_acquisition(p, output, analysis):
    """Acquisition validity is independent of turn/reply measurement findings."""
    from echoear_aec_noise_gate import baseline_level
    from sctp_recovery import check_active_recovery
    import host_nic
    out = Path(output)
    saved = a.read(out/'result.json',{})
    elapsed = a.read(out/'elapsed.json',{})
    checks = dict(completed=saved.get('completed') is True and not saved.get('error'),
        elapsed=elapsed.get('completed') is True and not elapsed.get('error') and
            elapsed.get('seconds',float('inf'))<=p['budget']['total_s'] and not saved.get('budget_expired'),
        cleanup=analysis['cleanup_complete'], profile=analysis['vendor_binding']['passed'],
        host=analysis['host_audit']['passed'], periodicity=saved.get('periodicity',{}).get('passed') is True,
        resources=saved.get('resources',{}).get('all_passed') is True)
    audits = {}
    try:
        checks['mixer_bytes'] = (out/'mixer-before.state').read_bytes()==(out/'mixer-after.state').read_bytes()
        checks['restore_boot'] = 'elf_sha256='+n.NORMAL_HASH in (out/'restore-serial.log').read_text()
        rf = saved['rf_active']
        checks['rf_active'] = host_nic.gate(rf['before'],rf['after'])['passed']
        checks['sctp'] = check_active_recovery(a.rows(out/'rtp/sctp.jsonl'),saved['active_end_ns'])['passed']
        raw,_,qref,_ = load_capture(out/'quiet.npz')
        baseline = baseline_level(raw,p['noise_gate'])
        recorded = sorted([r for f in (out/'sessions').glob('*/events.jsonl') for r in a.rows(f)],key=lambda r:r['monotonic_ns'])
        audits = {cell['id']+'-a1':acquisition_audit(p,out,cell,recorded,baseline,qref) for cell in p['cases']}
        checks['all_capture_gates'] = all(v['passed'] for v in audits.values())
        checks['accepted_schedule'] = [r['run_id'] for r in saved.get('cases',[])] == [c['id']+'-a1' for c in p['cases']]
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        checks['retained_witnesses'] = False
        return dict(passed=False,checks=checks,captures=audits,error=type(exc).__name__+': '+str(exc))
    return dict(passed=all(checks.values()),checks=checks,captures=audits,
        note='Turn splits, false turns and interrupted reply metrics remain findings; they do not invalidate acquisition.')


def combine(p, output):
    """Bind both runs; failed old normal and warm-up are never substitutes."""
    check_manifest(p['prior_sources_sha256'])
    if manifest(PRIOR) != p['prior_sources_sha256']: raise ValueError('Prior file set changed')
    if sha(p['prior_analysis']) != p['inputs_sha256'][p['prior_analysis']]: raise ValueError('Prior analysis changed')
    prior = a.read(p['prior_analysis'])
    current = a.analyze(p,output,include_rejected=True)
    acquisition = current_acquisition(p,output,current)
    policy = p['combination_policy']
    old = {m['id']:m for m in prior['measurements']}
    new = {m['id']:m for m in current['measurements']}
    for cid,m in new.items():
        m['acquisition_audit'] = acquisition['captures'].get(cid,dict(passed=False))
        m['retained_acquisition_usable'] = bool(acquisition['passed'] and m['accepted_by_runner'] and m['acquisition_audit']['passed'])
        m['strict_measurement_complete'] = m['retained_acquisition_usable'] and m['complete']
    selected = [copy.deepcopy(old[cid]) for cid in policy['prior_ids']]
    for cid in policy['current_ids']:
        m = copy.deepcopy(new[cid]); m['source_run']=str(output)
        selected.append(m)
    good = [m for m in selected if m['retained_acquisition_usable']]
    return dict(schema='nova-combined-prestep-v1', qualification=False, launch_authorized=False,
        final_freeze_ready=False, complete_measurements=all(m['strict_measurement_complete'] for m in selected),
        combination_policy=policy, source_runs=[dict(path=str(PRIOR),inputs_sha256=p['prior_sources_sha256']),
            dict(path=str(output),inputs_sha256=manifest(output,derived=False))],
        prior_analysis=dict(path=p['prior_analysis'],sha256=sha(p['prior_analysis'])),
        current_profile=p['profile'], current_runtime_sha256=p['runtime_sha256'],
        measurements=selected, excluded_prior_capture=old[policy['excluded_prior_id']],
        conditioning_only=new[policy['warmup_id']], current_hold_analysis=current,
        current_acquisition=acquisition, summary=summary(good), prior_run_limits=prior['limits'],
        limits=['Acquisition usability is separate from strict turn/reply findings; inspect every retained row.',
                'Stop VAD-relative timing remains unavailable; interrupted source contexts remain censored.',
                'No qualification credit or main-freeze authorization.'])


def prepare(destination, prior_analysis):
    destination = Path(destination).resolve()
    if destination.exists() or destination.is_symlink() or OUTPUT.exists() or OUTPUT.is_symlink():
        raise FileExistsError('Fresh prep directory and absent nova-prestep-02 output required')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True):
        raise ValueError('Commit preparation source first')
    p = plan_from(prior_analysis)
    old = a.read(PRIOR_PLAN)
    if p['profile'] != old['profile']: raise ValueError('Profile changed since pre-step 01')
    packages = n.installed_package_hashes()
    if {k:v['tree_sha256'] for k,v in packages.items()} != old['installed_packages_sha256']:
        raise ValueError('Installed implementations changed')
    inputs = (n.SOURCE,n.COORDINATOR,PRIOR_PLAN,Path(prior_analysis).resolve(),n.JOBS/'step-9-nova-prestep-02-request.md')
    p.update(inputs_sha256={str(f):sha(f) for f in inputs}, prior_sources_sha256=manifest(PRIOR),
        runtime_sha256={f:sha(ROOT/f) for f in n.runtime_files()},
        prepared_from_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        installed_packages_sha256=old['installed_packages_sha256'])
    validate(p)
    destination.mkdir(parents=True)
    write(destination/'plan.json',p)
    write(destination/'installed-package-hashes.json',packages)
    write(destination/'main-session-duration-contract.json',p['main_session'])
    write(destination/'combined-measurements.pending.json',combine(p,OUTPUT))
    script = destination/'launch-after-review.sh'
    script.write_text(n.launcher(destination/'plan.json',OUTPUT)); script.chmod(0o755)
    subprocess.run(['bash','-n',str(script)],check=True)
    digest = sha(destination/'plan.json')
    command = n.REVIEW_ENV+'='+digest+' bash '+shlex.quote(str(script))
    write(destination/'validation.json',dict(valid=True,run_executed=False,output_absent=not OUTPUT.exists(),plan_sha256=digest))
    (destination/'COORDINATOR.md').write_text('# Prepared only — not executed\n\nAfter separate coordinator review/go:\n\n```bash\n'+command+'\n```\n\n'
        'Output must remain absent: `'+str(OUTPUT)+'`. No USER-GO is created.\n\n'
        'One conditioning-only red-kite echo at -6 dB, then normal sentence04 +1.5 s with real reply, then the identical '
        '700 ms pause fixture with board initially silent and real reply. The warm-up conditions AEC after a fresh boot; '
        'it cannot recreate the exact earlier adaptive history and earns no measurement credit. No retries/spares.\n\n'
        'Same candidate 61aa38d5 / normal 9936da45, gain 15, guard 400, cat, dev Nova profile and port 7861. '
        'All noise, RF, capture, transport, resources, admission, observer, liveness and cleanup gates are unchanged. '
        '20 minute total: 840 s active + 360 s restore reserve; 210 s minimum remaining to start a cell, 28 s capture cap. '
        'Main-session 60 minute contract and denominators remain unchanged and blocked for separate integration/review.\n\n'
        'No live decision-file hash is a launch dependency: neither -c.md nor -d.md is opened or hashed by this validator. '
        'Decision 27 scope is fixed in the committed implementation and this plan. The old frozen plan is bound as historical '
        'bytes only; its embedded decision hash is never recursively validated. Later coordinator notes do not invalidate this launch.\n\n'
        'The prior plan, full run-01 file manifest and partial analysis are bound. Post-run combined-nova-preflight.json binds both '
        'runs and fixes slots 1–4 to run 01, slots 5–6 to run 02. Failed old normal remains excluded and visible. Warm-up is never '
        'a replacement. Missing Stop VAD timing and complete source continuity still require review; completion does not authorize a main freeze.\n\n'
        'No outer flock: Python owns the lock. Stop only the Python PID whose complete argv matches this plan, output and reviewed hash, '
        'using TERM. Never signal uv/nohup, use SIGKILL or force-release the lock. Wait for normal ELF '+n.NORMAL_HASH+', '
        'mixer_equal=true, lock_released=true, no cleanup_errors. Restore/mixer failure parks with the lock.\n')
    return p


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    analyze=sub.add_parser('analyze'); analyze.add_argument('--out',required=True)
    prep=sub.add_parser('prepare'); prep.add_argument('--destination',required=True); prep.add_argument('--prior-analysis',required=True)
    args=parser.parse_args()
    if args.command=='analyze': write(Path(args.out),partial())
    else: prepare(args.destination,args.prior_analysis)

"""Step-16 negative controls. Synthetic only; run after bench-free permission."""
import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import p4_parity as parity
import p4_parity_liveness as live
from p4_parity_touch import touch_witness
from test_p4_liveness import controls,drained,sample,p4_run
from test_wire_contract import capture,serialize,IDENTITY
from wire_contract import Bounds


def test_inventory_has_exactly_25_unique_features_and_six_images():
    assert [f[0] for f in parity.FEATURES]==['F'+str(i) for i in range(1,21)]+['X'+str(i) for i in range(1,6)]
    assert len(parity.BOARDS)*len(parity.VARIANTS)==6
    assert parity.sessions('normal')==['main']
    assert parity.sessions('aec-demo')==['main','resume','cold']


def test_empty_table_never_claims_parity_and_keeps_exceptions_and_labels():
    text=parity.render([])
    assert len(re.findall(r'^\| F\d+ ',text,re.M))==20
    assert len(re.findall(r'^\| X\d+ ',text,re.M))==5
    assert 'Smoke parity, not qualification' in text and 'measured-diagnostic' in text
    assert 'exception: no battery hardware' in text and 'upstream of AEC' in text
    assert 'provisional: bench speaker not final hardware' in text
    assert '| pass |' not in text


def coverage(board='p4xc',status='pass',step='16'):
    return [dict(board=board,category=kind,features=['F1'],variant=variant,status=status,step=step,
        sources={'/evidence/'+variant+'/'+kind+'.json':'a'*64}) for variant in parity.VARIANTS for kind in ('smoke','liveness')]


def row(text,feature):return next(line for line in text.splitlines() if line.startswith('| '+feature+' '))


def test_complete_coverage_is_required_for_each_variant_and_evidence_family():
    receipts=coverage()
    assert '| unverified | pass |' in row(parity.render(receipts),'F1')
    assert '| unverified | unverified |' in row(parity.render(receipts[:-1]),'F1')
    assert '| unverified | unverified |' in row(parity.render(coverage(step='14')),'F1')
    assert '| unverified | report-only |' in row(parity.render(coverage(status='report-only')),'F1')
    receipts[0]['status']='fail'
    assert '| unverified | fail |' in row(parity.render(receipts),'F1')


def test_unrelated_pass_does_not_promote_console_or_acoustic_dsp():
    text=parity.render(coverage())
    for feature in ('F6','F15','F16','F17'):assert '| unverified | unverified |' in row(text,feature)


def test_table_cannot_merge_passes_from_different_campaign_plans():
    receipts=coverage();receipts[0]['plan_sha256']='a'*64;receipts[1]['plan_sha256']='b'*64
    with pytest.raises(ValueError,match='different parity plans'):parity.render(receipts)


@pytest.mark.parametrize('untouched',[False,True])
def test_failed_attempts_are_retained_as_failed_or_unverified_receipts(tmp_path,monkeypatch,untouched):
    result=dict(board='p4xc',variant='aec-demo',acquisition_passed=False,device_untouched=untouched,error='failure')
    if untouched:result.update(status='preflight-noisy',retryable=True,score_produced=False,room=dict(noise_contaminated=True))
    (tmp_path/'result.json').write_text(json.dumps(result))
    def failed(*args):raise ValueError('missing measured gate')
    monkeypatch.setattr(parity,'replay',failed)
    receipt=parity.collect('smoke','p4xc','aec-demo',tmp_path)
    assert receipt['status']==('unverified' if untouched else 'fail')
    assert str(tmp_path/'result.json') in receipt['sources'] and 'F5' in receipt['features']


def test_output_must_be_fresh_and_under_the_campaign_root(tmp_path):
    plan=dict(root=str(tmp_path/'campaign'))
    assert parity.output_path(plan,tmp_path/'campaign/part/attempt-01')==tmp_path/'campaign/part/attempt-01'
    with pytest.raises(ValueError):parity.output_path(plan,tmp_path/'other/attempt-01')
    (tmp_path/'campaign').mkdir();(tmp_path/'campaign/attempt-01').mkdir()
    with pytest.raises(ValueError,match='retain every attempt'):parity.output_path(plan,tmp_path/'campaign/attempt-01')


def pin_evidence(tmp_path,monkeypatch):
    import p4_step14
    pin=tmp_path/'pin';root=pin/'coordinator-run-1'
    monkeypatch.setattr(parity,'PIN',pin)
    primary=tmp_path/'retained.json';primary.write_text('{}');bindings={str(primary):parity.digest(primary)}
    for board in parity.BOARDS:
        for variant in ('normal','aec-normal'):
            elf=hashlib.sha256((board+'/'+variant).encode()).hexdigest()
            parity.write_new(pin/'final-bundles'/('bundle-'+board+'-'+variant)/'manifest.json',dict(elf_sha256=elf))
            parity.write_new(root/('smoke-'+board+'-'+variant)/'result.json',dict(board=board,variant=variant,
                elf_sha256=elf,passed=True,acquisition_passed=True,calibration=False,reference_only=False,
                sessions=[dict(label='main')],turns=[{} for _ in range(12)],unmeasured_steps=[],identity=IDENTITY,
                restore=dict(clean=True,action='p4-product-of-record' if board=='p4xc' else 's3-restore')))
    normal=parity.load(pin/'final-bundles/bundle-p4xc-normal/manifest.json')['elf_sha256']
    aec=parity.load(pin/'final-bundles/bundle-p4xc-aec-normal/manifest.json')['elf_sha256']
    monkeypatch.setattr(p4_step14,'restore_record',lambda *args:dict(elf_sha256=normal))
    parity.write_new(root/'boot-proof/restore-boot-proof.json',dict(passed=True,record_sha256=parity.RESTORE_SHA256,
        elf_sha256=normal,restore=dict(clean=True)))
    parity.write_new(root/'p4-aec-report/report.json',dict(kind='p4-aec-applied-image-report',available=True,
        elf_sha256=aec,gain_db=30,median_erle_db=21.217592320562773,identity=IDENTITY,sources=bindings))
    parity.write_new(root/'pair-aec-normal.json',dict(checks_passed=True,variant='aec-normal',sources=[bindings,bindings]))
    return root,primary


def test_completed_pin_verification_populates_inputs_without_claiming_parity(tmp_path,monkeypatch):
    root,_=pin_evidence(tmp_path,monkeypatch);result=parity.pin_verification(root)
    assert result['passed'] and len(result['smokes'])==4
    assert result['p4_aec_report']['median_erle_db']==pytest.approx(21.217592320562773)
    assert result['p4_aec_report']['report_only'] and result['aec_pair']['checks_passed']
    assert 'coverage remains pending' in result['scope'] and not result['qualification']


@pytest.mark.parametrize('change',['boot','pair','erle-elf','unavailable','gain','nan','smoke','source'])
def test_pin_inputs_reject_missing_failed_or_changed_prerequisites(tmp_path,monkeypatch,change):
    root,source=pin_evidence(tmp_path,monkeypatch)
    if change=='source':source.write_text('{"changed":true}')
    else:
        path=root/('boot-proof/restore-boot-proof.json' if change=='boot' else 'pair-aec-normal.json' if change=='pair'
            else 'smoke-p4xc-aec-normal/result.json' if change=='smoke' else 'p4-aec-report/report.json')
        value=parity.load(path)
        if change in ('boot','pair'):value['passed' if change=='boot' else 'checks_passed']=False
        elif change=='erle-elf':value['elf_sha256']='0'*64
        elif change=='unavailable':value['available']=False
        elif change=='gain':value['gain_db']=27
        elif change=='nan':value['median_erle_db']=float('nan')
        else:value['unmeasured_steps']=['main:stack']
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):parity.pin_verification(root)


@pytest.mark.parametrize('change',['error','cleanup','restore','wrong-action','incomplete'])
def test_failed_or_incomplete_restore_cannot_support_parity(change):
    result=dict(board='p4xc',acquisition_passed=True,restore=dict(clean=True,action='p4-product-of-record'))
    if change=='error':result['error']='turn incomplete'
    elif change=='cleanup':result['cleanup_errors']=['failed disconnect']
    elif change=='restore':result['restore']['clean']=False
    elif change=='wrong-action':result['restore']['action']='factory'
    else:result['acquisition_passed']=False
    with pytest.raises(ValueError):parity.clean_result(result)


def test_receipt_rehashes_sources_and_cannot_invent_feature_or_status(tmp_path,monkeypatch):
    source=tmp_path/'source.json';source.write_text('{}')
    result=dict(kind='step16-evidence-receipt',board='p4xc',variant='normal',category='smoke',
        step='16',status='unverified',features=['F1'],run=str(tmp_path),peer=None,
        sources={str(source):parity.digest(source)},qualification=False,
        plan_file=str(source),plan_sha256=parity.digest(source))
    path=tmp_path/'receipt.json';path.write_text(json.dumps(result))
    monkeypatch.setattr(parity,'bound_receipt',lambda *args:copy.deepcopy(result))
    assert parity.receipt(path)==result
    forged=dict(result,status='pass');path.write_text(json.dumps(forged))
    with pytest.raises(ValueError,match='cannot override'):parity.receipt(path)
    path.write_text(json.dumps(result));source.write_text('{"changed":true}')
    with pytest.raises(ValueError,match='source changed'):parity.receipt(path)


@pytest.mark.parametrize('change',['commit','dirty','profile','manifest'])
def test_bundle_binding_rejects_old_or_dirty_or_different_pins(tmp_path,monkeypatch,change):
    import make_bundle
    pin=tmp_path/'pin';bundle=tmp_path/'bundle';bundle.mkdir()
    canonical=pin/'final-bundles/bundle-p4xc-demo';canonical.mkdir(parents=True)
    for p in (bundle,canonical):(p/'manifest.json').write_text('{}')
    m=dict(firmware_commit=parity.COMMIT,source_files_sha256={},source_diff_sha256=hashlib.sha256(b'').hexdigest(),profile='demo')
    monkeypatch.setattr(parity,'PIN',pin);monkeypatch.setattr(make_bundle,'verify_bundle',lambda *args:(m,{}))
    if change=='commit':m['firmware_commit']='a'*40
    elif change=='dirty':m['source_files_sha256']={'firmware.cpp':'a'*64}
    elif change=='profile':m['profile']='aec-normal'
    else:(bundle/'manifest.json').write_text('{"changed":true}')
    with pytest.raises(ValueError):parity.pinned_bundle(bundle,'p4xc','demo')


@pytest.mark.parametrize('aec',[False,True])
def test_liveness_wire_uses_actual_aec_intent_and_preserves_fixed_bound(aec):
    wire=capture('esp32p4',aec)
    lines=serialize(wire)+['PHASE closed']
    capability=[l for l in lines if 'DEMO_CAPABILITY ' in l]
    serial=[l for l in lines if 'DEMO_CAPABILITY ' not in l]
    parsed,_,timing=live.liveness_capture(serial,capability,'p4xc',Bounds(68411,7000),aec=aec)
    assert parsed.records['start']['body']['audio']['aec'] is aec
    assert 'report-only' in timing['status']
    with pytest.raises(ValueError):live.liveness_capture(serial,capability,'p4xc',Bounds(68411,7000),aec=not aec)


def test_normal_has_no_f20_and_demo_requires_complete_cold_asset_proof():
    assert live.session_labels(False)[:2]==['main','pre-wifi']
    assert 'cold' not in live.session_labels(False)
    assert live.session_labels(True)[:3]==['main','resume','cold']
    assert sum(s.startswith('cycle-') for s in live.session_labels(False))==20
    live.f20_check(dict(applicable=False,reason='normal/manual image'),False)
    evidence=dict(held=dict(held=True),resumed=True,cold=True,assets=dict(passed=True))
    live.f20_check(evidence,True)
    with pytest.raises(ValueError):live.f20_check(evidence,False)
    evidence['assets']['passed']=False
    with pytest.raises(ValueError):live.f20_check(evidence,True)


def test_new_s3_candidate_does_not_inherit_reference_resource_waiver():
    samples=[sample() for _ in range(21)]
    report=live.resource_reports(samples,samples,'echoear',drained())
    assert all(r['report_only'] is False for r in report)
    with pytest.raises(ValueError):live.resource_reports(samples,samples,'echoear',drained(150000))
    bad=sample();bad['heap']['internal']['largest']=32767
    with pytest.raises(ValueError):live.sample_check(bad,'echoear')


def populate_liveness_proof(tmp_path,a,monkeypatch,planned=None):
    import p4_parity_liveness_proof as proof
    policy=parity.fault_policy(IDENTITY) if planned is None else parity.load(planned)['fault_bot_policy']
    identity={k:policy[k] for k in ('bot_source_sha','policy_source_sha','profile_manifest')}
    identities=[dict(identity,bot_instance='boot:101:1'),dict(identity,bot_instance='boot:202:2')]
    before=next(s for s in a['sessions'] if s['label']=='bot-before');request=before['ready_ns']+50
    up=[1,request+25];raw=[[],[]];events=[json.loads(l) for l in (tmp_path/'control.jsonl').read_text().splitlines()]
    following=False
    for i,s in enumerate(a['sessions']):
        if s['label']=='bot-after':following=True
        side=int(following);old=s['channel'];s['channel']=('202' if side else '101')+':'+str(i+1)
        s['bot_instance']=identities[side]['bot_instance']
        raw[side].append(dict(kind='display-ready',channel=s['channel'],epoch=s['epoch'],monotonic_ns=s['ready_ns'],direction='received'))
        raw[side].extend(dict(e,channel=s['channel']) for e in events if e['channel']==old)
    a['control_sha256']='';(tmp_path/'control.jsonl').unlink()
    directories=[tmp_path/'fault-initial',tmp_path/'fault-replacement'];paths=[]
    for i,directory in enumerate(directories):
        directory.mkdir();marker=dict(up_monotonic_ns=up[i],identity=identities[i],observer_sha256=policy['observer_sha256'])
        parity.write_new(directory/'ready.json',marker);parity.write_new(directory/'identity.json',identities[i])
        parity.write_new(tmp_path/('initial-ready.json' if i==0 else 'replacement-ready.json'),marker)
        parity.write_new(tmp_path/('identity.json' if i==0 else 'replacement-identity.json'),identities[i])
        path=directory/'dc-events.jsonl';path.write_text(''.join(json.dumps(e)+'\n' for e in sorted(raw[i],key=lambda e:e['monotonic_ns'])))
        paths.append(path)
    parity.write_new(tmp_path/'restart-request.json',dict(bot_instance=identities[0]['bot_instance'],
        request_monotonic_ns=request,replacement_dir=str(directories[1])))
    a.update(control_sources=proof.capture_controls(paths,[0,0],identities,tmp_path),bot_identities=identities,
        initial_bot_dir=str(directories[0]),replacement_dir=str(directories[1]),
        control_sha256=parity.digest(tmp_path/'control.jsonl'),source_sha256=parity.source_hashes(),
        helper_sha256=parity.digest(Path(live.__file__)),observer_sha256=policy['observer_sha256'])
    a['settled_evidence']=[dict(samples=[copy.deepcopy(s) for _ in range(3)],converged=True,policy=copy.deepcopy(live.SETTLED))
        for s in a['settled_samples']]
    populate_liveness_serial(tmp_path,a)
    if planned is None:
        plan=dict(root=str(tmp_path),fault_bot_policy=policy,source_sha256=a['source_sha256'])
        parity.write_new(tmp_path/'plan.json',plan);planned=tmp_path/'plan.json'
        monkeypatch.setattr(proof,'checked_plan',lambda path:copy.deepcopy(plan))
    else:plan=parity.load(planned)
    a.update(plan_file=str(planned),plan_sha256=parity.digest(planned))
    return plan


def populate_liveness_serial(tmp_path,a):
    """Real Ready/close/PERF shapes, including the generation of each wire file."""
    from p4_parity_serial import window,text
    entries=[]
    def append(line,stamp):
        entries.append(dict(cursor=len(entries)+1,monotonic_ns=stamp,line=line))
    chip='esp32p4' if a['board']=='p4xc' else 'ESP32-S3 step 7'
    append('I (1) dg-esp32: '+chip+' audio elf_sha256='+a['elf_sha256'],1)
    append('I (2) dg-esp32: DG_BOOT board='+a['board']+' chip='+('esp32p4' if a['board']=='p4xc' else 'esp32s3')+' idf=v6.1',2)
    if a['aec_enabled']:append('MIC3_GAIN=0x15 analog_db=15',3)
    append('READY transport-console',4);boot_cursor=len(entries)
    for s in a['sessions']:
        start=len(entries);ready=s['ready_ns'];closed=s['ended_ns'];gen=s['generation']
        old=(tmp_path/s['wire_file']).read_text().splitlines()
        caps=[line for line in old if 'DEMO_CAPABILITY ' in line]
        wire=[re.sub(r'UPLINK_INTERVALS gen=\d+',f'UPLINK_INTERVALS gen={gen}',line) for line in old
            if 'DEMO_CAPABILITY ' not in line and 'PHASE closed' not in line]
        append('I (3) dg-app: STATE Starting gen='+str(gen),ready-2)
        append(wire[0],ready-1)
        append('I (4) dg-app: STATE Ready gen='+str(gen)+' desired=1 t=12345',ready);s['ready_cursor']=len(entries)
        append('DISPLAY '+json.dumps(dict(epoch=s['epoch'])),ready+1)
        for line in wire[1:]:append(line,ready+2)
        for sample in s['samples']:
            sample.update(generation=gen,pc_id='peer-'+s['label'])
            append('PERF '+json.dumps(sample),ready+3)
        s['stop_cursor']=len(entries)
        append('I (5) esp-peer: PHASE closed t=67890 internal=160000',closed)
        s.update(close_cursor=len(entries),boot_cursor=boot_cursor,serial_window=window(entries,start,len(entries)))
        segment=entries[start:];(tmp_path/(s['label']+'-serial.log')).write_text(text(segment))
        (tmp_path/(s['label']+'-bot.log')).write_text('\n'.join(caps)+'\n')
        _,saved,_=live.liveness_capture([r['line'] for r in segment],caps,a['board'],
            live.smoke.bounds_for(a['bounds_path'],a['variant']),aec=a['aec_enabled'])
        path=tmp_path/s['wire_file'];path.write_text('\n'.join(saved)+'\n');s['wire_sha256']=parity.digest(path)
    (tmp_path/'serial.log').write_text(text(entries))
    (tmp_path/'serial-entries.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries))


@pytest.mark.parametrize('board',parity.BOARDS)
@pytest.mark.parametrize('variant',parity.VARIANTS)
def test_all_six_candidate_liveness_scores_replay_every_required_session(tmp_path,monkeypatch,board,variant):
    a=p4_run(tmp_path);aec=variant=='aec-demo';demo=variant.endswith('demo')
    a.update(board=board,variant=variant,aec_enabled=aec,bundle=str(tmp_path/'bundle'),reference_only=False)
    if not demo:
        a['sessions'][1]['label']='pre-wifi';a['sessions'].pop(2)
        a['f20']=dict(applicable=False,reason='normal/manual image')
    for session in a['sessions']:
        wire=capture('esp32p4' if board=='p4xc' else 'esp32s3',aec)
        lines=serialize(wire);capability=[l for l in lines if 'DEMO_CAPABILITY ' in l]
        path=tmp_path/session['wire_file']
        path.write_text('\n'.join([l for l in lines if 'DEMO_CAPABILITY ' not in l]+['PHASE closed']+capability)+'\n')
        session['wire_sha256']=parity.digest(path)
    monkeypatch.setattr(live,'pinned_bundle',lambda *args:dict(autostart=demo,aec_enabled=aec,elf_sha256=a['elf_sha256'],boot_ref_gain_db=15))
    populate_liveness_proof(tmp_path,a,monkeypatch)
    assessment=live.score(tmp_path,acquisition=a)
    assert assessment['checks_passed'] and assessment['variant']==variant
    assert len(assessment['wire_timing'])==(27 if demo else 26)
    assert assessment['resources']['report_only'] is False
    a['sessions'][-1]['wire_sha256']='0'*64
    with pytest.raises(ValueError,match='wire capture changed'):live.score(tmp_path,acquisition=a)


def test_copied_control_gate_still_rejects_stale_pongs_and_applied_stale_work():
    sessions,events=controls();assert live.control_check(sessions,events)['stale_display_drops']==1
    events[-1]['accepted']=True
    with pytest.raises(ValueError):live.control_check(sessions,events)


def tap():
    before=dict(initialized=True,enabled=True,phase=1,generation=3,pending=False,requests=0,request_rejected=0,
        driver=dict(injected=0,double_taps=0))
    after=copy.deepcopy(before);after.update(generation=5,requests=1);after['driver']['double_taps']=1
    return before,after,['STATE Ready gen=5']


@pytest.mark.parametrize('change',['injected','missing','rejected','generation','no-ready','pending','not-live','extra-tap'])
def test_f11_requires_one_real_accepted_tap_and_new_ready_generation(change):
    before,after,lines=tap()
    assert touch_witness(before,after,lines)['physical_double_taps']==1
    if change=='injected':after['driver']['injected']=1
    elif change=='missing':after['driver']['double_taps']=0
    elif change=='rejected':after['request_rejected']=1
    elif change=='generation':after['generation']=3
    elif change=='no-ready':lines=[]
    elif change=='pending':after['pending']=True
    elif change=='not-live':before['phase']=2
    else:after['driver']['double_taps']=2
    with pytest.raises(ValueError):touch_witness(before,after,lines)

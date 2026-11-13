#!/usr/bin/env python3
"""Pinned step-16 evidence inventory, product wire pairs and conservative report.

All operations here are offline. Live helpers separately require bench.py.
No imports from the other worktree, no firmware builds and no bot lifecycle.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

COMMIT='52482495d1b859b5313cd8c76e4cd0cdd97ea507'
PIN=Path.home()/'src/evidence/echoear/p4/step-15-pin-round'/('commit-'+COMMIT)
RESTORE_SHA256='a2c846832b43636fc73564d6a1d32c22ebcbc95458c8906de0d0550f028d653b'
PIN_VERIFICATION=PIN/'coordinator-run-1'
RUN_RULES=Path.home()/'src/dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-03-step16-run-rules.md'
BOARDS=('echoear','p4xc')
VARIANTS=('demo','aec-demo','normal')
FEATURES=(
 ('F1','WiFi join/reconnect/backoff',('smoke','liveness')),
 ('F2','Start capability body',('product-pair',)),
 ('F3','WebRTC/Opus/playout',('smoke','product-pair')),
 ('F4','AEC-off talk-over gate',('smoke',)),
 ('F5','AEC/full-duplex reply-start guard',('smoke',)),
 ('F6','Playback DSP/default level/limiter',('smoke',)),
 ('F7','Display engine and presentation',('display','display-pair')),
 ('F8','Demo screens/character',('smoke','display')),
 ('F9','RTVI display requests',('display-pair',)),
 ('F10','LittleFS/assets/HTTP',('display',)),
 ('F11','Physical double tap',('touch',)),
 ('F12','Battery/low-battery UI',('battery',)),
 ('F13','NVS settings/provisioning',('liveness',)),
 ('F14','Autostart image',('smoke','liveness')),
 ('F15','Console command set',('console',)),
 ('F16','Diagnostics and worker/resource gates',('diagnostics',)),
 ('F17','Paced console exports',('console-export',)),
 ('F18','Ping/pong generation safety',('liveness',)),
 ('F19','Fault recovery/cycles/stale work',('liveness',)),
 ('F20','Demo hold/resume/cold assets',('liveness',)),
 ('X1','WiFi observation',('wifi-observe',)),
 ('X2','Cache counters',('cache-counters',)),
 ('X3','SPI/GDMA guards or DSI contention',('contention',)),
 ('X4','PHY calibration in NVS',('phy',)),
 ('X5','Camera/SD/USB-OTG/BLE',()))
KINDS={kind for _,_,kinds in FEATURES for kind in kinds}|{'erle'}


def fault_policy(identity):
    return dict(observer_sha256=digest(Path(__file__).with_name('p4_liveness_bot.py')),
        bot_source_sha=identity['bot_source_sha'],policy_source_sha=identity['policy_source_sha'],
        profile_manifest=identity['profile_manifest'],distinct_from_product_instance=identity['bot_instance'],
        processes=2,handoff_seconds=180,missed_handoff='scored failure; no retake',
        candidate_heap_policy='both boards strict; no reference-only waiver',
        decision_file=str(RUN_RULES),decision_sha256=digest(RUN_RULES))


def source_hashes():
    base=Path(__file__).parent
    files=list(base.glob('p4_parity*.py'))+[base/name for name in (
        'board_smoke.py','wire_contract.py','p4_step14.py','p4_product.py','p4_audio_fix4.py',
        'p4_liveness_bot.py','make_bundle.py','display_session.py','step9a_support.py',
        'step9a_merge_checks.py','echoear_status_assets.py','echoear_aec_bench.py',
        'echoear_restore_verify.py','p4_aec_close.py','p4_smoke_rescore.py','boards.py')]
    return {p.name:digest(p) for p in sorted(files)}


def require(value,message):
    if not value:raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def load(path):return json.loads(Path(path).read_text())


def write_new(path,row):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as stream:stream.write(json.dumps(row,indent=2,allow_nan=False)+'\n')


def pinned_bundle(path,board,variant=None):
    from make_bundle import verify_bundle
    m,_=verify_bundle(Path(path),board)
    require(m['firmware_commit']==COMMIT and not m.get('source_files_sha256') and
        m.get('source_diff_sha256')==hashlib.sha256(b'').hexdigest(),'clean step-15 pin required')
    require(m['profile'] in VARIANTS and (variant is None or m['profile']==variant),'step-16 image profile differs')
    canonical=PIN/'final-bundles'/('bundle-'+board+'-'+m['profile'])/'manifest.json'
    require(digest(Path(path)/'manifest.json')==digest(canonical),'step-15 canonical manifest differs')
    return m


def sessions(variant):return ['main','resume','cold'] if variant.endswith('demo') else ['main']


def clean_result(row):
    require(row.get('acquisition_passed') is True and not row.get('error') and
        not row.get('cleanup_errors') and not row.get('cleanup_error') and
        row.get('restore',{}).get('clean') is True,'incomplete acquisition, cleanup or restore')
    if row.get('board')=='p4xc':
        require(row['restore'].get('action')=='p4-product-of-record','P4 restore must use the pinned normal image')


def smoke_result(directory,board,variant):
    directory=Path(directory);row=load(directory/'result.json');clean_result(row)
    from p4_parity_serial import candidate_serial
    candidate_serial(directory)
    require(row.get('board')==board and row.get('variant')==variant and not row.get('reference_only') and
        row.get('calibration') is False,'candidate board/profile/scoring mode differs')
    image=load(directory/'image.json')
    require(image.get('elf_sha256')==row.get('elf_sha256') and image.get('firmware_commit')==COMMIT,
        'smoke image differs from pin')
    canonical=PIN/'final-bundles'/('bundle-'+board+'-'+variant)/'manifest.json'
    require(digest(directory/'image.json')==digest(canonical),'smoke image manifest differs')
    require([s['label'] for s in row.get('sessions',[])]==sessions(variant),'missing product session gates')
    expected=14 if variant.endswith('demo') else 12
    require(len(row.get('turns',[]))==expected,'full scripted smoke required')
    if variant=='aec-demo':
        from p4_smoke_rescore import completeness,rescore
        from p4_aec_close import adapters
        require(not completeness(row) and not row.get('unmeasured_steps'),'incomplete AEC smoke')
        with adapters():assessment=rescore(directory)
        require(assessment.get('passed') is True,'AEC retained turn/wire/stack replay failed')
    else:
        from p4_step14 import turn_checks
        require(row.get('passed') is True,'strict AEC-off smoke must pass')
        kinds=['france','talkover']+['long']*10+(['france','france'] if variant=='demo' else [])
        for index,(turn,kind) in enumerate(zip(row['turns'],kinds),1):
            require(turn.get('kind')==kind and load(directory/f'turn-{index:02d}.json')==turn,'smoke turn changed')
            turn_checks(turn['events'],kind,False)
        if variant=='demo':require(row.get('hold',{}).get('held') is True and row.get('cold_assets',{}).get('passed') is True,
            'demo hold/cold asset gates missing')
    from p4_parity_restore import restore_proof
    restore_proof(directory,dict(row,bundle=str(PIN/'final-bundles'/('bundle-'+board+'-'+variant))))
    return row


def product_pair(left,right,bounds_path):
    """Replay both exact wire captures for every full product session."""
    from board_smoke import boot_check,bounds_for,slice_sessions
    from wire_contract import compare,parse_capture
    captures=[];identities=[];sources={};rows=[]
    for directory,board in ((Path(left),'echoear'),(Path(right),'p4xc')):
        image=load(directory/'image.json');variant=image['profile']
        row=smoke_result(directory,board,variant);rows.append(row)
        from p4_parity_restore import restore_proof
        restored=restore_proof(directory,dict(row,bundle=str(PIN/'final-bundles'/('bundle-'+board+'-'+variant))))
        sources.update(restored['sources'])
        boot_check((directory/'boot.log').read_text().splitlines(),image,board)
        identity=load(directory/'identity.json');require(identity==row['identity'],'product identity changed')
        identities.append(identity);local=[]
        for label in sessions(variant):
            serial=directory/(label+'-serial.log');bot=directory/(label+'-bot.log')
            lines=slice_sessions(serial.read_text().splitlines());require(len(lines)==1,'one closed wire session required')
            require('\x1b[' not in bot.read_text(),'uncoloured bot log required')
            local.append(parse_capture(lines[0]+[l for l in bot.read_text().splitlines() if 'DEMO_CAPABILITY ' in l]))
            sources.update({str(p.resolve()):digest(p) for p in (serial,bot)})
        captures.append(local)
        sources.update({str((directory/n).resolve()):digest(directory/n) for n in ('result.json','image.json','identity.json','boot.log','serial.log')})
    require(rows[0]['variant']==rows[1]['variant'] and rows[0].get('pair_id') and
        rows[0]['pair_id']==rows[1].get('pair_id'),'product variants/pair identifiers differ')
    aec=rows[0]['variant']=='aec-demo'
    measured=[compare(a,b,profile='product',left_aec=aec,right_aec=aec,
        left_identity=identities[0],right_identity=identities[1],left_target='esp32s3',right_target='esp32p4',
        bounds=bounds_for(bounds_path,rows[0]['variant'])) for a,b in zip(*captures)]
    return dict(kind='step16-product-pair',checks_passed=True,qualification=False,variant=rows[0]['variant'],
        identity=identities[0],sources=sources,bounds_sha256=digest(bounds_path),measured=measured)


def pin_verification(root=None,boot_proof=None):
    """Authenticate completed coordinator prerequisites without running the bench.

    ERLE is retained as report-only. These are pin-verification inputs, not
    evidence that the six step-16 campaign images have completed their runs.
    """
    root=Path(root if root is not None else PIN_VERIFICATION).resolve()
    boot_proof=Path(boot_proof if boot_proof is not None else root/'boot-proof/restore-boot-proof.json').resolve()
    from p4_step14 import restore_record
    record=restore_record(PIN/'restore-of-record.json',RESTORE_SHA256)
    proof=load(boot_proof)
    require(proof.get('passed') is True and proof.get('record_sha256')==RESTORE_SHA256 and
        proof.get('elf_sha256')==record['elf_sha256'] and proof.get('restore',{}).get('clean') is True,
        'successful new-pin boot proof required before step 16')
    sources={str(boot_proof):digest(boot_proof)};smokes=[];identities=[]
    for board in BOARDS:
        for variant in ('normal','aec-normal'):
            path=root/('smoke-'+board+'-'+variant)/'result.json';r=load(path);clean_result(r)
            canonical=load(PIN/'final-bundles'/('bundle-'+board+'-'+variant)/'manifest.json')
            require(r.get('board')==board and r.get('variant')==variant and r.get('elf_sha256')==canonical['elf_sha256'] and
                not r.get('reference_only') and r.get('calibration') is False and
                [s['label'] for s in r.get('sessions',[])]==['main'] and len(r.get('turns',[]))==12,
                'four complete new-pin normal smokes required')
            if variant=='normal':require(r.get('passed') is True,'pin AEC-off smoke failed')
            else:require(r.get('passed') is True and r.get('unmeasured_steps')==[],'pin AEC smoke has missing gates')
            from wire_contract import identity_schema
            identity_schema(r['identity']);identities.append(r['identity'])
            smokes.append(dict(board=board,variant=variant,passed=True,elf_sha256=r['elf_sha256'],result_file=str(path)))
            sources[str(path.resolve())]=digest(path)
    require(all(identity==identities[0] for identity in identities),'pin smoke bot process/profile differs')
    report_path=root/'p4-aec-report/report.json';report=load(report_path)
    aec=load(PIN/'final-bundles/bundle-p4xc-aec-normal/manifest.json')
    erle=report.get('median_erle_db')
    require(report.get('kind')=='p4-aec-applied-image-report' and report.get('available') is True and
        report.get('elf_sha256')==aec['elf_sha256'] and report.get('gain_db')==30 and
        type(erle) in (int,float) and math.isfinite(erle),'available same-pin P4 AEC report required')
    require(report.get('identity')==identities[0],'pin ERLE bot process/profile differs')
    pair_path=root/'pair-aec-normal.json';pair=load(pair_path)
    require(pair.get('checks_passed') is True and pair.get('variant')=='aec-normal','passing pin AEC pair required')
    require(isinstance(report.get('sources'),dict) and report['sources'],'pin ERLE source bindings required')
    require(isinstance(pair.get('sources'),list) and len(pair['sources'])==2,'both pin AEC pair source bindings required')
    for bindings in [report['sources'],*pair['sources']]:
        require(isinstance(bindings,dict) and bindings,'retained pin source bindings required')
        for name,sha in bindings.items():
            require(digest(name)==sha,'pin verification source changed');sources[name]=sha
    sources.update({str(report_path):digest(report_path),str(pair_path):digest(pair_path)})
    return dict(kind='step16-pin-verification-inputs',passed=True,qualification=False,commit=COMMIT,
        coordinator_root=str(root),restore_record=str(PIN/'restore-of-record.json'),restore_record_sha256=RESTORE_SHA256,
        boot_proof=dict(passed=True,file=str(boot_proof),elf_sha256=record['elf_sha256']),smokes=smokes,
        p4_aec_report=dict(available=True,file=str(report_path),elf_sha256=aec['elf_sha256'],gain_db=30,
            median_erle_db=erle,report_only=True),aec_pair=dict(checks_passed=True,file=str(pair_path)),
        sources=sources,scope='Pin prerequisites passed; step-16 campaign coverage remains pending')


def create_plan(boot_proof,pin_smokes,root,product_identity):
    require(Path(root).resolve().is_relative_to(Path.home()/'src/evidence'),'campaign evidence must stay under ~/src/evidence')
    verification=pin_verification(pin_smokes,boot_proof)
    from wire_contract import identity_schema
    identity=load(product_identity);identity_schema(identity)
    sources=dict(verification['sources']);sources[str(Path(product_identity).resolve())]=digest(product_identity)
    images={}
    for board in BOARDS:
        for variant in VARIANTS:
            path=PIN/'final-bundles'/('bundle-'+board+'-'+variant);m=pinned_bundle(path,board,variant)
            images[board+'/'+variant]=dict(bundle=str(path),elf_sha256=m['elf_sha256'],manifest_sha256=digest(path/'manifest.json'))
    return dict(schema=1,kind='step16-parity-plan',qualification=False,commit=COMMIT,root=str(Path(root).resolve()),
        boot_proof=str(Path(boot_proof).resolve()),pin_smokes=str(Path(pin_smokes).resolve()),
        product_identity_file=str(Path(product_identity).resolve()),
        restore_record=str(PIN/'restore-of-record.json'),restore_record_sha256=RESTORE_SHA256,
        product_identity=identity,
        fault_bot_policy=fault_policy(identity),
        pin_verification=verification,
        prerequisite_sources=sources,images=images,variants=list(VARIANTS),
        source_sha256=source_hashes())


def checked_plan(path):
    plan=load(path);require(plan.get('schema')==1 and plan.get('kind')=='step16-parity-plan' and
        plan.get('commit')==COMMIT and plan.get('restore_record_sha256')==RESTORE_SHA256 and
        plan.get('variants')==list(VARIANTS),'invalid parity plan')
    require(plan['source_sha256']==source_hashes(),
        'parity source changed; freeze tools before acquisition')
    for name,sha in plan['prerequisite_sources'].items():require(digest(name)==sha,'pin verification evidence changed')
    require(set(plan['images'])=={b+'/'+v for b in BOARDS for v in VARIANTS},'six images required')
    for key,image in plan['images'].items():
        board,variant=key.split('/');m=pinned_bundle(image['bundle'],board,variant)
        require(m['elf_sha256']==image['elf_sha256'] and digest(Path(image['bundle'])/'manifest.json')==image['manifest_sha256'],
            'planned image changed')
    require(plan==create_plan(plan['boot_proof'],plan['pin_smokes'],plan['root'],plan['product_identity_file']),
        'plan/prerequisite selection changed')
    return plan


def output_path(plan,path):
    path=Path(path).resolve();root=Path(plan['root']).resolve()
    require(path.is_relative_to(root) and path!=root,'fresh acquisition output must be below the campaign root')
    require(not path.exists(),'retain every attempt; fresh acquisition output required')
    return path


def receipt(path):
    row=load(path)
    require(row.get('kind')=='step16-evidence-receipt' and row.get('step') in ('12b','13b','14','15','16'),
        'typed evidence receipt required')
    require(row.get('board') in BOARDS and row.get('category') in KINDS,'unknown receipt coverage')
    require(row.get('status') in ('pass','fail','unverified','report-only'),'unknown evidence status')
    require(isinstance(row.get('sources'),dict) and row['sources'],'receipt needs hashed source evidence')
    for name,sha in row['sources'].items():require(digest(name)==sha,'receipt source changed')
    require(digest(row['plan_file'])==row['plan_sha256'],'receipt plan changed')
    require(row==bound_receipt(row['plan_file'],row['category'],row['board'],row['variant'],row['run'],row.get('peer')),
        'receipt cannot override replayed status or feature coverage')
    return row


def replay(category,board,variant,run,peer=None):
    """Re-score retained inputs before attributing explicit feature coverage."""
    require(board in BOARDS and variant in VARIANTS,'unknown candidate')
    run=Path(run).resolve();sources={};status='pass';restored=None
    if (run/'serial.log').exists():
        from p4_parity_serial import candidate_serial
        candidate_serial(run)
    if category not in ('product-pair','display-pair'):
        name='result.json' if category in ('smoke','erle') else 'touch.json' if category=='touch' else 'acquisition.json'
        summary=load(run/name)
        from p4_parity_restore import restore_proof
        restored=restore_proof(run,dict(summary,bundle=str(PIN/'final-bundles'/('bundle-'+board+'-'+variant))))
    if category=='smoke':
        row=smoke_result(run,board,variant)
        features=['F1','F3']+(['F5'] if variant=='aec-demo' else ['F4'])
        if variant.endswith('demo'):features+=['F8','F14']
        if row.get('llm_content_findings'):status='report-only'
        # Acoustic guard validation is still provisional even when smoke passes.
        if board=='p4xc' and variant=='aec-demo':status='report-only'
        from board_smoke import boot_check,check_capture,bounds_for,slice_sessions
        image=load(run/'image.json');boot_check((run/'boot.log').read_text().splitlines(),image,board)
        for label in sessions(variant):
            serial=(run/(label+'-serial.log')).read_text().splitlines()
            closed=slice_sessions(serial);require(len(closed)==1,'one closed smoke session required')
            bot=(run/(label+'-bot.log')).read_text().splitlines()
            check_capture(closed[0],[l for l in bot if 'DEMO_CAPABILITY ' in l],manifest=image,board=board,
                headless=False,calibration=False,bounds=bounds_for(Path(__file__).parent/'fixtures/wire_contract_bounds.json',variant))
            for name in (label+'-serial.log',label+'-bot.log',label+'-bot-events.json'):
                sources[str(run/name)]=digest(run/name)
        for path in run.glob('turn-*.json'):sources[str(path)]=digest(path)
        for path in run.glob('perf-*.json'):sources[str(path)]=digest(path)
        sources.update({str(run/n):digest(run/n) for n in ('result.json','image.json','identity.json','boot.log')})
        sources[str(run/'serial.log')]=digest(run/'serial.log')
    elif category=='liveness':
        import p4_parity_liveness as live
        row=load(run/'acquisition.json');clean_result(row)
        require(row.get('board')==board and row.get('variant')==variant,'liveness board/profile differs')
        assessment=live.score(run)
        require(assessment.get('checks_passed') is True and not assessment.get('inconclusive'),'liveness unavailable')
        sources.update(assessment['bot_provenance']['sources'])
        features=['F1','F18','F19']+(['F13','F14','F20'] if variant.endswith('demo') else [])
        for path in run.glob('*'):
            if path.suffix in ('.json','.jsonl','.log') and path.is_file():sources[str(path)]=digest(path)
    elif category=='display':
        from p4_parity_display import score_display
        assessment=score_display(run,board,variant)
        sources.update(assessment['sources'])
        features=['F7','F8','F10']
    elif category in ('product-pair','display-pair'):
        require(peer is not None,'pair receipt requires both raw run directories')
        left,right=(run,Path(peer).resolve()) if board=='echoear' else (Path(peer).resolve(),run)
        if category=='product-pair':
            result=product_pair(left,right,Path(__file__).parent/'fixtures/wire_contract_bounds.json');features=['F2','F3']
        else:
            from p4_parity_display import pair
            result=pair(left,right);features=['F7','F9']
        require(result['variant']==variant,'pair profile differs');sources=result['sources']
    elif category=='touch':
        from p4_parity_touch import touch_witness
        row=load(run/'touch.json');clean_result(row)
        require(row.get('board')==board and row.get('variant')=='demo' and variant=='demo','touch board/profile differs')
        m=pinned_bundle(row['bundle'],board,variant);require(m['elf_sha256']==row['elf_sha256'],'touch image differs')
        features=['F11'];status='unverified'
        if row.get('touch_verified'):
            serial=(run/'serial.log').read_text().splitlines()
            statuses=[json.loads(line.split(' ',1)[1]) for line in serial if line.startswith('DEMO_TOUCH_STATUS {')]
            require(row['before'] in statuses and row['after'] in statuses and
                all(line in serial for line in row['serial_witness']),'touch receipt detached from actual serial capture')
            require(touch_witness(row['before'],row['after'],row['serial_witness'])==row['witness'],'touch witness differs')
            status='pass'
        sources={str(run/'touch.json'):digest(run/'touch.json'),str(run/'serial.log'):digest(run/'serial.log')}
    elif category=='erle':
        row=load(run/'result.json');clean_result(row)
        require(row.get('status')=='acquired' and row.get('score_produced') is True,'complete ERLE original required')
        features=[];status='report-only'
        for path in run.glob('*.json'):sources[str(path)]=digest(path)
    else:raise ValueError('unsupported automatic coverage; preserve unverified status for unaudited features')
    if restored:sources.update(restored['sources'])
    if (run/'serial.log').exists():sources[str(run/'serial.log')]=digest(run/'serial.log')
    return dict(kind='step16-evidence-receipt',board=board,variant=variant,category=category,
        step='16',status=status,features=features,run=str(run),peer=str(Path(peer).resolve()) if peer else None,
        sources=sources,qualification=False,restore_proof=restored)


def validated_unavailable(category,board,variant,run,primary):
    """Only positive typed evidence can soften an unavailable acquisition."""
    from p4_parity_campaign import untouched_noise
    if untouched_noise(run):return dict(kind='untouched-noisy')
    if category!='liveness' or primary.get('inconclusive') is not True:return None
    try:
        require(not primary.get('cleanup_error') and not primary.get('cleanup_errors'),'cleanup failed')
        from p4_parity_restore import restore_proof
        proof=restore_proof(run,dict(primary,bundle=str(PIN/'final-bundles'/('bundle-'+board+'-'+variant))))
        from p4_parity_liveness import score
        result=score(run,acquisition=primary)
        require(result.get('inconclusive') is True and result.get('checks_passed') is False,'causal reset not established')
        return dict(kind='causal-inconclusive',restore_proof=proof,assessment=result)
    except (ValueError,KeyError,TypeError,OSError,UnicodeError):return None


def collect(category,board,variant,run,peer=None):
    require(board in BOARDS and variant in VARIANTS,'unknown candidate')
    try:return replay(category,board,variant,run,peer)
    except (ValueError,KeyError,TypeError,OSError,UnicodeError,AttributeError,IndexError) as exc:
        # A failed attempt is evidence too. Never omit it because replay failed.
        run=Path(run).resolve()
        files=[p for p in run.rglob('*') if p.is_file() and p.suffix in ('.json','.jsonl','.log','.bin')]
        require(files,'no retained evidence: coverage remains unverified')
        primary_path=next((run/name for name in ('result.json','acquisition.json','touch.json') if (run/name).is_file()),None)
        try:primary=load(primary_path) if primary_path else {}
        except (ValueError,UnicodeError):primary={}
        if not isinstance(primary,dict):primary={}
        features={
            'smoke':['F1','F3']+(['F5'] if variant=='aec-demo' else ['F4'])+(['F8','F14'] if variant.endswith('demo') else []),
            'liveness':['F1','F18','F19']+(['F13','F14','F20'] if variant.endswith('demo') else []),
            'display':['F7','F8','F10'],'product-pair':['F2','F3'],'display-pair':['F7','F9'],'touch':['F11'],'erle':[]}
        require(category in features,'unsupported evidence category')
        unavailable=validated_unavailable(category,board,variant,run,primary)
        if primary.get('board',board)!=board or primary.get('variant',variant)!=variant:unavailable=None
        sources={str(path):digest(path) for path in files}
        if unavailable and unavailable.get('restore_proof'):sources.update(unavailable['restore_proof']['sources'])
        if peer:
            for path in Path(peer).resolve().glob('*'):
                if path.is_file() and path.suffix in ('.json','.jsonl','.log'):sources[str(path)]=digest(path)
        return dict(kind='step16-evidence-receipt',board=board,variant=variant,category=category,
            step='16',status='unverified' if unavailable else 'fail',features=features[category],run=str(run),
            peer=str(Path(peer).resolve()) if peer else None,sources=sources,qualification=False,
            unavailable_case=unavailable,
            reason=type(exc).__name__+': '+str(exc))


def bound_receipt(plan_path,category,board,variant,run,peer=None):
    plan=checked_plan(plan_path);root=Path(plan['root'])
    from p4_parity_campaign import binding
    attempt_binding=binding(plan_path,category,board,variant,run)
    peer_binding=binding(plan_path,category,'p4xc' if board=='echoear' else 'echoear',variant,peer) if peer else None
    row=collect(category,board,variant,run,peer)
    for b in (attempt_binding,peer_binding):
        if b:row['sources'].update(b['sources'])
    for directory in (run,peer):
        if directory is None:continue
        directory=Path(directory).resolve()
        require(directory.is_relative_to(root),'evidence from another campaign cannot be merged')
        identity=directory/'identity.json'
        if row['status'] in ('pass','report-only'):
            try:
                if category in ('smoke','product-pair','erle','touch'):
                    require(load(identity)==plan['product_identity'],'one planned product bot required across all pairs')
                if category in ('display','display-pair'):
                    require(load(identity)==load(root/'display-identity.json'),'one anchored display bot required across all pairs')
            except (ValueError,TypeError,OSError,UnicodeError) as exc:row.update(status='fail',reason=str(exc))
    if category=='liveness' and row['status'] in ('pass','report-only'):
        a=load(Path(run)/'acquisition.json')
        if a.get('plan_sha256')!=digest(plan_path) or Path(a['plan_file']).resolve()!=Path(plan_path).resolve():
            row.update(status='fail',reason='liveness provenance belongs to another plan')
    if any(b and b['disposition'] in ('failed','incomplete') for b in (attempt_binding,peer_binding)) and row['status']!='fail':
        row.update(status='fail',reason='registered acquisition failed or incomplete')
    return dict(row,plan_file=str(Path(plan_path).resolve()),plan_sha256=digest(plan_path),attempt_binding=attempt_binding,
        peer_attempt_binding=peer_binding)


def render(receipts,plan_path=None):
    """Never infer full feature coverage from an unrelated passing artifact."""
    require(len({r.get('plan_sha256') for r in receipts if r.get('plan_sha256')})<=1,'do not merge different parity plans')
    plans={r['plan_file'] for r in receipts if r.get('plan_file')}
    if plan_path is not None:plans.add(str(Path(plan_path).resolve()))
    require(len(plans)<=1,'table mixes campaign plans')
    inventory=[]
    if plans:
        from p4_parity_campaign import reconcile
        inventory=reconcile(next(iter(plans)),receipts)
    rows=['# P4 smoke parity','',
        'Smoke parity, not qualification. Missing evidence is unverified. Earlier steps are historical,',
        'and do not establish step-16 coverage on the new pins. Report-only findings never become strict passes.','',
        'Firmware pins: `'+COMMIT+'`. Restore record SHA256: `'+RESTORE_SHA256+'`.','',
        '| Feature | S3 status | P4 status | Evidence and verification step |','|---|---|---|---|']
    for feature,title,kinds in FEATURES:
        states=[];refs=[]
        for board in BOARDS:
            relevant=[r for r in receipts if r['board']==board and r['category'] in kinds]
            # Full coverage must be explicitly attested per feature and variant.
            relevant=[r for r in relevant if feature in r.get('features',[])]
            needed=('demo','aec-demo') if feature in ('F8','F13','F14','F20') else ('aec-demo',) if feature=='F5' else ('demo','normal') if feature=='F4' else VARIANTS
            if feature=='F11':needed=('demo',)
            statuses=[r['status'] for r in relevant]
            covered={(r.get('variant'),r['category']) for r in relevant if r.get('step')=='16' and r['status'] in ('pass','report-only')}
            complete=bool(kinds) and all((v,k) in covered for v in needed for k in kinds)
            status='fail' if 'fail' in statuses else 'report-only' if complete and 'report-only' in statuses else 'pass' if complete else 'unverified'
            if feature=='F12' and board=='p4xc':status='exception: no battery hardware'
            if feature in ('X1','X2') and board=='p4xc':status='not ported'
            if feature=='X3' and board=='p4xc':status='unverified: DSI contention'
            if feature=='X4' and board=='p4xc':status='not applicable: C6 owns RF'
            if feature=='X5':status='out of scope'
            states.append(status)
            for r in relevant:
                primary=r.get('run') or next(iter(r['sources']))
                refs.append(f"{board} step {r['step']} {r['category']}: `"+primary.replace('|','\\|')+'`')
        if any(s.startswith('unverified') or s=='not ported' for s in states):
            step=HISTORICAL[feature]
            path=RUN_RULES.parent/('step-'+step+'-sol-report.md')
            refs.append('[historical (step '+step+')]('+str(path)+'); step-16 coverage pending')
        rows.append('| '+feature+' '+title+' | '+' | '.join(states)+' | '+('; '.join(refs) or 'No accepted evidence supplied')+' |')
    rows+=['','P4 limitations: near-end mic-path deficit is localized upstream of AEC; guard 400/2000 ms is provisional;',
        'gain 30 dB is selected-single-trial; delay 1024 samples is measured-diagnostic under the closing decision.',
        'Acoustic findings are provisional: bench speaker not final hardware. Re-measure on final hardware.',
        'F11 requires a real user double tap on each board; injected taps cannot establish it.',
        'F12 is a P4 hardware exception. No camera/SD/USB-OTG/BLE parity is claimed.',
        'Historical authority: [closing decision](../../dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-03-close-15-and-pin.md)',
        'and [near-end localization](../../dg-esp32/proj-2026-10-30-2143/codex-jobs/measurement-2026-11-03-p4-nearend-localization.md).','']
    if inventory:
        rows+=['All registered acquisitions (including failed/unscored attempts):','']
        rows += ['- `'+r['run']+'`: '+r['disposition'] for r in inventory]
        rows.append('')
    return '\n'.join(rows)


# Historical reports are citations only, never automatic feature coverage.
HISTORICAL=dict(zip([f[0] for f in FEATURES],
    ('14','7','12b','12b','15','12b','13b','13b','13b','14','13b','10','14','14',
     '14','14','15','14','14','14','1a','1a','15','1a','0')))


def collect_all(plan_path,out):
    """Enumerate originals from the ledger, including every failed/noisy part."""
    from p4_parity_campaign import inventory
    _,registered,_=inventory(plan_path)
    out=Path(out);require(not out.exists(),'fresh receipt directory required');out.mkdir(parents=True)
    latest={}
    for row in registered.values():
        receipt=bound_receipt(plan_path,row['family'],row['board'],row['variant'],row['run'])
        write_new(out/('attempt-'+str(row['sequence'])+'.json'),receipt)
        latest[(row['family'],row['board'],row['variant'])]=row['run']
    for family,category in (('smoke','product-pair'),('display','display-pair')):
        for variant in VARIANTS:
            if not all((family,b,variant) in latest for b in BOARDS):continue
            for board,peer in (('echoear','p4xc'),('p4xc','echoear')):
                write_new(out/(category+'-'+board+'-'+variant+'.json'),bound_receipt(plan_path,category,board,variant,
                    latest[(family,board,variant)],latest[(family,peer,variant)]))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    plan=sub.add_parser('plan')
    for name in ('boot-proof','pin-smokes','root','out','product-identity'):plan.add_argument('--'+name,type=Path,required=True)
    check=sub.add_parser('check');check.add_argument('--plan',type=Path,required=True)
    verified=sub.add_parser('pin-verification')
    verified.add_argument('--root',type=Path,default=PIN_VERIFICATION);verified.add_argument('--out',type=Path,required=True)
    pair=sub.add_parser('product-pair');pair.add_argument('left',type=Path);pair.add_argument('right',type=Path)
    pair.add_argument('--bounds',type=Path,default=Path(__file__).parent/'fixtures/wire_contract_bounds.json');pair.add_argument('--out',type=Path,required=True)
    collect_parser=sub.add_parser('collect')
    collect_parser.add_argument('--plan',type=Path,required=True)
    collect_parser.add_argument('--category',choices=('smoke','liveness','display','product-pair','display-pair','touch','erle'),required=True)
    collect_parser.add_argument('--board',choices=BOARDS,required=True);collect_parser.add_argument('--variant',choices=VARIANTS,required=True)
    collect_parser.add_argument('--run',type=Path,required=True);collect_parser.add_argument('--peer',type=Path)
    collect_parser.add_argument('--out',type=Path,required=True)
    table=sub.add_parser('table');table.add_argument('--plan',type=Path,required=True)
    table.add_argument('--receipt',type=Path,action='append',default=[]);table.add_argument('--out',type=Path,required=True)
    abandon=sub.add_parser('abandon');abandon.add_argument('--plan',type=Path,required=True)
    abandon.add_argument('--run',type=Path,required=True);abandon.add_argument('--reason',required=True)
    all_parser=sub.add_parser('collect-all');all_parser.add_argument('--plan',type=Path,required=True)
    all_parser.add_argument('--out',type=Path,required=True)
    a=p.parse_args(argv)
    if a.action=='plan':write_new(a.out,create_plan(a.boot_proof,a.pin_smokes,a.root,a.product_identity))
    elif a.action=='pin-verification':write_new(a.out,pin_verification(a.root))
    elif a.action=='check':checked_plan(a.plan);print('Plan/source/pins authenticated; no hardware accessed')
    elif a.action=='product-pair':write_new(a.out,product_pair(a.left,a.right,a.bounds))
    elif a.action=='collect':write_new(a.out,bound_receipt(a.plan,a.category,a.board,a.variant,a.run,a.peer))
    elif a.action=='collect-all':collect_all(a.plan,a.out)
    elif a.action=='abandon':
        from p4_parity_campaign import finish
        finish(a.plan,a.run,error=a.reason,abandon=True)
    else:
        checked_plan(a.plan);content=render([receipt(path) for path in a.receipt],a.plan);a.out.parent.mkdir(parents=True,exist_ok=True)
        with a.out.open('x') as stream:stream.write(content)
    return 0


if __name__=='__main__':raise SystemExit(main())

"""Low-priority, device-free follow-up policy/merge regressions."""
import copy
import json
from pathlib import Path
import tempfile

import pytest
import p4_parity as core
import p4_parity_campaign as ledger
import p4_parity_followup as follow
import p4_parity_followup_campaign as retry
import p4_parity_followup_merge as merge
import p4_parity_followup_product as product
from test_p4_parity_fix1 import copy_restore,put
from test_p4_parity_fix3 import smoke_case
from test_board_smoke import events


@pytest.fixture(autouse=True)
def approved_close_addendum():
    from p4_step16_close import decision_runtime
    with decision_runtime():yield


@pytest.fixture
def plans():
    base=Path.home()/'src/evidence/echoear/p4/step-16/aecdemo-followup/host-validation-1'
    base.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='case-',dir=base) as name:
        root=Path(name);main=root/'main/plan.json';identity=core.PIN_VERIFICATION/'smoke-echoear-normal/identity.json'
        with follow.main_runtime():
            core.write_new(main,core.create_plan(core.PIN_VERIFICATION/'boot-proof/restore-boot-proof.json',
                core.PIN_VERIFICATION,main.parent,identity))
        (main.parent/'attempts.jsonl').touch()
        current=root/'identity.json';value=core.load(identity);value['bot_instance']='followup:601:123'
        core.write_new(current,value);plan=root/'follow/plan.json'
        core.write_new(plan,follow.create_plan(main,plan.parent,current))
        yield main,plan,root


def test_plan_all_six_product_cells_same_pins_separate_root_and_current_identity(plans):
    main,plan,root=plans;m=follow.checked_main(main);p=follow.checked_plan(plan)
    assert p['variants']==list(core.VARIANTS) and len(p['images'])==6 and p['images']==m['images']
    assert p['product_identity']['bot_instance']!=m['product_identity']['bot_instance']
    assert p['llm_content_mode']=='report-only' and p['decision_sha256']==follow.DECISION_SHA
    for directory in (main.parent,main.parent/'nested',root):
        with pytest.raises(ValueError,match='separate external'):follow.create_plan(main,directory,root/'identity.json')
    identity=core.load(root/'identity.json');identity['profile_manifest']={};put(root/'identity.json',identity)
    with pytest.raises(ValueError):follow.checked_plan(plan)


@pytest.mark.parametrize('family',['liveness','display','display-pair'])
def test_non_product_followup_scope_is_refused(plans,family):
    _,plan,root=plans
    with follow.adapters(),pytest.raises(ValueError,match='product cells'):
        ledger.register(plan,family,'p4xc','aec-demo',root/'follow/other/attempt-01')


@pytest.mark.parametrize('board,variant,part',[('echoear','aec-demo',0),('p4xc','normal',0),('p4xc','aec-demo',None),('p4xc','aec-demo',True),('p4xc','aec-demo',4)])
def test_only_typed_a0_report_parts_can_register(plans,board,variant,part):
    _,plan,root=plans
    with follow.adapters(),pytest.raises(ValueError):ledger.register(plan,'erle',board,variant,root/'follow/a0/attempt-01',part)


def errored_a0(plan,root,change=None):
    p=core.load(plan);image=root/'follow/a0/image-plan.json'
    core.write_new(image,dict(profile='aec-demo',elf_sha256=p['images']['p4xc/aec-demo']['elf_sha256']))
    first=root/'follow/a0/part-0/attempt-01';token=retry.INPUT.set(dict(file=str(image),sha256=core.digest(image)))
    with ledger.attempt(plan,'erle','p4xc','aec-demo',first,0):
        row=copy_restore(first,'p4xc','aec-demo')
        row.update(kind='p4-aec-gain',gain_db=30,part=0,identity=p['product_identity'],elf_sha256=p['images']['p4xc/aec-demo']['elf_sha256'],
            plan_sha256=core.digest(image),acquisition_passed=False,device_untouched=False,score_produced=False,
            retryable=True,status='acquisition-error',error='ValueError: Missing/reordered/duplicate AEC PCM block',cleanup_errors=[])
        if change=='scored':row['score_produced']=True
        elif change=='untouched':row['device_untouched']=True
        elif change=='marker':put(first/'scored-original.json',{})
        elif change=='not-retryable':row['retryable']=False
        elif change=='not-typed':row['score_produced']=0
        elif change=='part':row['part']=1
        elif change=='elf':row['elf_sha256']='f'*64
        elif change=='identity':row['identity']={}
        elif change=='plan':row['plan_sha256']='f'*64
        elif change=='restore':row['restore']['clean']=False;put(first/'restore.json',row['restore'])
        elif change=='cleanup':row['cleanup_errors']=['disconnect failed']
        elif change=='preflight':row['status']='preflight-error'
        elif change=='payload':put(first/'post-body-payload.json',{})
        elif change=='cases':row['cases']=[]
        put(first/'result.json',row)
    return first,image,token


def test_a0_retry_raw_result_cap_and_all_attempts_reported(plans):
    _,plan,root=plans
    with follow.adapters():
        first,image,token=errored_a0(plan,root)
        try:
            second=first.parent/'attempt-02'
            with ledger.attempt(plan,'erle','p4xc','aec-demo',second,0):
                second.mkdir();put(second/'result.json',dict(board='p4xc',acquisition_passed=False,error='second failed'))
            with pytest.raises(ValueError,match='retry cap'):ledger.register(plan,'erle','p4xc','aec-demo',first.parent/'attempt-03',0)
            core.collect_all(plan,root/'follow/receipts')
            rows=[core.receipt(p) for p in (root/'follow/receipts').glob('*.json')]
            assert len(rows)==2 and {r['run'] for r in rows}=={str(first),str(second)} and all(r['status']=='fail' for r in rows)
            with pytest.raises(ValueError,match='omitted'):ledger.reconcile(plan,rows[1:])
            entries=[json.loads(line) for line in (root/'follow/attempts.jsonl').read_text().splitlines()]
            assert entries[2]['retry_basis']=='unscored-prerequisite-error'
            put(first/'result.json',dict(acquisition_passed=True))
            with pytest.raises(ValueError):ledger.inventory(plan)
        finally:retry.INPUT.reset(token)


@pytest.mark.parametrize('change',['scored','untouched','marker','not-retryable','not-typed','part','elf','identity','plan','restore','cleanup','preflight','payload','cases'])
def test_ineligible_a0_cannot_retry(plans,change):
    _,plan,root=plans
    with follow.adapters():
        first,image,token=errored_a0(plan,root,change)
        try:
            with pytest.raises(ValueError,match='non-retryable'):ledger.register(plan,'erle','p4xc','aec-demo',first.parent/'attempt-02',0)
        finally:retry.INPUT.reset(token)


def test_missing_raw_or_unfinished_part_never_retries(plans):
    _,plan,root=plans
    with follow.adapters():
        first,image,token=errored_a0(plan,root)
        try:
            (first/'result.json').unlink()
            with pytest.raises(ValueError):ledger.register(plan,'erle','p4xc','aec-demo',first.parent/'attempt-02',0)
        finally:retry.INPUT.reset(token)


@pytest.mark.parametrize('board',core.BOARDS)
def test_smoke_acquisition_error_keeps_old_no_retake_rule(plans,board):
    _,plan,root=plans;first=root/'follow/smoke'/board/'attempt-01'
    with follow.adapters():
        with ledger.attempt(plan,'smoke',board,'demo',first):
            first.mkdir(parents=True);put(first/'result.json',dict(board=board,variant='demo',acquisition_passed=False,
                score_produced=False,retryable=True,status='acquisition-error',error='LLM/STT failure'))
        with pytest.raises(ValueError,match='non-retryable'):ledger.register(plan,'smoke',board,'demo',first.parent/'attempt-02')


@pytest.mark.parametrize('kind',['france','long','talkover'])
def test_aec_off_content_only_uses_same_strict_prompt_and_completion_rules(kind):
    data=events(kind,False)
    for e in data:
        if e['kind']=='aggregator-assistant-turn-stopped':e['text']='I do not know.'
    verdict=product.turn_checks(data,kind,False)
    assert verdict['llm_content_findings'] and verdict['stt_passed'] and verdict['turn_completed']
    for e in data:
        if e['kind']=='aggregator-user-turn-stopped':e['text']='Twenty. Sorry. Twenty million.'
    with pytest.raises(ValueError):product.turn_checks(data,kind,False)


@pytest.mark.parametrize('change',['self-interrupt','no-stop','pipeline','end','interrupted','italy-leak','calibration'])
def test_aec_off_report_only_does_not_relax_other_gates(change):
    data=events('talkover',False)
    if change=='self-interrupt':data.append(dict(kind='frame',frame='InterruptionFrame',direction='DOWNSTREAM',bot_active=True,monotonic_ns=100))
    elif change=='no-stop':data=[e for e in data if e.get('frame')!='BotStoppedSpeakingFrame']
    elif change=='pipeline':data.append(dict(kind='frame',frame='ErrorFrame',direction='DOWNSTREAM',monotonic_ns=100))
    elif change=='end':data.append(dict(kind='session-end'))
    elif change=='interrupted':data[-1]['interrupted']=True
    elif change=='italy-leak':
        next(e for e in data if e['kind']=='aggregator-user-turn-stopped')['text']='Count to twenty then Italy'
    with pytest.raises(ValueError):product.turn_checks(data,'talkover',False,calibration=change=='calibration')


def row(root,board='p4xc',variant='normal',category='smoke',status='pass',disposition='completed'):
    return dict(board=board,variant=variant,category=category,status=status,step='16',
        run=str(root/(board+'-'+variant+'-'+category)),peer=None,features=['F1','F3','F4','F8','F14'],
        sources={str(root/'raw.json'):'a'*64},attempt_binding=dict(disposition=disposition))


def test_merge_discloses_one_main_s3_demo_failure_without_reclassifying_it(plans):
    main,plan,root=plans
    original=row(root/'main',board='echoear',variant='demo',status='fail',disposition='failed')
    original['reason']='France/Paris turn failed during reported noisy room';before=copy.deepcopy(original)
    following=row(root/'follow',board='echoear',variant='demo')
    text=merge.merge_rows(main,plan,[original],[following])
    assert 'Main **FAIL** retained' in text and original['run'] in text and following['run'] in text and original==before
    assert 'authorized **after the failures**' in text and str(follow.DECISION) in text
    assert 'main plan' in text and 'follow-up plan' in text
    feature=next(line for line in text.splitlines() if line.startswith('| F4 '));assert '| fail |' not in feature


@pytest.mark.parametrize('category',['liveness','display','display-pair'])
def test_merge_refuses_non_product_followup_cells(plans,category):
    main,plan,root=plans
    with pytest.raises(ValueError,match='product cells'):merge.merge_rows(main,plan,[],[row(root/'follow',category=category)])


@pytest.mark.parametrize('board,variant',[('p4xc','demo'),('echoear','normal'),('p4xc','aec-demo')])
def test_merge_refuses_every_other_cross_plan_product_overlap(plans,board,variant):
    main,plan,root=plans
    with pytest.raises(ValueError,match='conflicting'):merge.merge_rows(main,plan,[row(root/'main',board,variant)],[row(root/'follow',board,variant)])


def test_merge_duplicate_receipt_and_incomplete_rerun_do_not_hide_failure(plans):
    main,plan,root=plans;duplicate=row(root/'follow')
    with pytest.raises(ValueError,match='duplicate'):merge.merge_rows(main,plan,[],[duplicate,duplicate])
    old=row(root/'main',board='echoear',variant='demo',status='fail',disposition='failed')
    noise=row(root/'follow',board='echoear',variant='demo',status='unverified',disposition='untouched-noisy')
    text=merge.merge_rows(main,plan,[old],[noise])
    assert '| fail | unverified |' in next(l for l in text.splitlines() if l.startswith('| F4 '))


def test_empty_two_plan_cli_is_read_only_and_each_row_cites_plan(plans):
    main,plan,root=plans
    with follow.adapters():
        with ledger.journal(plan):pass
    before=(main.parent/'attempts.jsonl').read_bytes()
    output=root/'merged.md'
    assert merge.main(['--plan',str(main),'--follow-up-plan',str(plan),'--out',str(output)])==0
    rows=[l for l in output.read_text().splitlines() if l.startswith('| F') or l.startswith('| X')]
    assert len(rows)==26 and all('main plan' in l for l in rows[1:])
    assert (main.parent/'attempts.jsonl').read_bytes()==before
    with pytest.raises(ValueError):merge.main(['--plan',str(main),'--follow-up-plan',str(plan),'--follow-up-plan',str(plan),'--out',str(root/'bad.md')])


def test_real_normal_followup_receipts_replay_canonical_smokes_and_pair(plans):
    main,plan,root=plans;p=core.load(plan)
    with follow.adapters():
        for board in core.BOARDS:
            run=root/'follow/product'/(board+'-normal')/'attempt-01'
            with ledger.attempt(plan,'smoke',board,'normal',run):
                smoke_case(run,board);data=core.load(run/'result.json')
                data['identity']=p['product_identity'];put(run/'identity.json',data['identity'])
                room=run.with_name(run.name+'-room');room.mkdir();put(room/'result.json',dict(noise_contaminated=False))
                data.update(room=core.load(room/'result.json'),llm_content_mode='report-only',
                    content_decision_file=str(follow.DECISION),content_decision_sha256=follow.DECISION_SHA)
                data['llm_content_findings']=product.findings(data);put(run/'result.json',data)
        core.collect_all(plan,root/'follow/receipts')
        rows=[core.receipt(path) for path in (root/'follow/receipts').glob('*.json')]
        assert len(rows)==4 and all(r['status']=='pass' for r in rows), [(r['category'],r['board'],r['status'],r.get('reason')) for r in rows]
    paths=list((root/'follow/receipts').glob('*.json'))
    text=merge.render(main,plan,paths)
    assert 'follow-up plan' in next(l for l in text.splitlines() if l.startswith('| F3 '))
    # Omitting a registered original is still forbidden in a merged table.
    with pytest.raises(ValueError,match='omitted'):merge.render(main,plan,paths[1:])


@pytest.mark.parametrize('board',core.BOARDS)
def test_f11_one_demo_attempt_per_board_and_originals_disclosed(plans,board):
    main,plan,root=plans;run=root/'follow/touch'/board/'attempt-01'
    with follow.adapters():
        with ledger.attempt(plan,'touch',board,'demo',run):
            data=copy_restore(run,board,'demo');data.update(board=board,variant='demo',acquisition_passed=True,
                touch_verified=False,elf_sha256=core.load(plan)['images'][board+'/demo']['elf_sha256'],
                identity=core.load(plan)['product_identity'],unverified_reason='no physical double tap')
            put(run/'identity.json',data['identity']);put(run/'touch.json',data)
            (run/'serial.log').write_text('No complete physical double-tap witness in this host fixture.\n')
        receipt=core.bound_receipt(plan,'touch',board,'demo',run)
        assert receipt['status']=='unverified' and receipt['features']==['F11'], receipt.get('reason')
        with pytest.raises(ValueError,match='retry cap'):ledger.register(plan,'touch',board,'demo',run.parent/'attempt-02')
        with pytest.raises(ValueError,match='demo image'):ledger.register(plan,'touch',board,'normal',run.parent/'other/attempt-01')
    old=row(root/'main',board,'demo','touch','unverified');old['features']=['F11']
    text=merge.merge_rows(main,plan,[old],[receipt])
    assert 'Main **unverified** retained' in text and old['run'] in text
    assert 'follow-up plan' in next(l for l in text.splitlines() if l.startswith('| F11 '))
    old['status']='pass'
    with pytest.raises(ValueError,match='unverified main F11'):merge.merge_rows(main,plan,[old],[receipt])


def test_f11_prompts_instructions_before_entry_and_immediate_window(monkeypatch,capsys):
    import p4_parity_touch as entry
    monkeypatch.setattr(follow,'checked_plan',lambda p:{})
    original=entry.checked_plan
    def fake_main(argv):
        before=capsys.readouterr().out
        assert 'BEFORE the window opens' in before and 'under 0.25 s' in before and '0.08–0.4 s' in before
        entry.print('USER ACTION: old frozen prompt')
        immediate=capsys.readouterr().out
        assert immediate.startswith('TAP NOW:') and 'same spot' in immediate
        return 2
    monkeypatch.setattr(entry,'main',fake_main)
    assert product.touch_main(['--plan','fixture.json'])==2
    assert entry.checked_plan is original and 'print' not in entry.__dict__


@pytest.mark.parametrize('noisy',[True,False])
def test_off_live_wrapper_room_gate_before_backend_and_strict_executor(plans,monkeypatch,noisy):
    from contextlib import nullcontext
    import alsa
    import p4_parity_smoke as entry
    import board_smoke as smoke
    import board_smoke_bounds
    import p4_product
    main,plan,root=plans;run=root/'follow/product/echoear-demo/attempt-01'
    monkeypatch.setattr(entry,'preflight',lambda *a:None)
    calls=[]
    def room(path,label):
        calls.append('room');path.mkdir(parents=True);data=dict(noise_contaminated=noisy);put(path/'result.json',data);return data
    monkeypatch.setattr(alsa,'record_room_baseline',room)
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *a:nullcontext())
    monkeypatch.setattr(product.step14,'restoring',lambda *a:nullcontext())
    class FakeIO:
        def __init__(self,*a):calls.append('backend')
    monkeypatch.setattr(smoke,'LiveIO',FakeIO)
    def execute(args,backend):
        calls.append('execute')
        assert not args.calibration and not args.reference_only and not args.headless
        turns=[]
        for kind in ['france','talkover']+['long']*10+['france','france']:
            data=events(kind,False)
            for event in data:
                if event['kind']=='aggregator-assistant-turn-stopped':event['text']='Off script.'
            turns.append(dict(smoke.turn_checks(data,kind,False,calibration=args.calibration),kind=kind,events=data))
        args.out.mkdir(parents=True)
        (args.out/'serial.log').write_text('Host-only fake acquisition completed.\n')
        return dict(acquisition_passed=True,turns=turns)
    monkeypatch.setattr(board_smoke_bounds,'execute',execute)
    result=follow.main(['smoke','--plan',str(plan),'--board','echoear','--variant','demo',
        '--llm-content','report-only','--bot-dir',str(root/'bot'),'--bot-identity',str(root/'identity.json'),'--out',str(run)])
    if noisy:
        assert result==2 and calls==['room']
        assert core.load(run/'result.json')['device_untouched'] is True
    else:
        assert result==0 and calls==['room','backend','execute'], core.load(run/'result.json')
        data=core.load(run/'result.json');assert len(data['turns'])==14 and len(data['llm_content_findings'])==14
    with follow.adapters():assert ledger.inventory(plan)[2][str(run)]['disposition']==('untouched-noisy' if noisy else 'completed')


@pytest.mark.parametrize('change',['room','findings','resource','generation'])
def test_off_replay_refuses_raw_room_content_resource_or_generation_tampering(tmp_path,change):
    run=tmp_path/'attempt-01';smoke_case(run,'echoear');data=core.load(run/'result.json')
    room=run.with_name(run.name+'-room');room.mkdir();put(room/'result.json',dict(noise_contaminated=False))
    data.update(room=core.load(room/'result.json'),llm_content_mode='report-only',
        content_decision_file=str(follow.DECISION),content_decision_sha256=follow.DECISION_SHA)
    data['llm_content_findings']=product.findings(data);put(run/'result.json',data)
    assert product.verify_off(run)==data
    if change=='room':put(room/'result.json',dict(noise_contaminated=True))
    elif change=='findings':
        data['llm_content_findings']=[dict(reason='invented')];put(run/'result.json',data)
    else:
        sample=run/'perf-003.json';old=core.load(sample);value=copy.deepcopy(old)
        if change=='generation':value['generation']+=1
        else:next(t for t in value['tasks'] if t['name']=='peer-loop')['stack_free_bytes']=100
        put(sample,value)
        if change=='resource':
            path=run/'main-serial.log';lines=path.read_text().splitlines()
            lines=['PERF '+json.dumps(value) if l.startswith('PERF {') and json.loads(l[5:])==old else l for l in lines]
            path.write_text('\n'.join(lines)+'\n')
    with pytest.raises(ValueError):product.verify_off(run)


def resource_demo_case(run,board):
    """Synthetic three boot/session windows over copied canonical normal evidence.

    Exercises production receipt/pair replay, without claiming hardware demo
    acquisition. Each session has its own peer, saved PERF and raw byte window.
    """
    import shutil
    from test_wire_contract import capture,serialize
    from test_p4_parity_fix1 import rebind_storage_fixture
    from p4_product import storage_plan
    smoke_case(run,board);data=core.load(run/'result.json');old_sha=data['elf_sha256']
    bundle=core.PIN/'final-bundles'/('bundle-'+board+'-demo');manifest=core.load(bundle/'manifest.json')
    shutil.copyfile(bundle/'manifest.json',run/'image.json')
    (run/'boot.log').write_text((run/'boot.log').read_text().replace(old_sha,manifest['elf_sha256']))
    if board=='p4xc':
        path=run/'candidate-storage-erase.json';value=core.load(path);value.update(storage_plan(bundle));put(path,value)
        rebind_storage_fixture(run,bundle)
    original=[core.load(path) for path in sorted(run.glob('perf-*.json'))]
    data.update(variant='demo',mode='demo',bundle=str(bundle),elf_sha256=manifest['elf_sha256'],
        pair_id='host-three-session-demo',hold=dict(held=True),cold_assets=dict(passed=True))
    data['turns']+=copy.deepcopy([data['turns'][0],data['turns'][0]])
    for i,turn in enumerate(data['turns'],1):put(run/f'turn-{i:02d}.json',turn)
    data['sessions']=[]
    for index,(label,gen) in enumerate(zip(('main','resume','cold'),(1,2,1))):
        samples=[]
        for offset,source in enumerate(original):
            value=copy.deepcopy(source);value.update(generation=gen,pc_id='host-'+label,host_fixture_snapshot=index*3+offset)
            value['heap']['psram']['free']-=index*4096
            samples.append(value);put(run/f'perf-{index*3+offset+1:03d}.json',value)
        wire=capture(manifest['target'],False);wire.intervals[0]['gen']=gen
        lines=serialize(wire);bot=[l for l in lines if 'DEMO_CAPABILITY ' in l]
        lines=[l for l in lines if 'DEMO_CAPABILITY ' not in l]
        # PERF belongs only to this begin/close window, including cold gen=1.
        lines=['HOST_FIXTURE_BOOT_SESSION '+label]+['PERF '+json.dumps(s) for s in samples]+lines+['PHASE closed']
        (run/(label+'-serial.log')).write_text('\n'.join(lines)+'\n')
        (run/(label+'-bot.log')).write_text('\n'.join(bot)+'\n')
        put(run/(label+'-bot-events.json'),[])
        data['sessions'].append(dict(label=label,resources=product.smoke.resource_check(samples[-2:],board)))
    room=run.with_name(run.name+'-room');room.mkdir();put(room/'result.json',dict(noise_contaminated=False))
    data.update(room=core.load(room/'result.json'),llm_content_mode='report-only',
        content_decision_file=str(follow.DECISION),content_decision_sha256=follow.DECISION_SHA)
    data['llm_content_findings']=product.findings(data);put(run/'result.json',data)
    return data


@pytest.mark.parametrize('board',core.BOARDS)
def test_three_session_demo_reuses_generation_with_session_specific_snapshots(tmp_path,board):
    data=resource_demo_case(tmp_path/'attempt-01',board)
    assert product.verify_off(tmp_path/'attempt-01')==data
    # The prior generation-only selection would select cold's final pair for main.
    raw=(tmp_path/'attempt-01/main-serial.log').read_text()
    assert json.dumps(core.load(tmp_path/'attempt-01/perf-009.json')) not in raw
    assert data['sessions'][0]['resources']!=data['sessions'][2]['resources']


@pytest.mark.parametrize('board',core.BOARDS)
@pytest.mark.parametrize('change',['missing','detached','resource','wrong-session'])
def test_three_session_demo_rejects_missing_detached_or_failing_snapshots(tmp_path,board,change):
    run=tmp_path/'attempt-01';resource_demo_case(run,board);sample=run/'perf-003.json'
    if change=='missing':sample.unlink();(run/'perf-002.json').unlink()
    elif change=='detached':
        value=core.load(sample);value['host_fixture_snapshot']=999;put(sample,value)
    elif change=='wrong-session':
        (run/'main-serial.log').write_text((run/'cold-serial.log').read_text())
    else:
        value=core.load(sample);old=json.dumps(value)
        next(t for t in value['tasks'] if t['name']=='peer-loop')['stack_free_bytes']=100;put(sample,value)
        path=run/'main-serial.log';path.write_text(path.read_text().replace('PERF '+old,'PERF '+json.dumps(value)))
    with pytest.raises(ValueError):product.verify_off(run)


def test_three_session_demo_smoke_receipts_and_pair_pass_on_both_boards(plans):
    main,plan,root=plans;p=core.load(plan)
    with follow.adapters():
        for board in core.BOARDS:
            run=root/'follow/product'/(board+'-demo')/'attempt-01'
            with ledger.attempt(plan,'smoke',board,'demo',run):
                data=resource_demo_case(run,board);data['identity']=p['product_identity']
                put(run/'identity.json',data['identity']);put(run/'result.json',data)
        core.collect_all(plan,root/'follow/receipts')
        rows=[core.receipt(path) for path in (root/'follow/receipts').glob('*.json')]
        assert len(rows)==4 and all(r['status']=='pass' for r in rows), [(r['category'],r['board'],r.get('reason')) for r in rows]
        assert {r['category'] for r in rows}=={'smoke','product-pair'}
    assert 'follow-up plan' in merge.render(main,plan,list((root/'follow/receipts').glob('*.json')))


def test_main_a0_failure_gets_separate_disclosure_without_coverage_reclassification(plans):
    main,plan,root=plans;original=row(root/'main',variant='aec-demo',category='erle',status='fail',disposition='failed')
    original.update(features=[],reason='Missing/reordered/duplicate AEC PCM block');before=copy.deepcopy(original)
    text=merge.merge_rows(main,plan,[original],[])
    assert 'Disclosed main-plan A0 prerequisite failure:' in text
    assert original['run'] in text and original['reason'] in text and original==before
    assert 'authorized after this failure' in text


@pytest.mark.parametrize('action',['erle','smoke','touch'])
def test_cli_main_validates_real_main_plan_inside_adapters_before_device_boundary(plans,action):
    """Load as __main__, as the coordinator does; never pre-import follow in child."""
    import subprocess
    import sys
    main,plan,root=plans;p=core.load(plan);run=root/'follow'/action/'attempt-01'
    args=[action,'--plan',str(plan),'--out',str(run),'--bot-dir',str(root/'bot'),
        '--bot-identity',str(root/'identity.json')]
    if action=='erle':
        import p4_aec_close
        from p4_aec_image_measure import make_plan
        image=root/'follow/image-plan.json'
        with p4_aec_close.adapters():
            core.write_new(image,make_plan(Path(p['images']['p4xc/aec-demo']['bundle']),
                Path.home()/'src/evidence/echoear/p4/step-15b-fix-5/recommendation.json'))
        args+=['--image-plan',str(image),'--part','0']
    else:args+=['--board','echoear']
    if action=='smoke':args+=['--variant','normal','--llm-content','report-only']
    driver=r'''
import json,runpy,sys
from pathlib import Path
import boards
import p4_parity as core
assert 'p4_parity_followup' not in sys.modules
checks=[];original=core.checked_plan
# Instrument the real checker; neither plan nor authentication is mocked.
def checked(path):
    checks.append(core.load(path)['kind'])
    return original(path)
core.checked_plan=checked
class DeviceBoundary(RuntimeError):pass
def stop():raise DeviceBoundary('host test stops before inherited bench/device access')
boards.require_bench=stop
sys.argv=['hil/p4_parity_followup.py',*json.loads(sys.argv[1])]
try:
    from p4_step16_close import decision_runtime
    with decision_runtime():runpy.run_path(sys.argv[0],run_name='__main__')
except DeviceBoundary:
    assert checks and set(checks)=={'step16-parity-plan'},checks
    assert core.checked_plan is checked,'adapter did not restore original checker'
    print('DEVICE_BOUNDARY '+json.dumps(dict(main_checks=len(checks))))
else:raise AssertionError('entry did not reach the device boundary')
'''
    result=subprocess.run([sys.executable,'-c',driver,json.dumps(args)],cwd=follow.BASE.parent,
        capture_output=True,text=True,timeout=120)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'DEVICE_BOUNDARY ' in result.stdout
    assert not (run/'serial.log').exists()

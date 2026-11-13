"""Offline adversarial replay; pin-dependent cases use copies, never devices."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace

import pytest
import p4_parity as parity
import p4_parity_campaign as campaign
import p4_parity_liveness as live
import p4_parity_restore as restore
from test_p4_parity import populate_liveness_proof
from test_p4_liveness import p4_run,sample
from test_wire_contract import capture,serialize,IDENTITY


@pytest.fixture
def real_plan():
    """Same authentication path as the coordinator, without mocked functions."""
    base=Path.home()/'src/evidence/echoear/p4/step-16/fix-1'
    base.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='regression-',dir=base) as temporary:
        root=Path(temporary);path=root/'plan.json'
        identity=parity.PIN_VERIFICATION/'smoke-echoear-normal/identity.json'
        parity.write_new(path,parity.create_plan(parity.PIN_VERIFICATION/'boot-proof/restore-boot-proof.json',
            parity.PIN_VERIFICATION,root,identity))
        yield path,root


def put(path,row):path.write_text(json.dumps(row))


def original(plan,root,family='display',board='p4xc',variant='normal',noisy=False):
    run=root/family/(board+'-'+variant)/'attempt-01'
    with campaign.attempt(plan,family,board,variant,run):
        run.mkdir(parents=True)
        put(run/'result.json',dict(board=board,variant=variant,acquisition_passed=False,
            device_untouched=noisy,status='preflight-noisy' if noisy else 'failed',retryable=noisy,
            score_produced=False,room=dict(noise_contaminated=noisy)))
    return run


def test_table_cli_rejects_omitted_failed_original_and_unregistered_passing_sibling(real_plan):
    plan,root=real_plan;failed=original(plan,root)
    # A manually retained later fixture cannot bypass registration or disappear.
    later=failed.parent/'attempt-02';later.mkdir();put(later/'acquisition.json',dict(acquisition_passed=True))
    with pytest.raises(ValueError,match='directories and ledger disagree'):
        parity.main(['table','--plan',str(plan),'--out',str(root/'omitted.md')])
    later.rename(root/'quarantined-fixture')
    with pytest.raises(ValueError,match='omitted a registered'):
        parity.main(['table','--plan',str(plan),'--out',str(root/'omitted.md')])
    with pytest.raises(ValueError,match='scored/unfinished'):
        campaign.register(plan,'display','p4xc','normal',later)
    receipt=parity.bound_receipt(plan,'display','p4xc','normal',failed)
    assert receipt['status']=='fail'
    text=parity.render([receipt],plan)
    assert str(failed) in text and '| unverified | fail |' in text


def test_only_one_untouched_noisy_retry_and_every_receipt_is_required(real_plan):
    plan,root=real_plan;first=original(plan,root,family='smoke',noisy=True)
    second=first.parent/'attempt-02'
    with campaign.attempt(plan,'smoke','p4xc','normal',second):
        second.mkdir();put(second/'result.json',dict(board='p4xc',variant='normal',acquisition_passed=False,error='scored failure'))
    with pytest.raises(ValueError,match='retry cap'):
        campaign.register(plan,'smoke','p4xc','normal',first.parent/'attempt-03')
    rows=[parity.bound_receipt(plan,'smoke','p4xc','normal',r) for r in (first,second)]
    assert [r['status'] for r in rows]==['unverified','fail']
    with pytest.raises(ValueError,match='omitted'):parity.render(rows[1:],plan)
    assert all(str(r) in parity.render(rows,plan) for r in (first,second))
    put(first/'result.json',dict(acquisition_passed=True))
    with pytest.raises(ValueError,match='result changed'):campaign.inventory(plan)


def test_interrupted_attempt_requires_disposition_and_cannot_be_replaced(real_plan):
    plan,root=real_plan;run=root/'touch/echoear/attempt-01'
    campaign.register(plan,'touch','echoear','demo',run)
    with pytest.raises(ValueError):campaign.inventory(plan)
    campaign.finish(plan,run,error='interrupted',abandon=True)
    row=parity.bound_receipt(plan,'touch','echoear','demo',run)
    assert row['status']=='fail' and row['attempt_binding']['disposition']=='incomplete'
    parity.collect_all(plan,root/'receipts')
    receipts=[parity.receipt(p) for p in (root/'receipts').glob('*.json')]
    assert len(receipts)==1 and str(run) in parity.render(receipts,plan)
    with pytest.raises(ValueError,match='scored/unfinished'):
        campaign.register(plan,'touch','echoear','demo',run.parent/'attempt-02')


def test_ledger_hash_chain_rejects_edited_dispositions(real_plan):
    plan,root=real_plan;original(plan,root)
    path=root/'attempts.jsonl';path.write_text(path.read_text().replace('"disposition":"failed"','"disposition":"completed"'))
    with pytest.raises(ValueError,match='hash chain'):campaign.inventory(plan)


def copy_restore(directory,board,variant='normal'):
    directory.mkdir(parents=True,exist_ok=True)
    source=parity.PIN_VERIFICATION/('smoke-'+board+'-normal')
    for path in source.iterdir():
        if path.name.startswith('restore') or (board=='p4xc' and path.name.startswith('candidate-storage')):
            if path.is_dir():shutil.copytree(path,directory/path.name)
            else:shutil.copyfile(path,directory/path.name)
    bundle=parity.PIN/'final-bundles'/('bundle-'+board+'-'+variant)
    if board=='p4xc' and variant!='normal':
        from p4_product import storage_plan
        path=directory/'candidate-storage-erase.json';row=parity.load(path)
        row.update(storage_plan(bundle));put(path,row)
        rebind_storage_fixture(directory,bundle)
    return dict(board=board,variant=variant,bundle=str(bundle),restore=parity.load(directory/'restore.json'))


def rebind_storage_fixture(directory,bundle):
    """Copy genuine esptool completion shapes onto a synthetic local path."""
    from p4_product import storage_plan
    from p4_parity_restore import storage_commands
    from boards import checked_esptool_python
    from make_bundle import local_file
    plan=storage_plan(bundle);path=directory/'candidate-storage-erase.json';row=parity.load(path)
    row['commands']=storage_commands(plan,(directory/'candidate-storage-readback.bin').resolve(),checked_esptool_python())
    put(path,row)
    verified=[]
    for offset,name in plan['flash']['flash_files'].items():
        file=local_file(bundle,name);size=file.stat().st_size
        verified.extend([f"Verifying {hex(size)} ({size}) bytes at {int(offset,16):#010x} in flash against '{file}'...",
            'Verification successful (digest matched).'])
    (directory/'candidate-storage-verify.log').write_text('\n'.join(verified+['','Staying in bootloader.'])+'\n')
    size=plan['partition']['size'];offset=plan['partition']['offset'];destination=row['commands'][2][-1]
    (directory/'candidate-storage-readback.log').write_text(f"Read {size} bytes from {offset:#010x} in 56.4 seconds (594.9 kbit/s) to '{destination}'.\n\nStaying in bootloader.\n")


@pytest.mark.parametrize('board',parity.BOARDS)
@pytest.mark.parametrize('change',['none','missing','summary','factory','wrong-elf','conflict','storage','record'])
def test_raw_restore_boot_record_and_storage_are_required(tmp_path,board,change):
    a=copy_restore(tmp_path,board);serial=tmp_path/('restore-serial.log' if board=='p4xc' else 'restore-1-serial.log')
    expected=parity.load(parity.PIN/'final-bundles/bundle-p4xc-normal/manifest.json')['elf_sha256'] if board=='p4xc' else restore.S3_RESTORE_ELF
    if change=='none':
        proof=restore.restore_proof(tmp_path,a)
        assert proof['restored_elf_sha256']==expected and str(tmp_path/'restore.json') in proof['sources']
        assert str(serial) in proof['sources']
        return
    if change=='missing':(tmp_path/'restore.json').unlink()
    elif change=='summary':a['restore']=dict(clean=False)
    elif change=='factory':
        a['restore']['action']='p4-factory';a['restore']['restored_elf_sha256']='f'*64;put(tmp_path/'restore.json',a['restore'])
    elif change=='wrong-elf':serial.write_text(serial.read_text().replace(expected,'f'*64))
    elif change=='conflict':serial.write_text('I (1) dg-esp32: esp32p4 audio elf_sha256='+'f'*64+'\n'+serial.read_text())
    elif change=='storage':
        if board=='p4xc':(next(tmp_path.glob('restore-storage-*'))/'candidate-storage-readback.bin').write_bytes(b'not erased')
        else:(tmp_path/'restore-1-image.json').write_text('{}')
    else:
        with pytest.raises(ValueError,match='record hash'):
            restore.restore_proof(tmp_path,a,dict(restore_record=str(parity.PIN/'restore-of-record.json'),restore_record_sha256='f'*64))
        return
    with pytest.raises((ValueError,OSError,KeyError)):restore.restore_proof(tmp_path,a)


@pytest.mark.parametrize('category',['smoke','display','liveness','touch','erle'])
@pytest.mark.parametrize('board',parity.BOARDS)
def test_every_single_receipt_family_rejects_conflicting_raw_restore(tmp_path,board,category):
    a=copy_restore(tmp_path,board);a.update(acquisition_passed=True,board=board,variant='normal')
    name='result.json' if category in ('smoke','erle') else 'touch.json' if category=='touch' else 'acquisition.json'
    put(tmp_path/name,a);put(tmp_path/'restore.json',dict(clean=False,action='factory'))
    with pytest.raises(ValueError,match='raw restore/summary conflict'):
        parity.replay(category,board,'normal',tmp_path)
    assert parity.collect(category,board,'normal',tmp_path)['status']=='fail'


def display_case(directory,board,variant):
    from p4_parity_display_score import fixture_assets
    from echoear_status_assets import status_assets
    from display_session import display_session
    a=copy_restore(directory,board,variant);m=parity.load(Path(a['bundle'])/'manifest.json')
    a.update(acquisition_passed=True,boot_verified=True,elf_sha256=m['elf_sha256'],screens=[],status_assets=[],wire_requests=[])
    lines=['I (1) dg-esp32: '+('esp32p4' if board=='p4xc' else 'ESP32-S3 step 7')+' audio elf_sha256='+m['elf_sha256'],
        'I (2) dg-esp32: DG_BOOT board='+board+' chip='+m['target']+' idf=v6.1','READY transport-console']
    if board=='p4xc':lines.append('800x800 RGB565 LE framebuffer PSRAM=1 DPI=40MHz')
    if board=='p4xc':lines.append('P4_SCAN '+json.dumps(dict(id=1,sample_every=16,timed=True,outcome='presented',
        committed_us32=1000,completed_us32=2000,latency_us=1000)))
    if m['aec_enabled']:
        gain=m['boot_ref_gain_db'];lines.append(f'MIC3_GAIN=0x{0x10+gain//3:02x} analog_db={gain}')
    commands=[]
    def emit(prefix,row):lines.append(prefix+' '+json.dumps(row,separators=(',',':')))
    def command(text,prefix,row):
        at=len(lines);emit(prefix,row)
        commands.append(dict(command=text,started_ns=at+1,ended_ns=len(lines)+1,start_cursor=at,end_cursor=len(lines),response=[lines[-1]]))
    for i,screen in enumerate(('disconnected','connecting','live')):
        # Rendered counts frames, while revision counts accepted scene edits.
        proof=dict(screen=i,n=i+1,revision=2*i+2,rendered=i+1,completed_us=i+100)
        command('disp demo-screen '+screen,'DISPLAY_DEMO_PROOF',dict(items=[proof]))
        a['screens'].append(dict(screen=screen,proof=proof))
    images=fixture_assets()[1];statuses=status_assets()
    def operation(kind,rid,image,persist=False):
        body=dict(id=rid,hash=image['hash'])
        if kind=='load':body.update(bytes=image['bytes'],url='http://fixture/'+image['hash']+'.dgi')
        completed=dict(id=rid,error='none',state=3,persisted=persist)
        command('disp '+kind+' '+json.dumps(body),'DISPLAY_RESULT',completed)
        return completed
    for i,image in enumerate(images[:16]):operation('load','fixture-'+str(i),image)
    for image in statuses:
        operation('load','status-load-'+str(image['slot']),image)
        a['status_assets'].append(operation('persist','status-persist-'+str(image['slot']),image,True))
    emit('DISPLAY_INVENTORY',dict(items=[[r['hash'],r['bytes'],0,True,True,True,'none'] for r in statuses]))
    operation('delete','step16-http-delete',images[-1]);operation('load','step16-http-load',images[-1])
    probe=images[-1]
    (directory/'http.jsonl').write_text(json.dumps(dict(path='/'+probe['hash']+'.dgi',status=200,
        content_length=probe['bytes'],body_bytes=probe['bytes'],body_sha256=probe['hash']))+'\n')
    wire_start=len(lines)
    commands.append(dict(command='connect',started_ns=len(lines)+1,ended_ns=len(lines)+1,start_cursor=len(lines),end_cursor=len(lines),response=[]))
    events=[dict(event='display-ready',pc_id='display-process',epoch=17)]
    a['display_session']=display_session(events,17);emit('DISPLAY',dict(epoch=17))
    for i,kind in enumerate(('load','show')):
        rid='p4-display-'+kind;body=dict(id=rid,seq=i+1,epoch=17,hash=images[0]['hash'])
        body.update(dict(bytes=images[0]['bytes']) if kind=='load' else dict(layer=7,x=0,y=0))
        sent=dict(event='display-sent',pc_id='display-process',packet=dict(t='display.'+kind,d=body))
        ack=dict(event='display-result',pc_id='display-process',result=dict(epoch=17,seq=i+1,accepted=True))
        events.extend([sent,ack]);completed=dict(id=rid,state=3,error='none',revision=4)
        emit('DISPLAY_RESULT',completed);saved=dict(operation=kind,sent=sent,ack=ack,completed=completed)
        if kind=='show':
            saved.update(status=dict(epoch=17,revisions=[4,4,4]),layers=dict(layers=[[7,images[0]['hash'],0,0,True]]))
            emit('DISPLAY',saved['status']);emit('DISPLAY_LAYERS',saved['layers'])
        a['wire_requests'].append(saved)
    wire=capture(m['target'],m['aec_enabled'],'bench-display')
    wire.records['msg=display-result']['values']['id']='p4-display-load'
    lines.extend(serialize(wire));wire_end=len(lines)
    command('disp character off','DISPLAY_CHARACTER',dict(enabled=False))
    (directory/'wire.log').write_text('\n'.join(lines[wire_start:wire_end])+'\n')
    (directory/'run-serial.log').write_text('\n'.join(lines)+'\n')
    from p4_parity_serial import window
    entries=[dict(cursor=i+1,monotonic_ns=i+1,line=line) for i,line in enumerate(lines)]
    a.update(run_window=window(entries,0,len(entries)),wire_window=window(entries,wire_start,wire_end))
    (directory/'serial.log').write_text('\n'.join(lines)+'\n')
    (directory/'serial-entries.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries))
    (directory/'commands.jsonl').write_text(''.join(json.dumps(c)+'\n' for c in commands))
    put(directory/'events.json',events);put(directory/'identity.json',IDENTITY)
    put(directory/'image.json',m);put(directory/'acquisition.json',a)
    return a


@pytest.mark.parametrize('board',parity.BOARDS)
@pytest.mark.parametrize('variant',parity.VARIANTS)
def test_display_positive_replays_canonical_assets_and_raw_restore(tmp_path,board,variant):
    from p4_parity_display_score import score_display
    display_case(tmp_path,board,variant)
    result=score_display(tmp_path,board,variant)
    assert result['checks_passed'] and str(tmp_path/'restore.json') in result['sources']
    assert parity.replay('display',board,variant,tmp_path)['status']=='pass'


@pytest.mark.parametrize('change',['empty','boot','screen','asset','persist','http','ack','render','terminal'])
def test_display_contradictory_raw_evidence_never_passes(tmp_path,change):
    from p4_parity_display_score import score_display
    a=display_case(tmp_path,'p4xc','normal')
    serial=tmp_path/'run-serial.log';text=serial.read_text()
    if change=='empty':serial.write_text('');put(tmp_path/'events.json',[])
    elif change=='boot':serial.write_text(text.replace(a['elf_sha256'],'f'*64))
    elif change=='screen':a['screens'][0]['proof']['rendered']=0
    elif change=='asset':
        path=tmp_path/'commands.jsonl';path.write_text(path.read_text().replace('fixture-0','missing-fixture'))
    elif change=='persist':a['status_assets'][0]['persisted']=False
    elif change=='http':(tmp_path/'http.jsonl').write_text('{}\n')
    elif change=='ack':
        events=parity.load(tmp_path/'events.json');events[2]['result']['accepted']=False;put(tmp_path/'events.json',events)
    elif change=='render':a['wire_requests'][1]['layers']['layers'][0][1]='f'*64
    else:serial.write_text(text.replace('"id":"p4-display-show"','"id":"not-the-show"'))
    put(tmp_path/'acquisition.json',a)
    with pytest.raises((ValueError,KeyError,OSError)):score_display(tmp_path,'p4xc','normal')
    assert parity.collect('display','p4xc','normal',tmp_path)['status']=='fail'


def test_empty_display_real_plan_receipts_and_pairs_never_pass(real_plan):
    from p4_parity_display import pair
    plan,root=real_plan;runs=[]
    put(root/'display-identity.json',IDENTITY)
    for board in parity.BOARDS:
        run=root/'display'/(board+'-normal')/'attempt-01'
        with campaign.attempt(plan,'display',board,'normal',run):
            a=display_case(run,board,'normal');a['screens']=[dict(screen=s) for s in ('disconnected','connecting','live')]
            a['wire_requests']=[{},{}];a.pop('status_assets');put(run/'acquisition.json',a)
            (run/'run-serial.log').write_text('');put(run/'events.json',[])
        runs.append(run)
    receipts=[parity.bound_receipt(plan,'display',board,'normal',run) for board,run in zip(parity.BOARDS,runs)]
    assert all(parity.receipt(_write_receipt(root,i,r))['status']=='fail' for i,r in enumerate(receipts))
    with pytest.raises(ValueError):pair(*runs)
    assert '| fail | fail |' in next(l for l in parity.render(receipts,plan).splitlines() if l.startswith('| F10 '))


def test_display_pair_replays_both_raw_gates(tmp_path):
    from p4_parity_display import pair
    left,right=tmp_path/'s3',tmp_path/'p4'
    display_case(left,'echoear','normal');display_case(right,'p4xc','normal')
    assert pair(left,right)['exact_wire_passed']
    (right/'events.json').write_text('[]')
    with pytest.raises((ValueError,TypeError)):pair(left,right)


def _write_receipt(root,i,row):
    path=root/('receipt-'+str(i)+'.json');parity.write_new(path,row);return path


def liveness_case(tmp_path,monkeypatch,board):
    a=p4_run(tmp_path);a.update(board=board,variant='demo',aec_enabled=False,bundle=str(tmp_path/'bundle'),reference_only=False)
    for session in a['sessions']:
        lines=serialize(capture('esp32p4' if board=='p4xc' else 'esp32s3',False))
        path=tmp_path/session['wire_file'];capability=[l for l in lines if 'DEMO_CAPABILITY ' in l]
        path.write_text('\n'.join([l for l in lines if 'DEMO_CAPABILITY ' not in l]+['PHASE closed']+capability)+'\n')
        session['wire_sha256']=parity.digest(path)
    monkeypatch.setattr(live,'pinned_bundle',lambda *args:dict(autostart=True,aec_enabled=False,elf_sha256=a['elf_sha256']))
    populate_liveness_proof(tmp_path,a,monkeypatch)
    return a


@pytest.mark.parametrize('board',parity.BOARDS)
def test_live_settling_timeout_is_strict_for_both_boards(board):
    class IO:
        args=SimpleNamespace(board=board)
        now=0;count=0
        def clock(self):return self.now
        def snapshot(self,_):self.count+=1;return sample(160000+1000*(self.count%2))
        def pause(self,_,seconds):self.now+=seconds
    with pytest.raises(ValueError,match='did not converge'):live.settled_heap(None,IO())


@pytest.mark.parametrize('board',parity.BOARDS)
@pytest.mark.parametrize('change',['missing','nonconverged','sample','policy','consecutive','too-many','spread','floor'])
def test_offline_all_21_convergence_witnesses_are_gated(tmp_path,monkeypatch,board,change):
    a=liveness_case(tmp_path,monkeypatch,board);assert live.score(tmp_path,acquisition=a)['checks_passed']
    witness=a['settled_evidence'][0]
    if change=='missing':a['settled_evidence'].pop()
    elif change=='nonconverged':witness['converged']=False
    elif change=='sample':witness['samples'][-1]['heap']['internal']['free']-=1
    elif change=='policy':witness['policy']['tolerance_bytes']=1024
    elif change=='consecutive':witness['samples'].pop()
    elif change=='too-many':witness['samples']*=15
    elif change=='spread':witness['samples'][0]['heap']['internal']['free']-=1000
    else:witness['samples'][0]['heap']['internal']['minimum_free']=81919
    with pytest.raises(ValueError):live.score(tmp_path,acquisition=a)


@pytest.mark.parametrize('change',['identities','process','source','profile','missing-ready','missing-request','startup',
    'handoff','swapped-controls','raw-control','extent','source-hash','observer','product-process'])
def test_liveness_observer_and_restart_provenance_replays(tmp_path,monkeypatch,change):
    a=liveness_case(tmp_path,monkeypatch,'p4xc');assert live.score(tmp_path,acquisition=a)['checks_passed']
    if change=='identities':a['bot_identities']=[{},{}]
    elif change in ('process','source','profile','product-process'):
        path=tmp_path/'replacement-identity.json';row=parity.load(path)
        if change=='process':row['bot_instance']=a['bot_identities'][0]['bot_instance']
        elif change=='source':row['bot_source_sha']='f'*40
        elif change=='profile':row['profile_manifest']={}
        else:row['bot_instance']=IDENTITY['bot_instance']
        put(path,row);put(Path(a['replacement_dir'])/'identity.json',row)
        a['bot_identities'][1]=row;a['control_sources'][1]['identity']=row
        marker=parity.load(tmp_path/'replacement-ready.json');marker['identity']=row
        put(tmp_path/'replacement-ready.json',marker);put(Path(a['replacement_dir'])/'ready.json',marker)
    elif change=='missing-ready':(tmp_path/'initial-ready.json').unlink()
    elif change=='missing-request':(tmp_path/'restart-request.json').unlink()
    elif change in ('startup','handoff'):
        path=tmp_path/'replacement-ready.json';row=parity.load(path)
        row['up_monotonic_ns']=1 if change=='startup' else row['up_monotonic_ns']+181*10**9;put(path,row)
    elif change=='swapped-controls':a['control_sources'].reverse()
    elif change=='raw-control':
        Path(a['control_sources'][0]['original_file']).write_text('{}\n')
    elif change=='extent':a['control_sources'][0]['start_byte']+=1
    elif change=='source-hash':a['source_sha256']={}
    else:a['observer_sha256']='f'*64
    with pytest.raises((ValueError,OSError,KeyError)):live.score(tmp_path,acquisition=a)
